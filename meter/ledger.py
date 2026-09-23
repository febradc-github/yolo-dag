"""M0 Ledger: node correlation, transcript-based token accounting, and reporting.

Behaviour per meter-handoff.md Section 4 (M0):
- SubagentStart records agent_id/agent_type/start time and the run/node correlation.
- SubagentStop reads the transcript and sums usage fields; never blocks, never guesses
  — an unparseable transcript records tokens: null with a reason.
- Round-over-round growth is tracked per resumed specialist (idle gap vs. cache-read
  ratio), because that is what section 8.7's cache-TTL recommendation is computed from.

Everything here is read-only with respect to the run: it writes only to
`.dag/runs/<run-id>/meter/` and to meter's own SQLite store, never to the working
tree, and never returns a hook decision that changes tool behaviour.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from . import attribution
from . import echo
from . import governor
from . import oracle
from . import quorum
from . import store
from . import throttle

_DAG_NODE_RE = re.compile(r"DAG-NODE:\s*([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")

# How long a PreToolUse-observed DAG-NODE marker waits for its SubagentStart before
# it's considered stale and dropped. Generous, because a pass can dispatch several
# spawns in one turn before any of them actually starts.
_PENDING_TTL_SECONDS = 900

_MODEL_WEIGHTS = {"inherit": 1.0, "sonnet": 0.5, "haiku": 0.1}


def extract_dag_node(prompt_text: str) -> tuple[str, str] | None:
    if not prompt_text:
        return None
    m = _DAG_NODE_RE.search(prompt_text)
    if not m:
        return None
    return m.group(1), m.group(2)


def phase_of(node_id: str) -> str:
    if node_id.startswith("P1"):
        return "1"
    if node_id.startswith("P2"):
        return "2"
    if node_id.startswith("P3"):
        return "3"
    if node_id.startswith("P4"):
        return "4"
    if node_id.startswith("P6"):
        return "6"
    if node_id.endswith("-worker") or node_id.endswith("-reviewer") or re.match(r"^T-", node_id):
        return "5"
    return "unknown"


class Correlator:
    """Maps a spawn tool's DAG-NODE marker (seen at PreToolUse) to the agent_id
    Claude Code assigns once the subagent actually starts (seen at SubagentStart).

    There is no shared key between those two events other than order — see
    meter-handoff.md Section 6.4 ("the hardest plumbing problem"). This is a FIFO
    queue per session_id: PreToolUse necessarily fires before its SubagentStart, and
    within one dispatch batch they're expected to start in roughly the order they
    were issued. It's a heuristic, not a guarantee; a session_id with no queued
    marker (or a stale one past the TTL) correlates to node: null, per the "never
    guess an attribution" rule — none is a correlation *failure*, not a fabricated
    one.
    """

    def __init__(self) -> None:
        self._queues: dict[str, list[tuple[float, str, str, str]]] = {}
        self._lock = threading.Lock()

    def push(self, session_id: str, run_id: str, node_id: str, prompt_text: str = "") -> None:
        with self._lock:
            self._queues.setdefault(session_id, []).append(
                (time.time(), run_id, node_id, prompt_text))

    def claim(self, session_id: str) -> tuple[str, str, str] | None:
        """Returns (run_id, node_id, prompt_text). `prompt_text` is the Task
        prompt seen at PreToolUse — carried through so M4 Echo can attribute
        it to the agent_id that only becomes known here, at SubagentStart."""
        with self._lock:
            queue = self._queues.get(session_id)
            if not queue:
                return None
            now = time.time()
            while queue and now - queue[0][0] > _PENDING_TTL_SECONDS:
                queue.pop(0)
            if not queue:
                return None
            _, run_id, node_id, prompt_text = queue.pop(0)
            return run_id, node_id, prompt_text


def parse_transcript_usage(transcript_path: str) -> dict[str, Any]:
    """Sum usage fields across every assistant message in a transcript JSONL.

    Field names (`input_tokens`, `output_tokens`, `cache_creation_input_tokens`,
    `cache_read_input_tokens`) are taken from meter-handoff.md Section 4 and are
    marked VERIFY there — they have not been confirmed against a live transcript
    from this installation. On any parse failure, or if a transcript is
    well-formed but never yields a recognizable usage block, this returns
    tokens: null with `reason` set, per the documented failure mode: never block,
    never guess a number.
    """
    path = Path(transcript_path)
    if not path.exists():
        return {"in_tok": None, "out_tok": None, "cache_read": None, "cache_write": None,
                "reason": "transcript_not_found"}

    in_tok = out_tok = cache_read = cache_write = 0
    saw_usage = False
    model: str | None = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                usage = _find_usage_block(entry)
                if usage is None:
                    continue
                saw_usage = True
                in_tok += int(usage.get("input_tokens") or 0)
                out_tok += int(usage.get("output_tokens") or 0)
                cache_read += int(usage.get("cache_read_input_tokens") or 0)
                cache_write += int(usage.get("cache_creation_input_tokens") or 0)
                model = _find_model(entry) or model
    except OSError as exc:
        return {"in_tok": None, "out_tok": None, "cache_read": None, "cache_write": None,
                "model": None, "reason": f"read_error: {exc}"}

    if not saw_usage:
        return {"in_tok": None, "out_tok": None, "cache_read": None, "cache_write": None,
                "model": None, "reason": "no_usage_block_found"}

    return {"in_tok": in_tok, "out_tok": out_tok, "cache_read": cache_read,
            "cache_write": cache_write, "model": model, "reason": None}


def _find_usage_block(entry: dict) -> dict | None:
    """Usage can plausibly live at a few different depths depending on transcript
    shape (top-level, under `message`, under `message.usage`). Try the plausible
    spots rather than assuming one; this is exactly the kind of guess
    dag-doctor's probe-capture mode exists to replace with a confirmed path."""
    for candidate in (
        entry.get("usage"),
        (entry.get("message") or {}).get("usage") if isinstance(entry.get("message"), dict) else None,
    ):
        if isinstance(candidate, dict) and any(
            k in candidate for k in ("input_tokens", "output_tokens",
                                      "cache_read_input_tokens", "cache_creation_input_tokens")
        ):
            return candidate
    return None


