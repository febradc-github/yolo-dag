"""Unit tests for meter.receipts (M1) and meter.schema_lite.

Run with: python3 -m unittest discover -s tests -v
No third-party test dependencies — stdlib unittest only, matching the plugin's
"Python 3.11+, standard library only" constraint (meter-handoff.md Section 5.3).
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from meter import receipts, schema_lite, store


VALID_RECEIPT = {
    "v": 1, "node": "T-003", "status": "done",
    "files": [{"path": "src/a.ts", "spans": [[10, 48]], "action": "modify"}],
    "symbols_touched": ["AuthService.refresh"],
    "ac": [{"id": "AC-02", "met": True, "evidence": "tests/auth.spec.ts:44"}],
    "verification": {"cmd": "npm test -- auth", "exit": 0},
    "contracts": {"owned": ["C-01"], "consumed": []},
    "risks": ["refresh path untested under clock skew"],
    "worktree": "/tmp/wt", "commit": "abc123",
}


def _fenced(receipt: dict) -> str:
    return "some prose\n```dag-receipt\n" + json.dumps(receipt) + "\n```\n"


class SchemaLiteTests(unittest.TestCase):
    def test_valid_receipt_has_no_errors(self):
        errors = schema_lite.validate(VALID_RECEIPT, receipts._schema())
        self.assertEqual(errors, [])

    def test_missing_required_field(self):
        bad = dict(VALID_RECEIPT)
        del bad["status"]
        errors = schema_lite.validate(bad, receipts._schema())
        self.assertTrue(any("status" in e for e in errors))

    def test_wrong_status_enum(self):
        bad = dict(VALID_RECEIPT, status="finished")
        errors = schema_lite.validate(bad, receipts._schema())
        self.assertTrue(any("finished" in e for e in errors))

    def test_const_v_must_be_1(self):
        bad = dict(VALID_RECEIPT, v=2)
        errors = schema_lite.validate(bad, receipts._schema())
        self.assertTrue(any("constant" in e for e in errors))

    def test_additional_property_rejected(self):
        bad = dict(VALID_RECEIPT, unexpected_field="x")
        errors = schema_lite.validate(bad, receipts._schema())
        self.assertTrue(any("unexpected_field" in e for e in errors))

    def test_span_must_have_two_ints(self):
        bad = json.loads(json.dumps(VALID_RECEIPT))
        bad["files"][0]["spans"] = [[10, 48, 99]]
        errors = schema_lite.validate(bad, receipts._schema())
        self.assertTrue(any("spans" in e for e in errors))


class ExtractReceiptBlockTests(unittest.TestCase):
    def test_extracts_last_block_when_multiple_present(self):
        text = _fenced({"v": 1, "node": "A"}) + "more prose\n" + _fenced({"v": 1, "node": "B"})
        raw = receipts.extract_receipt_block(text)
        self.assertEqual(json.loads(raw)["node"], "B")

    def test_none_when_absent(self):
        self.assertIsNone(receipts.extract_receipt_block("no receipt here"))

    def test_none_on_empty_string(self):
        self.assertIsNone(receipts.extract_receipt_block(""))


class ParseAndValidateTests(unittest.TestCase):
    def test_valid_block_round_trips(self):
        receipt, errors = receipts.parse_and_validate(_fenced(VALID_RECEIPT))
        self.assertEqual(errors, [])
        self.assertEqual(receipt["node"], "T-003")

    def test_missing_block_reports_error_and_none(self):
        receipt, errors = receipts.parse_and_validate("no block here")
        self.assertIsNone(receipt)
        self.assertTrue(errors)

    def test_malformed_json_reports_error(self):
        text = "```dag-receipt\n{not json\n```"
        receipt, errors = receipts.parse_and_validate(text)
        self.assertIsNone(receipt)
        self.assertTrue(any("not valid JSON" in e for e in errors))

    def test_schema_invalid_still_returns_parsed_dict(self):
        bad = dict(VALID_RECEIPT)
        del bad["status"]
        receipt, errors = receipts.parse_and_validate(_fenced(bad))
        self.assertIsNotNone(receipt)
        self.assertTrue(errors)


class CheckOutcomeTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.plugin_data_dir = Path(self._tmpdir.name)
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(store._SCHEMA)

    def tearDown(self):
        self.conn.close()
        self._tmpdir.cleanup()

    def _transcript(self, text: str) -> str:
        path = Path(self._tmpdir.name) / "transcript.jsonl"
        path.write_text(json.dumps({"message": {"role": "assistant", "content": text}}) + "\n",
                         encoding="utf-8")
        return str(path)

    def test_valid_receipt_never_blocks(self):
        transcript_path = self._transcript(_fenced(VALID_RECEIPT))
        outcome = receipts.check(self.conn, run_id="r1", node_id="T-003", agent_id="a1",
                                  transcript_path=transcript_path, repair_turns=1)
        self.assertFalse(outcome.block)
        self.assertEqual(outcome.attempt, 1)

    def test_invalid_receipt_blocks_within_repair_budget(self):
        transcript_path = self._transcript("no receipt at all")
        outcome = receipts.check(self.conn, run_id="r1", node_id="T-003", agent_id="a1",
                                  transcript_path=transcript_path, repair_turns=1)
        self.assertTrue(outcome.block)
        self.assertEqual(outcome.attempt, 1)

    def test_second_failure_lets_through_never_deadlocks(self):
        transcript_path = self._transcript("no receipt at all")
        first = receipts.check(self.conn, run_id="r1", node_id="T-003", agent_id="a1",
                                transcript_path=transcript_path, repair_turns=1)
        second = receipts.check(self.conn, run_id="r1", node_id="T-003", agent_id="a1",
                                 transcript_path=transcript_path, repair_turns=1)
        self.assertTrue(first.block)
        self.assertFalse(second.block)
        self.assertEqual(second.attempt, 2)

    def test_missing_transcript_path_never_blocks_forever(self):
        outcome = receipts.check(self.conn, run_id="r1", node_id="T-003", agent_id="a1",
                                  transcript_path=None, repair_turns=1)
        self.assertTrue(outcome.block)  # attempt 1: still within budget
        outcome2 = receipts.check(self.conn, run_id="r1", node_id="T-003", agent_id="a1",
                                   transcript_path=None, repair_turns=1)
        self.assertFalse(outcome2.block)  # attempt 2: repair budget exhausted, let through


if __name__ == "__main__":
    unittest.main()
