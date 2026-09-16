"""Request handlers for the meter daemon's HTTP endpoints.

Every handler here is a pure function of (payload, context) -> response dict,
so it can be unit-tested without a running HTTP server. None of them return a
`permissionDecision`, `updatedInput`, or `updatedToolOutput` — M0 observes and
measures only; Section 3.2's "no behavioural change" invariant is enforced by
these handlers simply never having that capability wired up, not by a runtime
check that could itself be wrong.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import config as config_mod
from . import ledger
from . import store

Handler = Callable[[dict, "Context"], dict]


class Context:
    def __init__(self, conn, cfg: dict, plugin_data_dir: Path, started_at: float,
                 token: str | None) -> None:
        self.conn = conn
        self.cfg = cfg
        self.plugin_data_dir = plugin_data_dir
        self.started_at = started_at
        self.token = token
        self.correlator = ledger.Correlator()
        self.stats_lock = threading.Lock()
        self.request_count = 0
        self.authenticated_count = 0
        self.unauthenticated_count = 0
        self.error_count = 0
        self.shutdown_event = threading.Event()
        # Transcript parsing (SubagentStop) can be slow on a long conversation, and
        # the 50ms soft deadline (Section 5.2) says a hot-path handler should return
        # an empty decision immediately and finish asynchronously rather than block
        # the caller. One background worker is enough: SubagentStop volume is at
        # most one per finished node, nowhere near PreToolUse's hot-path rate.
        self._work_queue: "queue.Queue[tuple]" = queue.Queue()
        self._worker = threading.Thread(target=self._drain_queue, daemon=True)
        self._worker.start()

    def enqueue(self, fn: Callable[[dict, "Context"], None], payload: dict) -> None:
        self._work_queue.put((fn, payload))

    def _drain_queue(self) -> None:
        while True:
            fn, payload = self._work_queue.get()
            try:
                fn(payload, self)
            except Exception:
                self.record_error()

    def record_request(self, authenticated: bool) -> None:
        with self.stats_lock:
            self.request_count += 1
            if authenticated:
                self.authenticated_count += 1
            else:
                self.unauthenticated_count += 1

    def record_error(self) -> None:
        with self.stats_lock:
            self.error_count += 1


def _repo_root_and_run_dir(cwd: str | None, run_id: str | None) -> tuple[Path | None, Path | None]:
    if not cwd:
        return None, None
    repo_root = config_mod.find_repo_root(Path(cwd))
    if repo_root is None:
        return None, None
    run_dir = repo_root / ".dag" / "runs" / (run_id or "_unattributed")
    return repo_root, run_dir


def _maybe_capture(ctx: Context, endpoint: str, payload: dict) -> None:
    if not ctx.cfg.get("probe_capture"):
        return
    try:
        capture_path = ctx.plugin_data_dir / "probe-capture.jsonl"
        with capture_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), "endpoint": endpoint,
                                  "payload_keys": sorted(payload.keys()),
                                  "payload": payload}, default=str) + "\n")
    except OSError:
        pass


def handle_health(payload: dict, ctx: Context) -> dict:
    return {
        "status": "ok",
        "uptime_s": round(time.time() - ctx.started_at, 1),
        "requests": ctx.request_count,
        "authenticated": ctx.authenticated_count,
        "unauthenticated": ctx.unauthenticated_count,
        "errors": ctx.error_count,
    }


def handle_subagent_start(payload: dict, ctx: Context) -> dict:
    _maybe_capture(ctx, "subagent_start", payload)
    session_id = payload.get("session_id") or payload.get("sessionId") or ""
    agent_id = payload.get("agent_id") or payload.get("agentId")
    agent_type = payload.get("agent_type") or payload.get("agentType") or "unknown"
    if not agent_id:
        return {}  # can't attribute anything without an agent_id; observe nothing

    correlation = ctx.correlator.claim(session_id)
    run_id, node_id = correlation if correlation else (None, None)

    try:
        ledger.record_subagent_start(
            ctx.conn, run_id=run_id, node_id=node_id, agent_id=agent_id,
            agent_type=agent_type, started_at=int(time.time()),
        )
    except Exception:
        ctx.record_error()
    return {}


def handle_subagent_stop(payload: dict, ctx: Context) -> dict:
    # Fast path: just capture + enqueue. Transcript parsing happens off-thread so
    # this handler stays well under the deadline regardless of transcript size.
    _maybe_capture(ctx, "subagent_stop", payload)
    ctx.enqueue(_process_subagent_stop, payload)
    return {}


def _process_subagent_stop(payload: dict, ctx: Context) -> None:
    agent_id = payload.get("agent_id") or payload.get("agentId")
    agent_type = payload.get("agent_type") or payload.get("agentType") or "unknown"
    transcript_path = (payload.get("agent_transcript_path")
                        or payload.get("transcript_path")
                        or payload.get("transcriptPath"))
    cwd = payload.get("cwd")
    if not agent_id:
        return

    existing = store.get_row_by_agent_id(ctx.conn, agent_id)
    run_id = existing["run_id"] if existing else None
    node_id = existing["node"] if existing else None
    started_at = existing["started_at"] if existing else None

    ledger.record_subagent_stop(
        ctx.conn, run_id=run_id, node_id=node_id, agent_id=agent_id,
        agent_type=agent_type, transcript_path=transcript_path,
        started_at=started_at, ended_at=int(time.time()),
    )

    if run_id and run_id != "_unattributed":
        repo_root, run_dir = _repo_root_and_run_dir(cwd, run_id)
        if run_dir is not None:
            run_json = _read_run_json(run_dir)
            ledger.write_run_artifacts(ctx.conn, run_dir, run_id, run_json)


def handle_tool_pre_agent(payload: dict, ctx: Context) -> dict:
    _maybe_capture(ctx, "tool_pre_agent", payload)
    session_id = payload.get("session_id") or payload.get("sessionId") or ""
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    prompt_text = ""
    if isinstance(tool_input, dict):
        prompt_text = str(tool_input.get("prompt") or tool_input.get("description") or "")
        if not prompt_text:
            prompt_text = json.dumps(tool_input, default=str)

    marker = ledger.extract_dag_node(prompt_text)
    if marker and session_id:
        run_id, node_id = marker
        ctx.correlator.push(session_id, run_id, node_id)

    return {}  # never a decision — M0 must not change tool behaviour


def handle_shutdown(payload: dict, ctx: Context) -> dict:
    ctx.shutdown_event.set()
    return {"status": "shutting_down"}


def _read_run_json(run_dir: Path) -> dict | None:
    # run_dir is the run's own directory (…/runs/<run-id>); run.json lives there,
    # meter's own artifacts go one level down in …/runs/<run-id>/meter/.
    path = run_dir / "run.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


ROUTES: dict[str, Handler] = {
    "/health": handle_health,
    "/subagent/start": handle_subagent_start,
    "/subagent/stop": handle_subagent_stop,
    "/tool/pre-agent": handle_tool_pre_agent,
    "/shutdown": handle_shutdown,
}
