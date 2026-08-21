#!/usr/bin/env python3
"""Structural validator for the yolo-dag plugin.

Checks the invariants that no amount of careful editing reliably preserves by hand:
manifests parse, agent frontmatter is well-formed, tool names are real, the phase
numbering agrees between the orchestrator and every agent that cites it, and the
literal status-line contracts the orchestrator branches on actually exist in the
agents that are supposed to emit them.

No third-party dependencies — runs on a bare Python 3.
Usage: python3 scripts/validate.py   (exit 0 = clean, 1 = failures)
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Tools the harness actually exposes. An agent naming anything outside this set has a
# typo that will silently give it no tool at all.
VALID_TOOLS = {
    "Agent", "Artifact", "AskUserQuestion", "Bash", "BashOutput", "CronCreate",
    "CronDelete", "CronList", "Edit", "EnterPlanMode", "ExitPlanMode", "Glob", "Grep",
    "KillShell", "ListAgents", "Monitor", "NotebookEdit", "Read", "ReportFindings",
    "ScheduleWakeup", "SendMessage", "Skill", "TaskOutput", "TaskStop", "TodoWrite",
    "WebFetch", "WebSearch", "Write",
}

VALID_MODELS = {"inherit", "haiku", "sonnet", "opus"}

MAX_PHASE = 6

# The phase each agent declares via the literal "Phase N of the `orchestrator` skill"
# sentence in its body. This table is the drift guard: the agent files were once written
# against a 6-phase numbering the orchestrator had already abandoned, and nothing caught it.
PRIMARY_PHASE = {
    "design-specialist": 1,
    "architecture-specialist": 1,
    "research-specialist": 1,
    "security-specialist": 1,
    "test-planning-specialist": 1,
    "cost-estimation-specialist": 1,
    "ux-copy-specialist": 1,
    "data-schema-specialist": 1,
    "spec-reviewer": 2,
    "spec-consolidator": 2,
    "spec-reconciler": 3,
    "task-specialist": 4,
    "task-worker": 5,
    "task-reviewer": 5,
}

# Literal status lines the orchestrator branches on. If an agent stops emitting its
# contract, the pipeline misroutes silently rather than erroring — so assert both ends.
CONTRACTS = {
    "spec-reviewer": ["REVIEW: CLEAN", "REVIEW: FINDINGS"],
    "spec-consolidator": ["CONSOLIDATED:"],
    "spec-reconciler": ["RECONCILE: CLEAN", "RECONCILE: CONTRADICTIONS"],
    "task-reviewer": ["VERDICT: PASS", "VERDICT: FLAGGED"],
}

# Tools an agent must NOT have, with the reason, so a regression explains itself.
FORBIDDEN_TOOLS = {
    "spec-reviewer": {
        "ReportFindings": "reviews in-context prose, which has no file/line to anchor a "
                          "finding to; it would have to invent paths to satisfy the schema"
    },
    "task-specialist": {
        "AskUserQuestion": "subagents must not own the user conversation; escalate to the "
                           "orchestrator in the final message instead"
    },
}

# Agents that inspect a codebase need real search tools rather than shelling out to grep.
NEEDS_SEARCH = set(PRIMARY_PHASE) - {"spec-consolidator"}

errors: list[str] = []
warnings: list[str] = []


def fail(msg: str) -> None:
    errors.append(msg)


def warn(msg: str) -> None:
    warnings.append(msg)


def parse_frontmatter(path: Path) -> tuple[dict[str, object], str]:
    """Minimal frontmatter reader for the subset this repo uses: `key: value` lines,
    with JSON-style arrays for `tools`. Avoids a PyYAML dependency in CI."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        fail(f"{path.relative_to(ROOT)}: missing YAML frontmatter")
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        fail(f"{path.relative_to(ROOT)}: unterminated frontmatter")
        return {}, text
    block, body = text[4:end], text[end + 5:]
    data: dict[str, object] = {}
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if value.startswith("["):
            try:
                data[key] = json.loads(value)
            except json.JSONDecodeError:
                fail(f"{path.relative_to(ROOT)}: `{key}` is not valid JSON: {value}")
                data[key] = []
        else:
            data[key] = value
    return data, body


def check_manifests() -> None:
    plugin_path = ROOT / ".claude-plugin" / "plugin.json"
    market_path = ROOT / ".claude-plugin" / "marketplace.json"

    try:
        plugin = json.loads(plugin_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f".claude-plugin/plugin.json: {exc}")
        return

    for field in ("name", "version", "description", "author", "license"):
        if field not in plugin:
            fail(f"plugin.json: missing required field `{field}`")

    if not isinstance(plugin.get("repository", ""), str):
        fail("plugin.json: `repository` must be a plain string")

    version = str(plugin.get("version", ""))
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        fail(f"plugin.json: `version` must be semver, got {version!r}")

    try:
        market = json.loads(market_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f".claude-plugin/marketplace.json: {exc}")
        return

    names = [p.get("name") for p in market.get("plugins", [])]
    if plugin.get("name") not in names:
        fail(f"marketplace.json: does not list the plugin `{plugin.get('name')}` "
             f"(lists {names})")


