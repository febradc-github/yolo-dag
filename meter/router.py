"""Request handlers for the meter daemon's HTTP endpoints.

Every handler here is a pure function of (payload, context) -> response dict,
so it can be unit-tested without a running HTTP server. `updatedInput` is
Clamp 3a's capability and hasn't shipped yet — bounded reads need to know
which spans are safe to narrow to, which is more than the current dossier
gives for free.

Behavioural-change exceptions so far:

- `handle_subagent_stop` (M1 Receipts) can return a top-level
  `{"decision": "block", "reason": ...}` to force exactly one repair turn on a
  missing/invalid dag-receipt block (see meter/receipts.py).
- `handle_tool_post` (M3b Clamp) can return
  `{"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": ...}}`
  to replace an oversized Bash/Grep result with a compressed one (see
  meter/clamp.py).
- `handle_subagent_start` (M2 Dossier) can return
  `{"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": ...}}`
  pointing at a precomputed context pack (see meter/dossier.py).
- `handle_tool_pre_dossier` (M2 Dossier's enforcement half) can deny a
  `Grep`/`Glob` call via `permissionDecision: "deny"` when the calling agent
  has a dossier it hasn't read yet, up to `meter.modules.dossier.max_denials`.

All of these wire-format choices are genuinely unverified — see
scripts/dag-doctor.py's VERIFY items — and all fail open by construction: an
exception in any of these modules is caught and swallowed before it can
affect the decision, dossier enforcement never denies without a resolved
`agent_id` to key its state on, and Clamp's compressors individually decline
whenever they aren't confident enough to compress safely.
"""

from __future__ import annotations

import functools
import json
import queue
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import attribution
from . import clamp
from . import config as config_mod
from . import dossier as dossier_mod
from . import echo
from . import governor
from . import ledger
from . import oracle
from . import receipts
from . import sieve
from . import store
from . import throttle
from . import vault

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
    run_id, node_id, prompt_text = correlation if correlation else (None, None, "")

    echo_cfg = (ctx.cfg.get("modules") or {}).get("echo") or {}
    if echo_cfg.get("enabled") and run_id and prompt_text:
        try:
            echo.record(ctx.conn, run_id=run_id, agent_id=agent_id, origin="task_prompt",
                        text=prompt_text)
        except Exception:
            ctx.record_error()

    try:
        ledger.record_subagent_start(
            ctx.conn, run_id=run_id, node_id=node_id, agent_id=agent_id,
            agent_type=agent_type, started_at=int(time.time()),
        )
    except Exception:
        ctx.record_error()

    additional_context = None
    dossier_cfg = (ctx.cfg.get("modules") or {}).get("dossier") or {}
    if dossier_cfg.get("enabled") and run_id and node_id:
        cwd = payload.get("cwd")
        repo_root, run_dir = _repo_root_and_run_dir(cwd, run_id)
        if run_dir is not None:
            safe_node = node_id.replace("/", "_")
            dossier_path = run_dir / "meter" / "dossiers" / f"{safe_node}.md"
            manifest_path = run_dir / "meter" / "dossiers" / safe_node / "manifest.md"
            if dossier_path.is_file():
                try:
                    store.set_dossier_state(ctx.conn, agent_id=agent_id, run_id=run_id,
                                             node=node_id, dossier_path=str(dossier_path))
                    additional_context = _dossier_pointer_text(node_id, dossier_path)
                except Exception:
                    ctx.record_error()
            elif manifest_path.is_file():
                # v2 M10 Pull: the manifest itself IS the additionalContext —
                # short enough (~200 tok) to inject directly rather than
                # pointing at it, unlike push mode's full dossier body.
                # Marked "read" immediately: unlike a push-mode dossier
                # pointer, there is nothing further the agent needs to Read
                # before searching, so M2's Glob/Grep-until-read enforcement
                # (meant for the push case) must not fire here.
                try:
                    store.set_dossier_state(ctx.conn, agent_id=agent_id, run_id=run_id,
                                             node=node_id, dossier_path=str(manifest_path))
                    store.mark_dossier_read(ctx.conn, agent_id)
                    additional_context = manifest_path.read_text(encoding="utf-8")
                except Exception:
                    ctx.record_error()

    if additional_context:
        return {"hookSpecificOutput": {"hookEventName": "SubagentStart",
                                        "additionalContext": additional_context}}
    return {}


