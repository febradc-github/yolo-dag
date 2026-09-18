"""M8 Quorum: adaptive review depth (meter-v2-handoff.md Section 3, M8).

**Class: statistical** — the only v2 module that trades quality, and the
spec's own hardest-gated one: adaptive quorum may ship for a domain only
after 100 nodes at full quorum measure that domain's critics-2/3
unique-finding rate below `quorum.unique_finding_threshold`. **Default:
DISABLED** (`meter.modules.quorum.enabled = false`) — same reasoning already
applied to M10 Pull: that measurement campaign needs real runs against this
implementation, which don't exist yet. Building the mechanism now and
leaving it off is deliberate, not a shortcut.

**Target in this codebase: Phase 2's spec-review loop.** "Three critics per
round... the most expensive phase in the pipeline" is Phase 2's 3
`spec-reviewer` instances per specialist per round
(`skills/orchestrator/SKILL.md`) — Phase 5's `task-reviewer` is already a
single reviewer, quorum 1, nothing to narrow. "Domain" is a Phase 1
specialist name (architecture, security, design, ...).

**Adaptation: no severity taxonomy exists here.** The spec's predicate
checks whether critic 1 raised a finding "at or above severity
`quorum.escalate_severity`." This repo's `spec-reviewer` doesn't report
per-finding severities — its contract is a bare `REVIEW: CLEAN` /
`REVIEW: FINDINGS <n>` count (`agents/spec-reviewer.md`). The adapted
predicate substitutes "critic 1 reported any findings at all" for the
severity check, which is the closest honest proxy available; it is a
materially different (stricter) signal than the spec's own severity
threshold, disclosed here rather than silently substituted.

**Fully implemented:** the contention predicate (deterministic, no model
call decides escalation — the spec's own hard requirement), the recall-guard
sampling, and the per-domain table. **Not implemented, deliberately:** any
path that actually recommends quorum 1 for a domain without 50+ full-quorum
samples showing a materially-zero unique-finding rate — `domain_quorum`
structurally cannot clear that bar without real runs this repo hasn't had.
"""

from __future__ import annotations

import random as _random
import sqlite3
from typing import Any

from . import store

DEFAULT_MIN_SAMPLES = 50


def contention_predicate(*, critic1_found_anything: bool, sieve_disagrees: bool = False,
                          domain_split_rate: float = 0.0, domain_threshold: float = 0.3,
                          large_or_novel: bool = False, is_rework: bool = False,
                          sampled: bool = False) -> bool:
    """True means: escalate to critics 2 and 3. Every input here is
    computable before critics 2/3 would spawn — no model call decides this,
    per the spec's own hard requirement. `critic1_found_anything` is the
    adapted substitute for a severity check (see module docstring)."""
    if is_rework or sampled or sieve_disagrees or large_or_novel:
        return True
    if domain_split_rate > domain_threshold:
        return True
    if critic1_found_anything:
        return True
    return False


def should_sample(sample_rate: float, rng: Any = None) -> bool:
    """The recall guard's dice roll, isolated into its own function so
    callers (and tests) can inject a deterministic `rng`."""
    r = rng or _random
    return r.random() < sample_rate


def record_full_quorum_sample(conn: sqlite3.Connection, *, domain: str,
                               unique_finding: bool) -> None:
    """Call this every time the recall guard (or an escalation) actually ran
    the full quorum, with whether critics 2/3 produced a finding critic 1
    didn't raise that the parent accepted as valid. This metric is, per the
    spec, "the entire justification for the module.\""""
    try:
        store.record_quorum_sample(conn, domain=domain, unique_finding=unique_finding)
    except Exception:
        pass


def domain_quorum(conn: sqlite3.Connection, *, domain: str,
                   unique_finding_threshold: float = 0.05,
                   min_samples: int = DEFAULT_MIN_SAMPLES) -> int:
    """Returns 3 (the always-safe default) until `domain` has at least
    `min_samples` full-quorum samples; only then can it return 1, and only
    when the measured unique-finding rate is below threshold."""
    try:
        stats = store.get_quorum_domain_stats(conn, domain)
    except Exception:
        return 3
    if stats is None or stats["full_quorum_samples"] < min_samples:
        return 3
    rate = stats["unique_finding_samples"] / stats["full_quorum_samples"]
    return 1 if rate < unique_finding_threshold else 3


def domain_quorum_table(conn: sqlite3.Connection, *, unique_finding_threshold: float = 0.05,
                         min_samples: int = DEFAULT_MIN_SAMPLES) -> dict[str, dict[str, Any]]:
    try:
        rows = store.get_all_quorum_domain_stats(conn)
    except Exception:
        return {}
    table: dict[str, dict[str, Any]] = {}
    for row in rows:
        total = row["full_quorum_samples"] or 0
        rate = (row["unique_finding_samples"] / total) if total else None
        quorum = 1 if (total >= min_samples and rate is not None
                       and rate < unique_finding_threshold) else 3
        table[row["domain"]] = {"samples": total, "unique_finding_rate": rate, "quorum": quorum}
    return table


def render_report_section(conn: sqlite3.Connection) -> str | None:
    table = domain_quorum_table(conn)
    if not table:
        return None
    lines = ["Quorum (v2 M8, disabled by default): per-domain measured unique-finding rate "
             f"for critics 2/3 (needs {DEFAULT_MIN_SAMPLES}+ full-quorum samples before any "
             "domain can narrow below quorum 3):"]
    for domain in sorted(table):
        info = table[domain]
        rate_str = (f"{info['unique_finding_rate'] * 100:.1f}%"
                    if info["unique_finding_rate"] is not None else "n/a")
        lines.append(f"  {domain:<24} {info['samples']:>4} sample(s)  rate {rate_str:>7}  "
                      f"-> quorum {info['quorum']}")
    return "\n".join(lines)
