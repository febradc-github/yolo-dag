"""M6 Governor (meter-handoff.md Section 4/M6).

**6a. Weight recalibration.** `skills/orchestrator/SKILL.md` weights each
spawn by model tier only (`inherit` 1.0, `sonnet` 0.5, `haiku` 0.1) against a
soft budget of 120/40/8 units per mode. That conflates cost with tier when
the real variance is per *agent type* — a `task-worker` and a
`spec-consolidator` on the same tier are not the same cost. `recalibrate_weights`
reads `baseline.json` (populated by every run's `meter.ledger.update_baseline`)
and writes `weights.json`: a measured coefficient per `(agent_type, mode)`,
scaled relative to that mode's own average agent_type cost — 1.0 means
"about average," 2.0 means "about twice the average spawn," and so on, the
same units the existing hardcoded tier weights already use. The orchestrator
reads this file if present and falls back to the hardcoded tier weights if
absent (see the `skills/orchestrator/SKILL.md` Budget section) — this module
only ever writes it, never reads or acts on it itself.

**6b. Runaway containment — NOT the spec's token-based signal.** The spec
asks for containment keyed on measured token spend at 2x/3x a prediction.
That is not implementable on this plugin's transcript-only accounting
(meter/ledger.py): tokens are summed once, from the full transcript, at
`SubagentStop` — by which point the node has already finished and there is
nothing left to contain. Real-time token tracking needs either OpenTelemetry
(Section 8.5's *optional* upgrade path, never a requirement) or re-parsing a
growing transcript on every tool call, which is exactly the hot-path cost
this daemon's whole HTTP-transport design (Section 5.2) exists to avoid.

Instead, `predict_call_budget`/the containment check in `meter/router.py`'s
`handle_tool_pre_governor` use **tool-call count** as the live proxy signal:
cheap to track (one counter increment per `PreToolUse`, no transcript
parsing), and cost-correlated in practice (more tool calls roughly tracks
more tokens for a given agent type). It is a genuinely different metric than
the spec names, not merely an implementation detail — a node making few but
very large tool calls (e.g. one giant `Read`) is undercounted; the tradeoff
is disclosed here and in `scripts/dag-doctor.py` rather than silently
substituted.

**6c. Model-routing report** is descriptive only: per-agent-type,
per-model-tier token cost from `baseline.json`. The spec also asks this to be
judged "by review-pass rates and rework counts" — that needs task-graph-level
data (`tasks.json` attempt counts, reviewer verdicts) this plugin's daemon
never parses, so this report states cost only and says plainly that the
quality half is not computed here, rather than inventing a routing verdict
from cost alone.

**6d. Cache-TTL recommendation** reads the same per-round idle-gap data M0
already collects (`ledger_rounds.idle_gap_ms` vs `cache_read`) and prints
Section 8.7's own advice when it applies. It never sets
`subagentPromptCacheTtl` itself.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import store

# The hardcoded tier weights in skills/orchestrator/SKILL.md's Budget section,
# duplicated here only so weights.json can state them as the documented
# fallback — never used as a computation input.
FALLBACK_TIER_WEIGHTS = {"inherit": 1.0, "sonnet": 0.5, "haiku": 0.1}

DEFAULT_CALL_BUDGET = 30  # 6b's fallback when no baseline call-count data exists yet
_CACHE_TTL_IDLE_THRESHOLD_MS = 5 * 60 * 1000  # Section 8.7: Anthropic's default 5-minute TTL


def _load_baseline(plugin_data_dir: Path) -> dict:
    try:
        return json.loads((plugin_data_dir / "baseline.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def recalibrate_weights(plugin_data_dir: Path) -> dict[str, dict[str, float]] | None:
    """Writes weights.json from baseline.json. Returns the computed weights,
    or None if there wasn't enough data to write anything (an absent or
    empty baseline — the orchestrator's fallback-to-hardcoded-weights path
    covers this exactly)."""
    baseline = _load_baseline(plugin_data_dir)
    if not baseline:
        return None

    weights: dict[str, dict[str, float]] = {}
    for repo_mode_key, entry in baseline.items():
        if ":" not in repo_mode_key:
            continue
        _, mode = repo_mode_key.rsplit(":", 1)
        by_agent_type = entry.get("by_agent_type", {})

        averages: dict[str, float] = {}
        for agent_type, agg in by_agent_type.items():
            samples = agg.get("samples", 0)
            if samples <= 0:
                continue
            averages[agent_type] = (agg.get("in_tok_total", 0) + agg.get("out_tok_total", 0)) / samples
        if not averages:
            continue

        overall_avg = sum(averages.values()) / len(averages)
        if overall_avg <= 0:
            continue

        mode_weights = weights.setdefault(mode, {})
        for agent_type, avg in averages.items():
            # Merge across repos sharing a mode by averaging with what's
            # already there, rather than letting the last repo processed win.
            coefficient = round(avg / overall_avg, 3)
            if agent_type in mode_weights:
                mode_weights[agent_type] = round((mode_weights[agent_type] + coefficient) / 2, 3)
            else:
                mode_weights[agent_type] = coefficient

    if not weights:
        return None

    out_path = plugin_data_dir / "weights.json"
    try:
        out_path.write_text(json.dumps({
            "schema_v": 1,
            "generated_at": int(time.time()),
            "fallback_tier_weights": FALLBACK_TIER_WEIGHTS,
            "weights": weights,
        }, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass
    return weights


def predict_call_budget(plugin_data_dir: Path, repo_fingerprint: str, mode: str,
                         agent_type: str) -> float:
    """6b's proxy prediction: the measured average tool-call count for this
    (repo, mode, agent_type) if there's at least one sample, else
    DEFAULT_CALL_BUDGET. Never returns None or raises — a containment check
    with no data to work from should fall back to a sane default, not skip
    containment silently forever."""
    baseline = _load_baseline(plugin_data_dir)
    entry = baseline.get(f"{repo_fingerprint}:{mode}", {})
    agg = entry.get("by_agent_type", {}).get(agent_type, {})
    samples = agg.get("call_samples", 0)
    if samples > 0:
        return agg.get("calls_total", 0) / samples
    return float(DEFAULT_CALL_BUDGET)


def model_routing_report(plugin_data_dir: Path) -> str | None:
    """6c: descriptive cost-by-agent-type-and-mode. See module docstring for
    why this doesn't attempt the spec's quality comparison."""
    baseline = _load_baseline(plugin_data_dir)
    if not baseline:
        return None

    lines = ["Model-routing report (cost only — see meter/governor.py for why review-pass "
             "rates and rework counts aren't included):"]
    for repo_mode_key in sorted(baseline):
        if ":" not in repo_mode_key:
            continue
        _, mode = repo_mode_key.rsplit(":", 1)
        by_agent_type = baseline[repo_mode_key].get("by_agent_type", {})
        for agent_type in sorted(by_agent_type):
            agg = by_agent_type[agent_type]
            samples = agg.get("samples", 0)
            if samples <= 0:
                continue
            avg_tokens = (agg.get("in_tok_total", 0) + agg.get("out_tok_total", 0)) / samples
            lines.append(f"  {mode:<6} {agent_type:<28} avg {avg_tokens:,.0f} tok/spawn "
                          f"({samples} sample(s))")
    return "\n".join(lines) if len(lines) > 1 else None


def cache_ttl_recommendation(ledger_rounds: list[dict]) -> str | None:
    """6d, per Section 8.7. Advisory only — never sets anything."""
    over_threshold = [r for r in ledger_rounds
                       if (r.get("idle_gap_ms") or 0) > _CACHE_TTL_IDLE_THRESHOLD_MS]
    if not over_threshold:
        return None
    return (f"{len(over_threshold)} resumed-specialist round(s) had an idle gap over 5 minutes "
            f"since their previous turn — under Claude Code's default prompt-cache TTL, their "
            f"accumulated history was likely rebilled at full input rate instead of the ~1/10th "
            f"cache-read rate. If this repeats across runs, consider "
            f"CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL=1h (needs Claude Code v2.1.242+); see "
            f"meter-handoff.md Section 8.7 for the break-even arithmetic before setting it.")


def report_section(plugin_data_dir: Path, run_id: str, conn: Any) -> str | None:
    """Assembled for /dag-cost's report.md."""
    parts = []
    routing = model_routing_report(plugin_data_dir)
    if routing:
        parts.append(routing)
    ttl = cache_ttl_recommendation(store.get_all_ledger_rounds_for_run(conn, run_id))
    if ttl:
        parts.append(ttl)
    weights_path = plugin_data_dir / "weights.json"
    if weights_path.exists():
        parts.append(f"Recalibrated weights available at {weights_path} — the orchestrator uses "
                      f"these instead of the hardcoded tier weights when present.")
    return "\n\n".join(parts) if parts else None
