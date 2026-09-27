"""Unit tests for meter.fold (M16) — deterministic findings consolidation."""

from __future__ import annotations

import unittest

from meter import fold


def finding(title, *, angle="completeness", confidence=90, issue="", resolution=""):
    return (
        f"### {title}\n"
        f"- **Angle:** {angle}\n"
        f"- **Confidence:** {confidence}\n"
        f"- **Issue:** {issue or title}\n"
        f"- **Evidence:** quoted from the deliverable\n"
        f"- **Suggested resolution:** {resolution or 'change it'}\n"
    )


class StemmerTests(unittest.TestCase):
    def test_folds_inflections_to_a_common_stem(self):
        for a, b in [("unique", "uniqueness"), ("user", "users"),
                     ("requires", "requirement"), ("charge", "charges")]:
            self.assertEqual(fold._stem(a), fold._stem(b), f"{a} vs {b}")

    def test_does_not_strip_a_word_that_merely_ends_in_s(self):
        for word in ("address", "status", "analysis"):
            self.assertEqual(fold._stem(word), word)

    def test_leaves_short_words_alone(self):
        self.assertEqual(fold._stem("add"), "add")
        self.assertEqual(fold._stem("key"), "key")


class ParseTests(unittest.TestCase):
    def test_parses_every_field(self):
        (f,) = fold.parse_findings(finding("No uniqueness constraint", confidence=92))
        self.assertEqual(f.title, "No uniqueness constraint")
        self.assertEqual(f.confidence, 92.0)
        self.assertEqual(f.angles, {"completeness"})
        self.assertTrue(f.evidence)

    def test_falls_back_to_the_filename_angle(self):
        text = "### A problem\n- **Confidence:** 85\n- **Issue:** something\n"
        (f,) = fold.parse_findings(text, default_angle="feasibility")
        self.assertEqual(f.angles, {"feasibility"})

    def test_tolerates_heading_depth_and_field_order(self):
        text = ("#### Late heading\n- **Issue:** the thing is wrong\n"
                "- **Confidence:** 81\n- **Angle:** consistency\n")
        (f,) = fold.parse_findings(text)
        self.assertEqual(f.confidence, 81.0)
        self.assertEqual(f.angles, {"consistency"})

    def test_ignores_a_wrapping_section_header(self):
        text = "## Findings\n" + finding("Real finding")
        parsed = fold.parse_findings(text)
        self.assertEqual([f.title for f in parsed], ["Real finding"])

    def test_continuation_lines_extend_the_open_field(self):
        text = ("### Wrapped\n- **Issue:** the first half\n  and the second half\n"
                "- **Confidence:** 90\n")
        (f,) = fold.parse_findings(text)
        self.assertIn("second half", f.issue)

    def test_empty_text_parses_to_nothing(self):
        self.assertEqual(fold.parse_findings(""), [])

    def test_angle_from_filename(self):
        self.assertEqual(fold.angle_from_filename("round-2-findings-consistency.md"),
                         "consistency")
        self.assertIsNone(fold.angle_from_filename("round-2.md"))