def _dossier_pointer_text(node_id: str, dossier_path: Path) -> str:
    """Factual statements only, per meter-handoff.md Section 6.2: Claude
    Code's own guidance treats imperative phrasing in additionalContext as a
    potential prompt-injection signal, which can get surfaced to the user
    instead of used."""
    return (f"A precomputed context pack for task {node_id} is at {dossier_path}. "
            f"It was built from the repository index by deterministic tooling, not by a model, "
            f"and lists the owned files, indexed symbols, relevant contract text, and any sibling "
            f"task receipts that already touched the same files.")


def handle_subagent_stop(payload: dict, ctx: Context) -> dict:
    _maybe_capture(ctx, "subagent_stop", payload)

    agent_id = payload.get("agent_id") or payload.get("agentId")
    transcript_path = (payload.get("agent_transcript_path")
                        or payload.get("transcript_path")
                        or payload.get("transcriptPath"))

    receipts_cfg = (ctx.cfg.get("modules") or {}).get("receipts") or {}
    outcome = None
    if agent_id and receipts_cfg.get("enabled") and transcript_path:
        existing = store.get_row_by_agent_id(ctx.conn, agent_id)
        run_id = existing["run_id"] if existing else "_unattributed"
        node_id = existing["node"] if existing else None
        try:
            outcome = receipts.check(
                ctx.conn, run_id=run_id, node_id=node_id, agent_id=agent_id,
                transcript_path=transcript_path,
                repair_turns=int(receipts_cfg.get("repair_turns", 1)),
            )
        except Exception:
            ctx.record_error()  # fail-open: a receipts bug must never block a subagent

    # Ledger accounting (transcript token summation) always happens off-thread —
    # it can be slow on a long conversation and isn't needed to answer this hook.
    ctx.enqueue(functools.partial(_process_subagent_stop, receipt_outcome=outcome), payload)

    if outcome is not None and outcome.block:
        return {"decision": "block", "reason": outcome.reason}  # top-level, not hookSpecificOutput — see receipts.py
    return {}


def _process_subagent_stop(payload: dict, ctx: Context, *, receipt_outcome=None) -> None:
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

    ledger_row = ledger.record_subagent_stop(
        ctx.conn, run_id=run_id, node_id=node_id, agent_id=agent_id,
        agent_type=agent_type, transcript_path=transcript_path,
        started_at=started_at, ended_at=int(time.time()),
    )

    if run_id and run_id != "_unattributed":
        repo_root, run_dir = _repo_root_and_run_dir(cwd, run_id)
        if run_dir is not None:
            run_json = _read_run_json(run_dir)
            ledger.write_run_artifacts(ctx.conn, run_dir, run_id, run_json, ctx.plugin_data_dir)

            # M1: persist the receipt and spool the transcript once we're not on the
            # hot path. Only when the block decision (if any) wasn't the outcome —
            # a blocked stop means no receipt (or an invalid one) exists to save yet;
            # the resumed turn's own SubagentStop event will persist the real one.
            if (receipt_outcome is not None and not receipt_outcome.block
                    and receipt_outcome.receipt is not None and node_id):
                try:
                    receipts.write_receipt(run_dir, node_id, receipt_outcome.receipt)
                except OSError:
                    pass

                # M5 Vault, shadow mode: checked from the same freshly-written
                # receipt, only when it was actually valid (never blocked).
                vault_cfg = (ctx.cfg.get("modules") or {}).get("vault") or {}
                if vault_cfg.get("mode") == "shadow" and repo_root is not None:
                    try:
                        vault.process_completed_receipt(
                            ctx.conn, ctx.plugin_data_dir, repo_root=repo_root, run_dir=run_dir,
                            run_id=run_id, node_id=node_id, agent_type=agent_type,
                            model_tier=ledger.tier_from_model(ledger_row.get("model")),
                            receipt=receipt_outcome.receipt,
                        )
                    except Exception:
                        ctx.record_error()

            if transcript_path and node_id:
                receipts.spool_transcript(run_dir, node_id, transcript_path)

            # v2 M13b Throttle: narration accounting, independent of receipt
            # validity — even a blocked/malformed receipt still has a transcript
            # worth measuring prose-vs-artifact output from.
            throttle_cfg = (ctx.cfg.get("modules") or {}).get("throttle") or {}
            if throttle_cfg.get("enabled") and transcript_path:
                try:
                    stats = throttle.narration_ratio(transcript_path)
                    if stats is not None:
                        store.record_narration(
                            ctx.conn, run_id=run_id, agent_id=agent_id, agent_type=agent_type,
                            node=node_id, prose_chars=stats["prose_chars"],
                            artifact_chars=stats["artifact_chars"], ratio=stats["ratio"],
                        )
                except Exception:
                    ctx.record_error()

            # v2 M14 Attribution: mark referenced any unit this node was
            # exposed to (via its dossier) and actually touched — see
            # meter/attribution.py for the detection method.
            attribution_cfg = (ctx.cfg.get("modules") or {}).get("attribution") or {}
            if attribution_cfg.get("enabled") and transcript_path and node_id:
                try:
                    attribution.record_references(
                        ctx.conn, run_id=run_id, node=node_id, transcript_path=transcript_path)
                except Exception:
                    ctx.record_error()


