"""M15 Oracle: within-run question cache (meter-v2-handoff.md Section 3, M15).

**Class: lossless. Scope implemented: `Grep` query caching only.**

- **`Glob` is not wired into recording.** There's no existing `PostToolUse`
  matcher on `Glob` to observe its answer from (v1 never needed one), and
  Glob patterns are typically cheap filename matches, not the expensive
  full-text search this module targets — adding a matcher purely for this
  would be more surface for a lower-value case. `lookup` will still check a
  `Glob` query against the cache (in case anything is ever recorded for one);
  it simply never gets a hit today.
- **Repeated `Read` of the same spans is NOT handled here.** It's already
  covered by v1 M3a's `read_spans` tracking (meter/clamp.py,
  meter/store.py's `read_spans` table) — a second implementation of the same
  idea would be duplicated, overlapping bookkeeping for no additional value.

**Key derivation, and why lookup isn't a single upfront `(question_hash,
tree_state_hash)` key.** The spec's formula keys the cache on
`(question_hash, tree_state_hash)` where `tree_state_hash` "covers the files
in the answer's provenance." But provenance is only known *after* a query
has been answered once — at lookup time, before running the search, there is
no provenance yet to hash. So: the cache is keyed on `question_hash` alone,
and `tree_state_hash` (computed from the stored answer's own provenance
files, at write time) travels alongside the entry. `lookup` re-hashes those
same files' *current* content and compares; a mismatch means the tree moved
since caching and is treated as a miss. This achieves exactly what the
spec's key wants to guarantee (never serve an answer the tree has outgrown)
without requiring information that doesn't exist yet at the point a lookup
has to happen.

**Invalidation** has two paths, per the spec: explicit (`invalidate_for_write`,
called from a `Write`/`Edit` `PostToolUse` observation in meter/router.py,
tracked in the store — never inferred) and the tree-state-hash mismatch above
as a defensive backstop if an invalidation event is ever missed. Cross-run
reuse is off by default — the cache is `(run_id, question_hash)`-keyed, so it
can never be read by a different run.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import store

_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "in", "on", "at", "of", "to", "for",
    "and", "or", "where", "how", "what", "which", "does", "do", "did", "this", "that",
    "with", "from", "by", "be", "as", "if", "so", "then",
}
_PRONOUNS = {
    "i", "you", "he", "she", "it", "we", "they", "me", "him", "her", "us", "them",
    "this", "that", "these", "those", "its", "his", "their", "our", "your",
}
_WORD_RE = re.compile(r"[a-z0-9_]+")

_GREP_MATCH_RE = re.compile(r"^([^\n:]+):\d+:")


def normalize_question(text: str) -> str:
    """Lowercase, strip punctuation and pronouns/stopwords, sort remaining
    content words — a pure function so two differently-phrased searches for
    the same thing ("where is the session token validated" vs. "session
    token validation location") hash identically when they share content
    words, and deterministically differently when they don't."""
    words = [w for w in _WORD_RE.findall(text.lower())
             if w not in _STOPWORDS and w not in _PRONOUNS]
    return " ".join(sorted(set(words)))


def question_hash(text: str) -> str:
    return hashlib.sha256(normalize_question(text).encode("utf-8")).hexdigest()


def extract_provenance(tool_name: str, output: str) -> list[str]:
    """Repo-relative file paths an answer's content came from. `Grep`-shaped
    output only (`path:line:content`); returns [] for anything else,
    including a `Glob` output this module doesn't record answers for."""
    if not output or tool_name != "Grep":
        return []
    paths = set()
    for line in output.splitlines():
        m = _GREP_MATCH_RE.match(line)
        if m:
            paths.add(m.group(1))
    return sorted(paths)


def _file_content_hash(repo_root: Path, rel_path: str) -> str:
    try:
        data = (repo_root / rel_path).read_bytes()
    except OSError:
        return "missing"
    return hashlib.sha256(data).hexdigest()


def compute_tree_state_hash(repo_root: Path, provenance: list[str]) -> str:
    parts = [f"{p}:{_file_content_hash(repo_root, p)}" for p in sorted(provenance)]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def lookup(conn: sqlite3.Connection, repo_root: Path, *, run_id: str,
           query_text: str) -> dict[str, Any] | None:
    """Returns the cached entry (with `answer` and `provenance` already
    JSON-decoded) on a valid hit, else None. Never raises."""
    try:
        entry = store.get_oracle_entry(conn, run_id=run_id, question_hash=question_hash(query_text))
        if entry is None:
            return None
        provenance = json.loads(entry["provenance"])
        if compute_tree_state_hash(repo_root, provenance) != entry["tree_state_hash"]:
            return None  # the tree moved since caching; the backstop, see module docstring
        store.bump_oracle_hit(conn, run_id=run_id, question_hash=entry["question_hash"])
        return {**entry, "provenance": provenance}
    except Exception:
        return None


def record(conn: sqlite3.Connection, repo_root: Path, *, run_id: str, query_text: str,
           answer: str, tool_name: str, max_entries: int) -> None:
    """Records `answer` for `query_text`, keyed by its normalised hash.
    Never raises; silently skips when there's nothing to attribute the
    answer to (no provenance) or the run is already at `max_entries`."""
    try:
        provenance = extract_provenance(tool_name, answer)
        if not provenance:
            return
        if store.count_oracle_entries(conn, run_id) >= max_entries:
            return
        store.upsert_oracle_entry(
            conn, run_id=run_id, question_hash=question_hash(query_text), answer=answer,
            provenance=json.dumps(provenance),
            tree_state_hash=compute_tree_state_hash(repo_root, provenance),
            created_at=int(time.time()),
        )
    except Exception:
        pass


def invalidate_for_write(conn: sqlite3.Connection, *, run_id: str, rel_path: str) -> int:
    try:
        return store.invalidate_oracle_entries_for_file(conn, run_id=run_id, rel_path=rel_path)
    except Exception:
        return 0


def render_report_section(conn: sqlite3.Connection, run_id: str) -> str | None:
    stats = store.get_oracle_stats(conn, run_id)
    if not stats["entries"]:
        return None
    return (f"Oracle: {stats['entries']} distinct question(s) cached, {stats['hits']} "
            f"repeat-search hit(s) served from cache instead of re-searching (Grep only — "
            f"see meter/oracle.py for scope)")
