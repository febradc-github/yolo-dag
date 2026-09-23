"""Tests for meter.router._git_is_tracked's caching layer (v2 M13 Throttle's
hot-path dependency). Before this cache existed, every single `Write` call
spawned a `git ls-files --error-unmatch` subprocess — this file checks the
cache actually avoids repeat subprocess calls between index changes, and that
it correctly invalidates when the git index changes (add/commit).
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from meter import router


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                           check=True)


class GitIsTrackedTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmpdir.name)
        _git(self.repo_root, "init", "-q")
        _git(self.repo_root, "config", "user.email", "t@example.com")
        _git(self.repo_root, "config", "user.name", "T")
        (self.repo_root / "tracked.py").write_text("x = 1\n", encoding="utf-8")
        _git(self.repo_root, "add", ".")
        _git(self.repo_root, "commit", "-q", "-m", "init")
        (self.repo_root / "untracked.py").write_text("y = 1\n", encoding="utf-8")
        router._TRACKED_CACHE.clear()

    def tearDown(self):
        self._tmpdir.cleanup()
        router._TRACKED_CACHE.clear()

    def test_tracked_file_reports_true(self):
        self.assertTrue(router._git_is_tracked(self.repo_root, "tracked.py"))

    def test_untracked_file_reports_false(self):
        self.assertFalse(router._git_is_tracked(self.repo_root, "untracked.py"))

    def test_repeated_lookups_hit_the_cache_not_a_new_subprocess(self):
        router._git_is_tracked(self.repo_root, "tracked.py")  # populates the cache
        with mock.patch.object(router.subprocess, "run") as mocked_run:
            result = router._git_is_tracked(self.repo_root, "tracked.py")
        self.assertTrue(result)
        mocked_run.assert_not_called()

    def test_cache_invalidates_after_index_changes(self):
        self.assertFalse(router._git_is_tracked(self.repo_root, "untracked.py"))
        _git(self.repo_root, "add", "untracked.py")  # bumps .git/index's mtime
        _git(self.repo_root, "commit", "-q", "-m", "track it")
        self.assertTrue(router._git_is_tracked(self.repo_root, "untracked.py"))

    def test_failed_git_call_falls_back_to_not_tracked(self):
        nonexistent = Path(self._tmpdir.name) / "not-a-repo"
        self.assertFalse(router._git_is_tracked(nonexistent, "anything.py"))

    def test_failed_lookup_is_never_cached(self):
        nonexistent = Path(self._tmpdir.name) / "not-a-repo"
        router._git_is_tracked(nonexistent, "anything.py")
        self.assertNotIn(nonexistent, router._TRACKED_CACHE)


if __name__ == "__main__":
    unittest.main()