def handle_tool_pre_agent(payload: dict, ctx: Context) -> dict:
    _maybe_capture(ctx, "tool_pre_agent", payload)
    session_id = payload.get("session_id") or payload.get("sessionId") or ""
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    prompt_text = str(tool_input.get("prompt") or tool_input.get("description") or "")
    if not prompt_text:
        prompt_text = json.dumps(tool_input, default=str)

    marker = ledger.extract_dag_node(prompt_text)
    if not marker or not session_id:
        return {}  # never a decision — M0's baseline behaviour for an unattributed spawn

    run_id, node_id = marker
    ctx.correlator.push(session_id, run_id, node_id, prompt_text)
    _maybe_build_dossier(payload, ctx, run_id=run_id, node_id=node_id, tool_input=tool_input)

    sieve_decision = _maybe_run_sieve(payload, ctx, run_id=run_id, node_id=node_id,
                                       tool_input=tool_input, prompt_text=prompt_text)
    if sieve_decision is not None:
        return sieve_decision
    return {}


def _maybe_build_dossier(payload: dict, ctx: Context, *, run_id: str, node_id: str,
                          tool_input: dict) -> None:
    """Builds the dossier now, "before a pass dispatches" (meter-handoff.md
    Section 4/M2) — this PreToolUse-on-Agent handler is the earliest point the
    daemon knows both the run/node (from the DAG-NODE marker) and the target
    agent type, and it fires before the subagent that will consume the
    dossier even starts. Runs synchronously: PreToolUse's 10s timeout
    (hooks/hooks.json) comfortably covers a bounded, capped index build, and a
    dossier that arrives after SubagentStart missed its only delivery window
    (handle_subagent_start only checks for a file that already exists)."""
    dossier_cfg = (ctx.cfg.get("modules") or {}).get("dossier") or {}
    if not dossier_cfg.get("enabled"):
        return

    # `subagent_type` is the field name in this repo's own Agent tool schema;
    # whether PreToolUse's tool_input payload uses the same key is unverified
    # (see scripts/dag-doctor.py) — apply_to gating simply doesn't fire if not.
    subagent_type = tool_input.get("subagent_type") or tool_input.get("subagentType")
    apply_to = dossier_cfg.get("apply_to") or []
    if subagent_type and apply_to and subagent_type not in apply_to:
        return

    cwd = payload.get("cwd")
    repo_root, run_dir = _repo_root_and_run_dir(cwd, run_id)
    if repo_root is None or run_dir is None:
        return

    intern_cfg = (ctx.cfg.get("modules") or {}).get("intern") or {}
    pull_cfg = (ctx.cfg.get("modules") or {}).get("pull") or {}
    # v2 M10 Pull: a per-agent-type override (written by measurement, once
    # that measurement exists — see meter/pull.py) beats the global default.
    pull_enabled = bool(
        (pull_cfg.get("per_agent_override") or {}).get(subagent_type, pull_cfg.get("enabled")))
    try:
        result = dossier_mod.build(repo_root=repo_root, run_dir=run_dir, node_id=node_id,
                                    max_tokens=int(dossier_cfg.get("max_tokens", 4000)),
                                    conn=ctx.conn, intern_cfg=intern_cfg,
                                    pull_cfg={"enabled": pull_enabled})
    except Exception:
        ctx.record_error()
        return

    attribution_cfg = (ctx.cfg.get("modules") or {}).get("attribution") or {}
    if attribution_cfg.get("enabled") and result.get("exposures"):
        try:
            attribution.record_exposures(
                ctx.conn, run_id=run_id, node=node_id,
                agent_type=str(subagent_type or "unknown"), exposures=result["exposures"])
        except Exception:
            ctx.record_error()

    if pull_enabled and result.get("pull_sections"):
        try:
            store.record_pull_sections(
                ctx.conn, run_id=run_id, node=node_id,
                agent_type=str(subagent_type or "unknown"), sections=result["pull_sections"])
        except Exception:
            ctx.record_error()


