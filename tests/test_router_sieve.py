"""Router-level tests for meter.router._maybe_run_sieve — the highest-blast-
radius rewrite in this plugin (a wrong `updatedInput` here corrupts every
review's prompt). These exist specifically because that risk profile
warrants direct coverage beyond meter.sieve's own module tests: every one of
these confirms the conservative "any uncertainty returns None" contract
holds, not just that the checkers themselves are correct.
"""

from __future__ import annotations

import json
import subprocess
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from meter import config as config_mod
from meter import router
from meter import store


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                           check=True)


class MaybeRunSieveTests(unittest.TestCase):
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
        (self.repo_root / "src" / "a.py").write_text("x = 2\n", encoding="utf-8")
        _git(self.repo_root, "add", ".")
        _git(self.repo_root, "commit", "-q", "-m", "change")
        self.commit = _git(self.repo_root, "rev-parse", "HEAD").stdout.strip()

        self.run_dir = self.repo_root / ".dag" / "runs" / "run-1"
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "tasks.json").write_text(json.dumps({
            "tasks": [{"id": "T-001", "title": "x", "owns": ["src/a.py"]}],
        }), encoding="utf-8")

        conn = sqlite3.connect(":memory:")
        conn.executescript(store._SCHEMA)
        cfg = dict(config_mod.DEFAULTS)
        cfg["modules"] = {**cfg["modules"], "sieve": {"enabled": True,
                                                        "checkers": ["ownership", "secrets"]}}
        self.ctx = router.Context(conn=conn, cfg=cfg, plugin_data_dir=Path(self._tmpdir.name),
                                   started_at=time.time(), token=None)

    def tearDown(self):
        self.ctx.conn.close()
        self._tmpdir.cleanup()

    def _payload(self, prompt: str, subagent_type: str = "task-reviewer") -> dict:
        return {"cwd": str(self.repo_root),
                "tool_input": {"prompt": prompt, "subagent_type": subagent_type}}

    def _good_prompt(self) -> str:
        return (f"Review this task.\nWORKTREE: {self.repo_root}\nCOMMIT: {self.commit}\n"
                f"Do the review.")

    def test_valid_reviewer_spawn_gets_declaration_appended(self):
        payload = self._payload(self._good_prompt())
        decision = router._maybe_run_sieve(
            payload, self.ctx, run_id="run-1", node_id="T-001-reviewer",
            tool_input=payload["tool_input"], prompt_text=payload["tool_input"]["prompt"],
        )
        self.assertIsNotNone(decision)
        updated_prompt = decision["hookSpecificOutput"]["updatedInput"]["prompt"]
        self.assertIn("Sieve", updated_prompt)
        self.assertIn(self._good_prompt(), updated_prompt)  # original content preserved verbatim
        # updatedInput must re-emit every other field untouched.
        self.assertEqual(decision["hookSpecificOutput"]["updatedInput"]["subagent_type"],
                          "task-reviewer")

    def test_non_reviewer_spawn_is_never_touched(self):
        payload = self._payload(self._good_prompt(), subagent_type="task-worker")
        decision = router._maybe_run_sieve(
            payload, self.ctx, run_id="run-1", node_id="T-001-worker",
            tool_input=payload["tool_input"], prompt_text=payload["tool_input"]["prompt"],
        )
        self.assertIsNone(decision)

    def test_missing_worktree_line_is_never_touched(self):
        prompt = f"Review this.\nCOMMIT: {self.commit}\nGo."
        payload = self._payload(prompt)
        decision = router._maybe_run_sieve(
            payload, self.ctx, run_id="run-1", node_id="T-001-reviewer",
            tool_input=payload["tool_input"], prompt_text=prompt,
        )
        self.assertIsNone(decision)

    def test_missing_commit_line_is_never_touched(self):
        prompt = f"Review this.\nWORKTREE: {self.repo_root}\nGo."
        payload = self._payload(prompt)
        decision = router._maybe_run_sieve(
            payload, self.ctx, run_id="run-1", node_id="T-001-reviewer",
            tool_input=payload["tool_input"], prompt_text=prompt,
        )
        self.assertIsNone(decision)

    def test_commit_none_is_never_touched(self):
        prompt = f"Review this.\nWORKTREE: {self.repo_root}\nCOMMIT: none\nGo."
        payload = self._payload(prompt)
        decision = router._maybe_run_sieve(
            payload, self.ctx, run_id="run-1", node_id="T-001-reviewer",
            tool_input=payload["tool_input"], prompt_text=prompt,
        )
        self.assertIsNone(decision)

    def test_nonexistent_worktree_is_never_touched(self):
        prompt = f"Review this.\nWORKTREE: /nonexistent/path\nCOMMIT: {self.commit}\nGo."
        payload = self._payload(prompt)
        decision = router._maybe_run_sieve(
            payload, self.ctx, run_id="run-1", node_id="T-001-reviewer",
            tool_input=payload["tool_input"], prompt_text=prompt,
        )
        self.assertIsNone(decision)

    def test_unknown_task_id_is_never_touched(self):
        payload = self._payload(self._good_prompt())
        decision = router._maybe_run_sieve(
            payload, self.ctx, run_id="run-1", node_id="T-999-reviewer",
            tool_input=payload["tool_input"], prompt_text=payload["tool_input"]["prompt"],
        )
        self.assertIsNone(decision)

    def test_node_id_not_shaped_like_a_reviewer_is_never_touched(self):
        payload = self._payload(self._good_prompt())
        decision = router._maybe_run_sieve(
            payload, self.ctx, run_id="run-1", node_id="T-001",
            tool_input=payload["tool_input"], prompt_text=payload["tool_input"]["prompt"],
        )
        self.assertIsNone(decision)

    def test_sieve_disabled_is_never_touched(self):
        self.ctx.cfg["modules"]["sieve"]["enabled"] = False
        payload = self._payload(self._good_prompt())
        decision = router._maybe_run_sieve(
            payload, self.ctx, run_id="run-1", node_id="T-001-reviewer",
            tool_input=payload["tool_input"], prompt_text=payload["tool_input"]["prompt"],
        )
        self.assertIsNone(decision)

    def test_missing_cwd_is_never_touched(self):
        payload = {"tool_input": {"prompt": self._good_prompt(), "subagent_type": "task-reviewer"}}
        decision = router._maybe_run_sieve(
            payload, self.ctx, run_id="run-1", node_id="T-001-reviewer",
            tool_input=payload["tool_input"], prompt_text=payload["tool_input"]["prompt"],
        )
        self.assertIsNone(decision)


if __name__ == "__main__":
    unittest.main()
