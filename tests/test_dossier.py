"""Unit tests for meter.dossier (M2)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from meter import dossier


class DossierBuildTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmpdir.name) / "repo"
        self.run_dir = self.repo_root / ".dag" / "runs" / "run-1"
        self.repo_root.mkdir(parents=True)
        self.run_dir.mkdir(parents=True)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _write_repo_file(self, rel_path: str, content: str) -> None:
        path = self.repo_root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _write_tasks_json(self, tasks: list[dict], contracts: dict | None = None) -> None:
        (self.run_dir / "tasks.json").write_text(
            json.dumps({"tasks": tasks, "contracts": contracts or {}}), encoding="utf-8")

    def test_missing_tasks_json_still_produces_a_dossier(self):
        result = dossier.build(repo_root=self.repo_root, run_dir=self.run_dir,
                                node_id="T-001", max_tokens=4000)
        self.assertTrue(result["built"])
        self.assertTrue(Path(result["path"]).is_file())
        self.assertEqual(result["owned_files"], [])

    def test_owned_file_symbols_are_indexed(self):
        self._write_repo_file("src/auth.py", "class AuthService:\n    def refresh(self):\n        pass\n")
        self._write_tasks_json([{"id": "T-001", "title": "Refresh tokens",
                                  "owns": ["src/auth.py"], "acceptance_criteria": ["AC-01"]}])
        result = dossier.build(repo_root=self.repo_root, run_dir=self.run_dir,
                                node_id="T-001", max_tokens=4000)
        self.assertTrue(result["built"])
        self.assertGreaterEqual(result["symbol_count"], 2)
        text = Path(result["path"]).read_text(encoding="utf-8")
        self.assertIn("AuthService", text)
        self.assertIn("refresh", text)

    def test_secret_shaped_owned_file_is_excluded(self):
        self._write_repo_file(".env", "SECRET=xyz\n")
        self._write_tasks_json([{"id": "T-001", "title": "x", "owns": [".env"]}])
        result = dossier.build(repo_root=self.repo_root, run_dir=self.run_dir,
                                node_id="T-001", max_tokens=4000)
        text = Path(result["path"]).read_text(encoding="utf-8")
        self.assertNotIn("SECRET=xyz", text)

    def test_contract_text_included(self):
        self._write_tasks_json(
            [{"id": "T-001", "title": "x", "owns": [], "contracts": ["C-AUTH"]}],
            contracts={"C-AUTH": "The auth contract text goes here."},
        )
        result = dossier.build(repo_root=self.repo_root, run_dir=self.run_dir,
                                node_id="T-001", max_tokens=4000)
        text = Path(result["path"]).read_text(encoding="utf-8")
        self.assertIn("The auth contract text goes here.", text)

    def test_build_returns_attribution_exposures_for_contract(self):
        self._write_tasks_json(
            [{"id": "T-001", "title": "x", "owns": [], "contracts": ["C-AUTH"]}],
            contracts={"C-AUTH": "contract text"},
        )
        result = dossier.build(repo_root=self.repo_root, run_dir=self.run_dir,
                                node_id="T-001", max_tokens=4000)
        self.assertIn({"unit_type": "contract", "unit_label": "C-AUTH", "token": "C-AUTH"},
                       result["exposures"])

    def test_build_exposures_excludes_owned_files(self):
        self._write_repo_file("src/a.py", "x = 1\n")
        self._write_tasks_json([{"id": "T-001", "title": "x", "owns": ["src/a.py"]}])
        result = dossier.build(repo_root=self.repo_root, run_dir=self.run_dir,
                                node_id="T-001", max_tokens=4000)
        unit_types = {e["unit_type"] for e in result["exposures"]}
        self.assertNotIn("owned_file", unit_types)

    def test_sibling_receipt_touching_same_file_is_surfaced(self):
        self._write_tasks_json([{"id": "T-002", "title": "x", "owns": ["src/shared.py"]}])
        receipts_dir = self.run_dir / "meter" / "receipts"
        receipts_dir.mkdir(parents=True)
        (receipts_dir / "T-001.json").write_text(json.dumps({
            "v": 1, "node": "T-001", "status": "done",
            "files": [{"path": "src/shared.py", "action": "modify"}],
        }), encoding="utf-8")
        result = dossier.build(repo_root=self.repo_root, run_dir=self.run_dir,
                                node_id="T-002", max_tokens=4000)
        self.assertEqual(result["sibling_receipts"], 1)
        text = Path(result["path"]).read_text(encoding="utf-8")
        self.assertIn("T-001", text)

    def test_own_receipt_is_not_counted_as_a_sibling(self):
        self._write_tasks_json([{"id": "T-001", "title": "x", "owns": ["src/shared.py"]}])
        receipts_dir = self.run_dir / "meter" / "receipts"
        receipts_dir.mkdir(parents=True)
        (receipts_dir / "T-001.json").write_text(json.dumps({
            "v": 1, "node": "T-001", "status": "done",
            "files": [{"path": "src/shared.py", "action": "modify"}],
        }), encoding="utf-8")
        result = dossier.build(repo_root=self.repo_root, run_dir=self.run_dir,
                                node_id="T-001", max_tokens=4000)
        self.assertEqual(result["sibling_receipts"], 0)

    def test_budget_drops_lowest_priority_sections_first(self):
        # A large owned file's symbol section is much bigger than the budget —
        # everything after the (always-kept) header should be dropped.
        big_body = "\n\n".join(f"def f_{i}():\n    return {i}\n" for i in range(200))
        self._write_repo_file("src/big.py", big_body)
        self._write_tasks_json([{"id": "T-001", "title": "AuthServiceRefreshFlow",
                                  "owns": ["src/big.py"],
                                  "acceptance_criteria": ["AuthServiceRefreshFlow works"]}])
        result_tight = dossier.build(repo_root=self.repo_root, run_dir=self.run_dir,
                                      node_id="T-001", max_tokens=10)
        result_roomy = dossier.build(repo_root=self.repo_root, run_dir=self.run_dir,
                                      node_id="T-001", max_tokens=100000)
        self.assertTrue(result_tight["built"])
        self.assertLess(result_tight["tokens_est"], result_roomy["tokens_est"])
        tight_text = Path(result_tight["path"]).read_text(encoding="utf-8")
        self.assertIn("# Dossier: T-001", tight_text)  # header always survives
        self.assertNotIn("f_199", tight_text)  # the huge symbol section does not

    def test_never_raises_on_broken_tasks_json(self):
        (self.run_dir / "tasks.json").write_text("{not json", encoding="utf-8")
        result = dossier.build(repo_root=self.repo_root, run_dir=self.run_dir,
                                node_id="T-001", max_tokens=4000)
        self.assertTrue(result["built"])

    def test_secret_and_binary_predicates(self):
        self.assertTrue(dossier.is_secret_shaped(".env"))
        self.assertTrue(dossier.is_secret_shaped("config/.env.production"))
        self.assertTrue(dossier.is_secret_shaped("id_rsa"))
        self.assertTrue(dossier.is_binary_shaped("assets/logo.png"))
        self.assertFalse(dossier.is_secret_shaped("src/auth.py"))
        self.assertFalse(dossier.is_binary_shaped("src/auth.py"))


class DossierInternTests(unittest.TestCase):
    """v2 M9: dossier-level interning of file paths."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmpdir.name) / "repo"
        self.run_dir = self.repo_root / ".dag" / "runs" / "run-1"
        self.repo_root.mkdir(parents=True)
        self.run_dir.mkdir(parents=True)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _write_repo_file(self, rel_path: str, content: str) -> None:
        path = self.repo_root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _write_tasks_json(self, tasks: list[dict], contracts: dict | None = None) -> None:
        (self.run_dir / "tasks.json").write_text(
            json.dumps({"tasks": tasks, "contracts": contracts or {}}), encoding="utf-8")

    def test_default_intern_cfg_is_off_and_full_paths_appear(self):
        self._write_repo_file("src/modules/auth/services/AuthService.ts", "class X {}\n")
        self._write_tasks_json([{"id": "T-001", "title": "x",
                                  "owns": ["src/modules/auth/services/AuthService.ts"]}])
        result = dossier.build(repo_root=self.repo_root, run_dir=self.run_dir,
                                node_id="T-001", max_tokens=4000)
        text = Path(result["path"]).read_text(encoding="utf-8")
        self.assertIn("src/modules/auth/services/AuthService.ts", text)

    def test_basename_mode_states_prefix_once_and_uses_suffix(self):
        self._write_repo_file("src/modules/auth/services/AuthService.ts",
                               "class AuthService {\n  refresh() {}\n}\n")
        self._write_repo_file("src/modules/auth/guards/SessionGuard.ts",
                               "class SessionGuard {\n  validate() {}\n}\n")
        self._write_tasks_json([{
            "id": "T-001", "title": "x",
            "owns": ["src/modules/auth/services/AuthService.ts",
                     "src/modules/auth/guards/SessionGuard.ts"],
        }])
        result = dossier.build(repo_root=self.repo_root, run_dir=self.run_dir,
                                node_id="T-001", max_tokens=4000,
                                intern_cfg={"enabled": True, "mode": "basename"})
        text = Path(result["path"]).read_text(encoding="utf-8")
        self.assertIn("Common path prefix", text)
        self.assertIn("services/AuthService.ts", text)
        self.assertIn("guards/SessionGuard.ts", text)
        # The symbol section (under our control) shouldn't repeat the full,
        # uncompressed path — the "Text search hits" section is raw grep
        # output and deliberately out of scope for interning (see
        # dossier.py's _build), so it's excluded from this check.
        symbols_section = text.split("## Text search hits")[0]
        self.assertEqual(symbols_section.count("src/modules/auth/services/AuthService.ts"), 0)

    def test_codebook_mode_writes_table_and_codes_and_sidecar(self):
        self._write_repo_file("src/a.py", "def f():\n    return 1\n")
        self._write_tasks_json([{"id": "T-001", "title": "x", "owns": ["src/a.py"]}])
        result = dossier.build(repo_root=self.repo_root, run_dir=self.run_dir,
                                node_id="T-001", max_tokens=4000,
                                intern_cfg={"enabled": True, "mode": "codebook"})
        text = Path(result["path"]).read_text(encoding="utf-8")
        self.assertIn("FILES", text)
        self.assertIn("f1 src/a.py", text)
        self.assertIn("`f1`", text)

        sidecar = Path(result["path"]).with_suffix("").with_suffix(".codebook.json")
        self.assertTrue(sidecar.exists())
        codebook = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(codebook["files"]["f1"], "src/a.py")

    def test_codebook_mode_is_lossless_via_decode(self):
        from meter import intern as intern_mod

        self._write_repo_file("src/modules/auth/AuthService.py", "def refresh():\n    pass\n")
        self._write_tasks_json([{"id": "T-001", "title": "x",
                                  "owns": ["src/modules/auth/AuthService.py"]}])
        result = dossier.build(repo_root=self.repo_root, run_dir=self.run_dir,
                                node_id="T-001", max_tokens=4000,
                                intern_cfg={"enabled": True, "mode": "codebook"})
        text = Path(result["path"]).read_text(encoding="utf-8")
        sidecar = Path(result["path"]).with_suffix("").with_suffix(".codebook.json")
        codebook = intern_mod.Codebook.from_dict(json.loads(sidecar.read_text(encoding="utf-8")))
        decoded = intern_mod.decode_text(codebook, text)
        self.assertIn("src/modules/auth/AuthService.py", decoded)

    def test_single_file_node_has_no_prefix_header_in_basename_mode(self):
        self._write_repo_file("src/a.py", "x = 1\n")
        self._write_tasks_json([{"id": "T-001", "title": "x", "owns": ["src/a.py"]}])
        result = dossier.build(repo_root=self.repo_root, run_dir=self.run_dir,
                                node_id="T-001", max_tokens=4000,
                                intern_cfg={"enabled": True, "mode": "basename"})
        text = Path(result["path"]).read_text(encoding="utf-8")
        # Only one file in the whole node — basename_compress needs >1 path
        # to be worth doing (see _intern_paths), so the full path is fine here.
        self.assertIn("src/a.py", text)


