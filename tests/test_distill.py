"""Unit tests for meter.distill (v2 M12)."""

from __future__ import annotations

import sqlite3
import unittest

from meter import distill, store


class DistillCacheTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(store._SCHEMA)
        self.spec = "This is the merged spec. It has decisions and contracts."

    def tearDown(self):
        self.conn.close()

    def test_miss_on_empty_cache(self):
        self.assertIsNone(distill.get_cached_brief(self.conn, self.spec))

    def test_store_then_hit(self):
        distill.store_brief(self.conn, spec_text=self.spec, brief="dense brief",
                             source_path="/repo/merged-spec.md")
        entry = distill.get_cached_brief(self.conn, self.spec)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["brief"], "dense brief")

    def test_different_spec_text_is_a_miss(self):
        distill.store_brief(self.conn, spec_text=self.spec, brief="dense brief", source_path=None)
        self.assertIsNone(distill.get_cached_brief(self.conn, self.spec + " extra"))

    def test_hash_is_deterministic(self):
        h1 = distill.compute_spec_hash(self.spec)
        h2 = distill.compute_spec_hash(self.spec)
        self.assertEqual(h1, h2)

    def test_delivery_count_increments_on_each_hit(self):
        distill.store_brief(self.conn, spec_text=self.spec, brief="b", source_path=None)
        distill.get_cached_brief(self.conn, self.spec)
        distill.get_cached_brief(self.conn, self.spec)
        entry = store.get_distill_brief(self.conn, distill.compute_spec_hash(self.spec))
        self.assertEqual(entry["delivery_count"], 2)

    def test_fetch_rate_none_when_never_delivered(self):
        distill.store_brief(self.conn, spec_text=self.spec, brief="b", source_path=None)
        self.assertIsNone(distill.fetch_rate(self.conn, self.spec))

    def test_fetch_rate_computed_after_deliveries_and_fetches(self):
        distill.store_brief(self.conn, spec_text=self.spec, brief="b", source_path=None)
        distill.get_cached_brief(self.conn, self.spec)
        distill.get_cached_brief(self.conn, self.spec)
        distill.record_full_spec_fetch(self.conn, self.spec)
        self.assertEqual(distill.fetch_rate(self.conn, self.spec), 0.5)

    def test_restore_overwrites_brief_for_same_spec(self):
        distill.store_brief(self.conn, spec_text=self.spec, brief="v1", source_path=None)
        distill.store_brief(self.conn, spec_text=self.spec, brief="v2", source_path=None)
        entry = distill.get_cached_brief(self.conn, self.spec)
        self.assertEqual(entry["brief"], "v2")


if __name__ == "__main__":
    unittest.main()
