#!/usr/bin/env python3
"""dag-doctor: capability matrix and VERIFY probes for meter (meter-handoff.md
Section 6.1). Run by hand: `python3 scripts/dag-doctor.py`.

Two different kinds of check live here, and the report keeps them visually
distinct on purpose:

- **Static checks** — python version, loopback bind, external binaries, port
  availability — this script can answer by itself, right now, with no live
  Claude Code session involved.
- **VERIFY items** — the exact tool name a subagent spawn shows up as, the
  field names in a real transcript's usage blocks, whether `agent_id`/
  `agent_type` actually appear on SubagentStart/SubagentStop payloads — this
  script cannot answer by itself. It can only report what a *previous* session
  observed, if probe-capture was turned on for one. Absent that, it says so
  plainly instead of guessing, per meter-handoff.md's Rule 2: "assumptions are
  not [cheap]."

Exit code: non-zero only if a **hard** prerequisite is missing (Python 3.11+,
a working loopback bind). Everything else here is informational — meter fails
open around a missing optional tool or an unconfirmed VERIFY item.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

try:
    from meter import config as config_mod
except Exception as exc:  # the meter package itself failing to import is news
    print(f"FAIL  could not import the meter package: {exc!r}")
    sys.exit(1)

PASS, WARN, INFO, FAIL, UNVERIFIED = "PASS", "WARN", "INFO", "FAIL", "UNVERIFIED"
_hard_failures: list[str] = []


def line(status: str, label: str, detail: str = "") -> None:
    marker = {"PASS": "  ok ", "WARN": " warn", "INFO": " info", "FAIL": " FAIL",
              "UNVERIFIED": " ????"}[status]
    print(f"[{marker}] {label}" + (f" — {detail}" if detail else ""))
    if status == FAIL:
        _hard_failures.append(label)


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


def check_runtime() -> None:
    section("Runtime")
    v = sys.version_info
    if v >= (3, 11):
        line(PASS, f"Python {v.major}.{v.minor}.{v.micro}", "meets the 3.11+ requirement")
    else:
        line(FAIL, f"Python {v.major}.{v.minor}.{v.micro}",
             "meter needs 3.11+; the daemon will refuse to start and the plugin "
             "runs unwrapped")
    line(INFO, "platform", sys.platform)


def check_environment() -> None:
    section("Environment")
    plugin_root_env = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root_env:
        line(PASS, "CLAUDE_PLUGIN_ROOT set", plugin_root_env)
    else:
        line(INFO, "CLAUDE_PLUGIN_ROOT not set in this shell",
             "expected when run by hand outside a Claude Code session; hooks.json "
             "paths are substituted by Claude Code itself regardless")

    plugin_data_env = os.environ.get("CLAUDE_PLUGIN_DATA")
    resolved = config_mod.plugin_data_dir()
    if plugin_data_env:
        line(PASS, "CLAUDE_PLUGIN_DATA set", plugin_data_env)
    else:
        line(WARN, "CLAUDE_PLUGIN_DATA not set",
             f"falling back to {resolved} — meter's persistent store (token, "
             f"vault.db, baseline.json) will not survive a plugin update or "
             f"uninstall the way the spec intends")

    if os.environ.get("DAG_METER_TOKEN"):
        line(PASS, "DAG_METER_TOKEN set in this shell",
             "HTTP hook header auth can work if this matches the daemon's persisted token")
    else:
        line(WARN, "DAG_METER_TOKEN not set in this shell",
             "HTTP hooks will authenticate as 'unauthenticated' — meter still "
             "records data (auth is advisory-only in M0), but see meter/daemon.py's "
             "module docstring for why this env var can't be set for you and how "
             "to set it yourself")


def _bind_loopback() -> tuple[bool, str]:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
        return True, ""
    except OSError as exc:
        return False, str(exc)


def _port_free(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def check_network(cfg: dict) -> None:
    section("Network")
    ok, detail = _bind_loopback()
    if ok:
        line(PASS, "loopback bind (127.0.0.1, ephemeral port)")
    else:
        line(FAIL, "loopback bind failed", detail)

    port = int(os.environ.get("DAG_METER_PORT", cfg.get("port", config_mod.DEFAULT_PORT)))
    plugin_data_dir = config_mod.plugin_data_dir()
    token_path = plugin_data_dir / "token"
    port_path = plugin_data_dir / "port"

    if token_path.exists() and port_path.exists():
        try:
            token = token_path.read_text(encoding="utf-8").strip()
            running_port = int(port_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            token, running_port = "", port
        healthy, body = _health_check(running_port, token)
        if healthy:
            line(PASS, f"daemon responding on 127.0.0.1:{running_port}", json.dumps(body))
            return
        line(WARN, f"daemon not responding on 127.0.0.1:{running_port}",
             "port/token files exist but /health failed — stale files from a "
             "prior boot, or the daemon crashed")

    if _port_free(port):
        line(INFO, f"port {port} is free", "no daemon currently listening (expected outside a session)")
    else:
        line(WARN, f"port {port} is in use by something else",
             "the daemon will fail to bind and meter fails open — set "
             "meter.port in .dag/meter.json or DAG_METER_PORT to move it")


def _health_check(port: int, token: str) -> tuple[bool, dict]:
    import urllib.error
    import urllib.request
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/health",
                                      headers={"X-Dag-Meter-Token": token})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            return True, json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError):
        return False, {}


def check_external_tools() -> None:
    section("External tools (all optional accelerators — never required)")
    for tool in ("git", "rg", "ctags", "tree-sitter"):
        path = shutil.which(tool)
        if path:
            line(PASS, tool, path)
        else:
            line(INFO, tool, "not on PATH — a slower stdlib fallback is used where this matters "
                              "(later milestones only; M0 doesn't use any of these)")


def _claude_code_version() -> str | None:
    for candidate in (shutil.which("claude"), shutil.which("cc")):
        if not candidate:
            continue
        try:
            result = subprocess.run([candidate, "--version"], capture_output=True,
                                     text=True, timeout=5, check=False)
            out = (result.stdout or result.stderr).strip()
            if out:
                return out
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def check_claude_code() -> None:
    section("Claude Code")
    version = _claude_code_version()
    if version:
        line(INFO, "version", version)
    else:
        line(WARN, "could not determine Claude Code version",
             "neither `claude` nor `cc` answered --version on PATH")

    for env_name in ("CLAUDE_CODE_PROMPT_CACHE_TTL", "CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL"):
        value = os.environ.get(env_name)
        line(INFO if value else INFO, env_name, value or "unset (defaults apply — see "
             "meter-handoff.md Section 8.7 for what that means on a subscription plan)")

    settings_candidates = [Path.home() / ".claude" / "settings.json",
                            Path.cwd() / ".claude" / "settings.json"]
    for path in settings_candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for key in ("promptCacheTtl", "subagentPromptCacheTtl"):
            if key in data:
                line(INFO, f"{path}: {key}", str(data[key]))


def check_verify_items() -> None:
    section("VERIFY items (meter-handoff.md Section 6.1 — cannot be answered without a live "
            "session; see probe-capture below)")
    items = [
        "Whether yolo-dag's agents are Task subagents or agent-team teammates",
        "The exact tool name a subagent spawn appears as in PreToolUse (this repo's own "
        "agents/*.md and scripts/validate.py use `Agent`, not `Task` — hooks.json's PreToolUse "
        "matcher below is set to `Agent` on that evidence, but it has not been confirmed against "
        "a live PreToolUse payload)",
        "Whether `agent_id`/`agent_type` appear on SubagentStart/SubagentStop payloads",
        "Whether SubagentStop payloads carry `agent_transcript_path` and `last_assistant_message`",
        "The exact usage field names inside a real transcript JSONL "
        "(input_tokens/output_tokens/cache_creation_input_tokens/cache_read_input_tokens)",
        "Whether `additionalContext` from SubagentStart lands before the subagent's first prompt",
        "Whether the DAG_METER_TOKEN header/allowedEnvVars mechanism authenticates any request "
        "at all (see meter/daemon.py's module docstring — this is suspected broken as specified)",
    ]
    for item in items:
        line(UNVERIFIED, item)

    plugin_data_dir = config_mod.plugin_data_dir()
    capture_path = plugin_data_dir / "probe-capture.jsonl"
    if not capture_path.exists():
        print()
        line(INFO, "no probe-capture.jsonl found",
             f"set \"probe_capture\": true in .dag/meter.json (or DAG_METER_PROBE_CAPTURE=1), "
             f"run a real yolo-dag pass, then re-run this script — it will read "
             f"{capture_path} and report what was actually observed")
        return

    print()
    _summarize_probe_capture(capture_path)


def _summarize_probe_capture(capture_path: Path) -> None:
    tool_names: set[str] = set()
    subagent_start_keys: set[str] = set()
    subagent_stop_keys: set[str] = set()
    count = 0
    try:
        with capture_path.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    entry = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                count += 1
                endpoint = entry.get("endpoint")
                payload = entry.get("payload") or {}
                if endpoint == "tool_pre_agent":
                    tn = payload.get("tool_name") or payload.get("toolName")
                    if tn:
                        tool_names.add(str(tn))
                elif endpoint == "subagent_start":
                    subagent_start_keys.update(payload.keys())
                elif endpoint == "subagent_stop":
                    subagent_stop_keys.update(payload.keys())
    except OSError as exc:
        line(WARN, "could not read probe-capture.jsonl", str(exc))
        return

    line(PASS, f"probe-capture.jsonl has {count} captured event(s)")
    if tool_names:
        line(PASS, "tool name(s) seen at PreToolUse for the Agent matcher", ", ".join(sorted(tool_names)))
    if subagent_start_keys:
        line(PASS, "SubagentStart payload keys observed", ", ".join(sorted(subagent_start_keys)))
    if subagent_stop_keys:
        line(PASS, "SubagentStop payload keys observed", ", ".join(sorted(subagent_stop_keys)))


def main() -> int:
    repo_root = config_mod.find_repo_root(Path.cwd())
    cfg = config_mod.load_config(repo_root)

    print("dag-doctor — meter capability matrix (M0)")
    check_runtime()
    check_environment()
    check_network(cfg)
    check_external_tools()
    check_claude_code()
    check_verify_items()

    print()
    if _hard_failures:
        print(f"{len(_hard_failures)} hard failure(s): {'; '.join(_hard_failures)}")
        print("meter will not start; yolo-dag itself is unaffected.")
        return 1
    print("No hard failures. Informational/WARN/UNVERIFIED items above don't block meter, "
          "but UNVERIFIED items should be resolved with probe-capture before anything past "
          "M0 is trusted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