_SIEVE_WORKTREE_RE = re.compile(r"^WORKTREE:\s*(\S+)", re.MULTILINE)
_SIEVE_COMMIT_RE = re.compile(r"^COMMIT:\s*(\S+)", re.MULTILINE)


def _maybe_run_sieve(payload: dict, ctx: Context, *, run_id: str, node_id: str, tool_input: dict,
                      prompt_text: str) -> dict | None:
    """v2 M11 Sieve: for a `task-reviewer` spawn only, runs the deterministic
    checker registry and appends a declaration to its prompt via
    `updatedInput`. Returns None (never touch the spawn) unless every
    required piece parses cleanly — this is the single highest-blast-radius
    rewrite in this plugin (a wrong `updatedInput` here would corrupt every
    review in the pipeline), so it is deliberately conservative: any
    uncertainty at all is a reason to return None, not a reason to guess."""
    sieve_cfg = (ctx.cfg.get("modules") or {}).get("sieve") or {}
    if not sieve_cfg.get("enabled"):
        return None

    subagent_type = tool_input.get("subagent_type") or tool_input.get("subagentType")
    if subagent_type != "task-reviewer":
        return None
    if not node_id.endswith("-reviewer"):
        return None  # the DAG-NODE convention (skills/orchestrator/SKILL.md) for this spawn kind

    worktree_m = _SIEVE_WORKTREE_RE.search(prompt_text)
    commit_m = _SIEVE_COMMIT_RE.search(prompt_text)
    if not worktree_m or not commit_m:
        return None  # the orchestrator didn't restate WORKTREE:/COMMIT: as literal lines

    worktree = worktree_m.group(1)
    commit = commit_m.group(1)
    if commit == "none" or not Path(worktree).is_dir():
        return None

    cwd = payload.get("cwd")
    repo_root = config_mod.find_repo_root(Path(cwd)) if cwd else None
    if repo_root is None:
        return None

    task_id = node_id[:-len("-reviewer")]
    run_dir = repo_root / ".dag" / "runs" / run_id
    task = dossier_mod.load_task(run_dir, task_id)
    if task is None:
        return None  # can't confirm `owns` — the ownership checker would be a guess
    owns = [p for p in (task.get("owns") or []) if isinstance(p, str)]

    try:
        results = sieve.run_registry(repo_root=repo_root, worktree=worktree, commit=commit,
                                      owns=owns, enabled=list(sieve_cfg.get("checkers") or []),
                                      integration_branch=f"dag/{run_id}")
    except Exception:
        ctx.record_error()
        return None
    if not results:
        return None

    original_prompt = tool_input.get("prompt")
    if not isinstance(original_prompt, str) or not original_prompt:
        return None  # nothing to safely append to

    declaration = sieve.render_declaration(results)
    updated_input = {**tool_input, "prompt": original_prompt + "\n\n" + declaration}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                    "updatedInput": updated_input}}


