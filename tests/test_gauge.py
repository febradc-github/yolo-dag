"""Unit tests for meter.gauge (M17) — risk-proportional Phase 2 review depth."""

from __future__ import annotations

import unittest

from meter import gauge

SENTENCE = "The component exposes a handler that validates input and returns a typed result. "
SMALL = SENTENCE * 30      # ~390 words
LARGE = SENTENCE * 220     # ~2860 words
HEDGY = (SENTENCE + "This is TBD and we assume it might probably work, unclear. ") * 30


class FloorTests(unittest.TestCase):
    """The safety property: Gauge only ever reduces, and not below the floor."""

    def test_high_risk_first_round_always_gets_the_full_panel(self):
        for domain in sorted(gauge.HIGH_RISK_DOMAINS):
            result = gauge.assess(SMALL, domain=domain, mode="full", round_no=1)
            self.assertEqual(result.reviewers, 3, domain)
            self.assertTrue(result.floor_applied, domain)

    def test_high_risk_floor_does_not_apply_after_the_first_round(self):
        result = gauge.assess(SMALL, domain="security-specialist", mode="full",
                              round_no=2, prior_findings=0)
        self.assertLess(result.reviewers, 3)

    def test_force_full_overrides_every_signal(self):
        result = gauge.assess(SMALL, domain="ux-copy-specialist", mode="full",
                              round_no=3, prior_findings=0, force_full=True)
        self.assertEqual(result.reviewers, 3)
        self.assertFalse(result.single_pass)

    def test_never_exceeds_the_mode_ceiling(self):
        for mode, ceiling in gauge.MODE_CEILING.items():
            for domain in ("security-specialist", "research-specialist", "design-specialist"):
                for round_no in (1, 2, 3):
                    result = gauge.assess(LARGE, domain=domain, mode=mode,
                                          round_no=round_no)
                    self.assertLessEqual(result.reviewers, ceiling)

    def test_never_recommends_a_negative_or_fractional_count(self):
        result = gauge.assess("", domain="design-specialist", mode="full", round_no=3,
                              prior_findings=0)
        self.assertIsInstance(result.reviewers, int)
        self.assertGreaterEqual(result.reviewers, 1)

    def test_micro_runs_no_review_at_all(self):
        result = gauge.assess(LARGE, domain="security-specialist", mode="micro")
        self.assertEqual(result.reviewers, 0)
        self.assertEqual(result.spawns, 0)


class SignalTests(unittest.TestCase):
    def test_a_large_high_risk_deliverable_gets_three(self):
        result = gauge.assess(LARGE, domain="security-specialist", mode="full", round_no=1)
        self.assertEqual(result.reviewers, 3)

    def test_a_short_low_risk_deliverable_gets_fewer(self):
        result = gauge.assess(SMALL, domain="ux-copy-specialist", mode="full", round_no=1)
        self.assertLess(result.reviewers, 3)

    def test_later_rounds_narrow(self):
        r1 = gauge.assess(LARGE, domain="architecture-specialist", mode="full", round_no=1)
        r3 = gauge.assess(LARGE, domain="architecture-specialist", mode="full",
                          round_no=3, prior_findings=0)
        self.assertLess(r3.reviewers, r1.reviewers)

    def test_a_productive_previous_round_keeps_scrutiny_up(self):
        quiet = gauge.assess(LARGE, domain="architecture-specialist", mode="full",
                             round_no=2, prior_findings=0)
        noisy = gauge.assess(LARGE, domain="architecture-specialist", mode="full",
                             round_no=2, prior_findings=6)
        self.assertGreaterEqual(noisy.reviewers, quiet.reviewers)

    def test_dense_hedging_earns_a_reviewer_back(self):
        plain = gauge.assess(SMALL, domain="design-specialist", mode="full", round_no=1)
        hedged = gauge.assess(HEDGY, domain="design-specialist", mode="full", round_no=1)
        self.assertGreaterEqual(hedged.reviewers, plain.reviewers)

    def test_hedge_density_is_zero_for_a_stub(self):
        self.assertEqual(gauge.hedge_density("TBD"), 0.0)

    def test_hedge_density_counts_per_thousand_words(self):
        self.assertGreater(gauge.hedge_density(HEDGY), gauge.HEDGE_DENSITY_HIGH)


class AngleTests(unittest.TestCase):
    def test_angles_come_from_the_priority_order(self):
        for n in range(1, 4):
            result = gauge.assess(LARGE, domain="security-specialist", mode="full",
                                  round_no=1)
            self.assertEqual(result.angles, gauge.ANGLE_PRIORITY[:result.reviewers])

    def test_completeness_is_never_the_angle_dropped_first(self):
        result = gauge.assess(SMALL, domain="ux-copy-specialist", mode="lite", round_no=1)
        self.assertIn("completeness", result.angles)

    def test_single_pass_collapses_two_angles_into_one_spawn(self):
        result = gauge.assess(SMALL, domain="research-specialist", mode="full", round_no=1)
        self.assertTrue(result.single_pass)
        self.assertEqual(result.reviewers, 2)
        self.assertEqual(result.spawns, 1)

    def test_single_pass_is_never_used_on_a_high_risk_domain(self):
        for round_no in (1, 2, 3):
            for prior in (None, 0, 5):
                result = gauge.assess(SMALL, domain="security-specialist", mode="full",
                                      round_no=round_no, prior_findings=prior)
                self.assertFalse(result.single_pass)

    def test_single_pass_is_never_used_on_a_large_deliverable(self):
        result = gauge.assess(LARGE, domain="design-specialist", mode="full", round_no=2,
                              prior_findings=0)
        self.assertFalse(result.single_pass)


class ReportingTests(unittest.TestCase):
    def test_every_assessment_states_its_reasons(self):
        result = gauge.assess(SMALL, domain="research-specialist", mode="full", round_no=1)
        self.assertTrue(result.reasons)
        self.assertIn("research-specialist", result.summary())

    def test_baseline_is_the_mode_constant(self):
        self.assertEqual(gauge.constant_baseline("full"), 3)
        self.assertEqual(gauge.constant_baseline("lite"), 2)

    def test_spawns_never_exceeds_the_baseline(self):
        for mode in ("full", "lite", "micro"):
            for domain in sorted(gauge.HIGH_RISK_DOMAINS | gauge.MEDIUM_RISK_DOMAINS |
                                 {"design-specialist", "ux-copy-specialist"}):
                for round_no in (1, 2, 3):
                    result = gauge.assess(LARGE, domain=domain, mode=mode,
                                          round_no=round_no)
                    self.assertLessEqual(result.spawns, gauge.constant_baseline(mode))


if __name__ == "__main__":
    unittest.main()
