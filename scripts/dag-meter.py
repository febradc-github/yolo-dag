#!/usr/bin/env python3
"""CLI for turning meter on/off and checking its live status.

Usage: python3 scripts/dag-meter.py <on|off|status>

Two things are involved in "on"/"off", and this handles both so a toggle
actually takes effect immediately rather than only on the next session:

- **Persisted preference**: `<repo>/.dag/meter.json`'s `enabled` key, read by
  `meter-boot.py` on every future `SessionStart`.
- **This session's already-running daemon**, if there is one: Claude Code's
  hooks (`hooks/hooks.json`) fire unconditionally once registered — they don't
  consult `.dag/meter.json` themselves — so editing the file alone doesn't stop
  a daemon that's already up, or start one that never came up. "off" also asks
  the live daemon to shut down (after that, hooks fail as non-blocking HTTP
  errors — fail-open, per meter-handoff.md); "on" also runs `meter-boot.py`
  directly instead of waiting for a `SessionStart` that already happened.

Always exits 0 — this is a convenience command, not something that should be
able to break a session by failing.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

try:
    from meter import config as config_mod
except Exception as exc:
    print(f"meter: could not load its own config module ({exc!r}); nothing to toggle.")
    sys.exit(0)


def _health(port: int, token: str) -> dict | None:
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/health",
                                      headers={"X-Dag-Meter-Token": token})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError):
        return None


def _live_daemon(plugin_data_dir: Path) -> tuple[int, str] | None:
    token_path, port_path = plugin_data_dir / "token", plugin_data_dir / "port"
    if not (token_path.exists() and port_path.exists()):
        return None
    try:
        token = token_path.read_text(encoding="utf-8").strip()
        port = int(port_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return (port, token) if _health(port, token) is not None else None


def _write_repo_config(repo_root: Path, enabled: bool) -> Path:
    path = repo_root / ".dag" / "meter.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(current, dict):
            current = {}
    except (OSError, json.JSONDecodeError):
        current = {}
    current["enabled"] = enabled
    path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def cmd_status(repo_root: Path | None) -> None:
    cfg = config_mod.load_config(repo_root)
    plugin_data_dir = config_mod.plugin_data_dir()
    live = _live_daemon(plugin_data_dir)

    print(f"configured: {'on' if config_mod.is_enabled(cfg) else 'off'} "
          f"({repo_root / '.dag' / 'meter.json' if repo_root else 'no repo found — defaults only'})")
    if live:
        port, _ = live
        print(f"running now: yes, on 127.0.0.1:{port}")
    else:
        print("running now: no")
        if config_mod.is_enabled(cfg):
            print("(configured on, but not running yet in this session — "
                  "run `python3 scripts/dag-meter.py on` to start it without waiting for the next session)")


def cmd_off(repo_root: Path | None) -> None:
    if repo_root is None:
        print("meter: not inside a git repo — nothing to persist, but stopping any live daemon anyway.")
    else:
        path = _write_repo_config(repo_root, enabled=False)
        print(f"meter: wrote \"enabled\": false to {path.relative_to(repo_root)} "
              f"— off for every future session.")

    plugin_data_dir = config_mod.plugin_data_dir()
    live = _live_daemon(plugin_data_dir)
    if live is None:
        print("meter: no daemon currently running in this session — already off for the rest of it.")
        return

    port, token = live
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/shutdown", data=b"{}",
                                      method="POST",
                                      headers={"X-Dag-Meter-Token": token,
                                               "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=2.0)
        time.sleep(0.3)
        print("meter: stopped the running daemon — off for the rest of this session too.")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        print(f"meter: could not reach the running daemon to stop it ({exc!r}) — "
              f"the config change is saved, but this session keeps recording until it ends.")


def cmd_on(repo_root: Path | None) -> None:
    if repo_root is None:
        print("meter: not inside a git repo — nothing to persist, but starting a daemon anyway.")
    else:
        path = _write_repo_config(repo_root, enabled=True)
        print(f"meter: wrote \"enabled\": true to {path.relative_to(repo_root)} "
              f"— on for every future session (this is already the default).")

    plugin_data_dir = config_mod.plugin_data_dir()
    if _live_daemon(plugin_data_dir) is not None:
        print("meter: already running in this session.")
        return

    boot_script = PLUGIN_ROOT / "scripts" / "meter-boot.py"
    result = subprocess.run([sys.executable, str(boot_script)], capture_output=True, text=True,
                             timeout=15, check=False)
    live = _live_daemon(plugin_data_dir)
    if live is not None:
        print(f"meter: started — running now on 127.0.0.1:{live[0]}.")
    else:
        print("meter: tried to start it but it isn't answering health checks yet — "
              "run `python3 scripts/dag-doctor.py` to see why.")
        if result.stdout.strip():
            print(f"(meter-boot.py said: {result.stdout.strip()})")


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("on", "off", "status"):
        print("usage: python3 scripts/dag-meter.py <on|off|status>")
        return 0

    repo_root = config_mod.find_repo_root(Path.cwd())
    {"status": cmd_status, "off": cmd_off, "on": cmd_on}[sys.argv[1]](repo_root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