def handle_tool_pre_dossier(payload: dict, ctx: Context) -> dict:
    """M2 Dossier's enforcement half (deny Glob/Grep until the dossier is
    read), M3a Clamp's bounded reads (rewrite/deny a Read against the
    index's known-relevant spans), and v2 M15 Oracle's Grep cache lookup —
    all three live here because all key off Read/Grep/Glob events on the
    same PreToolUse matcher (see the "known hazard" note in hooks.json).
    Requires an `agent_id` on the payload to know which agent is asking — an
    unverified field for a subagent's own tool calls (see
    scripts/dag-doctor.py); absent that, this never denies or rewrites
    anything, which is the correct fail-open direction."""
    _maybe_capture(ctx, "tool_pre_dossier", payload)

    dossier_cfg = (ctx.cfg.get("modules") or {}).get("dossier") or {}
    clamp_cfg = (ctx.cfg.get("modules") or {}).get("clamp") or {}
    oracle_cfg = (ctx.cfg.get("modules") or {}).get("oracle") or {}

    agent_id = payload.get("agent_id") or payload.get("agentId")
    if not agent_id:
        return {}

    tool_name = payload.get("tool_name") or payload.get("toolName") or ""
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    # Oracle's cache applies to any Grep call, regardless of whether this
    # agent has a dossier at all — checked first, and independently of the
    # dossier_state lookup below, so an agent with no dossier still benefits.
    if oracle_cfg.get("enabled") and tool_name == "Grep":
        query_text = str(tool_input.get("pattern") or tool_input.get("query") or "")
        if query_text:
            oracle_hit = _decide_oracle_lookup(payload, ctx, query_text=query_text)
            if oracle_hit is not None:
                return oracle_hit

    state = None
    if dossier_cfg.get("enabled") or clamp_cfg.get("enabled"):
        try:
            state = store.get_dossier_state(ctx.conn, agent_id)
        except Exception:
            ctx.record_error()
            return {}
    if state is None or not state.get("dossier_path"):
        return {}  # no dossier was built/delivered for this agent; nothing to enforce or bound

    if tool_name == "Read":
        file_path = tool_input.get("file_path") or tool_input.get("path") or ""

        if dossier_cfg.get("enabled"):
            try:
                if file_path and Path(file_path).resolve() == Path(state["dossier_path"]).resolve():
                    store.mark_dossier_read(ctx.conn, agent_id)
                    return {}  # reading the dossier itself is never a candidate for 3a rewriting
            except (OSError, ValueError):
                pass

        # v2 M10 Pull (disabled by default): a Read of one of this node's
        # split section files is a pull event, tracked regardless of
        # dossier_cfg — the manifest delivery already happened via
        # additionalContext, independent of M2 Dossier's own enabled flag.
        pull_cfg = (ctx.cfg.get("modules") or {}).get("pull") or {}
        if pull_cfg.get("enabled") and file_path and state.get("run_id") and state.get("node"):
            try:
                store.mark_pull_section_fetched_by_path(
                    ctx.conn, run_id=state["run_id"], node=state["node"],
                    path=str(Path(file_path).resolve()))
            except Exception:
                ctx.record_error()

        if clamp_cfg.get("enabled") and file_path:
            return _decide_clamp_read(payload, ctx, state=state, file_path=file_path,
                                       tool_input=tool_input, clamp_cfg=clamp_cfg)
        return {}

    if dossier_cfg.get("enabled") and tool_name in ("Grep", "Glob"):
        if state.get("read") or state.get("denials", 0) >= int(dossier_cfg.get("max_denials", 2)):
            return {}
        try:
            store.increment_dossier_denial(ctx.conn, agent_id)
        except Exception:
            ctx.record_error()
            return {}
        reason = (f"meter: a precomputed context pack for this task already exists at "
                  f"{state['dossier_path']}. It lists the owned files, indexed symbols, "
                  f"relevant contract text, and sibling task receipts. Read it before searching.")
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                        "permissionDecision": "deny",
                                        "permissionDecisionReason": reason}}
    return {}


def _decide_oracle_lookup(payload: dict, ctx: Context, *, query_text: str) -> dict | None:
    """Returns a deny-with-cached-answer hook response on a cache hit, or
    None (never `{}`, so the caller can tell "no hit" from "handled") when
    there's nothing cached or the prerequisites (repo, run) aren't resolvable."""
    agent_id = payload.get("agent_id") or payload.get("agentId")
    cwd = payload.get("cwd")
    repo_root = config_mod.find_repo_root(Path(cwd)) if cwd else None
    if repo_root is None:
        return None

    try:
        existing = store.get_row_by_agent_id(ctx.conn, agent_id)
    except Exception:
        ctx.record_error()
        return None
    if existing is None or not existing.get("run_id") or existing["run_id"] == "_unattributed":
        return None

    try:
        hit = oracle.lookup(ctx.conn, repo_root, run_id=existing["run_id"], query_text=query_text)
    except Exception:
        ctx.record_error()
        return None
    if hit is None:
        return None

    reason = (f"meter: this is semantically the same search as one already run earlier in this "
              f"run, and the files it matched haven't changed since. Prior result:\n\n"
              f"{hit['answer']}")
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                    "permissionDecisionReason": reason}}


