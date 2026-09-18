"""Unit tests for meter.throttle (v2 M13, 13a deny-write and 13b narration)."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from meter import store, throttle


class GeneratedShapedTests(unittest.TestCase):
    def test_dist_path(self):
        self.assertTrue(throttle.is_generated_shaped("dist/bundle.js"))

    def test_min_js_suffix(self):
        self.assertTrue(throttle.is_generated_shaped("src/app.min.js"))

    def test_normal_source_file(self):
        self.assertFalse(throttle.is_generated_shaped("src/service.ts"))


class ChangeRatioTests(unittest.TestCase):
    def test_identical_is_zero(self):
        self.assertEqual(throttle._change_ratio("a\nb\nc", "a\nb\nc"), 0.0)

    def test_completely_different_is_near_one(self):
        self.assertGreater(throttle._change_ratio("a\nb\nc", "x\ny\nz"), 0.9)

    def test_small_edit_is_small_ratio(self):
        old = "\n".join(f"line {i}" for i in range(100))
        new = old.replace("line 50", "line fifty")
        self.assertLess(throttle._change_ratio(old, new), 0.1)


class DecideWriteTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmpdir.name)
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(store._SCHEMA)

    def tearDown(self):
        self.conn.close()
        self._tmpdir.cleanup()

    def _write(self, rel_path: str, content: str) -> None:
        path = self.repo_root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_new_file_is_never_denied(self):
        decision = throttle.decide_write(
            conn=self.conn, repo_root=self.repo_root, agent_id="a1", rel_path="src/new.py",
            new_content="x = 1\n", is_tracked=False, rewrite_ratio=0.6, max_denials=2,
        )
        self.assertIsNone(decision)

    def test_untracked_existing_file_is_never_denied(self):
        self._write("src/scratch.py", "x = 1\n")
        decision = throttle.decide_write(
            conn=self.conn, repo_root=self.repo_root, agent_id="a1", rel_path="src/scratch.py",
            new_content="x = 2\n", is_tracked=False, rewrite_ratio=0.6, max_denials=2,
        )
        self.assertIsNone(decision)

    def test_generated_file_is_never_denied(self):
        self._write("dist/bundle.js", "old\n")
        decision = throttle.decide_write(
            conn=self.conn, repo_root=self.repo_root, agent_id="a1", rel_path="dist/bundle.js",
            new_content="new\n", is_tracked=True, rewrite_ratio=0.6, max_denials=2,
        )
        self.assertIsNone(decision)

    def test_small_change_to_tracked_file_is_denied(self):
        old = "\n".join(f"line {i}" for i in range(50))
        self._write("src/a.py", old)
        new_content = old.replace("line 25", "line twenty-five")
        decision = throttle.decide_write(
            conn=self.conn, repo_root=self.repo_root, agent_id="a1", rel_path="src/a.py",
            new_content=new_content, is_tracked=True, rewrite_ratio=0.6, max_denials=2,
        )
        self.assertEqual(decision["kind"], "deny")
        self.assertIn("Edit", decision["reason"])

    def test_large_rewrite_is_not_denied(self):
        old = "\n".join(f"old line {i}" for i in range(50))
        new = "\n".join(f"totally different content {i}" for i in range(50))
        self._write("src/a.py", old)
        decision = throttle.decide_write(
            conn=self.conn, repo_root=self.repo_root, agent_id="a1", rel_path="src/a.py",
            new_content=new, is_tracked=True, rewrite_ratio=0.6, max_denials=2,
        )
        self.assertIsNone(decision)

    def test_denial_cap_lets_through_after_max(self):
        old = "\n".join(f"line {i}" for i in range(50))
        new_content = old.replace("line 25", "line twenty-five")
        self._write("src/a.py", old)
        kwargs = dict(conn=self.conn, repo_root=self.repo_root, agent_id="a1",
                      rel_path="src/a.py", new_content=new_content, is_tracked=True,
                      rewrite_ratio=0.6, max_denials=2)
        first = throttle.decide_write(**kwargs)
        second = throttle.decide_write(**kwargs)
        third = throttle.decide_write(**kwargs)
        self.assertEqual(first["kind"], "deny")
        self.assertEqual(second["kind"], "deny")
        self.assertIsNone(third)  # cap of 2 reached; the 3rd attempt is let through


class NarrationRatioTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmpdir.cleanup()

    def _write_transcript(self, entries: list[dict]) -> str:
        path = Path(self._tmpdir.name) / "t.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry) + "\n")
        return str(path)

    def test_missing_transcript_returns_none(self):
        self.assertIsNone(throttle.narration_ratio(str(Path(self._tmpdir.name) / "nope.jsonl")))

    def test_computes_ratio_from_text_and_write_blocks(self):
        transcript = self._write_transcript([
            {"message": {"role": "assistant", "content": [
                {"type": "text", "text": "x" * 100},
                {"type": "tool_use", "name": "Write", "input": {"content": "y" * 50}},
            ]}},
        ])
        result = throttle.narration_ratio(transcript)
        self.assertEqual(result["prose_chars"], 100)
        self.assertEqual(result["artifact_chars"], 50)
        self.assertEqual(result["ratio"], 2.0)

    def test_edit_tool_uses_new_string_field(self):
        transcript = self._write_transcript([
            {"message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Edit", "input": {"new_string": "z" * 20}},
            ]}},
        ])
        result = throttle.narration_ratio(transcript)
        self.assertEqual(result["artifact_chars"], 20)

    def test_no_artifact_output_avoids_division_by_zero(self):
        transcript = self._write_transcript([
            {"message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}},
        ])
        result = throttle.narration_ratio(transcript)
        self.assertEqual(result["ratio"], 2.0)  # 2 chars / 1 (denom floor)


class RenderReportSectionTests(unittest.TestCase):
    def test_none_when_no_rows(self):
        self.assertIsNone(throttle.render_report_section([]))

    def test_averages_per_agent_type(self):
        rows = [
            {"agent_type": "task-worker", "ratio": 1.0},
            {"agent_type": "task-worker", "ratio": 3.0},
        ]
        section = throttle.render_report_section(rows)
        self.assertIn("task-worker", section)
        self.assertIn("2.0x", section)


if __name__ == "__main__":
    unittest.main()
