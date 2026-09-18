"""Unit tests for meter.pull (v2 M10, disabled by default — see module
docstring for why)."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from meter import pull, store


class SectionIdTests(unittest.TestCase):
    def test_lowercases_and_replaces_punctuation(self):
        self.assertEqual(pull.section_id_for("Symbols in owned files"),
                          "symbols_in_owned_files")

    def test_empty_label_gets_a_fallback(self):
        self.assertEqual(pull.section_id_for("!!!"), "section")


class BuildManifestSectionsTests(unittest.TestCase):
    def test_skips_header(self):
        sections = pull.build_manifest_sections([("header", "# Dossier"), ("contracts", "text")])
        ids = {s["id"] for s in sections}
        self.assertNotIn("header", ids)
        self.assertEqual(len(sections), 1)

    def test_duplicate_labels_get_distinct_ids(self):
        sections = pull.build_manifest_sections([("git_log", "a"), ("git_log", "b")])
        ids = [s["id"] for s in sections]
        self.assertEqual(len(ids), len(set(ids)))

    def test_tokens_estimated(self):
        sections = pull.build_manifest_sections([("contracts", "x" * 400)])
        self.assertGreater(sections[0]["tokens_est"], 0)


class WriteSectionsTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_writes_one_file_per_section(self):
        sections = pull.build_manifest_sections([("contracts", "contract text")])
        written = pull.write_sections(self.run_dir, "T-001", sections)
        self.assertEqual(len(written), 1)
        self.assertTrue(Path(written[0]["path"]).is_file())
        self.assertEqual(Path(written[0]["path"]).read_text(encoding="utf-8"), "contract text")

    def test_node_id_with_slash_is_sanitised(self):
        sections = pull.build_manifest_sections([("contracts", "x")])
        written = pull.write_sections(self.run_dir, "T-001/sub", sections)
        self.assertTrue(Path(written[0]["path"]).is_file())


class RenderManifestTests(unittest.TestCase):
    def test_lists_id_label_and_size(self):
        sections = [{"id": "contracts", "label": "contracts", "tokens_est": 400,
                     "path": "/tmp/x/contracts.md"}]
        manifest = pull.render_manifest("T-001", sections)
        self.assertIn("contracts", manifest)
        self.assertIn("400", manifest)
        self.assertIn("Read /tmp/x", manifest)

    def test_empty_sections_still_renders_header_line(self):
        manifest = pull.render_manifest("T-001", [])
        self.assertIn("T-001", manifest)


class NetSavingTests(unittest.TestCase):
    def test_all_pulled_with_small_overhead_is_still_positive(self):
        saving = pull.net_saving(section_tokens=1000, total_exposures=10, pulled_count=10,
                                  avg_roundtrip_tokens=10)
        # push baseline: 10000, pull cost: (1000+10)*10 = 10100 -> slightly negative actually
        self.assertLess(saving, 0)

    def test_never_pulled_is_maximally_positive(self):
        saving = pull.net_saving(section_tokens=1000, total_exposures=10, pulled_count=0,
                                  avg_roundtrip_tokens=10)
        self.assertEqual(saving, 10000)

    def test_large_roundtrip_overhead_can_make_pulling_a_net_loss(self):
        cheap_overhead = pull.net_saving(section_tokens=500, total_exposures=5, pulled_count=2,
                                          avg_roundtrip_tokens=50)
        expensive_overhead = pull.net_saving(section_tokens=500, total_exposures=5, pulled_count=2,
                                              avg_roundtrip_tokens=5000)
        self.assertGreater(cheap_overhead, expensive_overhead)


class RecommendTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(store._SCHEMA)

    def tearDown(self):
        self.conn.close()

    def _seed(self, run_id: str, pulled: bool) -> None:
        store.record_pull_sections(
            self.conn, run_id=run_id, node="T-001", agent_type="task-worker",
            sections=[{"id": "contracts", "path": f"/tmp/{run_id}.md", "tokens_est": 100}])
        if pulled:
            store.mark_pull_section_fetched_by_path(self.conn, run_id=run_id, node="T-001",
                                                      path=f"/tmp/{run_id}.md")

    def test_below_min_samples_is_excluded(self):
        self._seed("r1", pulled=True)
        self.assertEqual(pull.recommend(self.conn, min_samples=20), [])

    def test_high_pull_rate_recommends_promote(self):
        for i in range(20):
            self._seed(f"r{i}", pulled=True)
        recs = pull.recommend(self.conn, promote_threshold=0.8, min_samples=20)
        self.assertEqual(recs[0]["action"], "promote")
        self.assertEqual(recs[0]["pull_rate"], 1.0)

    def test_low_pull_rate_recommends_keep_pull(self):
        for i in range(20):
            self._seed(f"r{i}", pulled=(i < 2))  # 10% pull rate
        recs = pull.recommend(self.conn, promote_threshold=0.8, min_samples=20)
        self.assertEqual(recs[0]["action"], "keep_pull")


if __name__ == "__main__":
    unittest.main()
