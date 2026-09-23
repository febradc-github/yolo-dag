"""Unit tests for meter.ledger (M0) — previously untested despite being the
module every cost/budget figure ("the source of truth, never memory")
downstream depends on.

Run with: python3 -m unittest discover -s tests -v
No third-party test dependencies — stdlib unittest only, matching the plugin's
"Python 3.11+, standard library only" constraint (meter-handoff.md Section 5.3).
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from meter import ledger, store


def _transcript_line(*, input_tokens=0, output_tokens=0, cache_read=0, cache_write=0,
                      model: str | None = None) -> str:
    usage = {"input_tokens": input_tokens, "output_tokens": output_tokens,
              "cache_read_input_tokens": cache_read, "cache_creation_input_tokens": cache_write}
    message: dict = {"usage": usage}
    if model is not None:
        message["model"] = model
    return json.dumps({"message": message})


class ExtractDagNodeTests(unittest.TestCase):
    def test_extracts_run_and_node(self):
        self.assertEqual(ledger.extract_dag_node("preamble\nDAG-NODE: 2026-08-21-a3f9/T-003\nmore"),
                          ("2026-08-21-a3f9", "T-003"))

    def test_no_marker_returns_none(self):
        self.assertIsNone(ledger.extract_dag_node("just a normal prompt"))

    def test_empty_string_returns_none(self):
        self.assertIsNone(ledger.extract_dag_node(""))


class PhaseOfTests(unittest.TestCase):
    def test_p1_through_p4_and_p6(self):
        for prefix, expected in (("P1", "1"), ("P2", "2"), ("P3", "3"), ("P4", "4"), ("P6", "6")):
            self.assertEqual(ledger.phase_of(f"{prefix}-something"), expected)

    def test_task_shaped_ids_are_phase_5(self):
        self.assertEqual(ledger.phase_of("T-003"), "5")
        self.assertEqual(ledger.phase_of("T-003-worker"), "5")
        self.assertEqual(ledger.phase_of("T-003-reviewer"), "5")

    def test_unrecognized_shape_is_unknown(self):
        self.assertEqual(ledger.phase_of("something-else"), "unknown")


class CorrelatorTests(unittest.TestCase):
    def test_fifo_claim_order(self):
        c = ledger.Correlator()
        c.push("session-1", "run-1", "T-001", "prompt one")
        c.push("session-1", "run-1", "T-002", "prompt two")
        self.assertEqual(c.claim("session-1"), ("run-1", "T-001", "prompt one"))
        self.assertEqual(c.claim("session-1"), ("run-1", "T-002", "prompt two"))

    def test_claim_on_empty_queue_returns_none(self):
        c = ledger.Correlator()
        self.assertIsNone(c.claim("nonexistent-session"))

    def test_claim_drains_queue(self):
        c = ledger.Correlator()
        c.push("s", "run-1", "T-001", "p")
        c.claim("s")
        self.assertIsNone(c.claim("s"))

    def test_stale_entry_past_ttl_is_dropped_not_returned(self):
        c = ledger.Correlator()
        c.push("s", "run-1", "T-001", "p")
        # Backdate the queued entry past the TTL rather than sleeping in a test.
        stale_time = time.time() - ledger._PENDING_TTL_SECONDS - 1
        c._queues["s"][0] = (stale_time,) + c._queues["s"][0][1:]
        self.assertIsNone(c.claim("s"))

    def test_sessions_are_independent(self):
        c = ledger.Correlator()
        c.push("session-a", "run-1", "T-001", "p")
        self.assertIsNone(c.claim("session-b"))
        self.assertIsNotNone(c.claim("session-a"))


class TierFromModelTests(unittest.TestCase):
    def test_haiku_substring_match(self):
        self.assertEqual(ledger.tier_from_model("claude-haiku-4-5-20251001"), "haiku")

    def test_sonnet_substring_match(self):
        self.assertEqual(ledger.tier_from_model("claude-sonnet-5"), "sonnet")

    def test_unrecognized_model_falls_back_to_inherit(self):
        self.assertEqual(ledger.tier_from_model("claude-opus-5"), "inherit")

    def test_none_falls_back_to_inherit(self):
        self.assertEqual(ledger.tier_from_model(None), "inherit")

    def test_empty_string_falls_back_to_inherit(self):
        self.assertEqual(ledger.tier_from_model(""), "inherit")

    def test_case_insensitive(self):
        self.assertEqual(ledger.tier_from_model("CLAUDE-HAIKU-4-5"), "haiku")


class ParseTranscriptUsageTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _write(self, name: str, lines: list[str]) -> str:
        path = self.dir / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(path)

    def test_sums_usage_across_lines(self):
        path = self._write("t.jsonl", [
            _transcript_line(input_tokens=100, output_tokens=10, model="claude-sonnet-5"),
            _transcript_line(input_tokens=50, output_tokens=5, cache_read=20, cache_write=30),
        ])
        result = ledger.parse_transcript_usage(path)
        self.assertEqual(result["in_tok"], 150)
        self.assertEqual(result["out_tok"], 15)
        self.assertEqual(result["cache_read"], 20)
        self.assertEqual(result["cache_write"], 30)
        self.assertIsNone(result["reason"])

    def test_captures_model_from_transcript(self):
        path = self._write("t.jsonl", [_transcript_line(input_tokens=1, model="claude-haiku-4-5")])
        result = ledger.parse_transcript_usage(path)
        self.assertEqual(result["model"], "claude-haiku-4-5")

    def test_missing_model_field_is_none(self):
        path = self._write("t.jsonl", [_transcript_line(input_tokens=1)])
        result = ledger.parse_transcript_usage(path)
        self.assertIsNone(result["model"])

    def test_missing_file_never_guesses(self):
        result = ledger.parse_transcript_usage(str(self.dir / "does-not-exist.jsonl"))
        self.assertIsNone(result["in_tok"])
        self.assertEqual(result["reason"], "transcript_not_found")

    def test_unparseable_lines_are_skipped_not_fatal(self):
        path = self._write("t.jsonl", ["not json at all", _transcript_line(input_tokens=42)])
        result = ledger.parse_transcript_usage(path)
        self.assertEqual(result["in_tok"], 42)

    def test_well_formed_but_no_usage_block_records_reason(self):
        path = self._write("t.jsonl", [json.dumps({"message": {"role": "assistant"}})])
        result = ledger.parse_transcript_usage(path)
        self.assertIsNone(result["in_tok"])
        self.assertEqual(result["reason"], "no_usage_block_found")

    def test_blank_lines_are_skipped(self):
        path = self._write("t.jsonl", ["", "  ", _transcript_line(input_tokens=7)])
        result = ledger.parse_transcript_usage(path)
        self.assertEqual(result["in_tok"], 7)


class RecordSubagentTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(store._SCHEMA)
        self._tmpdir = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmpdir.name)

    def tearDown(self):
        self.conn.close()
        self._tmpdir.cleanup()

    def test_record_subagent_start_writes_row_with_no_model_yet(self):
        ledger.record_subagent_start(self.conn, run_id="run-1", node_id="T-001",
                                      agent_id="agent-1", agent_type="task-worker",
                                      started_at=1000)
        row = store.get_row_by_agent_id(self.conn, "agent-1")
        self.assertEqual(row["run_id"], "run-1")
        self.assertEqual(row["node"], "T-001")
        self.assertIsNone(row["model"])

    def test_record_subagent_stop_populates_model_from_transcript(self):
        ledger.record_subagent_start(self.conn, run_id="run-1", node_id="T-001",
                                      agent_id="agent-1", agent_type="task-worker",
                                      started_at=1000)
        transcript = self.dir / "t.jsonl"
        transcript.write_text(_transcript_line(input_tokens=5, model="claude-sonnet-5") + "\n",
                               encoding="utf-8")
        row = ledger.record_subagent_stop(
            self.conn, run_id="run-1", node_id="T-001", agent_id="agent-1",
            agent_type="task-worker", transcript_path=str(transcript),
            started_at=1000, ended_at=1010,
        )
        self.assertEqual(row["model"], "claude-sonnet-5")
        self.assertEqual(ledger.tier_from_model(row["model"]), "sonnet")
        persisted = store.get_row_by_agent_id(self.conn, "agent-1")
        self.assertEqual(persisted["model"], "claude-sonnet-5")

    def test_record_subagent_stop_without_transcript_path_has_no_model(self):
        ledger.record_subagent_start(self.conn, run_id="run-1", node_id="T-001",
                                      agent_id="agent-1", agent_type="task-worker",
                                      started_at=1000)
        row = ledger.record_subagent_stop(
            self.conn, run_id="run-1", node_id="T-001", agent_id="agent-1",
            agent_type="task-worker", transcript_path=None,
            started_at=1000, ended_at=1010,
        )
        self.assertIsNone(row["model"])
        self.assertIsNone(row["in_tok"])


if __name__ == "__main__":
    unittest.main()