def _decide_clamp_read(payload: dict, ctx: Context, *, state: dict, file_path: str,
                        tool_input: dict, clamp_cfg: dict) -> dict:
    cwd = payload.get("cwd")
    repo_root = config_mod.find_repo_root(Path(cwd)) if cwd else None
    if repo_root is None or not state.get("run_id") or not state.get("node"):
        return {}

    # The "known hazard" mitigation (meter-handoff.md Section 6.2): a
    # conflicting PreToolUse-on-Read hook from user/project settings, if
    # detected at SessionStart (scripts/meter-boot.py), disables 3a's
    # rewriting for the rest of this repo's session rather than risk a
    # last-writer-wins race against `updatedInput`.
    if config_mod.read_conflict_marker_path(ctx.plugin_data_dir, repo_root).exists():
        return {}

    try:
        rel_path = str(Path(file_path).resolve().relative_to(repo_root.resolve()))
    except (OSError, ValueError):
        return {}  # outside the repo, or unresolvable — nothing 3a has an opinion on

    agent_id = payload.get("agent_id") or payload.get("agentId")
    try:
        run_dir = repo_root / ".dag" / "runs" / state["run_id"]
        decision = clamp.decide_read(
            conn=ctx.conn, repo_root=repo_root, run_dir=run_dir, run_id=state["run_id"],
            node_id=state["node"], agent_id=agent_id, rel_path=rel_path,
            requested_offset=tool_input.get("offset"), requested_limit=tool_input.get("limit"),
            full_read_threshold=int(clamp_cfg.get("full_read_threshold", 400)),
        )
    except Exception:
        ctx.record_error()
        return {}

    if decision is None:
        return {}
    if decision["kind"] == "deny":
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                        "permissionDecision": "deny",
                                        "permissionDecisionReason": decision["reason"]}}
    # "rewrite": updatedInput replaces the WHOLE input object (meter-handoff.md
    # Section 6.2) — re-emit file_path plus the narrowed offset/limit.
    updated_input = {**tool_input, "offset": decision["offset"], "limit": decision["limit"]}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                    "permissionDecision": "allow",
                                    "permissionDecisionReason": decision["reason"],
                                    "updatedInput": updated_input}}


def _extract_tool_output_text(payload: dict) -> str:
    """PostToolUse's response field name and shape are unverified (see
    scripts/dag-doctor.py) — tries the plausible spots rather than assuming
    one, exactly like ledger.py's `_find_usage_block`."""
    response = payload.get("tool_response")
    if response is None:
        response = payload.get("toolResponse") or payload.get("tool_output")
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        for key in ("output", "stdout", "content", "text"):
            if isinstance(response.get(key), str):
                stderr = response.get("stderr")
                text = response[key]
                if isinstance(stderr, str) and stderr:
                    text = text + "\n" + stderr
                return text
        return json.dumps(response, default=str)
    return ""


def _best_effort_run_dir(cwd: str | None) -> Path | None:
    """Clamp's spooled-output pointer needs *a* run directory, but PostToolUse
    fires inside a subagent's own tool calls, which carry that subagent's own
    session_id — not the parent session_id the Correlator's DAG-NODE queue is
    keyed on (see Correlator's docstring). There is currently no verified way
    to map a subagent's own tool-call events back to its run_id. Falling back
    to "the most recently active run in this repo" (the same heuristic
    meter-hook.py's SessionEnd handler already uses for main-thread
    attribution) gets the artifact into *a* real run directory rather than
    nowhere; it can misattribute across two runs racing in the same repo, but
    the compressed output is unaffected either way — only the pointer's
    filing location is approximate."""
    if not cwd:
        return None
    repo_root = config_mod.find_repo_root(Path(cwd))
    if repo_root is None:
        return None
    latest = ledger.find_latest_run(repo_root)
    return latest[1] if latest else None


def _resolve_run_id_for_oracle(payload: dict, conn, repo_root: Path) -> str | None:
    """Prefers an exact run_id via agent_id (when the payload happens to
    carry one — unverified for a subagent's own tool calls, see
    scripts/dag-doctor.py) over the "most recently active run" heuristic
    _best_effort_run_dir already uses for Clamp 3b's spool path. A wrong
    run_id here only means Oracle's cache lives under the wrong run (a
    within-run-scoped cache that then never gets read back, i.e. a silent
    miss) — never a wrong answer served, so the weaker fallback is safe."""
    agent_id = payload.get("agent_id") or payload.get("agentId")
    if agent_id:
        try:
            existing = store.get_row_by_agent_id(conn, agent_id)
        except Exception:
            existing = None
        if existing and existing.get("run_id") and existing["run_id"] != "_unattributed":
            return existing["run_id"]
    latest = ledger.find_latest_run(repo_root)
    return latest[0] if latest else None


