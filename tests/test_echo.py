"""Unit tests for meter.echo (M4, measure-only) and the Correlator's prompt-text
carry-through it depends on."""

from __future__ import annotations

import sqlite3
import unittest

from meter import echo, ledger, store


class NormalizeChunksTests(unittest.TestCase):
    def test_short_chunks_are_dropped(self):
        self.assertEqual(echo.normalize_chunks("hi\n\nbye"), [])

    def test_empty_text(self):
        self.assertEqual(echo.normalize_chunks(""), [])

    def test_splits_on_blank_lines(self):
        text = ("This is the first paragraph and it is long enough to count as a real chunk.\n\n"
                 "This is the second paragraph, also long enough to be kept as its own chunk.")
        chunks = echo.normalize_chunks(text)
        self.assertEqual(len(chunks), 2)

    def test_fenced_block_kept_atomic(self):
        text = ("intro text that is long enough on its own to be a real paragraph chunk here\n\n"
                 "```python\ndef f(x, y):\n    total = x + y\n    return total * 2\n```\n\n"
                 "outro text that is also long enough on its own to be a real paragraph chunk")
        chunks = echo.normalize_chunks(text)
        self.assertTrue(any("```python" in c for c in chunks))

    def test_whitespace_insensitive(self):
        a = "This paragraph has some   extra   spacing and\nline breaks inside of it here."
        b = "This paragraph has some extra spacing and line breaks inside of it here."
        chunk_a = echo.normalize_chunks(a)
        chunk_b = echo.normalize_chunks(b)
        self.assertEqual(chunk_a, chunk_b)

    def test_same_content_same_hash(self):
        a = "  This   is definitely long enough to be counted as a real chunk of text.  "
        b = "This is definitely long enough to be counted as a real chunk of text."
        (chunk_a,) = echo.normalize_chunks(a)
        (chunk_b,) = echo.normalize_chunks(b)
        self.assertEqual(echo.chunk_hash(chunk_a), echo.chunk_hash(chunk_b))


class RecordAndReportTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(store._SCHEMA)
        self.paragraph = ("This paragraph is shared verbatim across every sibling agent in the "
                           "pass, which is exactly the kind of duplication Echo exists to measure.")

    def tearDown(self):
        self.conn.close()

    def test_unattributed_run_is_never_recorded(self):
        count = echo.record(self.conn, run_id="_unattributed", agent_id="a1",
                             origin="task_prompt", text=self.paragraph)
        self.assertEqual(count, 0)
        self.assertEqual(store.get_echo_rows(self.conn, "_unattributed"), [])

    def test_same_chunk_two_agents_recipients_is_two(self):
        echo.record(self.conn, run_id="r1", agent_id="a1", origin="task_prompt",
                    text=self.paragraph)
        echo.record(self.conn, run_id="r1", agent_id="a2", origin="task_prompt",
                    text=self.paragraph)
        rows = store.get_echo_rows(self.conn, "r1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["recipients"], 2)

    def test_same_agent_twice_does_not_double_count(self):
        echo.record(self.conn, run_id="r1", agent_id="a1", origin="task_prompt",
                    text=self.paragraph)
        echo.record(self.conn, run_id="r1", agent_id="a1", origin="task_prompt",
                    text=self.paragraph)
        rows = store.get_echo_rows(self.conn, "r1")
        self.assertEqual(rows[0]["recipients"], 1)

    def test_echoed_tokens_formula(self):
        rows = [{"tokens": 100, "recipients": 3}, {"tokens": 50, "recipients": 1}]
        # (100 * (3-1)) + (50 * (1-1)) = 200
        self.assertEqual(echo.echoed_tokens(rows), 200)

    def test_report_section_none_when_no_rows(self):
        self.assertIsNone(echo.render_report_section([]))

    def test_report_section_mentions_top_chunk(self):
        echo.record(self.conn, run_id="r1", agent_id="a1", origin="task_prompt",
                    text=self.paragraph)
        echo.record(self.conn, run_id="r1", agent_id="a2", origin="task_prompt",
                    text=self.paragraph)
        rows = store.get_echo_rows(self.conn, "r1")
        section = echo.render_report_section(rows)
        self.assertIn("echoed tokens", section)
        self.assertIn("task_prompt", section)


class CorrelatorPromptTextTests(unittest.TestCase):
    def test_prompt_text_round_trips_through_claim(self):
        c = ledger.Correlator()
        c.push("sess-1", "run-1", "T-003", "the task prompt text")
        result = c.claim("sess-1")
        self.assertEqual(result, ("run-1", "T-003", "the task prompt text"))

    def test_push_without_prompt_text_defaults_empty(self):
        c = ledger.Correlator()
        c.push("sess-1", "run-1", "T-003")
        result = c.claim("sess-1")
        self.assertEqual(result, ("run-1", "T-003", ""))


if __name__ == "__main__":
    unittest.main()
