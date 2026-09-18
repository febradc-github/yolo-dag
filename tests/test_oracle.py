"""Unit tests for meter.oracle (v2 M15)."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from meter import oracle, store


class NormalizeQuestionTests(unittest.TestCase):
    def test_case_insensitive(self):
        self.assertEqual(oracle.normalize_question("Session Token"),
                          oracle.normalize_question("session token"))

    def test_word_order_insensitive(self):
        self.assertEqual(oracle.normalize_question("token session"),
                          oracle.normalize_question("session token"))

    def test_stopwords_and_pronouns_dropped(self):
        a = oracle.normalize_question("where is the session token validated")
        b = oracle.normalize_question("session token validated")
        self.assertEqual(a, b)

    def test_punctuation_ignored(self):
        self.assertEqual(oracle.normalize_question("session-token, validated!"),
                          oracle.normalize_question("session token validated"))

    def test_different_content_words_differ(self):
        self.assertNotEqual(oracle.normalize_question("session token"),
                             oracle.normalize_question("refresh token"))


class ExtractProvenanceTests(unittest.TestCase):
    def test_grep_output_extracts_files(self):
        output = "src/auth.py:10:def validate():\nsrc/session.py:5:token = None"
        self.assertEqual(oracle.extract_provenance("Grep", output),
                          ["src/auth.py", "src/session.py"])

    def test_non_grep_tool_returns_empty(self):
        self.assertEqual(oracle.extract_provenance("Glob", "src/auth.py\nsrc/session.py"), [])

    def test_empty_output(self):
        self.assertEqual(oracle.extract_provenance("Grep", ""), [])

    def test_deduplicates_files(self):
        output = "src/a.py:1:x\nsrc/a.py:2:y"
        self.assertEqual(oracle.extract_provenance("Grep", output), ["src/a.py"])


class LookupAndRecordTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmpdir.name)
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(store._SCHEMA)

    def tearDown(self):
        self.conn.close()
        self._tmpdir.cleanup()

    def _write(self, rel_path: str, content: str) -> None:
        path = self.repo_root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_lookup_miss_when_never_recorded(self):
        self.assertIsNone(oracle.lookup(self.conn, self.repo_root, run_id="r1",
                                         query_text="session token"))

    def test_record_then_lookup_hits(self):
        self._write("src/a.py", "token = 1\n")
        answer = "src/a.py:1:token = 1"
        oracle.record(self.conn, self.repo_root, run_id="r1", query_text="session token",
                       answer=answer, tool_name="Grep", max_entries=100)
        hit = oracle.lookup(self.conn, self.repo_root, run_id="r1", query_text="session token")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["answer"], answer)

    def test_semantically_equivalent_query_still_hits(self):
        self._write("src/a.py", "token = 1\n")
        answer = "src/a.py:1:token = 1"
        oracle.record(self.conn, self.repo_root, run_id="r1", query_text="where is session token",
                       answer=answer, tool_name="Grep", max_entries=100)
        hit = oracle.lookup(self.conn, self.repo_root, run_id="r1", query_text="session token")
        self.assertIsNotNone(hit)

    def test_no_provenance_is_not_recorded(self):
        oracle.record(self.conn, self.repo_root, run_id="r1", query_text="session token",
                       answer="no file matches here", tool_name="Grep", max_entries=100)
        self.assertIsNone(oracle.lookup(self.conn, self.repo_root, run_id="r1",
                                         query_text="session token"))

    def test_file_change_invalidates_via_tree_state_mismatch(self):
        self._write("src/a.py", "token = 1\n")
        answer = "src/a.py:1:token = 1"
        oracle.record(self.conn, self.repo_root, run_id="r1", query_text="session token",
                       answer=answer, tool_name="Grep", max_entries=100)
        self._write("src/a.py", "token = 2\n")  # tree moved
        self.assertIsNone(oracle.lookup(self.conn, self.repo_root, run_id="r1",
                                         query_text="session token"))

    def test_explicit_invalidate_for_write_removes_entry(self):
        self._write("src/a.py", "token = 1\n")
        answer = "src/a.py:1:token = 1"
        oracle.record(self.conn, self.repo_root, run_id="r1", query_text="session token",
                       answer=answer, tool_name="Grep", max_entries=100)
        removed = oracle.invalidate_for_write(self.conn, run_id="r1", rel_path="src/a.py")
        self.assertEqual(removed, 1)
        self.assertIsNone(oracle.lookup(self.conn, self.repo_root, run_id="r1",
                                         query_text="session token"))

    def test_different_run_never_hits(self):
        self._write("src/a.py", "token = 1\n")
        answer = "src/a.py:1:token = 1"
        oracle.record(self.conn, self.repo_root, run_id="r1", query_text="session token",
                       answer=answer, tool_name="Grep", max_entries=100)
        self.assertIsNone(oracle.lookup(self.conn, self.repo_root, run_id="r2",
                                         query_text="session token"))

    def test_max_entries_cap_stops_new_recordings(self):
        self._write("src/a.py", "x\n")
        oracle.record(self.conn, self.repo_root, run_id="r1", query_text="q1",
                       answer="src/a.py:1:x", tool_name="Grep", max_entries=1)
        oracle.record(self.conn, self.repo_root, run_id="r1", query_text="q2",
                       answer="src/a.py:1:x", tool_name="Grep", max_entries=1)
        self.assertIsNotNone(oracle.lookup(self.conn, self.repo_root, run_id="r1",
                                            query_text="q1"))
        self.assertIsNone(oracle.lookup(self.conn, self.repo_root, run_id="r1", query_text="q2"))


if __name__ == "__main__":
    unittest.main()
