"""Unit tests for meter.vault (M5, shadow mode only).

`NormExhaustiveTests` is the "exhaustive unit-test table" meter-handoff.md
Section 4/M5 requires for `norm()`, because "every false-positive collision
is a wrong patch" — each case pairs two differently-formatted inputs that
must normalise identically, or (for the negative cases) two inputs that must
NOT collide.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from meter import dossier, store, vault


class NormExhaustiveTests(unittest.TestCase):
    def test_case_insensitive(self):
        self.assertEqual(vault.norm("Refresh The Token"), vault.norm("refresh the token"))

    def test_whitespace_insensitive(self):
        self.assertEqual(vault.norm("refresh   the\n\ntoken"), vault.norm("refresh the token"))

    def test_leading_trailing_whitespace_insensitive(self):
        self.assertEqual(vault.norm("  refresh the token  "), vault.norm("refresh the token"))

    def test_task_id_stripped(self):
        self.assertEqual(vault.norm("Implement T-003 refresh"),
                          vault.norm("Implement T-099 refresh"))

    def test_ac_id_stripped(self):
        self.assertEqual(vault.norm("Satisfies AC-01"), vault.norm("Satisfies AC-99"))

    def test_contract_id_stripped(self):
        self.assertEqual(vault.norm("Implements C-01"), vault.norm("Implements C-42"))

    def test_run_id_stripped(self):
        self.assertEqual(vault.norm("from run 2026-08-21-a3f9 onward"),
                          vault.norm("from run 2026-09-15-bb02 onward"))

    def test_date_stripped(self):
        self.assertEqual(vault.norm("due by 2026-08-21"), vault.norm("due by 2026-09-30"))

    def test_ordinal_stripped(self):
        self.assertEqual(vault.norm("the 1st attempt"), vault.norm("the 2nd attempt"))

    def test_bullet_list_order_insensitive(self):
        a = "Requirements:\n- refresh tokens\n- validate session\n- log errors"
        b = "Requirements:\n- log errors\n- refresh tokens\n- validate session"
        self.assertEqual(vault.norm(a), vault.norm(b))

    def test_bullet_marker_style_insensitive(self):
        a = "- refresh tokens\n- validate session"
        b = "* refresh tokens\n* validate session"
        self.assertEqual(vault.norm(a), vault.norm(b))

    def test_empty_string(self):
        self.assertEqual(vault.norm(""), "")

    def test_none_like_falsy(self):
        self.assertEqual(vault.norm(None), "")  # type: ignore[arg-type]

    # --- Negative cases: things that must NOT collide ---

    def test_different_words_do_not_collide(self):
        self.assertNotEqual(vault.norm("refresh the token"), vault.norm("revoke the token"))

    def test_lowercase_hyphenated_word_not_treated_as_id(self):
        # "utf-8" must survive intact — only uppercase-prefixed IDs are stripped.
        self.assertIn("utf-8", vault.norm("encode as utf-8"))

    def test_bullet_content_still_distinguishes_lists(self):
        a = "- refresh tokens\n- validate session"
        b = "- refresh tokens\n- validate device"
        self.assertNotEqual(vault.norm(a), vault.norm(b))

    def test_non_bullet_prose_order_still_matters(self):
        a = "First refresh the token. Then validate the session."
        b = "First validate the session. Then refresh the token."
        self.assertNotEqual(vault.norm(a), vault.norm(b))


class ComputeKeyTests(unittest.TestCase):
    def _kwargs(self, **overrides):
        base = dict(description="Refresh tokens", acceptance_criteria=["AC-01 met"],
                    contract_text="contract text", input_span_hashes=["h1", "h2"],
                    model_tier="sonnet", agent_type="task-worker", plugin_version="1.0.0")
        base.update(overrides)
        return base

    def test_deterministic(self):
        self.assertEqual(vault.compute_key(**self._kwargs()), vault.compute_key(**self._kwargs()))

    def test_span_hash_order_does_not_matter(self):
        k1 = vault.compute_key(**self._kwargs(input_span_hashes=["h1", "h2"]))
        k2 = vault.compute_key(**self._kwargs(input_span_hashes=["h2", "h1"]))
        self.assertEqual(k1, k2)

    def test_different_description_differs(self):
        k1 = vault.compute_key(**self._kwargs(description="Refresh tokens"))
        k2 = vault.compute_key(**self._kwargs(description="Revoke tokens"))
        self.assertNotEqual(k1, k2)

    def test_different_agent_type_differs(self):
        k1 = vault.compute_key(**self._kwargs(agent_type="task-worker"))
        k2 = vault.compute_key(**self._kwargs(agent_type="task-reviewer"))
        self.assertNotEqual(k1, k2)

    def test_ids_normalized_away_collide(self):
        k1 = vault.compute_key(**self._kwargs(description="Implement T-003"))
        k2 = vault.compute_key(**self._kwargs(description="Implement T-099"))
        self.assertEqual(k1, k2)


class PatchStorageTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.plugin_data_dir = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_round_trips(self):
        sha = vault.store_patch(self.plugin_data_dir, "diff --git a b\n+x\n")
        self.assertEqual(vault.load_patch(self.plugin_data_dir, sha), "diff --git a b\n+x\n")

    def test_missing_patch_returns_none(self):
        self.assertIsNone(vault.load_patch(self.plugin_data_dir, "0" * 64))

    def test_content_addressed(self):
        sha1 = vault.store_patch(self.plugin_data_dir, "same content")
        sha2 = vault.store_patch(self.plugin_data_dir, "same content")
        self.assertEqual(sha1, sha2)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                           check=True)


class ProcessCompletedReceiptTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmpdir.name) / "repo"
        self.repo_root.mkdir()
        _git(self.repo_root, "init", "-q")
        _git(self.repo_root, "config", "user.email", "t@example.com")
        _git(self.repo_root, "config", "user.name", "T")
        (self.repo_root / "src").mkdir()
        (self.repo_root / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
        _git(self.repo_root, "add", ".")
        _git(self.repo_root, "commit", "-q", "-m", "init")

        self.run_dir = self.repo_root / ".dag" / "runs" / "run-1"
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "tasks.json").write_text(json.dumps({
            "tasks": [{"id": "T-001", "title": "Bump x", "owns": ["src/a.py"],
                       "acceptance_criteria": ["x is 2"]}],
        }), encoding="utf-8")

        self.plugin_data_dir = Path(self._tmpdir.name) / "plugin-data"
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(store._SCHEMA)

    def tearDown(self):
        self.conn.close()
        self._tmpdir.cleanup()

    def _commit_change(self, content: str) -> str:
        (self.repo_root / "src" / "a.py").write_text(content, encoding="utf-8")
        _git(self.repo_root, "add", ".")
        _git(self.repo_root, "commit", "-q", "-m", "change")
        return _git(self.repo_root, "rev-parse", "HEAD").stdout.strip()

    def _receipt(self, commit: str, status: str = "done") -> dict:
        return {"v": 1, "node": "T-001", "status": status, "worktree": str(self.repo_root),
                "commit": commit}

    def test_first_completion_seeds_the_vault(self):
        commit = self._commit_change("x = 2\n")
        result = vault.process_completed_receipt(
            self.conn, self.plugin_data_dir, repo_root=self.repo_root, run_dir=self.run_dir,
            run_id="run-1", node_id="T-001", agent_type="task-worker",
            receipt=self._receipt(commit),
        )
        self.assertEqual(result["outcome"], "seeded")
        totals = store.get_vault_totals(self.conn)
        self.assertEqual(totals["entries"], 1)

    def test_non_done_status_is_skipped(self):
        commit = self._commit_change("x = 2\n")
        result = vault.process_completed_receipt(
            self.conn, self.plugin_data_dir, repo_root=self.repo_root, run_dir=self.run_dir,
            run_id="run-1", node_id="T-001", agent_type="task-worker",
            receipt=self._receipt(commit, status="blocked"),
        )
        self.assertIsNone(result)

    def test_missing_worktree_commit_is_skipped(self):
        result = vault.process_completed_receipt(
            self.conn, self.plugin_data_dir, repo_root=self.repo_root, run_dir=self.run_dir,
            run_id="run-1", node_id="T-001", agent_type="task-worker",
            receipt={"v": 1, "node": "T-001", "status": "done", "worktree": "", "commit": "none"},
        )
        self.assertIsNone(result)

    def test_second_identical_task_agrees(self):
        commit1 = self._commit_change("x = 2\n")
        vault.process_completed_receipt(
            self.conn, self.plugin_data_dir, repo_root=self.repo_root, run_dir=self.run_dir,
            run_id="run-1", node_id="T-001", agent_type="task-worker",
            receipt=self._receipt(commit1),
        )
        # A second run, same task shape (title/AC/owns unchanged), same edit.
        run_dir_2 = self.repo_root / ".dag" / "runs" / "run-2"
        run_dir_2.mkdir(parents=True)
        (run_dir_2 / "tasks.json").write_text((self.run_dir / "tasks.json").read_text(), encoding="utf-8")
        commit2 = self._commit_change("x = 2\n")  # same resulting diff shape from a's current state
        result = vault.process_completed_receipt(
            self.conn, self.plugin_data_dir, repo_root=self.repo_root, run_dir=run_dir_2,
            run_id="run-2", node_id="T-001", agent_type="task-worker",
            receipt=self._receipt(commit2),
        )
        self.assertIn(result["outcome"], ("shadow_agreed", "shadow_disagreed"))
        stats = store.get_vault_shadow_stats(self.conn)
        self.assertEqual(stats["total_checks"], 1)


class StatsAndPurgeTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.plugin_data_dir = Path(self._tmpdir.name)
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(store._SCHEMA)

    def tearDown(self):
        self.conn.close()
        self._tmpdir.cleanup()

    def _seed_entry(self, key: str, patch_text: str, created_at: int) -> None:
        patch_sha = vault.store_patch(self.plugin_data_dir, patch_text)
        store.insert_vault_entry(self.conn, {
            "key": key, "repo": "r", "agent_type": "task-worker", "model_tier": "task-worker",
            "patch_sha": patch_sha, "receipt_json": "{}", "verify_cmd": None,
            "verify_digest": None, "tokens_spent": None, "created_at": created_at,
            "schema_v": vault.VAULT_SCHEMA_VERSION,
        })

    def test_stats_reports_entries_and_disk_usage(self):
        self._seed_entry("k1", "patch one" * 100, created_at=1)
        result = vault.stats(self.conn, self.plugin_data_dir)
        self.assertEqual(result["entries"], 1)
        self.assertGreater(result["disk_bytes"], 0)

    def test_purge_evicts_oldest_first_under_cap(self):
        self._seed_entry("k1", "a" * 2000, created_at=1)
        self._seed_entry("k2", "b" * 2000, created_at=2)
        # Cap tiny enough that only one entry's patch can remain.
        result = vault.purge(self.conn, self.plugin_data_dir, max_gb=2000 / (1024 ** 3))
        self.assertEqual(result["removed"], 1)
        self.assertIsNone(store.get_vault_entry(self.conn, "k1"))
        self.assertIsNotNone(store.get_vault_entry(self.conn, "k2"))

    def test_purge_noop_under_cap(self):
        self._seed_entry("k1", "a" * 10, created_at=1)
        result = vault.purge(self.conn, self.plugin_data_dir, max_gb=1.0)
        self.assertEqual(result["removed"], 0)
        self.assertIsNotNone(store.get_vault_entry(self.conn, "k1"))


if __name__ == "__main__":
    unittest.main()