def handle_tool_post(payload: dict, ctx: Context) -> dict:
    _maybe_capture(ctx, "tool_post", payload)

    tool_name = payload.get("tool_name") or payload.get("toolName") or ""
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    tool_use_id = payload.get("tool_use_id") or payload.get("toolUseId")
    cwd = payload.get("cwd")
    output = _extract_tool_output_text(payload)

    oracle_cfg = (ctx.cfg.get("modules") or {}).get("oracle") or {}
    if oracle_cfg.get("enabled") and cwd:
        repo_root = config_mod.find_repo_root(Path(cwd))
        if repo_root is not None:
            run_id = _resolve_run_id_for_oracle(payload, ctx.conn, repo_root)
            if run_id:
                try:
                    if tool_name == "Grep":
                        query_text = str(tool_input.get("pattern") or tool_input.get("query") or "")
                        if query_text and output:
                            oracle.record(ctx.conn, repo_root, run_id=run_id,
                                          query_text=query_text, answer=output,
                                          tool_name="Grep",
                                          max_entries=int(oracle_cfg.get("max_entries", 5000)))
                    elif tool_name in ("Write", "Edit"):
                        file_path = tool_input.get("file_path") or tool_input.get("path")
                        if file_path:
                            rel_path = str(Path(file_path).resolve().relative_to(repo_root.resolve()))
                            oracle.invalidate_for_write(ctx.conn, run_id=run_id, rel_path=rel_path)
                except Exception:
                    ctx.record_error()

    clamp_cfg = (ctx.cfg.get("modules") or {}).get("clamp") or {}
    if not clamp_cfg.get("enabled"):
        return {}

    try:
        replacement = clamp.decide(
            tool_name=str(tool_name), tool_input=tool_input,
            output=output, byte_threshold=int(clamp_cfg.get("output_threshold_bytes", 8000)),
            run_dir=_best_effort_run_dir(cwd), tool_use_id=str(tool_use_id) if tool_use_id else None,
        )
    except Exception:
        ctx.record_error()
        replacement = None

    if replacement is None:
        return {}
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": replacement}}


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


_GOVERNOR_WARN_RATIO = 2.0  # meter-handoff.md Section 4/M6b: "one warning at 2x prediction"


def handle_tool_pre_governor(payload: dict, ctx: Context) -> dict:
    """M6 Governor 6b: runaway containment via the tool-call-count proxy (see
    meter/governor.py's module docstring for why this isn't the spec's
    token-based signal). Registered with NO matcher in hooks.json — it must
    see every tool call to count them, which is a materially bigger surface
    than any other PreToolUse hook in this plugin and is called out as such
    in scripts/dag-doctor.py.

    Fails open at every step: no `agent_id` (main-thread call, or an
    unconfirmed payload shape), no ledger row for it yet, no repo, or no
    positive prediction all return `{}` — containment simply doesn't fire
    rather than guessing."""
    _maybe_capture(ctx, "tool_pre_governor", payload)

    governor_cfg = (ctx.cfg.get("modules") or {}).get("governor") or {}
    if not governor_cfg.get("enabled"):
        return {}

    agent_id = payload.get("agent_id") or payload.get("agentId")
    if not agent_id:
        return {}

    try:
        existing = store.get_row_by_agent_id(ctx.conn, agent_id)
    except Exception:
        ctx.record_error()
        return {}
    if existing is None or not existing.get("run_id") or existing["run_id"] == "_unattributed":
        return {}

    cwd = payload.get("cwd")
    repo_root = config_mod.find_repo_root(Path(cwd)) if cwd else None
    if repo_root is None:
        return {}

    run_id, node_id, agent_type = existing["run_id"], existing.get("node"), existing["agent_type"]
    run_dir = repo_root / ".dag" / "runs" / run_id
    run_json = _read_run_json(run_dir)
    mode = (run_json or {}).get("mode", "unknown")

    try:
        count = store.increment_tool_call_count(ctx.conn, agent_id=agent_id, run_id=run_id,
                                                  node=node_id)
        prediction = governor.predict_call_budget(
            ctx.plugin_data_dir, config_mod.repo_fingerprint(repo_root), mode, agent_type)
    except Exception:
        ctx.record_error()
        return {}

    if prediction <= 0:
        return {}
    ratio = count / prediction
    deny_multiplier = float(governor_cfg.get("runaway_multiplier", 3.0))

    if ratio >= deny_multiplier:
        reason = (f"meter: this task has made {count} tool calls, {ratio:.1f}x its predicted "
                  f"~{prediction:.0f} for {agent_type} (measured from past runs, or a default "
                  f"if none exist yet). Wrap up now: emit your receipt/trailer even if partial — "
                  f"a partial result beats an unbounded one.")
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                        "permissionDecision": "deny",
                                        "permissionDecisionReason": reason}}

    if ratio >= _GOVERNOR_WARN_RATIO:
        try:
            already_warned = store.was_call_warned(ctx.conn, agent_id)
        except Exception:
            already_warned = True  # fail open toward silence, not repeated noise
        if not already_warned:
            try:
                store.mark_call_warned(ctx.conn, agent_id)
            except Exception:
                pass
            # The spec (Section 4/M6b) asks for this note via PostToolUse's
            # additionalContext, which isn't a documented PostToolUse capability
            # (Section 6.2 only shows updatedToolOutput there) — delivered here,
            # on the allowed PreToolUse decision, as the closest verified
            # mechanism. Another unverified wire-format choice; see dag-doctor.py.
            reason = (f"meter: this task has made {count} tool calls, {ratio:.1f}x its predicted "
                      f"~{prediction:.0f} for {agent_type}. Consider wrapping up soon.")
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                            "permissionDecision": "allow",
                                            "permissionDecisionReason": reason}}

    return {}


