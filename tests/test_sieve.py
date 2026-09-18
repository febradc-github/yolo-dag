"""Unit tests for meter.sieve (v2 M11).

The one invariant that matters most (per the module's own docstring): a
declaration must never claim a class is covered unless it both ran and
passed. `RenderDeclarationTests` exercises that directly.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from meter import sieve


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                           check=True)


class CheckSecretsTests(unittest.TestCase):
    def test_clean_diff_passes(self):
        result = sieve.check_secrets("+ def f():\n+     return 1\n")
        self.assertTrue(result["ran"])
        self.assertTrue(result["passed"])

    def test_empty_diff_passes(self):
        result = sieve.check_secrets("")
        self.assertTrue(result["ran"])
        self.assertTrue(result["passed"])

    def test_aws_key_is_flagged(self):
        result = sieve.check_secrets("+ key = 'AKIAABCDEFGHIJKLMNOP'\n")
        self.assertTrue(result["ran"])
        self.assertFalse(result["passed"])
        self.assertTrue(result["findings"])

    def test_private_key_header_is_flagged(self):
        result = sieve.check_secrets("+-----BEGIN RSA PRIVATE KEY-----\n")
        self.assertFalse(result["passed"])

    def test_hardcoded_password_assignment_is_flagged(self):
        result = sieve.check_secrets('+ password = "hunter2ishere"\n')
        self.assertFalse(result["passed"])

    def test_short_value_not_flagged(self):
        # Under the length threshold — avoids flagging trivial placeholders.
        result = sieve.check_secrets('+ password = "x"\n')
        self.assertTrue(result["passed"])


class CheckOwnershipTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmpdir.name)
        _git(self.repo_root, "init", "-q")
        _git(self.repo_root, "config", "user.email", "t@example.com")
        _git(self.repo_root, "config", "user.name", "T")
        (self.repo_root / "src").mkdir()
        (self.repo_root / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
        (self.repo_root / "src" / "b.py").write_text("y = 1\n", encoding="utf-8")
        _git(self.repo_root, "add", ".")
        _git(self.repo_root, "commit", "-q", "-m", "init")

    def tearDown(self):
        self._tmpdir.cleanup()

    def _commit_change(self, rel_path: str, content: str) -> str:
        (self.repo_root / rel_path).write_text(content, encoding="utf-8")
        _git(self.repo_root, "add", ".")
        _git(self.repo_root, "commit", "-q", "-m", "change")
        return _git(self.repo_root, "rev-parse", "HEAD").stdout.strip()

    def test_change_within_owns_passes(self):
        commit = self._commit_change("src/a.py", "x = 2\n")
        result = sieve.check_ownership(self.repo_root, str(self.repo_root), commit, ["src/a.py"])
        self.assertTrue(result["ran"])
        self.assertTrue(result["passed"])

    def test_change_outside_owns_fails(self):
        commit = self._commit_change("src/b.py", "y = 2\n")
        result = sieve.check_ownership(self.repo_root, str(self.repo_root), commit, ["src/a.py"])
        self.assertTrue(result["ran"])
        self.assertFalse(result["passed"])
        self.assertTrue(result["findings"])

    def test_nonexistent_worktree_reports_not_ran(self):
        result = sieve.check_ownership(self.repo_root, "/nonexistent/path", "HEAD", ["src/a.py"])
        self.assertFalse(result["ran"])


class CheckTypesLintFallbackTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.worktree = self._tmpdir.name

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_types_not_run_without_tsconfig_or_mypy(self):
        result = sieve.check_types(self.worktree)
        self.assertFalse(result["ran"])
        self.assertIsNotNone(result["reason"])

    def test_lint_not_run_without_config(self):
        result = sieve.check_lint(self.worktree)
        self.assertFalse(result["ran"])

    def test_tests_always_not_run(self):
        result = sieve.check_tests()
        self.assertFalse(result["ran"])

    def test_ast_always_not_run(self):
        result = sieve.check_ast()
        self.assertFalse(result["ran"])


class RunRegistryTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmpdir.name)
        _git(self.repo_root, "init", "-q")
        _git(self.repo_root, "config", "user.email", "t@example.com")
        _git(self.repo_root, "config", "user.name", "T")
        (self.repo_root / "src").mkdir()
        (self.repo_root / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
        _git(self.repo_root, "add", ".")
        _git(self.repo_root, "commit", "-q", "-m", "init")
        (self.repo_root / "src" / "a.py").write_text("x = 2\n", encoding="utf-8")
        _git(self.repo_root, "add", ".")
        _git(self.repo_root, "commit", "-q", "-m", "change")
        self.commit = _git(self.repo_root, "rev-parse", "HEAD").stdout.strip()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_only_enabled_checkers_run(self):
        results = sieve.run_registry(repo_root=self.repo_root, worktree=str(self.repo_root),
                                      commit=self.commit, owns=["src/a.py"],
                                      enabled=["ownership", "secrets"])
        names = {r["name"] for r in results}
        self.assertEqual(names, {"ownership", "secrets"})

    def test_unknown_checker_name_is_ignored(self):
        results = sieve.run_registry(repo_root=self.repo_root, worktree=str(self.repo_root),
                                      commit=self.commit, owns=["src/a.py"],
                                      enabled=["ownership", "nonexistent"])
        names = {r["name"] for r in results}
        self.assertEqual(names, {"ownership"})

    def test_never_raises_even_on_bad_input(self):
        results = sieve.run_registry(repo_root=self.repo_root, worktree="/nope",
                                      commit="deadbeef", owns=[],
                                      enabled=["ownership", "secrets", "types", "lint",
                                               "tests", "ast"])
        self.assertEqual(len(results), 6)


class RenderDeclarationTests(unittest.TestCase):
    def test_covered_only_when_ran_and_passed(self):
        results = [
            {"name": "ownership", "ran": True, "passed": True, "findings": [], "reason": None},
            {"name": "secrets", "ran": True, "passed": False, "findings": ["bad"], "reason": None},
            {"name": "types", "ran": False, "passed": False, "findings": [], "reason": "no tsconfig"},
        ]
        text = sieve.render_declaration(results)
        self.assertIn("ownership", text.split("NOT checked")[0].split("FAILED")[0])
        self.assertIn("[secrets] bad", text)
        self.assertIn("types (no tsconfig)", text)

    def test_never_claims_a_not_run_checker_as_covered(self):
        results = [{"name": "tests", "ran": False, "passed": False, "findings": [],
                    "reason": "not safe to run"}]
        text = sieve.render_declaration(results)
        self.assertNotIn("Already checked and passed", text)
        self.assertIn("NOT checked", text)

    def test_empty_results_still_renders(self):
        text = sieve.render_declaration([])
        self.assertIn("Sieve", text)


if __name__ == "__main__":
    unittest.main()
