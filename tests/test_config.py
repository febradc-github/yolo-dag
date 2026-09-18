"""Unit tests for the parts of meter.config not already covered elsewhere:
the M3a read-hook-conflict detector."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from meter import config as config_mod


class MatcherCoversReadTests(unittest.TestCase):
    def test_empty_matcher_covers_everything(self):
        self.assertTrue(config_mod._matcher_covers_read(""))

    def test_wildcard_matcher_covers_everything(self):
        self.assertTrue(config_mod._matcher_covers_read("*"))

    def test_read_matcher_matches(self):
        self.assertTrue(config_mod._matcher_covers_read("Read"))
        self.assertTrue(config_mod._matcher_covers_read("Read|Grep"))

    def test_unrelated_matcher_does_not_match(self):
        self.assertFalse(config_mod._matcher_covers_read("Bash"))
        self.assertFalse(config_mod._matcher_covers_read("Write|Edit"))

    def test_substring_is_not_a_false_match(self):
        # "ReadOnly" contains "Read" but isn't the tool named Read.
        self.assertFalse(config_mod._matcher_covers_read("ReadOnlyThing"))


class DetectReadHookConflictTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmpdir.name)
        (self.repo_root / ".claude").mkdir()

    def tearDown(self):
        self._tmpdir.cleanup()

    def _write_settings(self, hooks_pretooluse: list[dict]) -> None:
        (self.repo_root / ".claude" / "settings.json").write_text(
            json.dumps({"hooks": {"PreToolUse": hooks_pretooluse}}), encoding="utf-8")

    def test_no_settings_file_is_no_conflict(self):
        with mock.patch.object(Path, "home", return_value=self.repo_root / "nonexistent-home"):
            self.assertIsNone(config_mod.detect_read_hook_conflict(self.repo_root))

    def test_conflicting_matcher_detected(self):
        self._write_settings([{"matcher": "Read", "hooks": []}])
        with mock.patch.object(Path, "home", return_value=self.repo_root / "nonexistent-home"):
            result = config_mod.detect_read_hook_conflict(self.repo_root)
        self.assertIsNotNone(result)
        self.assertIn("Read", result)

    def test_unrelated_matcher_is_not_a_conflict(self):
        self._write_settings([{"matcher": "Bash", "hooks": []}])
        with mock.patch.object(Path, "home", return_value=self.repo_root / "nonexistent-home"):
            self.assertIsNone(config_mod.detect_read_hook_conflict(self.repo_root))

    def test_malformed_settings_json_is_not_a_conflict(self):
        (self.repo_root / ".claude" / "settings.json").write_text("{not json", encoding="utf-8")
        with mock.patch.object(Path, "home", return_value=self.repo_root / "nonexistent-home"):
            self.assertIsNone(config_mod.detect_read_hook_conflict(self.repo_root))


if __name__ == "__main__":
    unittest.main()
