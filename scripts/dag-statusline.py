#!/usr/bin/env python3
"""CLI for turning this project directory's statusline on/off, or checking its state.

Usage: python3 scripts/dag-statusline.py <on|off|status>

Claude Code's statusline is configured via the `statusLine` key in
`.claude/settings.json` (an object such as {"type": "command", "command": "..."}).
There's no separate enabled/disabled flag in that schema — "off" here means
removing the key; "on" means putting it back. To do that without losing the
configuration, "off" moves the value to `_statusLineDisabled` (a key name
Claude Code itself ignores) instead of deleting it, and "on" moves it back.

`.claude/settings.json` is scoped to a *project directory* (wherever Claude
Code was launched from), not to a git repository — the two aren't the same
thing, and this script used to conflate them (it walked up looking for
`.git`, and refused to do anything outside one). Fixed: it operates on
`<cwd>/.claude/settings.json` directly, no git involved. (`meter`'s own
scripts still require git deliberately — the DAG pipeline itself does — but
that requirement never actually applied to the statusline.)

"on" also **bootstraps** a statusline in the current directory if none is
configured there yet, rather than requiring `/statusline` to be run manually
first — the plugin is used from whatever repo the user is actually working
in, not just yolo-dag's own, so each of those repos should be able to get its
own local statusline straight from `/dag-statusline on`. The template it
copies from is this same plugin's own `.claude/statusline.sh` (found relative
to this script's own location, so it works whether this is running from the
plugin's installed copy or a dev checkout) — copied into `<cwd>/.claude/`, so
the result is self-contained and independently committable per repo, exactly
like yolo-dag's own.

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

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def _settings_path(project_dir: Path) -> Path:
    return project_dir / ".claude" / "settings.json"


def _template_statusline() -> Path:
    return PLUGIN_ROOT / ".claude" / "statusline.sh"


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
        print(f"statusline: not configured ({path} has no statusLine key) — "
              f"run `on` to set one up here")


def cmd_off(path: Path) -> None:
    data = _load(path)
    if "statusLine" not in data:
        if "_statusLineDisabled" in data:
            print("statusline: already off.")
        else:
            print("statusline: nothing configured — nothing to turn off.")
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
    if "_statusLineDisabled" in data:
        data["statusLine"] = data.pop("_statusLineDisabled")
        _save(path, data)
        print(f"statusline: on, restored in {path}. Takes effect next session.")
        return

    # Nothing configured here at all — bootstrap it from the plugin's own
    # template instead of requiring /statusline to be run manually first.
    template = _template_statusline()
    if not template.exists():
        print(f"statusline: nothing configured, and no template found at {template} to set "
              f"one up from. Run /statusline in this directory instead.")
        return

    target_script = path.parent / "statusline.sh"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        target_script.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
        target_script.chmod(0o755)
    except OSError as exc:
        print(f"statusline: could not set up {target_script}: {exc!r}")
        return

    data["statusLine"] = {"type": "command", "command": "bash .claude/statusline.sh"}
    _save(path, data)
    print(f"statusline: set up and on in {path} (script copied to {target_script} from this "
          f"plugin's template). Takes effect next session.")


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("on", "off", "status"):
        print("usage: python3 scripts/dag-statusline.py <on|off|status>")
        return 0

    path = _settings_path(Path.cwd())
    {"status": cmd_status, "off": cmd_off, "on": cmd_on}[sys.argv[1]](path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