def _find_model(entry: dict) -> str | None:
    """The raw model identifier can plausibly live alongside usage at the same
    depths `_find_usage_block` checks — same reasoning, same VERIFY caveat."""
    message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
    for candidate in (entry.get("model"), message.get("model")):
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def tier_from_model(model: str | None) -> str:
    """Buckets a raw model identifier (e.g. `claude-haiku-4-5-20251001`) into
    meter's coarse tier vocabulary (`inherit`/`sonnet`/`haiku` — the weights
    `_MODEL_WEIGHTS` and Vault's key formula use), substring-matched
    case-insensitively since exact model ids change across releases.
    Anything unrecognised, including `None`, normalises to `inherit` — the
    same default `_MODEL_WEIGHTS.get(..., 1.0)` already assumed."""
    if not model:
        return "inherit"
    lowered = model.lower()
    if "haiku" in lowered:
        return "haiku"
    if "sonnet" in lowered:
        return "sonnet"
    return "inherit"


def record_subagent_start(conn: sqlite3.Connection, *, run_id: str | None, node_id: str | None,
                           agent_id: str, agent_type: str, started_at: int) -> None:
    store.upsert_ledger_row(conn, {
        "run_id": run_id or "_unattributed",
        "node": node_id,
        "agent_id": agent_id,
        "agent_type": agent_type,
        "phase": phase_of(node_id) if node_id else None,
        "model": None,  # not known until the transcript exists at SubagentStop
        "in_tok": None, "out_tok": None, "cache_read": None, "cache_write": None,
        "wall_ms": None,
        "source": "live",
        "started_at": started_at,
        "ended_at": None,
    })