class DossierPullModeTests(unittest.TestCase):
    """v2 M10 Pull (disabled by default — see meter/pull.py)."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmpdir.name) / "repo"
        self.run_dir = self.repo_root / ".dag" / "runs" / "run-1"
        self.repo_root.mkdir(parents=True)
        self.run_dir.mkdir(parents=True)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _write_repo_file(self, rel_path: str, content: str) -> None:
        path = self.repo_root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _write_tasks_json(self, tasks: list[dict], contracts: dict | None = None) -> None:
        (self.run_dir / "tasks.json").write_text(
            json.dumps({"tasks": tasks, "contracts": contracts or {}}), encoding="utf-8")

    def test_default_pull_cfg_produces_normal_pushed_body(self):
        self._write_tasks_json([{"id": "T-001", "title": "x", "contracts": ["C-A"]}],
                                contracts={"C-A": "contract text"})
        result = dossier.build(repo_root=self.repo_root, run_dir=self.run_dir,
                                node_id="T-001", max_tokens=4000)
        self.assertIsNone(result.get("manifest"))
        self.assertIsNotNone(result["path"])

    def test_pull_enabled_produces_manifest_and_section_files(self):
        self._write_tasks_json([{"id": "T-001", "title": "x", "contracts": ["C-A"]}],
                                contracts={"C-A": "contract text"})
        result = dossier.build(repo_root=self.repo_root, run_dir=self.run_dir,
                                node_id="T-001", max_tokens=4000, pull_cfg={"enabled": True})
        self.assertIsNotNone(result["manifest"])
        self.assertIn("Context available for T-001", result["manifest"])
        self.assertTrue(result["pull_sections"])
        for section in result["pull_sections"]:
            self.assertTrue(Path(section["path"]).is_file())

    def test_pull_section_content_matches_source_section(self):
        self._write_tasks_json([{"id": "T-001", "title": "x", "contracts": ["C-A"]}],
                                contracts={"C-A": "the contract body text"})
        result = dossier.build(repo_root=self.repo_root, run_dir=self.run_dir,
                                node_id="T-001", max_tokens=4000, pull_cfg={"enabled": True})
        contract_section = next(s for s in result["pull_sections"] if s["id"] == "contracts")
        self.assertIn("the contract body text", Path(contract_section["path"]).read_text())


if __name__ == "__main__":
    unittest.main()
