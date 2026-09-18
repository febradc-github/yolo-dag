"""Router-level integration test for v2 M10 Pull's delivery path — the
dossier is built in one hook call (PreToolUse on Agent) and delivered in a
later, separate hook call (SubagentStart) that shares no Python state with
the first, only what landed on disk. That handoff is exactly the kind of
thing a unit test on either half alone wouldn't catch if it broke.
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


class PullDeliveryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmpdir.name) / "repo"
        self.run_dir = self.repo_root / ".dag" / "runs" / "run-1"
        self.run_dir.mkdir(parents=True)
        _git(self.repo_root, "init", "-q")  # find_repo_root needs a real .git marker
        (self.run_dir / "tasks.json").write_text(json.dumps({
            "tasks": [{"id": "T-001", "title": "x", "contracts": ["C-A"]}],
            "contracts": {"C-A": "the contract text"},
        }), encoding="utf-8")

        conn = sqlite3.connect(":memory:")
        conn.executescript(store._SCHEMA)
        cfg = dict(config_mod.DEFAULTS)
        cfg["modules"] = {**cfg["modules"],
                          "dossier": {"enabled": True, "max_tokens": 4000, "max_denials": 2,
                                      "apply_to": []},
                          "pull": {"enabled": True, "promote_threshold": 0.8,
                                   "per_agent_override": {}}}
        self.ctx = router.Context(conn=conn, cfg=cfg, plugin_data_dir=Path(self._tmpdir.name),
                                   started_at=time.time(), token=None)

    def tearDown(self):
        self.ctx.conn.close()
        self._tmpdir.cleanup()

    def test_build_then_deliver_yields_manifest_as_additional_context(self):
        # Step 1: PreToolUse on Agent builds the dossier in pull mode.
        payload_pre = {
            "cwd": str(self.repo_root), "session_id": "sess-1",
            "tool_input": {"prompt": "DAG-NODE: run-1/T-001\ndo work",
                            "subagent_type": "task-worker"},
        }
        router.handle_tool_pre_agent(payload_pre, self.ctx)

        # Step 2: a separate SubagentStart request delivers it.
        payload_start = {"cwd": str(self.repo_root), "session_id": "sess-1",
                          "agent_id": "agent-1", "agent_type": "task-worker"}
        result = router.handle_subagent_start(payload_start, self.ctx)

        additional_context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Context available for T-001", additional_context)
        self.assertIn("contracts", additional_context)
        # The full contract text must NOT be inline — that's the whole point
        # of pull mode over push.
        self.assertNotIn("the contract text", additional_context)

    def test_reading_a_pulled_section_is_tracked(self):
        payload_pre = {
            "cwd": str(self.repo_root), "session_id": "sess-1",
            "tool_input": {"prompt": "DAG-NODE: run-1/T-001\ndo work",
                            "subagent_type": "task-worker"},
        }
        router.handle_tool_pre_agent(payload_pre, self.ctx)
        payload_start = {"cwd": str(self.repo_root), "session_id": "sess-1",
                          "agent_id": "agent-1", "agent_type": "task-worker"}
        router.handle_subagent_start(payload_start, self.ctx)

        section_path = self.run_dir / "meter" / "dossiers" / "T-001" / "contracts.md"
        self.assertTrue(section_path.is_file())

        payload_read = {"cwd": str(self.repo_root), "agent_id": "agent-1",
                         "tool_name": "Read", "tool_input": {"file_path": str(section_path)}}
        router.handle_tool_pre_dossier(payload_read, self.ctx)

        sections = store.get_pull_sections_for_node(self.ctx.conn, run_id="run-1", node="T-001")
        contract_section = next(s for s in sections if s["section_id"] == "contracts")
        self.assertEqual(contract_section["pulled"], 1)

    def test_grep_is_never_denied_in_pull_mode(self):
        # M2's "read the dossier first" enforcement must not fire for a
        # manifest that was already injected via additionalContext.
        payload_pre = {
            "cwd": str(self.repo_root), "session_id": "sess-1",
            "tool_input": {"prompt": "DAG-NODE: run-1/T-001\ndo work",
                            "subagent_type": "task-worker"},
        }
        router.handle_tool_pre_agent(payload_pre, self.ctx)
        payload_start = {"cwd": str(self.repo_root), "session_id": "sess-1",
                          "agent_id": "agent-1", "agent_type": "task-worker"}
        router.handle_subagent_start(payload_start, self.ctx)

        payload_grep = {"cwd": str(self.repo_root), "agent_id": "agent-1",
                         "tool_name": "Grep", "tool_input": {"pattern": "foo"}}
        result = router.handle_tool_pre_dossier(payload_grep, self.ctx)
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
