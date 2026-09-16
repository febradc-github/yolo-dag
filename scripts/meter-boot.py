#!/usr/bin/env python3
"""SessionStart hook: starts (or refcounts an existing) meter daemon.

Fail-open at every step: if anything here goes wrong, meter simply never comes
up and yolo-dag behaves exactly as it does without this plugin. This script
must always exit 0 and must never write anything but one JSON object to
stdout — stray stdout is a documented way to break hook JSON parsing, so every
diagnostic goes to the plugin data dir's log file instead.
"""

from __future__ import annotations

import json
import os
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
except Exception:
    # If the meter package itself can't import, there is nothing more to try.
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart"}}))
    sys.exit(0)


def _log(plugin_data_dir: Path, msg: str) -> None:
    try:
        with (plugin_data_dir / "daemon.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} boot: {msg}\n")
    except OSError:
        pass


def _emit(context: str | None) -> None:
    out: dict = {"hookSpecificOutput": {"hookEventName": "SessionStart"}}
    if context:
        out["hookSpecificOutput"]["additionalContext"] = context
    print(json.dumps(out))


def _health_check(port: int, token: str, timeout: float = 1.0) -> bool:
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/health",
            headers={"X-Dag-Meter-Token": token},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def _bump_refcount(plugin_data_dir: Path, delta: int) -> None:
    path = plugin_data_dir / "refcount"
    try:
        current = int(path.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        current = 0
    path.write_text(str(max(0, current + delta)), encoding="utf-8")


def main() -> int:
    if sys.version_info < (3, 11):
        _emit("meter: needs Python 3.11+, found "
              f"{sys.version_info.major}.{sys.version_info.minor}; not starting.")
        return 0

    plugin_data_dir = config_mod.plugin_data_dir()
    try:
        plugin_data_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        _emit(None)
        return 0

    repo_root = config_mod.find_repo_root(Path.cwd())
    cfg = config_mod.load_config(repo_root)
    if not config_mod.is_enabled(cfg):
        _log(plugin_data_dir, "disabled via config")
        _emit(None)
        return 0

    port = int(os.environ.get("DAG_METER_PORT", cfg.get("port", config_mod.DEFAULT_PORT)))
    token_path = plugin_data_dir / "token"
    port_path = plugin_data_dir / "port"

    lock_path = plugin_data_dir / "boot.lock"
    lock_fh = None
    try:
        lock_fh = open(lock_path, "a+")
        try:
            import fcntl
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
        except ImportError:
            pass  # best-effort on platforms without fcntl

        already_running = False
        if token_path.exists() and port_path.exists():
            try:
                existing_token = token_path.read_text(encoding="utf-8").strip()
                existing_port = int(port_path.read_text(encoding="utf-8").strip())
                already_running = _health_check(existing_port, existing_token)
            except (OSError, ValueError):
                already_running = False

        if not already_running:
            log_path = plugin_data_dir / "daemon.log"
            try:
                log_fh = open(log_path, "a")
            except OSError:
                log_fh = subprocess.DEVNULL
            try:
                # The daemon is one process shared by every session on this
                # machine ("refcounted per session", Section 5.1) — it has no
                # repo of its own, so it can't resolve a `.dag/meter.json`
                # itself. Whichever session actually boots it is the one with
                # repo context, so relay this session's already-resolved
                # config via env vars the daemon already reads. This means
                # the config of the *first* booter in the daemon's current
                # lifetime wins for every session sharing it afterward — a
                # real limit of "one daemon per machine", not a bug, but a
                # per-repo port override only actually works if that repo's
                # own hooks.json (static, plugin-shipped) also points at it.
                daemon_env = dict(os.environ)
                daemon_env["DAG_METER_PORT"] = str(port)
                if cfg.get("probe_capture"):
                    daemon_env["DAG_METER_PROBE_CAPTURE"] = "1"
                subprocess.Popen(
                    [sys.executable, "-m", "meter.daemon"],
                    cwd=str(PLUGIN_ROOT),
                    env=daemon_env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_fh,
                    stderr=log_fh,
                    start_new_session=True,
                )
            except OSError as exc:
                _log(plugin_data_dir, f"failed to spawn daemon: {exc!r}")
                _emit(None)
                return 0

            # Bounded wait for the daemon to come up; proceed either way.
            deadline = time.monotonic() + 3.0
            token = token_path.read_text(encoding="utf-8").strip() if token_path.exists() else ""
            healthy = False
            while time.monotonic() < deadline:
                time.sleep(0.15)
                if token_path.exists() and port_path.exists():
                    try:
                        token = token_path.read_text(encoding="utf-8").strip()
                        started_port = int(port_path.read_text(encoding="utf-8").strip())
                    except (OSError, ValueError):
                        continue
                    if _health_check(started_port, token):
                        healthy = True
                        break
            if not healthy:
                _log(plugin_data_dir, "daemon did not become healthy within 3s")

        _bump_refcount(plugin_data_dir, +1)
    finally:
        if lock_fh is not None:
            try:
                import fcntl
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
            except ImportError:
                pass
            lock_fh.close()

    _emit(f"meter: measuring (M0), port {port}. Run /dag-cost after a run to see it. "
          f"Disable with DAG_METER_DISABLE=1 or {{\"enabled\": false}} in .dag/meter.json.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Absolute last resort: never let this script's own bug block a session.
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart"}}))
        sys.exit(0)