def record_subagent_stop(conn: sqlite3.Connection, *, run_id: str | None, node_id: str | None,
                          agent_id: str, agent_type: str, transcript_path: str | None,
                          started_at: int | None, ended_at: int) -> dict:
    """Returns the row written, for callers that want to log/report immediately."""
    run_id = run_id or "_unattributed"
    usage = (parse_transcript_usage(transcript_path) if transcript_path
             else {"in_tok": None, "out_tok": None, "cache_read": None, "cache_write": None,
                   "model": None, "reason": "no_transcript_path_in_payload"})

    wall_ms = None
    if started_at is not None:
        wall_ms = max(0, (ended_at - started_at) * 1000)

    last_round = store.get_last_round(conn, run_id, agent_id)
    idle_gap_ms = None
    if last_round is not None and last_round.get("ended_at") is not None:
        idle_gap_ms = max(0, (ended_at - last_round["ended_at"]) * 1000)

    row = {
        "run_id": run_id,
        "node": node_id,
        "agent_id": agent_id,
        "agent_type": agent_type,
        "phase": phase_of(node_id) if node_id else None,
        "model": usage.get("model"),  # raw identifier; bucket with tier_from_model() at point of use
        "in_tok": usage["in_tok"], "out_tok": usage["out_tok"],
        "cache_read": usage["cache_read"], "cache_write": usage["cache_write"],
        "wall_ms": wall_ms,
        "source": "live",
        "started_at": started_at,
        "ended_at": ended_at,
        # M6 Governor 6b's proxy signal (meter/governor.py) — the final tally
        # of every PreToolUse call this agent_id made, if any were counted.
        "call_count": store.get_tool_call_count(conn, agent_id) or None,
    }
    store.upsert_ledger_row(conn, row)
    store.append_ledger_round(conn, {
        "run_id": run_id, "agent_id": agent_id,
        "in_tok": usage["in_tok"], "out_tok": usage["out_tok"],
        "cache_read": usage["cache_read"], "cache_write": usage["cache_write"],
        "wall_ms": wall_ms, "idle_gap_ms": idle_gap_ms, "ended_at": ended_at,
        "reason": usage.get("reason"),
    })
    return row


def write_run_artifacts(conn: sqlite3.Connection, run_dir: Path, run_id: str,
                         run_json: dict | None, plugin_data_dir: Path | None = None) -> None:
    """Regenerate ledger.jsonl and report.md for one run from the SQLite ledger.
    Idempotent and cheap enough to call after every SubagentStop for that run.
    `plugin_data_dir` is optional so existing callers don't break; without it,
    the report simply has no Governor section (6c/6d — see meter/governor.py)."""
    meter_dir = run_dir / "meter"
    meter_dir.mkdir(parents=True, exist_ok=True)
    rows = store.get_ledger_rows(conn, run_id)

    with (meter_dir / "ledger.jsonl").open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")

    echo_rows = store.get_echo_rows(conn, run_id)
    governor_section = (governor.report_section(plugin_data_dir, run_id, conn)
                         if plugin_data_dir is not None else None)
    narration_section = throttle.render_report_section(store.get_narration_rows(conn, run_id))
    oracle_section = oracle.render_report_section(conn, run_id)
    attribution_section = attribution.render_report_section(conn, run_id)
    quorum_section = quorum.render_report_section(conn)
    report = _render_report(rows, run_id, run_json, echo_rows, governor_section,
                             narration_section, oracle_section, attribution_section,
                             quorum_section)
    (meter_dir / "report.md").write_text(report, encoding="utf-8")


