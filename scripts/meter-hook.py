#!/usr/bin/env python3
"""Thin command-hook client for meter's rare lifecycle events (SessionEnd; a
`precompact` no-op is kept for forward compatibility, though PreCompact isn't
registered in hooks.json yet — M0 has nothing that needs it).

Talks directly to the SQLite store rather than the daemon over HTTP: the daemon
may already be gone by SessionEnd (a prior session's refcount reached zero), and
main-thread token accounting doesn't need it running. Fail-open throughout —
this script always exits 0.

Usage: meter-hook.py <sessionend|precompact>, hook payload JSON on stdin.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

try:
    from meter import config as config_mod
    from meter import governor
    from meter import ledger
    from meter import store
except Exception:
    sys.exit(0)


def _log(plugin_data_dir: Path, msg: str) -> None:
    try:
        with (plugin_data_dir / "daemon.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} hook: {msg}\n")
    except OSError:
        pass


def _read_stdin_payload() -> dict:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        return json.loads(raw)
    except (json.JSONDecodeError, OSError):
        return {}


def _bump_refcount(plugin_data_dir: Path, delta: int) -> int:
    path = plugin_data_dir / "refcount"
    try:
        current = int(path.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        current = 0
    new_value = max(0, current + delta)
    try:
        path.write_text(str(new_value), encoding="utf-8")
    except OSError:
        pass
    return new_value


def _request_shutdown(plugin_data_dir: Path) -> None:
    token_path = plugin_data_dir / "token"
    port_path = plugin_data_dir / "port"
    if not (token_path.exists() and port_path.exists()):
        return
    try:
        token = token_path.read_text(encoding="utf-8").strip()
        port = int(port_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/shutdown", data=b"{}", method="POST",
            headers={"X-Dag-Meter-Token": token, "Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=1.0)
    except (urllib.error.URLError, OSError, TimeoutError):
        pass  # daemon already gone, or unreachable — nothing more to do


def _handle_sessionend(payload: dict) -> None:
    plugin_data_dir = config_mod.plugin_data_dir()
    cwd = payload.get("cwd") or str(Path.cwd())
    repo_root = config_mod.find_repo_root(Path(cwd))

    if repo_root is not None:
        latest = ledger.find_latest_run(repo_root)
        if latest is not None:
            run_id, run_dir = latest
            transcript_path = payload.get("transcript_path") or payload.get("transcriptPath")
            session_id = payload.get("session_id") or payload.get("sessionId") or "main"
            try:
                conn = store.connect(plugin_data_dir)
                usage = (ledger.parse_transcript_usage(transcript_path) if transcript_path
                         else {"in_tok": None, "out_tok": None, "cache_read": None,
                               "cache_write": None, "reason": "no_transcript_path_in_payload"})
                store.upsert_ledger_row(conn, {
                    "run_id": run_id, "node": "_main", "agent_id": f"main:{session_id}",
                    "agent_type": "orchestrator", "phase": None, "model": None,
                    "in_tok": usage["in_tok"], "out_tok": usage["out_tok"],
                    "cache_read": usage["cache_read"], "cache_write": usage["cache_write"],
                    "wall_ms": None, "source": "live",
                    "started_at": None, "ended_at": int(time.time()),
                })
                run_json = None
                try:
                    run_json = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    pass
                ledger.write_run_artifacts(conn, run_dir, run_id, run_json, plugin_data_dir)

                rows = store.get_ledger_rows(conn, run_id)
                mode = (run_json or {}).get("mode", "unknown")
                ledger.update_baseline(plugin_data_dir, config_mod.repo_fingerprint(repo_root),
                                        mode, rows)
                # M6 Governor 6a: recompute weights.json now that baseline.json just
                # changed, so the orchestrator's next run picks up fresher coefficients.
                governor.recalibrate_weights(plugin_data_dir)
            except Exception as exc:
                _log(plugin_data_dir, f"sessionend accounting failed: {exc!r}")

    remaining = _bump_refcount(plugin_data_dir, -1)
    if remaining <= 0:
        _request_shutdown(plugin_data_dir)


def main() -> int:
    if len(sys.argv) < 2:
        return 0
    event = sys.argv[1]
    payload = _read_stdin_payload()

    if event == "sessionend":
        _handle_sessionend(payload)
    # "precompact" and anything else: no-op, reserved for a later milestone.

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
