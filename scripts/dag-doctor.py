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
import threading
from pathlib import Path

# Every individual probe below already carries its own timeout, but a probe on a
# platform where a timeout parameter is silently ignored (or a subprocess that
# ignores its own kill signal) would otherwise hang this command indefinitely.
# This is a backstop, not the primary defense: force the process to exit rather
# than hang forever, well past the sum of every probe's own timeout.
_GLOBAL_DEADLINE_SECONDS = 45.0

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


def check_read_hook_conflict(repo_root: Path | None) -> None:
    section("Clamp 3a hazard check (meter-handoff.md Section 6.2)")
    conflict = config_mod.detect_read_hook_conflict(repo_root)
    if conflict:
        line(WARN, "a user/project PreToolUse hook also matches Read", conflict)
        line(INFO, "M3a's bounded reads are disabled for this repo's sessions",
             "scripts/meter-boot.py wrote a marker at SessionStart; remove the conflicting "
             "hook (or narrow its matcher to exclude Read) and start a new session to clear it")
    else:
        line(PASS, "no conflicting PreToolUse-on-Read hook found in user/project settings")


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
        "Whether a top-level {\"decision\": \"block\", \"reason\": ...} response to the "
        "SubagentStop HTTP hook actually forces a repair turn (meter/receipts.py, M1) — the "
        "Stop hook is documented to support this shape; SubagentStop's exact parity with Stop "
        "over an HTTP (not command) transport is unconfirmed. Fails open: an unconfirmed "
        "mechanism here means a malformed receipt is recorded but not repaired, never a hang",
        "The exact field name and shape of a PostToolUse payload's tool output (tried: "
        "tool_response/toolResponse/tool_output, then output/stdout/content/text keys inside "
        "it) — meter/router.py's _extract_tool_output_text, used by Clamp 3b (M3)",
        "Whether PostToolUse's session_id (or any other payload field) identifies which "
        "run/node a subagent's own tool call belongs to — Clamp 3b currently falls back to "
        "\"the most recently active run in this repo\" for where it files a spooled full-output "
        "copy, which can misattribute the *filing location* (never the compression itself) "
        "when two runs are active in the same repo at once",
        "Whether a PreToolUse/PostToolUse payload for a subagent's OWN tool call (Read/Grep/"
        "Glob/Bash fired from inside a spawned task-worker, not the orchestrator's own Agent "
        "call) carries that subagent's agent_id at all — M2 Dossier's enforcement half "
        "(meter/router.py's handle_tool_pre_dossier) can only deny a Glob/Grep or notice a "
        "dossier Read when agent_id is present; absent it, enforcement silently never fires "
        "(fail-open: the dossier is still delivered via additionalContext, it just isn't "
        "enforced)",
        "Whether the Agent tool's PreToolUse tool_input payload names the target agent type "
        "as `subagent_type` (this repo's own Agent tool schema uses that key) or something "
        "else — meter/router.py's _maybe_build_dossier gates dossier building on "
        "meter.modules.dossier.apply_to using this field; if the key differs, dossiers are "
        "built for every subagent type rather than just task-worker/task-reviewer, which is "
        "extra (wasted) work, never a wrong dossier",
        "Whether the matcher-less PreToolUse entry in hooks.json actually fires on every tool "
        "call (Governor 6b, meter/router.py's handle_tool_pre_governor) — this is a materially "
        "larger surface than any other hook meter registers, and doubly depends on agent_id "
        "being present on a subagent's own tool calls (see the item above about Dossier "
        "enforcement, which has the identical dependency)",
        "Whether a top-level PreToolUse `allow` decision's `permissionDecisionReason` is ever "
        "actually surfaced to the model — Governor's 2x overrun warning "
        "(meter/router.py's handle_tool_pre_governor) delivers its note this way because the "
        "spec's own suggested mechanism (PostToolUse additionalContext) isn't a documented "
        "PostToolUse capability; if this doesn't surface, the 2x warning is silently inert "
        "while the 3x deny (a well-established `permissionDecision: deny` shape) still works",
        "Whether the installed Claude Code exposes per-node reasoning effort to a hook, via an "
        "`effort` field on hook payloads or a $CLAUDE_EFFORT environment variable — v2 M13c "
        "(effort routing) is deliberately NOT implemented pending this; meter-v2-handoff.md's "
        "own instruction for exactly this situation is \"if it does not, close 13c and note "
        "it,\" which is what meter/throttle.py's module docstring does",
        "Whether a PostToolUse payload for Write/Edit carries agent_id (the same open question "
        "as elsewhere in this list) — v2 M15 Oracle's invalidate-on-write path "
        "(meter/router.py's handle_tool_post) falls back to \"the most recently active run in "
        "this repo\" when absent, same as Clamp 3b; a wrong run_id here means an invalidation "
        "or a cached answer lands under the wrong run's scope, which is a silent miss, never a "
        "stale answer served (the tree_state_hash re-check at lookup, meter/oracle.py, is the "
        "correctness backstop regardless of which run_id an entry ends up under)",
        "Whether the orchestrator actually restates a task-reviewer spawn's WORKTREE/COMMIT as "
        "literal `WORKTREE: <path>` / `COMMIT: <sha>` lines (skills/orchestrator/SKILL.md's own "
        "instruction, added alongside v2 M11 Sieve) — meter/router.py's _maybe_run_sieve regexes "
        "for exactly this shape and returns None (never a guess) if it can't find both lines, "
        "so the worst case of this being wrong is Sieve simply never firing, not a corrupted "
        "reviewer prompt",
        "v2 M10 Pull is implemented but DISABLED BY DEFAULT (meter.modules.pull.enabled = "
        "false) pending real measured evidence from M14 Attribution — see meter/pull.py's "
        "module docstring. If ever enabled for testing: it shares the same agent_id-on-a-"
        "subagent's-own-tool-calls dependency as Clamp 3a/Dossier enforcement above, since "
        "pull-section tracking (meter/router.py's handle_tool_pre_dossier) keys off the same "
        "dossier_state row",
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
    tool_post_keys: set[str] = set()
    tool_pre_dossier_keys: set[str] = set()
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
                elif endpoint == "tool_post":
                    tool_post_keys.update(payload.keys())
                elif endpoint == "tool_pre_dossier":
                    tool_pre_dossier_keys.update(payload.keys())
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
    if tool_post_keys:
        line(PASS, "PostToolUse payload keys observed", ", ".join(sorted(tool_post_keys)))
    if tool_pre_dossier_keys:
        line(PASS, "PreToolUse (Read/Grep/Glob) payload keys observed",
             ", ".join(sorted(tool_pre_dossier_keys)))


def main() -> int:
    watchdog = threading.Timer(_GLOBAL_DEADLINE_SECONDS, os._exit, args=(1,))
    watchdog.daemon = True
    watchdog.start()
    try:
        return _run_checks()
    finally:
        watchdog.cancel()


def _run_checks() -> int:
    repo_root = config_mod.find_repo_root(Path.cwd())
    cfg = config_mod.load_config(repo_root)

    print("dag-doctor — meter capability matrix")
    check_runtime()
    check_environment()
    check_network(cfg)
    check_external_tools()
    check_claude_code()
    check_read_hook_conflict(repo_root)
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
