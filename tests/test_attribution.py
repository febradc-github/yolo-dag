"""Unit tests for meter.attribution (v2 M14)."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from meter import attribution, store


class BuildExposuresTests(unittest.TestCase):
    def test_owned_files_are_not_exposures(self):
        exposures = attribution.build_exposures(
            owns=["src/a.py"], contracts_text={}, neighbour_paths=[], sibling_receipts=[])
        self.assertEqual(exposures, [])

    def test_neighbour_files_are_exposures(self):
        exposures = attribution.build_exposures(
            owns=[], contracts_text={}, neighbour_paths=["src/b.py"], sibling_receipts=[])
        self.assertEqual(exposures, [{"unit_type": "neighbour_file", "unit_label": "src/b.py",
                                       "token": "src/b.py"}])

    def test_contracts_are_exposures(self):
        exposures = attribution.build_exposures(
            owns=[], contracts_text={"C-AUTH": "text"}, neighbour_paths=[], sibling_receipts=[])
        self.assertEqual(exposures, [{"unit_type": "contract", "unit_label": "C-AUTH",
                                       "token": "C-AUTH"}])

    def test_sibling_receipts_are_exposures(self):
        exposures = attribution.build_exposures(
            owns=[], contracts_text={}, neighbour_paths=[],
            sibling_receipts=[{"node": "T-001", "status": "done"}])
        self.assertEqual(exposures, [{"unit_type": "sibling_receipt", "unit_label": "T-001",
                                       "token": "T-001"}])

    def test_sibling_receipt_without_node_is_skipped(self):
        exposures = attribution.build_exposures(
            owns=[], contracts_text={}, neighbour_paths=[], sibling_receipts=[{"status": "done"}])
        self.assertEqual(exposures, [])


class RecordReferencesTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(store._SCHEMA)

    def tearDown(self):
        self.conn.close()
        self._tmpdir.cleanup()

    def _write_transcript(self, blocks: list[dict]) -> str:
        path = Path(self._tmpdir.name) / "t.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps({"message": {"role": "assistant", "content": blocks}}) + "\n")
        return str(path)

    def test_no_exposures_returns_zero(self):
        transcript = self._write_transcript([{"type": "text", "text": "hello"}])
        self.assertEqual(
            attribution.record_references(self.conn, run_id="r1", node="T-001",
                                           transcript_path=transcript), 0)

    def test_mention_in_text_marks_referenced(self):
        attribution.record_exposures(self.conn, run_id="r1", node="T-001", agent_type="task-worker",
                                      exposures=[{"unit_type": "contract", "unit_label": "C-AUTH",
                                                  "token": "C-AUTH"}])
        transcript = self._write_transcript(
            [{"type": "text", "text": "I implemented my side of C-AUTH."}])
        count = attribution.record_references(self.conn, run_id="r1", node="T-001",
                                               transcript_path=transcript)
        self.assertEqual(count, 1)
        rows = store.get_exposures_for_node(self.conn, run_id="r1", node="T-001")
        self.assertTrue(rows[0]["referenced"])

    def test_tool_call_targeting_file_marks_referenced(self):
        attribution.record_exposures(self.conn, run_id="r1", node="T-001", agent_type="task-worker",
                                      exposures=[{"unit_type": "neighbour_file",
                                                  "unit_label": "src/b.py", "token": "src/b.py"}])
        transcript = self._write_transcript(
            [{"type": "tool_use", "name": "Read", "input": {"file_path": "src/b.py"}}])
        count = attribution.record_references(self.conn, run_id="r1", node="T-001",
                                               transcript_path=transcript)
        self.assertEqual(count, 1)

    def test_unmentioned_exposure_stays_unreferenced(self):
        attribution.record_exposures(self.conn, run_id="r1", node="T-001", agent_type="task-worker",
                                      exposures=[{"unit_type": "contract", "unit_label": "C-AUTH",
                                                  "token": "C-AUTH"}])
        transcript = self._write_transcript([{"type": "text", "text": "unrelated work"}])
        count = attribution.record_references(self.conn, run_id="r1", node="T-001",
                                               transcript_path=transcript)
        self.assertEqual(count, 0)

    def test_already_referenced_is_not_recounted(self):
        attribution.record_exposures(self.conn, run_id="r1", node="T-001", agent_type="task-worker",
                                      exposures=[{"unit_type": "contract", "unit_label": "C-AUTH",
                                                  "token": "C-AUTH"}])
        transcript = self._write_transcript([{"type": "text", "text": "C-AUTH mentioned"}])
        attribution.record_references(self.conn, run_id="r1", node="T-001", transcript_path=transcript)
        second_count = attribution.record_references(self.conn, run_id="r1", node="T-001",
                                                       transcript_path=transcript)
        self.assertEqual(second_count, 0)

    def test_missing_transcript_returns_zero(self):
        attribution.record_exposures(self.conn, run_id="r1", node="T-001", agent_type="task-worker",
                                      exposures=[{"unit_type": "contract", "unit_label": "C-AUTH",
                                                  "token": "C-AUTH"}])
        count = attribution.record_references(self.conn, run_id="r1", node="T-001",
                                               transcript_path="/nonexistent.jsonl")
        self.assertEqual(count, 0)


class ReportSectionTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(store._SCHEMA)

    def tearDown(self):
        self.conn.close()

    def test_none_when_no_exposures(self):
        self.assertIsNone(attribution.render_report_section(self.conn, "r1"))

    def test_reports_rate(self):
        attribution.record_exposures(self.conn, run_id="r1", node="T-001", agent_type="task-worker",
                                      exposures=[{"unit_type": "contract", "unit_label": "C-AUTH",
                                                  "token": "C-AUTH"}])
        section = attribution.render_report_section(self.conn, "r1")
        self.assertIn("task-worker", section)
        self.assertIn("contract", section)
        self.assertIn("0/1", section)


if __name__ == "__main__":
    unittest.main()
