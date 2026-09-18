"""M4 Echo Index, measure-only mode (meter-handoff.md Section 4/M4).

Purpose: measure R1 — the same text (a merged spec section, a contract, a
sibling's receipt) sent byte-for-byte to many agents, none of whom share a
cache for it because nothing currently makes them ordered identically.

Scope of this implementation: only the **Task prompt** channel is wired up.
The spec also lists dossiers, `additionalContext`, and tool results as
observable channels, but dossiers/`additionalContext` don't exist until M2,
and tool-result echo tracking needs an `agent_id` on PostToolUse payloads that
dag-doctor's VERIFY items say is unconfirmed — recording against a shaky
correlation would silently produce a wrong recipient count, which is worse
than under-counting. Task prompts alone already cover the highest-value case
in practice (the same pasted spec/contract text repeated across every sibling
worker in a pass) and are correlated through the same Correlator that already
underpins M0's node attribution, so nothing new and unverified is required to
wire this up. Extend to the other channels once M2/M3 land and the
PostToolUse correlation gap is closed.

Promotion (writing a shared preamble file so a cohort's Task prompts share a
byte-identical prefix) is M3/Governor territory and isn't implemented here —
this module only ever measures.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3

from . import store

_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_WHITESPACE_RE = re.compile(r"\s+")

# Below this, a chunk (a short heading, a single word, a stray punctuation
# line) isn't worth a row: the bookkeeping cost would exceed any plausible
# saving from ever deduplicating it.
_MIN_CHUNK_CHARS = 40

# Characters-per-token, per meter-handoff.md Section 6.5: "a calibrated
# heuristic... plus or minus 10 percent is fine for budgeting." Uncalibrated
# for now — this is an estimate for ranking chunks, never a substitute for the
# Ledger's measured tokens (Section 6.5's "keep the two clearly separated").
_CHARS_PER_TOKEN = 4


def _collapse_whitespace(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text.strip())


def _split_paragraphs(text: str) -> list[str]:
    return [_collapse_whitespace(p) for p in re.split(r"\n\s*\n", text) if p.strip()]


def normalize_chunks(text: str) -> list[str]:
    """Splits `text` into paragraph- or fenced-block-shaped chunks, whitespace-
    collapsed, dropping anything under `_MIN_CHUNK_CHARS`. A pure function —
    the whole point of "normalise" is that the same underlying content
    produces the same chunk string regardless of incidental formatting, so two
    agents receiving the same paragraph with different indentation still hash
    to the same chunk."""
    if not text:
        return []

    chunks: list[str] = []
    pos = 0
    for m in _FENCE_RE.finditer(text):
        chunks.extend(_split_paragraphs(text[pos:m.start()]))
        fenced = _collapse_whitespace(m.group(0))
        if fenced:
            chunks.append(fenced)
        pos = m.end()
    chunks.extend(_split_paragraphs(text[pos:]))

    return [c for c in chunks if len(c) >= _MIN_CHUNK_CHARS]


def chunk_hash(chunk: str) -> str:
    """64-bit hash per meter-handoff.md Section 4/M4."""
    return hashlib.blake2b(chunk.encode("utf-8"), digest_size=8).hexdigest()


def estimate_tokens(chunk: str) -> int:
    return max(1, len(chunk) // _CHARS_PER_TOKEN)


def record(conn: sqlite3.Connection, *, run_id: str, agent_id: str, origin: str,
           text: str) -> int:
    """Records every chunk in `text` as delivered to `agent_id`. Returns the
    number of chunks recorded, for callers/tests that want a count."""
    if not run_id or run_id == "_unattributed" or not agent_id or not text:
        return 0
    count = 0
    for chunk in normalize_chunks(text):
        store.record_echo_chunk(
            conn, run_id=run_id, chunk_hash=chunk_hash(chunk),
            tokens=estimate_tokens(chunk), origin=origin, agent_id=agent_id,
        )
        count += 1
    return count


def echoed_tokens(rows: list[dict]) -> int:
    """sum over chunks of (tokens x (recipients - 1)) — meter-handoff.md
    Section 4/M4's headline metric: the measured size of R1."""
    return sum((r.get("tokens") or 0) * max(0, (r.get("recipients") or 0) - 1) for r in rows)


def render_report_section(rows: list[dict]) -> str | None:
    """Renders the `/dag-cost` "Echo" line and its top chunk, or None when
    there's nothing to report (echo disabled, or no run data yet)."""
    if not rows:
        return None
    duplicated = [r for r in rows if (r.get("recipients") or 0) > 1]
    total_echoed = echoed_tokens(rows)
    lines = [f"Echo: {total_echoed:,} echoed tokens across {len(duplicated)} chunk(s) "
             f"(of {len(rows)} distinct chunk(s) seen)"]
    if duplicated:
        top = max(duplicated, key=lambda r: (r.get("tokens") or 0) * (r.get("recipients") or 0))
        lines.append(f"  top chunk: {top.get('origin')}, ~{top.get('tokens', 0):,} tok "
                     f"x {top.get('recipients', 0)} agent(s)")
    return "\n".join(lines)
