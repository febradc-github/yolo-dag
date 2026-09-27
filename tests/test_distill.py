"""Unit tests for meter.distill (v2 M12)."""

from __future__ import annotations

import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from meter import config, distill, store

ROOT = pathlib.Path(__file__).resolve().parent.parent


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


class CompileOnMissTests(unittest.TestCase):
    """v0.17.0: a cache *hit* is free, but compiling a fresh brief costs 2-4 foreground
    spawns to save one consumer's read of the spec — arithmetic that never closes at one
    consumer. So `compile_on_miss` defaults off, and the CLI's exit code says so."""

    def test_default_config_does_not_compile_on_miss(self):
        self.assertFalse(distill.compile_on_miss(config.DEFAULTS))

    def test_missing_key_reads_as_off(self):
        self.assertFalse(distill.compile_on_miss({"modules": {"distill": {}}}))
        self.assertFalse(distill.compile_on_miss({"modules": {}}))
        self.assertFalse(distill.compile_on_miss({}))

    def test_repo_config_can_turn_it_back_on(self):
        cfg = {"modules": {"distill": {"enabled": True, "compile_on_miss": True}}}
        self.assertTrue(distill.compile_on_miss(cfg))

    def test_distill_is_still_enabled_by_default(self):
        """Only the compile branch is off. The cache itself stays on, so a hit still
        short-circuits Phase 4 for free."""
        self.assertTrue(config.DEFAULTS["modules"]["distill"]["enabled"])


class DistillCliExitCodeTests(unittest.TestCase):
    """Phase 4 branches on three distinct exit codes; the script is the only place that
    decides between 1 and 3, so pin the mapping here."""

    def setUp(self):
        spec = pathlib.Path(tempfile.mkdtemp()) / "merged-spec.md"
        spec.write_text("a spec that is not in any cache", encoding="utf-8")
        self.spec = spec

    def _run(self, env_extra=None):
        env = dict(os.environ)
        env["CLAUDE_PLUGIN_DATA"] = tempfile.mkdtemp()
        env.update(env_extra or {})
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "dag-distill.py"), "check", str(self.spec)],
            capture_output=True, text=True, env=env, cwd=tempfile.mkdtemp(), check=False)

    def test_miss_with_compile_off_exits_3(self):
        result = self._run()
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, "", "exit 3 must print no brief to use")

    def test_meter_disabled_still_exits_1(self):
        """The off switch keeps its old meaning: an ordinary miss, not the new signal."""
        result = self._run({"DAG_METER_DISABLE": "1"})
        self.assertEqual(result.returncode, 1)


if __name__ == "__main__":
    unittest.main()
