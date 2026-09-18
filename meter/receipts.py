"""M1 Receipts: a bounded structured record per node, replacing unbounded prose.

Behaviour per meter-handoff.md Section 4 (M1) and 6.2:
- Agent templates gain a required trailing fenced ```dag-receipt block, additive
  to whatever prose trailer the template already has (WORKTREE/BRANCH/COMMIT/
  BLOCKER, VERDICT, ...).
- On SubagentStop, the daemon extracts the last such block from the transcript's
  final assistant message and validates it against schemas/receipt.v1.json.
- A missing or invalid receipt gets exactly one repair turn: the daemon reports
  the block as invalid, the subagent is asked to re-emit it, and a second
  failure is recorded and let through — a malformed receipt must never
  deadlock a node.
- Full transcripts are spooled to .dag/runs/<run-id>/meter/transcripts/<node>.jsonl
  and never re-injected into the orchestrator automatically.

Everything here is a pure function of its inputs except the two disk-writing
helpers at the bottom, so the parse/validate/attempt-tracking logic can be
tested without a running daemon or a real transcript file.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from . import schema_lite
from . import store

_FENCE_RE = re.compile(r"```dag-receipt\s*\n(.*?)\n```", re.DOTALL)

_SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "receipt.v1.json"
_schema_cache: dict | None = None


def _schema() -> dict:
    global _schema_cache
    if _schema_cache is None:
        _schema_cache = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return _schema_cache


def extract_receipt_block(text: str) -> str | None:
    """Returns the raw JSON text of the *last* ```dag-receipt fenced block in
    `text`, or None if there isn't one. Last, not first, because a repair turn
    appends a corrected block after the original rather than editing it in
    place — the transcript is append-only."""
    matches = _FENCE_RE.findall(text or "")
    return matches[-1].strip() if matches else None


def parse_and_validate(text: str) -> tuple[dict | None, list[str]]:
    """Returns (receipt, errors). `receipt` is None when no block was found at
    all or the block wasn't valid JSON; `errors` is non-empty whenever the
    receipt is not schema-valid. A present-but-invalid receipt still gets its
    parsed dict returned when the JSON itself parsed, so callers that want to
    log what was actually sent can do so."""
    raw = extract_receipt_block(text)
    if raw is None:
        return None, ["no ```dag-receipt block found in the final message"]

    try:
        receipt = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, [f"```dag-receipt block is not valid JSON: {exc}"]

    if not isinstance(receipt, dict):
        return receipt, ["```dag-receipt block must be a JSON object"]

    errors = schema_lite.validate(receipt, _schema())
    return receipt, errors


def last_assistant_text(transcript_path: str) -> str:
    """Concatenates the text content of the final assistant message in a
    transcript JSONL. Field shape mirrors ledger.py's tolerance of a few
    plausible depths — VERIFY against a live transcript before trusting this
    exclusively; on any read/parse failure this returns "" and callers treat
    that exactly like "no receipt found", which is the correct fail-open
    behaviour (a receipt the daemon can't read must never deadlock the node)."""
    path = Path(transcript_path)
    if not path.exists():
        return ""

    last_text = ""
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
                text = _flatten_content(content)
                if text:
                    last_text = text
    except OSError:
        return ""
    return last_text


def _flatten_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(parts)
    return ""


class Outcome:
    """Result of `check`, translated by the router into whatever the
    transport-specific hook response shape turns out to be (see router.py)."""

    def __init__(self, *, block: bool, reason: str | None, receipt: dict | None,
                 attempt: int) -> None:
        self.block = block
        self.reason = reason
        self.receipt = receipt
        self.attempt = attempt


def check(conn: sqlite3.Connection, *, run_id: str, node_id: str | None, agent_id: str,
          transcript_path: str | None, repair_turns: int) -> Outcome:
    """The M1 decision function. `repair_turns` is meter.modules.receipts.repair_turns
    (default 1): the number of additional turns a malformed receipt is allowed
    before it's recorded and let through unconditionally."""
    text = last_assistant_text(transcript_path) if transcript_path else ""
    receipt, errors = parse_and_validate(text)

    attempt = store.bump_receipt_attempt(conn, run_id=run_id, agent_id=agent_id, node=node_id)

    if not errors:
        return Outcome(block=False, reason=None, receipt=receipt, attempt=attempt)

    if attempt <= repair_turns:
        reason = ("meter: invalid or missing dag-receipt block — " + "; ".join(errors) +
                   ". Re-emit a corrected ```dag-receipt block as the very last thing "
                   "in your final message, matching schemas/receipt.v1.json.")
        return Outcome(block=True, reason=reason, receipt=receipt, attempt=attempt)

    # Repair turns exhausted: let it through, but the receipt is still absent/invalid.
    return Outcome(block=False, reason="; ".join(errors), receipt=receipt, attempt=attempt)


def write_receipt(run_dir: Path, node_id: str, receipt: dict) -> None:
    receipts_dir = run_dir / "meter" / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    safe_node = node_id.replace("/", "_")
    (receipts_dir / f"{safe_node}.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8")


def spool_transcript(run_dir: Path, node_id: str, transcript_path: str) -> None:
    """Copies the full transcript aside for `/dag-status --explain`. Best-effort:
    a spooling failure must never affect the run, so every error is swallowed."""
    src = Path(transcript_path)
    if not src.exists():
        return
    transcripts_dir = run_dir / "meter" / "transcripts"
    try:
        transcripts_dir.mkdir(parents=True, exist_ok=True)
        safe_node = node_id.replace("/", "_")
        shutil.copyfile(src, transcripts_dir / f"{safe_node}.jsonl")
    except OSError:
        pass
