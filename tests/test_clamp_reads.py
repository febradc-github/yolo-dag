"""Unit tests for meter.clamp's M3a bounded-read logic (decide_read and its
interval-math helpers)."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from meter import clamp, store


class IntervalHelperTests(unittest.TestCase):
    def test_merge_overlapping(self):
        self.assertEqual(clamp._merge_intervals([(1, 5), (4, 10)]), [[1, 10]])

    def test_merge_adjacent(self):
        self.assertEqual(clamp._merge_intervals([(1, 5), (6, 10)]), [[1, 10]])

    def test_merge_disjoint(self):
        self.assertEqual(clamp._merge_intervals([(1, 5), (20, 30)]), [[1, 5], [20, 30]])

    def test_fully_covered_true(self):
        self.assertTrue(clamp._fully_covered((10, 20), [(1, 30)]))

    def test_fully_covered_false_gap(self):
        self.assertFalse(clamp._fully_covered((10, 20), [(1, 12), (18, 30)]))

    def test_generated_path_detection(self):
        self.assertTrue(clamp._is_generated_shaped("dist/bundle.js"))
        self.assertTrue(clamp._is_generated_shaped("src/foo.min.js"))
        self.assertTrue(clamp._is_generated_shaped("node_modules/pkg/index.js"))
        self.assertFalse(clamp._is_generated_shaped("src/service.ts"))


class DecideReadTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmpdir.name)
        self.run_dir = self.repo_root / ".dag" / "runs" / "run-1"
        self.run_dir.mkdir(parents=True)
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(store._SCHEMA)

    def tearDown(self):
        self.conn.close()
        self._tmpdir.cleanup()

    def _write(self, rel_path: str, lines: int) -> None:
        path = self.repo_root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(f"line {i}" for i in range(lines)) + "\n", encoding="utf-8")

    def _write_tasks(self, owns: list[str]) -> None:
        (self.run_dir / "tasks.json").write_text(
            json.dumps({"tasks": [{"id": "T-001", "owns": owns}]}), encoding="utf-8")

    def test_requested_offset_is_never_touched(self):
        self._write("src/big.py", 1000)
        self._write_tasks([])
        decision = clamp.decide_read(
            conn=self.conn, repo_root=self.repo_root, run_dir=self.run_dir, run_id="run-1",
            node_id="T-001", agent_id="a1", rel_path="src/big.py",
            requested_offset=10, requested_limit=50, full_read_threshold=400,
        )
        self.assertIsNone(decision)

    def test_owned_file_is_never_touched(self):
        self._write("src/big.py", 1000)
        self._write_tasks(["src/big.py"])
        decision = clamp.decide_read(
            conn=self.conn, repo_root=self.repo_root, run_dir=self.run_dir, run_id="run-1",
            node_id="T-001", agent_id="a1", rel_path="src/big.py",
            requested_offset=None, requested_limit=None, full_read_threshold=400,
        )
        self.assertIsNone(decision)

    def test_small_file_is_never_touched(self):
        self._write("src/small.py", 10)
        self._write_tasks([])
        decision = clamp.decide_read(
            conn=self.conn, repo_root=self.repo_root, run_dir=self.run_dir, run_id="run-1",
            node_id="T-001", agent_id="a1", rel_path="src/small.py",
            requested_offset=None, requested_limit=None, full_read_threshold=400,
        )
        self.assertIsNone(decision)

    def test_no_index_opinion_is_never_touched(self):
        self._write("src/big.py", 1000)
        self._write_tasks([])
        decision = clamp.decide_read(
            conn=self.conn, repo_root=self.repo_root, run_dir=self.run_dir, run_id="run-1",
            node_id="T-001", agent_id="a1", rel_path="src/big.py",
            requested_offset=None, requested_limit=None, full_read_threshold=400,
        )
        self.assertIsNone(decision)

    def test_rewrites_to_bounded_span_when_index_has_an_opinion(self):
        self._write("src/big.py", 1000)
        self._write_tasks([])
        store.set_dossier_spans(self.conn, run_id="run-1", node="T-001",
                                 spans=[{"file": "src/big.py", "start": 100, "end": 110}])
        decision = clamp.decide_read(
            conn=self.conn, repo_root=self.repo_root, run_dir=self.run_dir, run_id="run-1",
            node_id="T-001", agent_id="a1", rel_path="src/big.py",
            requested_offset=None, requested_limit=None, full_read_threshold=400, margin=20,
        )
        self.assertEqual(decision["kind"], "rewrite")
        self.assertEqual(decision["offset"], 80)
        self.assertEqual(decision["limit"], 51)  # 80..130 inclusive

    def test_repeat_read_of_fully_covered_span_is_denied(self):
        self._write("src/big.py", 1000)
        self._write_tasks([])
        store.set_dossier_spans(self.conn, run_id="run-1", node="T-001",
                                 spans=[{"file": "src/big.py", "start": 100, "end": 110}])
        first = clamp.decide_read(
            conn=self.conn, repo_root=self.repo_root, run_dir=self.run_dir, run_id="run-1",
            node_id="T-001", agent_id="a1", rel_path="src/big.py",
            requested_offset=None, requested_limit=None, full_read_threshold=400,
        )
        self.assertEqual(first["kind"], "rewrite")
        second = clamp.decide_read(
            conn=self.conn, repo_root=self.repo_root, run_dir=self.run_dir, run_id="run-1",
            node_id="T-001", agent_id="a1", rel_path="src/big.py",
            requested_offset=None, requested_limit=None, full_read_threshold=400,
        )
        self.assertEqual(second["kind"], "deny")

    def test_generated_file_is_never_touched(self):
        self._write("dist/bundle.js", 1000)
        self._write_tasks([])
        store.set_dossier_spans(self.conn, run_id="run-1", node="T-001",
                                 spans=[{"file": "dist/bundle.js", "start": 1, "end": 5}])
        decision = clamp.decide_read(
            conn=self.conn, repo_root=self.repo_root, run_dir=self.run_dir, run_id="run-1",
            node_id="T-001", agent_id="a1", rel_path="dist/bundle.js",
            requested_offset=None, requested_limit=None, full_read_threshold=400,
        )
        self.assertIsNone(decision)

    def test_missing_file_is_never_touched(self):
        self._write_tasks([])
        decision = clamp.decide_read(
            conn=self.conn, repo_root=self.repo_root, run_dir=self.run_dir, run_id="run-1",
            node_id="T-001", agent_id="a1", rel_path="src/nope.py",
            requested_offset=None, requested_limit=None, full_read_threshold=400,
        )
        self.assertIsNone(decision)


if __name__ == "__main__":
    unittest.main()
