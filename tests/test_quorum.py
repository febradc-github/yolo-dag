"""Unit tests for meter.quorum (v2 M8, disabled by default — see module
docstring for why)."""

from __future__ import annotations

import sqlite3
import unittest

from meter import quorum, store


class ContentionPredicateTests(unittest.TestCase):
    def test_no_signal_does_not_escalate(self):
        self.assertFalse(quorum.contention_predicate(critic1_found_anything=False))

    def test_critic1_found_something_escalates(self):
        self.assertTrue(quorum.contention_predicate(critic1_found_anything=True))

    def test_sieve_disagreement_escalates_regardless(self):
        self.assertTrue(quorum.contention_predicate(critic1_found_anything=False,
                                                      sieve_disagrees=True))

    def test_rework_always_escalates(self):
        self.assertTrue(quorum.contention_predicate(critic1_found_anything=False,
                                                      is_rework=True))

    def test_recall_sample_always_escalates(self):
        self.assertTrue(quorum.contention_predicate(critic1_found_anything=False, sampled=True))

    def test_large_or_novel_always_escalates(self):
        self.assertTrue(quorum.contention_predicate(critic1_found_anything=False,
                                                      large_or_novel=True))

    def test_domain_split_rate_above_threshold_escalates(self):
        self.assertTrue(quorum.contention_predicate(
            critic1_found_anything=False, domain_split_rate=0.5, domain_threshold=0.3))

    def test_domain_split_rate_below_threshold_does_not_escalate(self):
        self.assertFalse(quorum.contention_predicate(
            critic1_found_anything=False, domain_split_rate=0.1, domain_threshold=0.3))


class ShouldSampleTests(unittest.TestCase):
    def test_deterministic_with_injected_rng(self):
        class FakeRng:
            def random(self):
                return 0.05
        self.assertTrue(quorum.should_sample(0.15, rng=FakeRng()))

    def test_false_when_roll_above_rate(self):
        class FakeRng:
            def random(self):
                return 0.99
        self.assertFalse(quorum.should_sample(0.15, rng=FakeRng()))


class DomainQuorumTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(store._SCHEMA)

    def tearDown(self):
        self.conn.close()

    def test_unknown_domain_defaults_to_three(self):
        self.assertEqual(quorum.domain_quorum(self.conn, domain="security"), 3)

    def test_below_min_samples_stays_three(self):
        for _ in range(10):
            quorum.record_full_quorum_sample(self.conn, domain="architecture", unique_finding=False)
        self.assertEqual(quorum.domain_quorum(self.conn, domain="architecture", min_samples=50), 3)

    def test_enough_samples_with_zero_unique_findings_narrows_to_one(self):
        for _ in range(50):
            quorum.record_full_quorum_sample(self.conn, domain="architecture", unique_finding=False)
        self.assertEqual(quorum.domain_quorum(self.conn, domain="architecture", min_samples=50,
                                               unique_finding_threshold=0.05), 1)

    def test_enough_samples_with_high_unique_finding_rate_stays_three(self):
        for i in range(50):
            quorum.record_full_quorum_sample(self.conn, domain="security", unique_finding=(i < 10))
        self.assertEqual(quorum.domain_quorum(self.conn, domain="security", min_samples=50,
                                               unique_finding_threshold=0.05), 3)

    def test_never_recommends_one_without_the_sample_floor_regardless_of_rate(self):
        # Even a perfect zero-unique-finding record with too few samples must
        # not narrow the quorum — this is the module's one load-bearing gate.
        for _ in range(49):
            quorum.record_full_quorum_sample(self.conn, domain="design", unique_finding=False)
        self.assertEqual(quorum.domain_quorum(self.conn, domain="design", min_samples=50), 3)


class DomainQuorumTableTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(store._SCHEMA)

    def tearDown(self):
        self.conn.close()

    def test_empty_table_when_no_samples(self):
        self.assertEqual(quorum.domain_quorum_table(self.conn), {})

    def test_table_includes_every_domain_seen(self):
        quorum.record_full_quorum_sample(self.conn, domain="architecture", unique_finding=False)
        quorum.record_full_quorum_sample(self.conn, domain="security", unique_finding=True)
        table = quorum.domain_quorum_table(self.conn)
        self.assertEqual(set(table), {"architecture", "security"})


class RenderReportSectionTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(store._SCHEMA)

    def tearDown(self):
        self.conn.close()

    def test_none_when_no_data(self):
        self.assertIsNone(quorum.render_report_section(self.conn))

    def test_reports_domain_and_quorum(self):
        quorum.record_full_quorum_sample(self.conn, domain="architecture", unique_finding=False)
        section = quorum.render_report_section(self.conn)
        self.assertIn("architecture", section)
        self.assertIn("quorum 3", section)  # below min_samples, always 3


if __name__ == "__main__":
    unittest.main()
