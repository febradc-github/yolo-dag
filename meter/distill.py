"""M12 Distill: spec compilation (meter-v2-handoff.md Section 3, M12).

**Class: verified.** The fidelity gate (generate closed questions from the
full spec, answer them from the brief alone, reject on any miss) is what
makes this verified rather than a hopeful compression — see this module's
docstring note below on why the gate itself lives in the orchestrator, not
here.

**Architectural split, and why.** Compiling a brief needs an actual model
call ("one Haiku-tier pass"), and so does the fidelity gate (generating
probes, then answering them from the brief alone). This daemon is a
credential-less local HTTP server — it cannot invoke a model. So:

- **The orchestrator** (`skills/orchestrator/SKILL.md`) does the actual work:
  spawning `spec-distiller` in compile mode (produces the brief and the probe
  set from the full spec), spawning it again in verify mode (answers the
  probes from the brief alone, blind to the full spec), and grading the
  answers itself — no extra spawn needed for grading, since the orchestrator
  is already a capable reader.
- **This module** owns exactly what a credential-less daemon safely can:
  content-hashing the spec so an identical spec (same run resumed, or a
  second run with unchanged input) skips recompilation entirely, storing the
  resulting brief keyed by that hash, and tracking how often an agent still
  needed the full text (the fetch-rate signal the spec asks for).

**Scope: applied to `task-specialist`'s Phase 4 spawn only.** Checking
`skills/orchestrator/SKILL.md`, this repo's existing design does not
actually paste the merged spec into "every agent" the way the spec assumes —
`task-worker`/`task-reviewer` in Phase 5 only ever receive their own task's
slice (title, acceptance criteria, contract text), never the whole spec.
`task-specialist` is the one spawn that receives the full spec today, once
per run. The mechanism is built for real and applied there; its payoff in
this specific codebase is narrower than the spec's own framing assumes
(cross-run/cross-repo reuse of an identical spec, not "every agent in the
run" — there's only one full-spec consumer per run here), and that's stated
plainly rather than oversold.

**`compile_on_miss` defaults to False, and the arithmetic is why.** With one
consumer, a cache miss can never pay for itself. Compiling costs a
full-spec read (S), a verify read of the brief (~0.3S), and the brief plus a
25-probe answer key as *output* — which bills at roughly 5x input — then the
verify pass's answers again: call it 2.8S + ~15k tokens, before the
revise-and-re-verify round a failed gate adds. The saving is one agent
reading a brief instead of the spec: 0.7S. 2.8S + 15k < 0.7S has no
solution, so on a miss this module spends tokens to save fewer tokens, every
time, and it also puts 2-4 serial foreground spawns on the critical path.

A cache *hit* is genuinely free — a local hash lookup, no model call — so
`check` stays on the Phase 4 path unconditionally and the store keeps
working. Only the compile-on-miss branch is off. Turn it back on for a
deployment that grows a second full-spec consumer, or where the same spec
recurs often enough that priming the cache pays for itself; the fidelity gate
that makes a brief safe to use is unchanged either way, and nothing here
judges brief *quality* — the full spec is strictly the more faithful input,
so this default costs no fidelity at all.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

from . import store


def compile_on_miss(cfg: dict[str, Any]) -> bool:
    """Whether a cache miss should compile a fresh brief. Default False — see this
    module's docstring for the arithmetic. A hit is unaffected and always free."""
    modules = cfg.get("modules") or {}
    distill_cfg = modules.get("distill") or {}
    return bool(distill_cfg.get("compile_on_miss", False))


def compute_spec_hash(spec_text: str) -> str:
    return hashlib.sha256(spec_text.encode("utf-8", errors="replace")).hexdigest()


def get_cached_brief(conn: sqlite3.Connection, spec_text: str) -> dict[str, Any] | None:
    """Returns the cached entry (with a delivery recorded) on a hit, else
    None. Never raises."""
    try:
        entry = store.get_distill_brief(conn, compute_spec_hash(spec_text))
        if entry is None:
            return None
        store.bump_distill_delivery(conn, entry["spec_hash"])
        return entry
    except Exception:
        return None


def store_brief(conn: sqlite3.Connection, *, spec_text: str, brief: str,
                 source_path: str | None) -> None:
    try:
        store.store_distill_brief(conn, spec_hash=compute_spec_hash(spec_text), brief=brief,
                                   source_path=source_path)
    except Exception:
        pass


def record_full_spec_fetch(conn: sqlite3.Connection, spec_text: str) -> None:
    try:
        store.bump_distill_fetch(conn, compute_spec_hash(spec_text))
    except Exception:
        pass


def fetch_rate(conn: sqlite3.Connection, spec_text: str) -> float | None:
    """fetch_count / delivery_count for this spec's cache entry, or None if
    there's no entry or it's never been delivered (division by zero would
    otherwise silently look like a 0% fetch rate, which is a different and
    stronger claim than "no data yet")."""
    try:
        entry = store.get_distill_brief(conn, compute_spec_hash(spec_text))
    except Exception:
        return None
    if entry is None or not entry.get("delivery_count"):
        return None
    return entry["fetch_count"] / entry["delivery_count"]
