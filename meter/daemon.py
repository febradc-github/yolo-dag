"""meter daemon: a loopback-only HTTP server that Claude Code's hooks POST to.

Run as `python3 -m meter.daemon` (started by scripts/meter-boot.py on
SessionStart). Binds 127.0.0.1 only, never touches the network otherwise, and
never writes to the repo working tree — only to its own SQLite store and to
`.dag/runs/<run-id>/meter/` in whichever repo a request's `cwd` points at.

Auth: meter-handoff.md Section 5.2 specifies a per-boot-regenerated bearer token
delivered to HTTP hooks via `allowedEnvVars` header interpolation. That
mechanism does not work as specified — confirmed against Claude Code's hooks
documentation: a SessionStart command hook cannot inject an environment
variable for a later HTTP hook in the same session to read, because header
interpolation only ever reads from Claude Code's own process environment, fixed
at launch. There is no way for this daemon's freshly-generated secret to reach
that substitution.

Given that, auth here is deliberately downgraded to advisory: the token is
generated once and persisted (not regenerated per boot, so a user who exports
it in their shell profile keeps working across restarts), and a request with a
missing or mismatched token is still processed rather than rejected — this
daemon only ever measures in M0 (no `updatedInput`/`updatedToolOutput`/deny
decision exists anywhere in this module), so the blast radius of an
unauthenticated local loopback request is a polluted ledger row, not a changed
tool outcome. `/health` reports the authenticated/unauthenticated split so
`dag-doctor.py` can tell a user plainly whether the header mechanism is wired
up in their environment.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config as config_mod
from . import router
from . import store

_MAX_BODY_BYTES = 2_000_000  # a hook payload should be small; refuse anything absurd


def _log_path(plugin_data_dir: Path) -> Path:
    return plugin_data_dir / "daemon.log"


def _log(plugin_data_dir: Path, msg: str) -> None:
    try:
        with _log_path(plugin_data_dir).open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
    except OSError:
        pass


def _ensure_token(plugin_data_dir: Path) -> str:
    token_path = plugin_data_dir / "token"
    if token_path.exists():
        try:
            existing = token_path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        except OSError:
            pass
    token = secrets.token_hex(32)
    token_path.write_text(token, encoding="utf-8")
    try:
        os.chmod(token_path, 0o600)
    except OSError:
        pass
    return token


def _write_port_file(plugin_data_dir: Path, port: int) -> None:
    try:
        (plugin_data_dir / "port").write_text(str(port), encoding="utf-8")
    except OSError:
        pass


class Handler(BaseHTTPRequestHandler):
    ctx: router.Context  # set on the class before serving, one ctx for all instances
    deadline_ms: int = config_mod.DEFAULT_DEADLINE_MS
    plugin_data_dir: Path

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 — stdlib hook name
        _log(self.plugin_data_dir, "http: " + (fmt % args))

    def _send_json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_payload(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}  # legitimate: no body at all — not worth a log line
        if length > _MAX_BODY_BYTES:
            _log(self.plugin_data_dir, f"payload: rejected oversized body ({length} bytes > "
                                        f"{_MAX_BODY_BYTES}) on {self.path}")
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            _log(self.plugin_data_dir, f"payload: malformed body on {self.path} ({exc})")
            return {}

    def _authenticated(self) -> bool:
        supplied = self.headers.get("X-Dag-Meter-Token", "")
        return bool(supplied) and bool(self.ctx.token) and secrets.compare_digest(supplied, self.ctx.token)

    def _dispatch(self) -> None:
        handler = router.ROUTES.get(self.path)
        if handler is None:
            self._send_json(404, {"error": "not_found"})
            return

        authenticated = self._authenticated()
        self.ctx.record_request(authenticated)
        payload = self._read_payload()

        start = time.monotonic()
        try:
            result = handler(payload, self.ctx)
        except Exception as exc:  # fail-open: never let a handler bug break the hook
            self.ctx.record_error()
            _log(self.plugin_data_dir, f"handler error on {self.path}: {exc!r}")
            result = {}
        elapsed_ms = (time.monotonic() - start) * 1000
        if elapsed_ms > self.deadline_ms:
            _log(self.plugin_data_dir,
                 f"deadline exceeded on {self.path}: {elapsed_ms:.1f}ms > {self.deadline_ms}ms")

        self._send_json(200, result)

    def do_GET(self) -> None:  # noqa: N802 — stdlib method name
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch()


def _install_signal_handlers(server: ThreadingHTTPServer, ctx: router.Context) -> None:
    def _stop(signum, frame):
        ctx.shutdown_event.set()

    try:
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
    except (ValueError, OSError):
        pass  # not in the main thread, or platform doesn't support it — best effort


def main() -> int:
    plugin_data_dir = config_mod.plugin_data_dir()
    plugin_data_dir.mkdir(parents=True, exist_ok=True)

    cfg = config_mod.load_config(repo_root=None)
    if not config_mod.is_enabled(cfg):
        _log(plugin_data_dir, "disabled via config; exiting without binding")
        return 0

    port = int(os.environ.get("DAG_METER_PORT", cfg.get("port", config_mod.DEFAULT_PORT)))
    token = _ensure_token(plugin_data_dir)

    conn = store.connect(plugin_data_dir)
    ctx = router.Context(conn=conn, cfg=cfg, plugin_data_dir=plugin_data_dir,
                          started_at=time.time(), token=token)

    Handler.ctx = ctx
    Handler.deadline_ms = int(cfg.get("deadline_ms", config_mod.DEFAULT_DEADLINE_MS))
    Handler.plugin_data_dir = plugin_data_dir

    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        # Fail open: another process holds the port (maybe another daemon instance,
        # maybe an unrelated program). Don't fight for it — hooks that can't reach
        # this daemon fail as non-blocking HTTP errors and the run proceeds
        # unwrapped, exactly per Section 5.2.
        _log(plugin_data_dir, f"bind failed on 127.0.0.1:{port}: {exc!r}")
        return 1

    _write_port_file(plugin_data_dir, port)
    _install_signal_handlers(server, ctx)
    _log(plugin_data_dir, f"listening on 127.0.0.1:{port} (pid {os.getpid()})")

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    ctx.shutdown_event.wait()
    _log(plugin_data_dir, "shutdown requested")
    server.shutdown()
    server.server_close()
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
