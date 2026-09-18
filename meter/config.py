"""Config resolution for meter.

Resolution order (meter-handoff.md Section 8.2), minus the `user_config` tier:
package defaults -> <repo>/.dag/meter.json -> DAG_METER_* environment variables.

The `user_config` tier (plugin.json `user_config`) is intentionally not implemented
yet: its exact schema and whether Claude Code exposes it to plugin processes is
unverified, and guessing wrong risks a malformed plugin.json breaking plugin install
entirely — a much worse failure than one config tier being a no-op. Once verified,
add it between the repo-config and env-var tiers to match the documented order.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

DEFAULT_PORT = 47615
DEFAULT_DEADLINE_MS = 50

# Full module shape per meter-handoff.md Section 8.2, extended by
# meter-v2-handoff.md Section 5 for M8-M15 (same `modules` namespace, per the
# v2 doc's own instruction). v1: `ledger` (M0), `receipts` (M1), `dossier`
# (M2), `clamp` (M3, both halves), `echo` (M4, measure-only — Task-prompt
# channel only, see meter/echo.py), `vault` (M5, shadow mode only — see
# meter/vault.py's module docstring for exactly what "shadow mode only"
# excludes), and `governor` (M6 — 6a/6c/6d as specified, 6b via a tool-call-
# count proxy instead of the spec's token-based signal; see
# meter/governor.py's module docstring) have real implementations; `codemod`
# is still a placeholder. v2: `throttle` (M13, 13a/13b — 13c stays closed per
# the spec's own instruction when its prerequisite can't be verified; see
# meter/throttle.py) and `oracle` (M15, Grep-only — see meter/oracle.py's
# module docstring for why Glob and Read aren't covered here), and `intern`
# (M9, dossier file paths only, default `basename` mode — see
# meter/intern.py's module docstring for why `codebook` mode isn't the
# default), `sieve` (M11 — `ownership`/`secrets` are real checkers,
# `types`/`lint` are best-effort against detected tooling, `tests`/`ast`
# always report `not run`; see meter/sieve.py's module docstring), and
# `distill` (M12 — the daemon only owns the hash-keyed brief cache;
# scripts/dag-distill.py and skills/orchestrator/SKILL.md's Phase 4 do the
# actual compile+fidelity-gate work, since only the orchestrator can make a
# model call; see meter/distill.py's module docstring for scope), and
# `attribution` (M14, measurement only — dossier sections are the unit
# granularity; see meter/attribution.py's module docstring), and `pull` (M10
# — implemented and tested, but DEFAULT DISABLED: the spec's own release
# plan gates it on a measured M14 reference rate that needs a full release
# of real run data to exist; see meter/pull.py's module docstring), and
# `quorum` (M8 — implemented and tested, DEFAULT DISABLED per the spec's own
# instruction; its gate needs 50+ full-quorum samples per domain that don't
# exist yet; see meter/quorum.py's module docstring, including the one
# adaptation this codebase's spec-reviewer contract requires) have real
# implementations. Every v2 module from the handoff doc (M8-M15) now has a
# real implementation except M7's v1 sibling Codemod, which the spec itself
# says ships last or not at all.
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "port": DEFAULT_PORT,
    "transport": "http",
    "deadline_ms": DEFAULT_DEADLINE_MS,
    "modules": {
        "ledger": {"enabled": True},
        "receipts": {"enabled": True, "repair_turns": 1},
        "dossier": {"enabled": True, "max_tokens": 4000, "max_denials": 2,
                    "apply_to": ["task-worker", "task-reviewer"]},
        "clamp": {"enabled": True, "full_read_threshold": 400, "output_threshold_bytes": 8000},
        "echo": {"enabled": True, "mode": "measure", "promote_threshold": 4},
        "vault": {"mode": "shadow", "warm_threshold": 0.85, "max_gb": 2},
        "governor": {"enabled": True, "mode": "advise", "runaway_multiplier": 3.0,
                     "write_weights": False},
        "codemod": {"enabled": False, "scope": "repo"},
        # Default mode is "basename", not the spec sample's "codebook" — see
        # meter/intern.py's module docstring for why: codebook mode needs an
        # A/B harness (which doesn't exist here) to confirm no comprehension
        # regression before it's safe to default on. "fallback" names what
        # `dag-doctor`/an operator should switch to if codebook mode is ever
        # tried and regresses; it is not read/applied automatically.
        "intern": {"enabled": True, "mode": "basename", "fallback": "basename", "min_refs": 3},
        "throttle": {"enabled": True, "deny_write_existing": True, "rewrite_ratio": 0.6,
                     "max_denials_per_path": 2, "effort_routing": False},
        "oracle": {"enabled": True, "scope": "run", "max_entries": 5000},
        "sieve": {"enabled": True, "checkers": ["types", "lint", "tests", "ast", "ownership",
                                                 "secrets"], "declare_only_passed": True},
        "distill": {"enabled": True, "probe_count": 25, "retry": 1, "fetch_rate_alarm": 0.2},
        "attribution": {"enabled": True, "prune_threshold": 0.15, "min_exposures": 50,
                        "prune": False},
        "pull": {"enabled": False, "promote_threshold": 0.8, "per_agent_override": {}},
        "quorum": {"enabled": False, "escalate_severity": "major", "sample_rate": 0.15,
                   "unique_finding_threshold": 0.05, "domain_table": {}},
    },
    # Diagnostics-only: when true, every hook payload the daemon receives is
    # appended verbatim to probe-capture.jsonl in the plugin data dir, for
    # dag-doctor.py to inspect. Off by default — payloads may contain repo text.
    "probe_capture": False,
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def find_repo_root(start: Path) -> Path | None:
    """Walk up from `start` looking for a `.git` directory or file (worktrees use a
    file). Returns None if nothing is found before the filesystem root."""
    cur = start.resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def plugin_data_dir() -> Path:
    """${CLAUDE_PLUGIN_DATA}/meter, or a local fallback if the env var is unset —
    e.g. when this code is invoked outside a Claude Code plugin context (manual
    testing, dag-doctor run by hand). The fallback is never used silently in a
    real session without being reported: callers should check `used_fallback`."""
    raw = os.environ.get("CLAUDE_PLUGIN_DATA")
    if raw:
        return Path(raw) / "meter"
    return Path.home() / ".cache" / "yolo-dag-meter-fallback" / "meter"


def load_config(repo_root: Path | None) -> dict[str, Any]:
    config = json.loads(json.dumps(DEFAULTS))  # cheap deep copy

    if repo_root is not None:
        repo_config_path = repo_root / ".dag" / "meter.json"
        if repo_config_path.exists():
            try:
                repo_config = json.loads(repo_config_path.read_text(encoding="utf-8"))
                if isinstance(repo_config, dict):
                    config = _deep_merge(config, repo_config)
            except (OSError, json.JSONDecodeError):
                pass  # fail-open: a malformed config file is ignored, not fatal

    if os.environ.get("DAG_METER_DISABLE") == "1":
        config["enabled"] = False
    port_env = os.environ.get("DAG_METER_PORT")
    if port_env and port_env.isdigit():
        config["port"] = int(port_env)
    if os.environ.get("DAG_METER_LOG"):
        config["log_level"] = os.environ["DAG_METER_LOG"]
    if os.environ.get("DAG_METER_PROBE_CAPTURE") == "1":
        config["probe_capture"] = True

    return config


def is_enabled(config: dict[str, Any]) -> bool:
    return bool(config.get("enabled", True))


def repo_fingerprint(repo_root: Path) -> str:
    """sha256(remote origin url + first commit sha), per meter-handoff.md Section
    5.4 — never the path alone, or worktrees (which share history but not a path)
    would fragment the cache. Falls back to the absolute path if git can't answer
    either question (no remote, shallow clone with no reachable root commit)."""
    import hashlib
    import subprocess

    def _git(*args: str) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(repo_root), *args],
                capture_output=True, text=True, timeout=5, check=False,
            )
            return result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    remote = _git("remote", "get-url", "origin")
    first_commit_lines = _git("rev-list", "--max-parents=0", "HEAD").splitlines()
    first_commit = first_commit_lines[0] if first_commit_lines else ""

    basis = (remote + first_commit) if (remote or first_commit) else str(repo_root.resolve())
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _matcher_covers_read(matcher: str) -> bool:
    if not matcher or not matcher.strip() or matcher.strip() == "*":
        return True  # no matcher, or a wildcard, fires on every tool including Read
    return bool(re.search(r"\bRead\b", matcher))


def detect_read_hook_conflict(repo_root: Path | None) -> str | None:
    """meter-handoff.md Section 6.2's "known hazard": `updatedInput` is
    reportedly ignored when multiple `PreToolUse` hooks match the same event,
    since matching hooks run in parallel and the last writer wins. This
    plugin registers exactly one `PreToolUse` hook matching `Read`
    (hooks/hooks.json) — this scans user and project Claude Code settings for
    another `PreToolUse` hook whose matcher would also fire on `Read`, which
    is the condition that trips the hazard. A false positive here (a matcher
    string this doesn't parse precisely) only means M3a's read-rewriting gets
    conservatively disabled for the session — see meter/router.py's
    `_decide_clamp_read`, which is the fail-open direction the mitigation is
    supposed to have."""
    candidates = [Path.home() / ".claude" / "settings.json"]
    if repo_root is not None:
        candidates += [repo_root / ".claude" / "settings.json",
                        repo_root / ".claude" / "settings.local.json"]

    for path in candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        for entry in (data.get("hooks", {}).get("PreToolUse") or []):
            if not isinstance(entry, dict):
                continue
            matcher = entry.get("matcher", "")
            if _matcher_covers_read(matcher):
                return f"{path}: PreToolUse matcher {matcher!r} also matches Read"
    return None


def read_conflict_marker_path(plugin_data_dir: Path, repo_root: Path) -> Path:
    return plugin_data_dir / f"read_conflict_{repo_fingerprint(repo_root)}"
