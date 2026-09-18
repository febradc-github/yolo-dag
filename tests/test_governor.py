"""Unit tests for meter.governor (M6)."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from meter import governor, store


class RecalibrateWeightsTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.plugin_data_dir = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _write_baseline(self, data: dict) -> None:
        (self.plugin_data_dir / "baseline.json").write_text(json.dumps(data), encoding="utf-8")

    def test_no_baseline_returns_none(self):
        self.assertIsNone(governor.recalibrate_weights(self.plugin_data_dir))

    def test_empty_baseline_returns_none(self):
        self._write_baseline({})
        self.assertIsNone(governor.recalibrate_weights(self.plugin_data_dir))

    def test_average_agent_type_gets_weight_one(self):
        self._write_baseline({
            "fp1:full": {"runs": 1, "by_agent_type": {
                "task-worker": {"samples": 2, "in_tok_total": 2000, "out_tok_total": 0},
                "task-reviewer": {"samples": 2, "in_tok_total": 2000, "out_tok_total": 0},
            }},
        })
        weights = governor.recalibrate_weights(self.plugin_data_dir)
        self.assertAlmostEqual(weights["full"]["task-worker"], 1.0)
        self.assertAlmostEqual(weights["full"]["task-reviewer"], 1.0)

    def test_expensive_agent_type_gets_higher_weight(self):
        self._write_baseline({
            "fp1:full": {"runs": 1, "by_agent_type": {
                "cheap-agent": {"samples": 1, "in_tok_total": 1000, "out_tok_total": 0},
                "expensive-agent": {"samples": 1, "in_tok_total": 5000, "out_tok_total": 0},
            }},
        })
        weights = governor.recalibrate_weights(self.plugin_data_dir)
        self.assertGreater(weights["full"]["expensive-agent"], weights["full"]["cheap-agent"])

    def test_writes_weights_json(self):
        self._write_baseline({
            "fp1:full": {"runs": 1, "by_agent_type": {
                "task-worker": {"samples": 1, "in_tok_total": 1000, "out_tok_total": 0},
            }},
        })
        governor.recalibrate_weights(self.plugin_data_dir)
        written = json.loads((self.plugin_data_dir / "weights.json").read_text(encoding="utf-8"))
        self.assertIn("weights", written)
        self.assertIn("fallback_tier_weights", written)
        self.assertEqual(written["fallback_tier_weights"], governor.FALLBACK_TIER_WEIGHTS)

    def test_zero_sample_agent_type_is_skipped(self):
        self._write_baseline({
            "fp1:full": {"runs": 1, "by_agent_type": {
                "task-worker": {"samples": 0, "in_tok_total": 0, "out_tok_total": 0},
            }},
        })
        self.assertIsNone(governor.recalibrate_weights(self.plugin_data_dir))


class PredictCallBudgetTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.plugin_data_dir = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_no_data_falls_back_to_default(self):
        prediction = governor.predict_call_budget(self.plugin_data_dir, "fp1", "full", "task-worker")
        self.assertEqual(prediction, governor.DEFAULT_CALL_BUDGET)

    def test_uses_measured_average_when_present(self):
        (self.plugin_data_dir / "baseline.json").write_text(json.dumps({
            "fp1:full": {"runs": 1, "by_agent_type": {
                "task-worker": {"samples": 1, "call_samples": 2, "calls_total": 40},
            }},
        }), encoding="utf-8")
        prediction = governor.predict_call_budget(self.plugin_data_dir, "fp1", "full", "task-worker")
        self.assertEqual(prediction, 20.0)

    def test_never_returns_none_or_raises_on_malformed_baseline(self):
        (self.plugin_data_dir / "baseline.json").write_text("{not json", encoding="utf-8")
        prediction = governor.predict_call_budget(self.plugin_data_dir, "fp1", "full", "task-worker")
        self.assertEqual(prediction, governor.DEFAULT_CALL_BUDGET)


class ModelRoutingReportTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.plugin_data_dir = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_none_when_no_baseline(self):
        self.assertIsNone(governor.model_routing_report(self.plugin_data_dir))

    def test_reports_average_tokens_per_agent_type(self):
        (self.plugin_data_dir / "baseline.json").write_text(json.dumps({
            "fp1:full": {"runs": 1, "by_agent_type": {
                "task-worker": {"samples": 2, "in_tok_total": 4000, "out_tok_total": 1000},
            }},
        }), encoding="utf-8")
        report = governor.model_routing_report(self.plugin_data_dir)
        self.assertIn("task-worker", report)
        self.assertIn("2,500", report)  # (4000+1000)/2


class CacheTtlRecommendationTests(unittest.TestCase):
    def test_none_when_no_rounds(self):
        self.assertIsNone(governor.cache_ttl_recommendation([]))

    def test_none_when_all_gaps_short(self):
        rounds = [{"idle_gap_ms": 1000}, {"idle_gap_ms": None}]
        self.assertIsNone(governor.cache_ttl_recommendation(rounds))

    def test_recommends_when_gap_exceeds_five_minutes(self):
        rounds = [{"idle_gap_ms": 6 * 60 * 1000}]
        result = governor.cache_ttl_recommendation(rounds)
        self.assertIsNotNone(result)
        self.assertIn("CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL", result)


class ToolCallCountStoreTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(store._SCHEMA)

    def tearDown(self):
        self.conn.close()

    def test_increment_starts_at_one(self):
        count = store.increment_tool_call_count(self.conn, agent_id="a1", run_id="r1", node="T-001")
        self.assertEqual(count, 1)

    def test_increment_accumulates(self):
        store.increment_tool_call_count(self.conn, agent_id="a1", run_id="r1", node="T-001")
        store.increment_tool_call_count(self.conn, agent_id="a1", run_id="r1", node="T-001")
        count = store.increment_tool_call_count(self.conn, agent_id="a1", run_id="r1", node="T-001")
        self.assertEqual(count, 3)

    def test_get_tool_call_count_unknown_agent_is_zero(self):
        self.assertEqual(store.get_tool_call_count(self.conn, "nope"), 0)

    def test_warned_flag_round_trips(self):
        store.increment_tool_call_count(self.conn, agent_id="a1", run_id="r1", node="T-001")
        self.assertFalse(store.was_call_warned(self.conn, "a1"))
        store.mark_call_warned(self.conn, "a1")
        self.assertTrue(store.was_call_warned(self.conn, "a1"))


if __name__ == "__main__":
    unittest.main()