def check_agents() -> dict[str, dict]:
    agents: dict[str, dict] = {}
    agent_dir = ROOT / "agents"
    for path in sorted(agent_dir.glob("*.md")):
        data, body = parse_frontmatter(path)
        rel = path.relative_to(ROOT)
        stem = path.stem

        name = data.get("name")
        if name != stem:
            fail(f"{rel}: frontmatter name {name!r} does not match filename {stem!r}")
        if not data.get("description"):
            fail(f"{rel}: missing `description`")

        model = data.get("model")
        if model not in VALID_MODELS:
            fail(f"{rel}: model {model!r} not one of {sorted(VALID_MODELS)}")

        tools = data.get("tools")
        if not isinstance(tools, list) or not tools:
            fail(f"{rel}: `tools` must be a non-empty array")
            tools = []
        for tool in tools:
            if tool not in VALID_TOOLS:
                fail(f"{rel}: unknown tool {tool!r}")

        # The flat fan-out invariant: all spawning happens from the orchestrator skill.
        if "Agent" in tools:
            fail(f"{rel}: has `Agent` in tools — no plugin agent may spawn further agents; "
                 f"all fan-out happens from the orchestrator skill")

        for tool, reason in FORBIDDEN_TOOLS.get(stem, {}).items():
            if tool in tools:
                fail(f"{rel}: must not have `{tool}` — {reason}")

        if stem in NEEDS_SEARCH:
            for tool in ("Grep", "Glob"):
                if tool not in tools:
                    fail(f"{rel}: inspects a codebase but is missing `{tool}`")

        # Primary phase declaration.
        expected = PRIMARY_PHASE.get(stem)
        if expected is None:
            warn(f"{rel}: not listed in PRIMARY_PHASE; add it to scripts/validate.py")
        else:
            match = re.search(r"Phase (\d+) of the `orchestrator` skill", body)
            if not match:
                fail(f"{rel}: no \"Phase N of the `orchestrator` skill\" declaration in body")
            elif int(match.group(1)) != expected:
                fail(f"{rel}: declares Phase {match.group(1)}, expected Phase {expected}")

        # No reference to a phase the orchestrator does not define.
        for cited in {int(n) for n in re.findall(r"Phase (\d+)", data.get("description", "") + body)}:
            if not 1 <= cited <= MAX_PHASE:
                fail(f"{rel}: cites Phase {cited}, but the orchestrator defines 1-{MAX_PHASE}")

        for contract in CONTRACTS.get(stem, []):
            if contract not in body:
                fail(f"{rel}: missing required status-line contract {contract!r}")

        agents[stem] = {"tools": tools, "body": body}
    return agents


def check_orchestrator(agents: dict[str, dict]) -> None:
    path = ROOT / "skills" / "orchestrator" / "SKILL.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"skills/orchestrator/SKILL.md: {exc}")
        return

    phases = {int(n) for n in re.findall(r"^## Phase (\d+)", text, re.MULTILINE)}
    if phases != set(range(1, MAX_PHASE + 1)):
        fail(f"orchestrator: defines phases {sorted(phases)}, expected 1-{MAX_PHASE}")

    for name in agents:
        if name not in text:
            fail(f"orchestrator: never references agent `{name}` — it is unreachable")

    for referenced in set(re.findall(r"`([a-z][a-z-]*-(?:specialist|reviewer|consolidator|reconciler|worker))`", text)):
        if referenced not in agents:
            fail(f"orchestrator: references `{referenced}`, which has no file in agents/")

    # The orchestrator must branch on the same literal strings the agents emit.
    for owner, contracts in CONTRACTS.items():
        for contract in contracts:
            head = contract.split(":")[0]
            if head not in text:
                fail(f"orchestrator: never reads the `{head}` contract emitted by `{owner}`")


def check_commands() -> None:
    for path in sorted((ROOT / "commands").glob("*.md")):
        data, _ = parse_frontmatter(path)
        if not data.get("description"):
            fail(f"{path.relative_to(ROOT)}: missing `description`")
        tools = data.get("allowed-tools")
        if isinstance(tools, list):
            for tool in tools:
                if tool not in VALID_TOOLS:
                    fail(f"{path.relative_to(ROOT)}: unknown tool {tool!r}")


def check_skills() -> None:
    for path in sorted((ROOT / "skills").glob("*/SKILL.md")):
        data, _ = parse_frontmatter(path)
        if not data.get("description"):
            fail(f"{path.relative_to(ROOT)}: missing `description`")


def main() -> int:
    check_manifests()
    agents = check_agents()
    check_orchestrator(agents)
    check_commands()
    check_skills()

    for w in warnings:
        print(f"warning: {w}")
    for e in errors:
        print(f"error: {e}")

    if errors:
        print(f"\n{len(errors)} error(s), {len(warnings)} warning(s)")
        return 1
    print(f"ok: {len(agents)} agents, manifests, commands, and skills all valid"
          + (f" ({len(warnings)} warning(s))" if warnings else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
