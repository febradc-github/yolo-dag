#!/usr/bin/env python3
"""CLI for turning this repo's statusline on/off, or checking its state.

Usage: python3 scripts/dag-statusline.py <on|off|status>

Claude Code's statusline is configured via the `statusLine` key in
`.claude/settings.json` (an object such as {"type": "command", "command": "..."}).
There's no separate enabled/disabled flag in that schema — "off" here means
removing the key; "on" means putting it back. To do that without losing the
configuration, "off" moves the value to `_statusLineDisabled` (a key name
Claude Code itself ignores) instead of deleting it, and "on" moves it back.

Like most settings.json edits, a change here may not affect the *current*
session's rendered statusline until the next one starts — this script edits
the file honestly and says what it did; it doesn't claim to hot-apply it.
Always exits 0 — this is a convenience toggle, not something that should be
able to fail a session.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def find_repo_root(start: Path) -> Path | None:
    cur = start.resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def _settings_path(repo_root: Path) -> Path:
    return repo_root / ".claude" / "settings.json"


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def cmd_status(path: Path) -> None:
    data = _load(path)
    if "statusLine" in data:
        print(f"statusline: on ({path})")
        print(json.dumps(data["statusLine"], indent=2))
    elif "_statusLineDisabled" in data:
        print(f"statusline: off ({path}) — configuration preserved, run `on` to restore it")
    else:
        print(f"statusline: not configured ({path} has no statusLine key)")


def cmd_off(path: Path) -> None:
    data = _load(path)
    if "statusLine" not in data:
        if "_statusLineDisabled" in data:
            print("statusline: already off.")
        else:
            print("statusline: nothing configured — nothing to turn off. Run /statusline first.")
        return
    data["_statusLineDisabled"] = data.pop("statusLine")
    _save(path, data)
    print(f"statusline: off. Configuration preserved in {path} under `_statusLineDisabled` — "
          f"run `on` to restore it. Takes effect next session.")


def cmd_on(path: Path) -> None:
    data = _load(path)
    if "statusLine" in data:
        print("statusline: already on.")
        return
    if "_statusLineDisabled" not in data:
        print("statusline: nothing to turn on — no statusline has been configured yet. "
              "Run /statusline to set one up.")
        return
    data["statusLine"] = data.pop("_statusLineDisabled")
    _save(path, data)
    print(f"statusline: on, restored in {path}. Takes effect next session.")


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("on", "off", "status"):
        print("usage: python3 scripts/dag-statusline.py <on|off|status>")
        return 0

    repo_root = find_repo_root(Path.cwd())
    if repo_root is None:
        print("statusline: not inside a git repo — nothing to toggle.")
        return 0

    path = _settings_path(repo_root)
    {"status": cmd_status, "off": cmd_off, "on": cmd_on}[sys.argv[1]](path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