def handle_tool_pre_throttle(payload: dict, ctx: Context) -> dict:
    """v2 M13a: deny a `Write` to an existing, git-tracked file with a reason
    naming `Edit`. See meter/throttle.py for the four exceptions. Fails open
    without `cwd` or a resolvable repo — never guesses at git-tracked status."""
    _maybe_capture(ctx, "tool_pre_throttle", payload)

    throttle_cfg = (ctx.cfg.get("modules") or {}).get("throttle") or {}
    if not throttle_cfg.get("deny_write_existing"):
        return {}

    agent_id = payload.get("agent_id") or payload.get("agentId")
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    if not agent_id or not isinstance(tool_input, dict):
        return {}

    file_path = tool_input.get("file_path") or tool_input.get("path")
    new_content = tool_input.get("content")
    if not file_path or new_content is None:
        return {}

    cwd = payload.get("cwd")
    repo_root = config_mod.find_repo_root(Path(cwd)) if cwd else None
    if repo_root is None:
        return {}

    try:
        rel_path = str(Path(file_path).resolve().relative_to(repo_root.resolve()))
    except (OSError, ValueError):
        return {}

    is_tracked = _git_is_tracked(repo_root, rel_path)

    try:
        decision = throttle.decide_write(
            conn=ctx.conn, repo_root=repo_root, agent_id=agent_id, rel_path=rel_path,
            new_content=str(new_content), is_tracked=is_tracked,
            rewrite_ratio=float(throttle_cfg.get("rewrite_ratio", 0.6)),
            max_denials=int(throttle_cfg.get("max_denials_per_path", 2)),
        )
    except Exception:
        ctx.record_error()
        return {}

    if decision is None:
        return {}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                    "permissionDecisionReason": decision["reason"]}}


_TRACKED_CACHE: dict[Path, tuple[float, set[str]]] = {}
_TRACKED_CACHE_LOCK = threading.Lock()


def _git_index_mtime(repo_root: Path) -> float | None:
    try:
        return (repo_root / ".git" / "index").stat().st_mtime
    except OSError:
        return None


def _git_tracked_set(repo_root: Path) -> set[str] | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _git_is_tracked(repo_root: Path, rel_path: str) -> bool:
    """Cached per `repo_root`, invalidated by `.git/index`'s mtime (bumped by
    every `git add`/`commit`/`rm`) — avoids a `git ls-files --error-unmatch`
    subprocess spawn on every single `Write` call via Throttle's `PreToolUse`
    handler, which risked the daemon's own 50ms soft-deadline design across a
    hundred-agent run. Any failure to read the index or list files falls
    through to the same fail-safe direction this always had: report `False`
    ("not tracked"), which only ever widens Throttle's new-file exception —
    it never wrongly denies a write, and a caching bug can't change that."""
    mtime = _git_index_mtime(repo_root)
    if mtime is not None:
        with _TRACKED_CACHE_LOCK:
            cached = _TRACKED_CACHE.get(repo_root)
            if cached is not None and cached[0] == mtime:
                return rel_path in cached[1]

    tracked = _git_tracked_set(repo_root)
    if tracked is None:
        return False

    if mtime is not None:
        with _TRACKED_CACHE_LOCK:
            _TRACKED_CACHE[repo_root] = (mtime, tracked)

    return rel_path in tracked


ROUTES: dict[str, Handler] = {
    "/health": handle_health,
    "/subagent/start": handle_subagent_start,
    "/subagent/stop": handle_subagent_stop,
    "/tool/pre-agent": handle_tool_pre_agent,
    "/tool/pre-dossier": handle_tool_pre_dossier,
    "/tool/pre-governor": handle_tool_pre_governor,
    "/tool/pre-throttle": handle_tool_pre_throttle,
    "/tool/post": handle_tool_post,
    "/shutdown": handle_shutdown,
}
