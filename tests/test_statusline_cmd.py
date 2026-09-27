"""Unit tests for scripts/dag-statusline.py's `on` — the settings it writes and the
in-place upgrade of the legacy config."""

from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "dag-statusline.py"
_spec = importlib.util.spec_from_file_location("dag_statusline", _SCRIPT)
dag_statusline = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dag_statusline)


def _run_on(settings: Path) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        dag_statusline.cmd_on(settings)
    return buf.getvalue()


class StatuslineOnTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = Path(self._tmp.name) / ".claude" / "settings.json"

    def _read(self) -> dict:
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def _write(self, data: dict) -> None:
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        self.settings.write_text(json.dumps(data), encoding="utf-8")

    def test_bootstrap_writes_refresh_interval_and_project_dir_command(self):
        _run_on(self.settings)
        block = self._read()["statusLine"]
        self.assertEqual(block["type"], "command")
        self.assertEqual(block["refreshInterval"], dag_statusline.STATUSLINE_REFRESH_SECONDS)
        self.assertIn("${CLAUDE_PROJECT_DIR:-.}", block["command"])
        self.assertTrue((self.settings.parent / "statusline.sh").exists())

    def test_legacy_config_is_upgraded_in_place(self):
        self._write({"statusLine": {"type": "command", "command": "bash .claude/statusline.sh"},
                     "other": 1})
        out = _run_on(self.settings)
        data = self._read()
        self.assertIn("upgraded", out)
        self.assertEqual(data["statusLine"]["command"], dag_statusline.STATUSLINE_COMMAND)
        self.assertEqual(data["statusLine"]["refreshInterval"],
                         dag_statusline.STATUSLINE_REFRESH_SECONDS)
        self.assertEqual(data["other"], 1)

    def test_user_configured_statusline_is_left_alone(self):
        custom = {"type": "command", "command": "~/my-own-statusline.sh"}
        self._write({"statusLine": custom})
        out = _run_on(self.settings)
        self.assertIn("already on", out)
        self.assertEqual(self._read()["statusLine"], custom)

    def test_legacy_config_with_a_refresh_interval_is_left_alone(self):
        mine = {"type": "command", "command": "bash .claude/statusline.sh", "refreshInterval": 3}
        self._write({"statusLine": mine})
        _run_on(self.settings)
        self.assertEqual(self._read()["statusLine"], mine)

    def test_disabled_config_is_restored_unchanged(self):
        legacy = {"type": "command", "command": "bash .claude/statusline.sh"}
        self._write({"_statusLineDisabled": legacy})
        _run_on(self.settings)
        self.assertEqual(self._read()["statusLine"], legacy)


if __name__ == "__main__":
    unittest.main()
