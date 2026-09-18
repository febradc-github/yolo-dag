"""M13 Throttle: output-side reduction (meter-v2-handoff.md Section 3, M13).

**Class: lossless.** Output bills at roughly 5x input, and nothing in v1
addresses it.

**13a implemented.** Deny `Write` on an existing, git-tracked file with a
reason naming `Edit` — a full-file rewrite pays the entire file in output
tokens, an `Edit` pays only the hunk. Four deterministic exceptions, checked
in this module, mirror the spec exactly: the file is new (not tracked),
the file is generated-shaped, the diff would exceed `rewrite_ratio` of the
file (a genuinely large rewrite gets no benefit from denial), or the agent
has already been denied twice for this exact path (the cap that guarantees
an agent that truly needs a rewrite can always get one).

**13b implemented.** Narration accounting: the ratio of prose to artifact
output per agent type, per node, computed from the same transcript the
Ledger already parses at `SubagentStop`. Reported, never enforced — a hook
cannot tell a useful explanation from padding, so this only makes the number
visible for template tuning.

**13c NOT implemented.** "VERIFY whether the installed Claude Code exposes
per-node reasoning effort to a hook... if it does not, close 13c and note
it." There's no live session here to probe with, and dag-doctor.py's own
VERIFY items are exactly the place this belongs once one exists — see the
item it prints. Building effort routing against an unconfirmed capability
would be worse than not building it, so 13c stays closed per the spec's own
instruction for exactly this situation.
"""

from __future__ import annotations

import difflib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from . import store

_GENERATED_PATH_RE = re.compile(
    r"(^|/)(dist|build|generated|out|\.next|\.nuxt|vendor|node_modules|target)(/|$)"
)
_GENERATED_SUFFIX_RE = re.compile(r"\.(min\.js|min\.css|generated\.\w+|pb2\.py|pb\.go)$")


def is_generated_shaped(rel_path: str) -> bool:
    return bool(_GENERATED_PATH_RE.search(rel_path)) or bool(_GENERATED_SUFFIX_RE.search(rel_path))


def _change_ratio(old_text: str, new_text: str) -> float:
    """1.0 = completely different, 0.0 = identical, by line-level similarity."""
    if not old_text and not new_text:
        return 0.0
    matcher = difflib.SequenceMatcher(a=old_text.splitlines(), b=new_text.splitlines())
    return 1.0 - matcher.ratio()


def decide_write(*, conn: sqlite3.Connection, repo_root: Path, agent_id: str, rel_path: str,
                  new_content: str, is_tracked: bool, rewrite_ratio: float,
                  max_denials: int) -> dict | None:
    """Returns `{"kind": "deny", "reason": str}` or None to leave the Write
    alone. Pure with respect to disk except for reading the existing file's
    current content (needed for the exceptions) and the denial counter."""
    abs_path = repo_root / rel_path
    if not abs_path.is_file():
        return None  # the file is new — first exception, and also the primary trigger's negation

    if not is_tracked:
        return None  # exists on disk but not in git — not "existing" in the sense that matters

    if is_generated_shaped(rel_path):
        return None

    denials = store.get_throttle_denials(conn, agent_id=agent_id, path=rel_path)
    if denials >= max_denials:
        return None  # the cap: an agent that genuinely needs this must always be able to get it

    try:
        old_content = abs_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    if _change_ratio(old_content, new_content) > rewrite_ratio:
        return None  # a rewrite this large gets little benefit from being denied

    store.increment_throttle_denial(conn, agent_id=agent_id, path=rel_path)
    return {"kind": "deny",
            "reason": f"meter: {rel_path} already exists and is tracked by git — use Edit for "
                      f"a targeted change instead of rewriting the whole file; Write pays for "
                      f"the entire file in output tokens, Edit pays only for the hunk."}


# --- 13b: narration accounting -------------------------------------------

_ARTIFACT_TOOLS = {"Write", "Edit", "NotebookEdit"}


def narration_ratio(transcript_path: str) -> dict[str, Any] | None:
    """Returns {"prose_chars": int, "artifact_chars": int, "ratio": float} from
    one transcript, or None if it can't be read/parsed. `ratio` is prose
    divided by artifact chars — a number well above 1 across many nodes of
    the same agent template is the tuning signal the spec describes."""
    path = Path(transcript_path)
    if not path.exists():
        return None

    prose_chars = 0
    artifact_chars = 0
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
                message = entry.get("message") if isinstance(entry.get("message"), dict) else entry
                if not isinstance(message, dict) or message.get("role") != "assistant":
                    continue
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        prose_chars += len(str(block.get("text", "")))
                    elif block.get("type") == "tool_use" and block.get("name") in _ARTIFACT_TOOLS:
                        tool_input = block.get("input") or {}
                        artifact_chars += len(str(tool_input.get("content")
                                                   or tool_input.get("new_string") or ""))
    except OSError:
        return None

    denom = artifact_chars or 1
    return {"prose_chars": prose_chars, "artifact_chars": artifact_chars,
            "ratio": round(prose_chars / denom, 2)}


def render_report_section(rows: list[dict]) -> str | None:
    if not rows:
        return None
    by_agent_type: dict[str, list[float]] = {}
    for row in rows:
        by_agent_type.setdefault(row["agent_type"], []).append(row["ratio"])
    lines = ["Throttle: prose-to-artifact ratio per agent type (13b, reported only — a high "
             "number is a template-tuning signal, not something meter enforces)"]
    for agent_type in sorted(by_agent_type):
        ratios = by_agent_type[agent_type]
        avg = sum(ratios) / len(ratios)
        lines.append(f"  {agent_type:<28} avg {avg:.1f}x  ({len(ratios)} node(s))")
    return "\n".join(lines)
