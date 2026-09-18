"""Golden-style tests for M3b's output compressors (meter.compressors.*) and
the meter.clamp dispatcher. Each compressor test fixes an input/output pair —
the "golden file" the spec asks for, kept inline rather than on disk since
there's no existing fixture-file convention in this repo to match."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from meter import clamp
from meter.compressors import build, git, grep, tests as tests_compressor


class TestsCompressorTests(unittest.TestCase):
    def test_under_threshold_is_untouched(self):
        self.assertIsNone(tests_compressor.compress("short output", byte_threshold=8000))

    def test_no_recognisable_structure_is_untouched(self):
        noise = "\n".join(f"line {i}" for i in range(200))
        self.assertIsNone(tests_compressor.compress(noise, byte_threshold=100))

    def test_failure_block_and_summary_survive_compression(self):
        lines = ["running suite..."]
        lines += [f"PASS test_{i} ({i}ms)" for i in range(300)]
        lines += ["FAIL test_broken_thing", "  AssertionError: expected 1 to equal 2",
                  "    at test_broken_thing (spec.js:42:10)", "PASS test_after (1ms)"]
        lines += ["2 failed, 300 passed, 302 total"]
        output = "\n".join(lines)
        compressed = tests_compressor.compress(output, byte_threshold=100)
        self.assertIsNotNone(compressed)
        self.assertIn("FAIL test_broken_thing", compressed)
        self.assertIn("AssertionError: expected 1 to equal 2", compressed)
        self.assertIn("2 failed, 300 passed, 302 total", compressed)
        self.assertLess(len(compressed), len(output))

    def test_never_shrinks_below_meaningful_savings_threshold(self):
        # A short, mostly-signal log shouldn't be "compressed" into something
        # barely smaller — the 80%-kept guard should decline instead.
        lines = ["FAIL a"] * 3 + ["PASS b"] * 2
        output = "\n".join(lines)
        result = tests_compressor.compress(output, byte_threshold=1)
        # Every line matches either the failure-start or is within the tail window,
        # so this is expected to decline (kept fraction too high) rather than
        # produce a compressed version that saves nothing meaningful.
        self.assertIsNone(result)


class BuildCompressorTests(unittest.TestCase):
    def test_under_threshold_is_untouched(self):
        self.assertIsNone(build.compress("all fine", byte_threshold=8000))

    def test_no_signal_declines(self):
        noise = "\n".join(f"Downloading package-{i}..." for i in range(200))
        self.assertIsNone(build.compress(noise, byte_threshold=100))

    def test_keeps_errors_and_tail(self):
        lines = [f"Downloading package-{i}..." for i in range(200)]
        lines.insert(50, "ERROR: could not resolve dependency foo@2.0.0")
        lines += ["Build failed with 1 error"]
        output = "\n".join(lines)
        compressed = build.compress(output, byte_threshold=100)
        self.assertIsNotNone(compressed)
        self.assertIn("ERROR: could not resolve dependency foo@2.0.0", compressed)
        self.assertIn("Build failed with 1 error", compressed)
        self.assertLess(len(compressed), len(output))


class GitCompressorTests(unittest.TestCase):
    def test_is_diff_command_detection(self):
        self.assertTrue(git.is_diff_command("git diff HEAD~1"))
        self.assertFalse(git.is_diff_command("git status"))
        self.assertFalse(git.is_diff_command("git commit -m diff"))

    def test_under_threshold_is_untouched(self):
        self.assertIsNone(git.compress("diff --git a b\n+x", byte_threshold=8000))

    def test_hunk_body_elided_headers_kept(self):
        body_lines = [f"+added line {i}" for i in range(200)]
        diff = "\n".join([
            "diff --git a/f.py b/f.py",
            "index abc..def 100644",
            "--- a/f.py",
            "+++ b/f.py",
            "@@ -1,3 +1,203 @@",
            *body_lines,
        ])
        compressed = git.compress(diff, byte_threshold=100)
        self.assertIsNotNone(compressed)
        self.assertIn("diff --git a/f.py b/f.py", compressed)
        self.assertIn("@@ -1,3 +1,203 @@", compressed)
        self.assertIn("hunk line(s) elided", compressed)
        self.assertNotIn("+added line 100", compressed)
        self.assertLess(len(compressed), len(diff))

    def test_no_hunk_body_declines(self):
        # Over threshold but nothing is actually a hunk line (e.g. --stat output).
        stat_lines = [f"file_{i}.py | {i} +++---" for i in range(300)]
        output = "\n".join(["3 files changed, 10 insertions(+)"] + stat_lines)
        self.assertIsNone(git.compress(output, byte_threshold=100))


class GrepCompressorTests(unittest.TestCase):
    def test_under_threshold_is_untouched(self):
        self.assertIsNone(grep.compress("f.py:1:x", byte_threshold=8000))

    def test_caps_hits_per_file(self):
        lines = [f"src/big.py:{i}:match number {i}" for i in range(100)]
        output = "\n".join(lines)
        compressed = grep.compress(output, byte_threshold=100, max_per_file=10)
        self.assertIsNotNone(compressed)
        self.assertIn("src/big.py:0:", compressed)
        self.assertIn("src/big.py:9:", compressed)
        self.assertNotIn("src/big.py:10:", compressed)
        self.assertIn("90 more match(es) in src/big.py elided", compressed)

    def test_non_matching_lines_pass_through(self):
        lines = [f"src/a.py:{i}:hit" for i in range(50)] + ["", "50 matches"]
        output = "\n".join(lines)
        compressed = grep.compress(output, byte_threshold=100, max_per_file=5)
        self.assertIn("50 matches", compressed)

    def test_no_elision_declines(self):
        lines = [f"src/f{i}.py:1:hit" for i in range(200)]  # one hit per distinct file
        output = "\n".join(lines)
        self.assertIsNone(grep.compress(output, byte_threshold=100, max_per_file=10))


class ClampDispatchTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._tmpdir.name) / "run"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_selects_git_compressor_for_bash_git_diff(self):
        body_lines = [f"+line {i}" for i in range(200)]
        diff = "\n".join(["diff --git a/f b/f", "@@ -1,1 +1,201 @@", *body_lines])
        result = clamp.decide(
            tool_name="Bash", tool_input={"command": "git diff"}, output=diff,
            byte_threshold=100, run_dir=self.run_dir, tool_use_id="abc",
        )
        self.assertIsNotNone(result)
        self.assertIn("full output", result)
        spooled = list((self.run_dir / "meter" / "tool-output").glob("*.txt"))
        self.assertEqual(len(spooled), 1)
        self.assertEqual(spooled[0].read_text(encoding="utf-8"), diff)

    def test_selects_grep_compressor_for_grep_tool(self):
        output = "\n".join(f"src/big.py:{i}:hit" for i in range(100))
        result = clamp.decide(
            tool_name="Grep", tool_input={}, output=output,
            byte_threshold=100, run_dir=self.run_dir, tool_use_id="xyz",
        )
        self.assertIsNotNone(result)

    def test_unrecognised_bash_command_is_untouched(self):
        result = clamp.decide(
            tool_name="Bash", tool_input={"command": "ls -la"}, output="a\nb\nc" * 1000,
            byte_threshold=10, run_dir=self.run_dir, tool_use_id="q",
        )
        self.assertIsNone(result)

    def test_non_bash_non_grep_tool_is_untouched(self):
        result = clamp.decide(
            tool_name="Write", tool_input={}, output="x" * 100000,
            byte_threshold=10, run_dir=self.run_dir, tool_use_id="q",
        )
        self.assertIsNone(result)

    def test_empty_output_is_untouched(self):
        result = clamp.decide(
            tool_name="Bash", tool_input={"command": "git diff"}, output="",
            byte_threshold=10, run_dir=self.run_dir, tool_use_id="q",
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