class DedupeTests(unittest.TestCase):
    def test_merges_the_same_issue_seen_from_two_angles(self):
        a = finding("No uniqueness constraint on email", angle="completeness",
                    confidence=92,
                    issue="The user schema has no uniqueness constraint on the email "
                          "column, which the request explicitly requires.",
                    resolution="Add a UNIQUE index on users.email.")
        b = finding("Email uniqueness missing from the schema", angle="consistency",
                    confidence=88,
                    issue="The requirements say email must be unique but the users "
                          "schema column has no uniqueness constraint.",
                    resolution="Add a unique constraint to the email column.")
        markdown, count = fold.consolidate([(a, None), (b, None)])
        self.assertEqual(count, 1)
        self.assertIn("completeness, consistency", markdown)
        self.assertIn("corroborated", markdown)

    def test_a_merge_keeps_both_suggested_resolutions(self):
        a = finding("Rate limit is unspecified", confidence=90,
                    issue="The rate limit for the public endpoint is unspecified.",
                    resolution="Specify a per-IP limit.")
        b = finding("Unspecified rate limit on the endpoint", angle="feasibility",
                    confidence=85,
                    issue="The public endpoint rate limit is never specified anywhere.",
                    resolution="Specify a per-token limit.")
        markdown, count = fold.consolidate([(a, None), (b, None)])
        self.assertEqual(count, 1)
        self.assertIn("per-IP", markdown)
        self.assertIn("per-token", markdown)

    def test_unrelated_findings_are_not_merged(self):
        a = finding("Session expiry is unspecified",
                    issue="Sessions are created but never expire.")
        b = finding("Argon2 memory cost exceeds the container limit",
                    angle="feasibility",
                    issue="The Argon2id memory cost of 1GiB exceeds the 512MiB limit.")
        _, count = fold.consolidate([(a, None), (b, None)])
        self.assertEqual(count, 2)

    def test_a_high_ratio_on_tiny_findings_does_not_merge(self):
        # Both normalize to very small stem sets; without MIN_SHARED_STEMS the
        # ratio alone would merge two unrelated one-liners.
        a = "### Cache miss\n- **Issue:** cache\n- **Confidence:** 90\n"
        b = "### Cache warm\n- **Issue:** cache\n- **Confidence:** 90\n"
        _, count = fold.consolidate([(a, None), (b, None)])
        self.assertEqual(count, 2)

    def test_ranks_by_confidence_then_corroboration(self):
        high = finding("Token refresh has no expiry check", confidence=97,
                       issue="Refresh tokens are accepted without checking expiry.")
        low = finding("Log format is inconsistent", confidence=82,
                      issue="Structured and plain log lines are mixed throughout.")
        markdown, count = fold.consolidate([(low, None), (high, None)])
        self.assertEqual(count, 2)
        self.assertLess(markdown.index("Token refresh"), markdown.index("Log format"))

    def test_a_merge_never_drops_the_losing_issue_statement(self):
        a = finding("Cache eviction is unbounded", confidence=90,
                    issue="A distinct unbounded cache eviction problem.")
        b = finding("Unbounded eviction in the cache", angle="feasibility",
                    confidence=95,
                    issue="A distinct cache eviction problem that is unbounded.")
        markdown, count = fold.consolidate([(a, None), (b, None)])
        self.assertEqual(count, 1)
        self.assertIn("Also stated as", markdown)
        self.assertIn("A distinct unbounded cache eviction problem.", markdown)

    def test_corroboration_breaks_a_near_tie(self):
        lone = finding("Solo walrus problem", confidence=86,
                       issue="A solo walrus problem nobody else saw.")
        a = finding("Shared otter problem", confidence=84,
                    issue="The otter handler drops its error result silently.")
        b = finding("Otter problem shared", angle="feasibility", confidence=84,
                    issue="The otter handler silently drops the error result it gets.")
        markdown, count = fold.consolidate([(lone, None), (a, None), (b, None)])
        self.assertEqual(count, 2)
        self.assertLess(markdown.index("otter"), markdown.index("walrus"))

    def test_output_is_stable_across_repeated_runs(self):
        src = [(finding(f"Issue number {i}", confidence=90 - i,
                        issue=f"Distinct problem number {i} in the deliverable."), None)
               for i in range(5)]
        first, _ = fold.consolidate(src)
        second, _ = fold.consolidate(src)
        self.assertEqual(first, second)


class EscalationTests(unittest.TestCase):
    def test_escalates_on_opposing_resolutions_about_the_same_subject(self):
        a = finding("Retry layer around the payment call", confidence=90,
                    issue="The payment call has no retry handling for transient "
                          "gateway failures.",
                    resolution="Add an exponential-backoff retry wrapper around the "
                               "payment gateway call.")
        b = finding("Retry on payments risks double charges", angle="feasibility",
                    confidence=88,
                    issue="Retrying the payment gateway call without an idempotency "
                          "key double-charges customers.",
                    resolution="Remove the retry around the payment call entirely.")
        with self.assertRaises(fold.EscalationRequired):
            fold.consolidate([(a, None), (b, None)])

    def test_same_direction_resolutions_do_not_escalate(self):
        a = finding("Missing index on email", confidence=90,
                    issue="The email column on the users table has no index.",
                    resolution="Add an index on users.email.")
        b = finding("Email column lacks an index", angle="feasibility", confidence=88,
                    issue="No index exists on the users table email column.",
                    resolution="Add a unique index to the email column.")
        _, count = fold.consolidate([(a, None), (b, None)])
        self.assertEqual(count, 1)

    def test_escalates_when_text_parses_to_no_findings(self):
        with self.assertRaises(fold.EscalationRequired):
            fold.consolidate([("I was unable to review this deliverable.", None)])

    def test_genuinely_empty_file_is_clean_not_an_escalation(self):
        markdown, count = fold.consolidate([("", None), ("   \n", None)])
        self.assertEqual(count, 0)
        self.assertIn("No findings", markdown)


class RenderTests(unittest.TestCase):
    def test_header_names_the_specialist_and_round(self):
        markdown, _ = fold.consolidate([(finding("A thing"), None)],
                                        specialist="security-specialist", round_no=2)
        self.assertIn("security-specialist", markdown)
        self.assertIn("round 2", markdown)

    def test_says_it_added_and_dropped_nothing(self):
        markdown, _ = fold.consolidate([(finding("A thing"), None)])
        self.assertIn("no finding was added, dropped, or judged", markdown)


if __name__ == "__main__":
    unittest.main()
