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
from pathlib import Path
from typing import Any

DEFAULT_PORT = 47615
DEFAULT_DEADLINE_MS = 50

# Full module shape per meter-handoff.md Section 8.2. Only `ledger` has a real
# implementation in M0; the rest are placeholders so the config format doesn't
# have to change shape when later milestones land.
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "port": DEFAULT_PORT,
    "transport": "http",
    "deadline_ms": DEFAULT_DEADLINE_MS,
    "modules": {
        "ledger": {"enabled": True},
        "receipts": {"enabled": False, "repair_turns": 1},
        "dossier": {"enabled": False, "max_tokens": 4000, "max_denials": 2,
                    "apply_to": ["task-worker", "task-reviewer"]},
        "clamp": {"enabled": False, "full_read_threshold": 400, "output_threshold_bytes": 8000},
        "echo": {"enabled": False, "mode": "measure", "promote_threshold": 4},
        "vault": {"mode": "off", "warm_threshold": 0.85, "max_gb": 2},
        "governor": {"enabled": False, "mode": "advise", "runaway_multiplier": 3.0,
                     "write_weights": False},
        "codemod": {"enabled": False, "scope": "repo"},
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