def _render_report(rows: list[dict], run_id: str, run_json: dict | None,
                    echo_rows: list[dict] | None = None,
                    governor_section: str | None = None,
                    narration_section: str | None = None,
                    oracle_section: str | None = None,
                    attribution_section: str | None = None,
                    quorum_section: str | None = None) -> str:
    total_in = sum(r["in_tok"] or 0 for r in rows)
    total_out = sum(r["out_tok"] or 0 for r in rows)
    total_cache_read = sum(r["cache_read"] or 0 for r in rows)
    total_cache_write = sum(r["cache_write"] or 0 for r in rows)
    unattributed = sum(1 for r in rows if r["in_tok"] is None)
    denom = total_in + total_cache_read
    cache_ratio = (total_cache_read / denom * 100) if denom else 0.0

    lines = [f"# meter report — {run_id}", ""]
    lines.append(
        f"Measured: {total_in:,} in / {total_out:,} out  "
        f"(cache read {total_cache_read:,}, cache write {total_cache_write:,}, "
        f"cache-read ratio {cache_ratio:.0f}%)"
    )
    if unattributed:
        lines.append(f"{unattributed} of {len(rows)} node(s) have unmeasured tokens "
                      f"(transcript unreadable or usage fields not found — see `reason` "
                      f"in ledger.jsonl).")

    if run_json is not None:
        spawns = run_json.get("spawns") or []
        weighted = sum(_MODEL_WEIGHTS.get(s.get("model", "inherit"), 1.0) for s in spawns)
        lines.append(f"Spawn-unit estimate (orchestrator budget): {weighted:.1f} units "
                      f"across {len(spawns)} spawn(s).")

    by_phase: dict[str, dict[str, int]] = {}
    for r in rows:
        phase = r.get("phase") or "unknown"
        agg = by_phase.setdefault(phase, {"in": 0, "out": 0, "agents": 0})
        agg["in"] += r["in_tok"] or 0
        agg["out"] += r["out_tok"] or 0
        agg["agents"] += 1

    lines.append("")
    lines.append("By phase")
    for phase in sorted(by_phase, key=lambda p: (p == "unknown", p)):
        agg = by_phase[phase]
        lines.append(f"  Phase {phase:<8} {agg['in']:>10,} in  {agg['out']:>8,} out  "
                      f"{agg['agents']:>3} agent(s)")

    top = sorted(rows, key=lambda r: (r["in_tok"] or 0) + (r["out_tok"] or 0), reverse=True)[:5]
    if top:
        lines.append("")
        lines.append("Top nodes by spend")
        for r in top:
            label = r.get("node") or f"{r.get('agent_type')}/{r.get('agent_id', '')[:8]}"
            lines.append(f"  {label:<40} {r['in_tok'] or 0:>8,} in  {r['out_tok'] or 0:>8,} out")

    echo_section = echo.render_report_section(echo_rows) if echo_rows else None
    if echo_section:
        lines.append("")
        lines.append(echo_section)

    if governor_section:
        lines.append("")
        lines.append(governor_section)

    if narration_section:
        lines.append("")
        lines.append(narration_section)

    if oracle_section:
        lines.append("")
        lines.append(oracle_section)

    if attribution_section:
        lines.append("")
        lines.append(attribution_section)

    if quorum_section:
        lines.append("")
        lines.append(quorum_section)

    return "\n".join(lines) + "\n"


def find_latest_run(repo_root: Path) -> tuple[str, Path] | None:
    """The most recently modified `.dag/runs/<run-id>/run.json`, for attributing
    main-thread (orchestrator) tokens at SessionEnd — the main thread's transcript
    has no DAG-NODE marker of its own, since it's never spawned as a subagent."""
    runs_dir = repo_root / ".dag" / "runs"
    if not runs_dir.is_dir():
        return None
    candidates = []
    for run_json in runs_dir.glob("*/run.json"):
        try:
            candidates.append((run_json.stat().st_mtime, run_json.parent.name, run_json.parent))
        except OSError:
            continue
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0], reverse=True)
    _, run_id, run_dir = candidates[0]
    return run_id, run_dir


def update_baseline(plugin_data_dir: Path, repo_fingerprint: str, mode: str,
                     rows: list[dict]) -> None:
    """Aggregate stats for the Governor's cost model (meter-handoff.md Section 6d /
    M6a), keyed by repo fingerprint and mode. Read by meter/governor.py's
    `recalibrate_weights` (6a, token-based) and `predict_call_budget` (6b,
    the call-count proxy — see governor.py's module docstring)."""
    baseline_path = plugin_data_dir / "baseline.json"
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        baseline = {}

    key = f"{repo_fingerprint}:{mode}"
    entry = baseline.setdefault(key, {"runs": 0, "by_agent_type": {}})
    entry["runs"] += 1
    for row in rows:
        agent_type = row.get("agent_type") or "unknown"
        agg = entry["by_agent_type"].setdefault(
            agent_type, {"samples": 0, "in_tok_total": 0, "out_tok_total": 0,
                         "call_samples": 0, "calls_total": 0})
        # A baseline.json written before M6 existed has agent_type entries
        # missing the two call_* keys — top them up rather than KeyError.
        agg.setdefault("call_samples", 0)
        agg.setdefault("calls_total", 0)
        if row.get("in_tok") is not None:
            agg["samples"] += 1
            agg["in_tok_total"] += row["in_tok"]
            agg["out_tok_total"] += row.get("out_tok") or 0
        if row.get("call_count") is not None:
            agg["call_samples"] += 1
            agg["calls_total"] += row["call_count"]

    try:
        plugin_data_dir.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(json.dumps(baseline, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass  # fail-open: baseline.json is a convenience, not load-bearing in M0
