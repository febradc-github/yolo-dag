"""Shared SQLite store for meter, at ${CLAUDE_PLUGIN_DATA}/meter/vault.db.

Schema is the one given in meter-handoff.md Section 6.3, verbatim, plus one
additive table not in that section: `ledger_rounds`. Section 6.3's `ledger` table
has a PRIMARY KEY of (run_id, agent_id) — one row per node — but Section 4's M0
spec requires per-round breakdowns for resumed specialists ("record, per
specialist, per round: input tokens, cache-read tokens, cache-write tokens, and
the wall-clock gap since its previous turn"). One row per node cannot hold that.
`ledger_rounds` is the supplement: `ledger` stays exactly as specified, cumulative
as of the most recent stop event, and `ledger_rounds` holds the per-stop-event
detail the M0 prose asks for. `entries` and `minhash` are the M5 Vault schema
from Section 6.3, verbatim; `entries` is now written by meter/vault.py in
shadow mode (`minhash`'s near-hit warm start is not implemented yet — see
vault.py's module docstring for scope). `echo` belongs to M4 and is written by
meter/echo.py.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS entries (
  key            TEXT PRIMARY KEY,
  repo           TEXT NOT NULL,
  agent_type     TEXT NOT NULL,
  model_tier     TEXT NOT NULL,
  patch_sha      TEXT NOT NULL,
  receipt_json   TEXT NOT NULL,
  verify_cmd     TEXT,
  verify_digest  TEXT,
  tokens_spent   INTEGER,
  created_at     INTEGER NOT NULL,
  last_hit_at    INTEGER,
  hit_count      INTEGER DEFAULT 0,
  fail_count     INTEGER DEFAULT 0,
  schema_v        INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS minhash (key TEXT, band INTEGER, sig TEXT, PRIMARY KEY (band, sig, key));
CREATE INDEX IF NOT EXISTS idx_entries_repo ON entries(repo, agent_type);

CREATE TABLE IF NOT EXISTS ledger (
  run_id TEXT, node TEXT, agent_id TEXT, agent_type TEXT, phase TEXT,
  model TEXT, in_tok INTEGER, out_tok INTEGER, cache_read INTEGER, cache_write INTEGER,
  wall_ms INTEGER, source TEXT, started_at INTEGER, ended_at INTEGER,
  call_count INTEGER,
  PRIMARY KEY (run_id, agent_id)
);

CREATE TABLE IF NOT EXISTS echo (
  run_id TEXT, chunk_hash TEXT, tokens INTEGER, recipients INTEGER, origin TEXT,
  PRIMARY KEY (run_id, chunk_hash)
);

-- Additive, not in meter-handoff.md Section 6.3. `echo.recipients` is a count,
-- and a count alone can't tell a second delivery to the *same* agent apart
-- from a delivery to a *new* one — this table dedupes by agent_id so
-- `echo.recipients` is always COUNT(DISTINCT agent_id), matching the
-- "number of distinct agents that received it" wording in Section 4/M4.
CREATE TABLE IF NOT EXISTS echo_recipients (
  run_id TEXT NOT NULL, chunk_hash TEXT NOT NULL, agent_id TEXT NOT NULL,
  PRIMARY KEY (run_id, chunk_hash, agent_id)
);

-- Additive, not in meter-handoff.md Section 6.3. See module docstring.
CREATE TABLE IF NOT EXISTS ledger_rounds (
  run_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  round_index INTEGER NOT NULL,
  in_tok INTEGER, out_tok INTEGER, cache_read INTEGER, cache_write INTEGER,
  wall_ms INTEGER, idle_gap_ms INTEGER, ended_at INTEGER, reason TEXT,
  PRIMARY KEY (run_id, agent_id, round_index)
);

-- M1 Receipts: how many SubagentStop events an agent_id has gone through
-- without a schema-valid dag-receipt block, so the "one repair turn" rule
-- (meter-handoff.md Section 4/M1) can be enforced across daemon requests
-- without keeping state in the router's in-memory Context.
CREATE TABLE IF NOT EXISTS receipt_attempts (
  agent_id TEXT PRIMARY KEY,
  run_id TEXT,
  node TEXT,
  attempts INTEGER NOT NULL DEFAULT 0
);

-- M2 Dossier: whether a given agent_id has read its dossier yet, and how many
-- times it's been denied a Glob/Grep for not having done so (the escape hatch
-- in meter-handoff.md Section 4/M2 is a per-agent cap, not a per-query one).
CREATE TABLE IF NOT EXISTS dossier_state (
  agent_id TEXT PRIMARY KEY,
  run_id TEXT,
  node TEXT,
  dossier_path TEXT,
  read INTEGER NOT NULL DEFAULT 0,
  denials INTEGER NOT NULL DEFAULT 0
);

-- M3a Clamp bounded reads: "which spans does the index think this node cares
-- about, in this file" (meter-handoff.md Section 4/M3, "where the symbol
-- index knows which spans this node needs"). Populated by meter/dossier.py
-- from the neighbour-file symbol index while building a node's dossier.
CREATE TABLE IF NOT EXISTS dossier_spans (
  run_id TEXT NOT NULL,
  node TEXT NOT NULL,
  file TEXT NOT NULL,
  start INTEGER NOT NULL,
  end INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dossier_spans ON dossier_spans(run_id, node, file);

-- M3a Clamp bounded reads: spans of a file already delivered to a given
-- agent_id in this session, so a repeat unbounded Read of the same file can
-- be answered with a short note instead of the content again.
CREATE TABLE IF NOT EXISTS read_spans (
  agent_id TEXT NOT NULL,
  file TEXT NOT NULL,
  start INTEGER NOT NULL,
  end INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_read_spans ON read_spans(agent_id, file);

-- M5 Vault, shadow mode: one row per node whose completion was checked
-- against an existing `entries` row sharing its key — "candidate replays
-- recorded... afterwards the would-be replay is diffed against what the
-- worker actually produced" (meter-handoff.md Section 4/M5). This is the
-- data /dag-vault's shadow-agreement report reads.
CREATE TABLE IF NOT EXISTS vault_shadow_log (
  run_id TEXT NOT NULL,
  node TEXT NOT NULL,
  key TEXT NOT NULL,
  agreed INTEGER NOT NULL,
  checked_at INTEGER NOT NULL,
  PRIMARY KEY (run_id, node)
);

-- M6 Governor 6b: live tool-call count per agent_id, the proxy signal for
-- runaway containment. See meter/governor.py's module docstring for why this
-- is a call-count proxy rather than the spec's own token-based signal —
-- transcript-only accounting (Section 4/M0) has no live token count to
-- compare against a prediction until SubagentStop, by which point a node is
-- already finished and there is nothing left to contain.
CREATE TABLE IF NOT EXISTS tool_call_counts (
  agent_id TEXT PRIMARY KEY,
  run_id TEXT,
  node TEXT,
  count INTEGER NOT NULL DEFAULT 0,
  warned INTEGER NOT NULL DEFAULT 0
);

-- v2 M13 Throttle 13a: per-(agent_id, path) Write-denial count, so the
-- "denied twice" escape hatch (meter-v2-handoff.md Section 3) is enforced
-- across daemon requests rather than in router-local memory.
CREATE TABLE IF NOT EXISTS throttle_denials (
  agent_id TEXT NOT NULL,
  path TEXT NOT NULL,
  denials INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (agent_id, path)
);

-- v2 M12 Distill: a dense brief compiled from a spec, keyed by the spec's
-- own content hash so it survives across runs and repos with an identical
-- spec (meter-v2-handoff.md Section 3, M12: "cached in the Vault... reused
-- across every agent in the run and across resumed runs"). `source_path` is
-- what the brief names as "one Read away" for an agent that needs the full
-- text; `fetch_count` tracks how often that actually happens, the signal
-- the spec says indicates the brief is too thin if it's ever high.
CREATE TABLE IF NOT EXISTS distill_cache (
  spec_hash TEXT PRIMARY KEY,
  brief TEXT NOT NULL,
  source_path TEXT,
  created_at INTEGER NOT NULL,
  delivery_count INTEGER NOT NULL DEFAULT 0,
  fetch_count INTEGER NOT NULL DEFAULT 0
);

-- v2 M14 Attribution: what a node was exposed to (a dossier section — see
-- meter/attribution.py's module docstring for why sections are the unit
-- granularity here) and whether it was ever referenced. Persists across
-- every run in this database, matching the spec's "reference rate... over
-- time" — no separate baseline file needed, this table already accumulates.
CREATE TABLE IF NOT EXISTS attribution_exposures (
  run_id TEXT NOT NULL,
  node TEXT NOT NULL,
  agent_type TEXT NOT NULL,
  unit_type TEXT NOT NULL,
  unit_label TEXT NOT NULL,
  token TEXT NOT NULL,
  referenced INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, node, unit_type, unit_label)
);

-- v2 M10 Pull (DISABLED by default — see meter/pull.py's module docstring
-- for why): one row per dossier section offered via a manifest instead of
-- pushed inline, and whether that section was actually pulled (Read).
-- Persists across runs so promote/revert recommendations have more than one
-- run's evidence behind them.
CREATE TABLE IF NOT EXISTS pull_sections (
  run_id TEXT NOT NULL,
  node TEXT NOT NULL,
  agent_type TEXT NOT NULL,
  section_id TEXT NOT NULL,
  path TEXT NOT NULL,
  tokens_est INTEGER NOT NULL,
  pulled INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, node, section_id)
);

-- v2 M8 Quorum (DISABLED by default — see meter/quorum.py's module
-- docstring): per-domain (Phase 1 specialist) accumulation of full-quorum
-- recall-guard samples and how many produced a unique accepted finding from
-- critics 2/3. Persists across runs — the spec's own gate needs 50+ samples
-- per domain, which no single run can supply.
CREATE TABLE IF NOT EXISTS quorum_domain_stats (
  domain TEXT PRIMARY KEY,
  full_quorum_samples INTEGER NOT NULL DEFAULT 0,
  unique_finding_samples INTEGER NOT NULL DEFAULT 0
);

-- v2 M15 Oracle: a normalised-question -> answer cache, scoped to one run
-- (meter-v2-handoff.md Section 3, M15). `provenance` is a JSON list of
-- repo-relative file paths the answer's content came from; `tree_state_hash`
-- is a hash of those same files' content at cache-write time, re-checked at
-- lookup as a defensive backstop to the proactive invalidate-on-write path
-- (meter/oracle.py's module docstring explains why lookup needs this rather
-- than folding tree_state_hash into a single upfront cache key).
CREATE TABLE IF NOT EXISTS oracle_cache (
  run_id TEXT NOT NULL,
  question_hash TEXT NOT NULL,
  answer TEXT NOT NULL,
  provenance TEXT NOT NULL,
  tree_state_hash TEXT NOT NULL,
  hits INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (run_id, question_hash)
);

-- v2 M13b Throttle narration accounting: prose-to-artifact character ratio
-- per node, computed once from the same transcript the Ledger already reads
-- at SubagentStop. Reported only — nothing here is ever enforced.
CREATE TABLE IF NOT EXISTS narration_stats (
  run_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  agent_type TEXT NOT NULL,
  node TEXT,
  prose_chars INTEGER NOT NULL,
  artifact_chars INTEGER NOT NULL,
  ratio REAL NOT NULL,
  PRIMARY KEY (run_id, agent_id)
);
"""

# Defensive: `call_count` is now part of `ledger`'s CREATE TABLE above, but an
# existing vault.db created before M6 (which only ever ran the old schema)
# needs this ALTER to pick the column up — CREATE TABLE IF NOT EXISTS is a
# no-op against a table that already exists with the old column set. Harmless
# (caught and ignored) against a fresh database that already has the column.
_MIGRATIONS = [
    "ALTER TABLE ledger ADD COLUMN call_count INTEGER",
]

# Deliberately one process-wide lock around one shared connection, not a
# thread-local reader pool. WAL mode (above) would let separate reader
# connections run concurrently with a writer, but the hot-path callers that
# would benefit (oracle.py's cache lookup, clamp.py's read-span tracking) each
# interleave a read with a write in the same call (e.g. a cache hit immediately
# bumps a hit counter) — splitting connections there buys concurrency on the
# read half while still serializing on the write half moments later, for a
# real refactor across ~9 call sites. Each hook already fails open on its own
# deadline (meter/daemon.py), so a lock-contention delay degrades to "this
# hook's decision arrives late or not at all," never a wrong or corrupted one.
# Revisit if `dag-doctor`/the ledger ever shows this lock as a measured
# bottleneck, not a theoretical one.
_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def connect(plugin_data_dir: Path) -> sqlite3.Connection:
    """Process-wide single connection, guarded by a lock for the daemon's few
    concurrent handler threads (up to ~3 workers in parallel per
    meter-handoff.md 6.3). Safe to call repeatedly; returns the cached handle."""
    global _conn
    with _lock:
        if _conn is not None:
            return _conn
        plugin_data_dir.mkdir(parents=True, exist_ok=True)
        db_path = plugin_data_dir / "vault.db"
        conn = sqlite3.connect(str(db_path), timeout=5, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(_SCHEMA)
        for migration in _MIGRATIONS:
            try:
                conn.execute(migration)
            except sqlite3.OperationalError:
                pass  # already applied in a previous boot
        conn.commit()
        _conn = conn
        return conn


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def upsert_ledger_row(conn: sqlite3.Connection, row: dict) -> None:
    row = {**row, "call_count": row.get("call_count")}  # optional: only known at SubagentStop
    with _lock:
        conn.execute(
            """
            INSERT INTO ledger (run_id, node, agent_id, agent_type, phase, model,
                                 in_tok, out_tok, cache_read, cache_write, wall_ms,
                                 source, started_at, ended_at, call_count)
            VALUES (:run_id, :node, :agent_id, :agent_type, :phase, :model,
                    :in_tok, :out_tok, :cache_read, :cache_write, :wall_ms,
                    :source, :started_at, :ended_at, :call_count)
            ON CONFLICT(run_id, agent_id) DO UPDATE SET
              node=excluded.node, agent_type=excluded.agent_type, phase=excluded.phase,
              model=excluded.model, in_tok=excluded.in_tok, out_tok=excluded.out_tok,
              cache_read=excluded.cache_read, cache_write=excluded.cache_write,
              wall_ms=excluded.wall_ms, source=excluded.source,
              started_at=COALESCE(ledger.started_at, excluded.started_at),
              ended_at=excluded.ended_at,
              call_count=COALESCE(excluded.call_count, ledger.call_count)
            """,
            row,
        )
        conn.commit()


def append_ledger_round(conn: sqlite3.Connection, row: dict) -> None:
    with _lock:
        cur = conn.execute(
            "SELECT COALESCE(MAX(round_index), -1) + 1 FROM ledger_rounds "
            "WHERE run_id = ? AND agent_id = ?",
            (row["run_id"], row["agent_id"]),
        )
        next_index = cur.fetchone()[0]
        conn.execute(
            """
            INSERT INTO ledger_rounds (run_id, agent_id, round_index, in_tok, out_tok,
                                        cache_read, cache_write, wall_ms, idle_gap_ms,
                                        ended_at, reason)
            VALUES (:run_id, :agent_id, :round_index, :in_tok, :out_tok, :cache_read,
                    :cache_write, :wall_ms, :idle_gap_ms, :ended_at, :reason)
            """,
            {**row, "round_index": next_index},
        )
        conn.commit()


def get_row_by_agent_id(conn: sqlite3.Connection, agent_id: str) -> dict | None:
    """agent_id is assigned by Claude Code per spawn and is treated as unique
    across runs; SubagentStop payloads don't carry run_id directly (only
    SubagentStart correlation does), so this is how a stop event finds its way
    back to the row its start event wrote."""
    with _lock:
        cur = conn.execute(
            "SELECT * FROM ledger WHERE agent_id = ? ORDER BY started_at DESC LIMIT 1",
            (agent_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))


def get_ledger_rows(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    with _lock:
        cur = conn.execute("SELECT * FROM ledger WHERE run_id = ? ORDER BY started_at", (run_id,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def get_ledger_rounds(conn: sqlite3.Connection, run_id: str, agent_id: str) -> list[dict]:
    with _lock:
        cur = conn.execute(
            "SELECT * FROM ledger_rounds WHERE run_id = ? AND agent_id = ? ORDER BY round_index",
            (run_id, agent_id),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def get_all_ledger_rounds_for_run(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    with _lock:
        cur = conn.execute("SELECT * FROM ledger_rounds WHERE run_id = ?", (run_id,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def bump_receipt_attempt(conn: sqlite3.Connection, *, run_id: str | None, agent_id: str,
                          node: str | None) -> int:
    """Increments and returns the attempt count for `agent_id`. First call for a
    given agent_id returns 1."""
    with _lock:
        conn.execute(
            """
            INSERT INTO receipt_attempts (agent_id, run_id, node, attempts)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(agent_id) DO UPDATE SET attempts = attempts + 1,
              run_id = excluded.run_id, node = excluded.node
            """,
            (agent_id, run_id, node),
        )
        conn.commit()
        cur = conn.execute("SELECT attempts FROM receipt_attempts WHERE agent_id = ?", (agent_id,))
        row = cur.fetchone()
        return row[0] if row else 1


def record_echo_chunk(conn: sqlite3.Connection, *, run_id: str, chunk_hash: str, tokens: int,
                       origin: str, agent_id: str) -> None:
    with _lock:
        conn.execute(
            "INSERT OR IGNORE INTO echo_recipients (run_id, chunk_hash, agent_id) VALUES (?, ?, ?)",
            (run_id, chunk_hash, agent_id),
        )
        cur = conn.execute(
            "SELECT COUNT(DISTINCT agent_id) FROM echo_recipients WHERE run_id = ? AND chunk_hash = ?",
            (run_id, chunk_hash),
        )
        recipients = cur.fetchone()[0]
        conn.execute(
            """
            INSERT INTO echo (run_id, chunk_hash, tokens, recipients, origin)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(run_id, chunk_hash) DO UPDATE SET
              tokens = excluded.tokens, recipients = excluded.recipients
            """,
            (run_id, chunk_hash, tokens, recipients, origin),
        )
        conn.commit()


def get_echo_rows(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    with _lock:
        cur = conn.execute("SELECT * FROM echo WHERE run_id = ?", (run_id,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def set_dossier_state(conn: sqlite3.Connection, *, agent_id: str, run_id: str | None,
                       node: str | None, dossier_path: str) -> None:
    with _lock:
        conn.execute(
            """
            INSERT INTO dossier_state (agent_id, run_id, node, dossier_path, read, denials)
            VALUES (?, ?, ?, ?, 0, 0)
            ON CONFLICT(agent_id) DO UPDATE SET
              run_id = excluded.run_id, node = excluded.node, dossier_path = excluded.dossier_path
            """,
            (agent_id, run_id, node, dossier_path),
        )
        conn.commit()


def get_dossier_state(conn: sqlite3.Connection, agent_id: str) -> dict | None:
    with _lock:
        cur = conn.execute("SELECT * FROM dossier_state WHERE agent_id = ?", (agent_id,))
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))


def mark_dossier_read(conn: sqlite3.Connection, agent_id: str) -> None:
    with _lock:
        conn.execute("UPDATE dossier_state SET read = 1 WHERE agent_id = ?", (agent_id,))
        conn.commit()


def increment_dossier_denial(conn: sqlite3.Connection, agent_id: str) -> int:
    with _lock:
        conn.execute("UPDATE dossier_state SET denials = denials + 1 WHERE agent_id = ?", (agent_id,))
        conn.commit()
        cur = conn.execute("SELECT denials FROM dossier_state WHERE agent_id = ?", (agent_id,))
        row = cur.fetchone()
        return row[0] if row else 0


def set_dossier_spans(conn: sqlite3.Connection, *, run_id: str, node: str,
                       spans: list[dict]) -> None:
    with _lock:
        conn.execute("DELETE FROM dossier_spans WHERE run_id = ? AND node = ?", (run_id, node))
        conn.executemany(
            "INSERT INTO dossier_spans (run_id, node, file, start, end) VALUES (?, ?, ?, ?, ?)",
            [(run_id, node, s["file"], s["start"], s["end"]) for s in spans],
        )
        conn.commit()


def get_dossier_spans(conn: sqlite3.Connection, *, run_id: str, node: str,
                       file: str) -> list[tuple[int, int]]:
    with _lock:
        cur = conn.execute(
            "SELECT start, end FROM dossier_spans WHERE run_id = ? AND node = ? AND file = ?",
            (run_id, node, file),
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


def record_read_span(conn: sqlite3.Connection, *, agent_id: str, file: str, start: int,
                      end: int) -> None:
    with _lock:
        conn.execute(
            "INSERT INTO read_spans (agent_id, file, start, end) VALUES (?, ?, ?, ?)",
            (agent_id, file, start, end),
        )
        conn.commit()


def get_read_spans(conn: sqlite3.Connection, *, agent_id: str, file: str) -> list[tuple[int, int]]:
    with _lock:
        cur = conn.execute(
            "SELECT start, end FROM read_spans WHERE agent_id = ? AND file = ?", (agent_id, file),
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


def get_vault_entry(conn: sqlite3.Connection, key: str) -> dict | None:
    with _lock:
        cur = conn.execute("SELECT * FROM entries WHERE key = ?", (key,))
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))


def insert_vault_entry(conn: sqlite3.Connection, entry: dict) -> None:
    """No-op if `entry['key']` already exists — the caller (meter/vault.py)
    only calls this when `get_vault_entry` found nothing, but a second writer
    racing between those two calls must not overwrite a real entry with a
    duplicate description of the same underlying work."""
    with _lock:
        conn.execute(
            """
            INSERT INTO entries (key, repo, agent_type, model_tier, patch_sha, receipt_json,
                                  verify_cmd, verify_digest, tokens_spent, created_at,
                                  last_hit_at, hit_count, fail_count, schema_v)
            VALUES (:key, :repo, :agent_type, :model_tier, :patch_sha, :receipt_json,
                    :verify_cmd, :verify_digest, :tokens_spent, :created_at,
                    NULL, 0, 0, :schema_v)
            ON CONFLICT(key) DO NOTHING
            """,
            entry,
        )
        conn.commit()


def bump_vault_hit(conn: sqlite3.Connection, key: str) -> None:
    with _lock:
        conn.execute(
            "UPDATE entries SET hit_count = hit_count + 1, last_hit_at = ? WHERE key = ?",
            (int(time.time()), key),
        )
        conn.commit()


def bump_vault_fail(conn: sqlite3.Connection, key: str) -> None:
    with _lock:
        conn.execute("UPDATE entries SET fail_count = fail_count + 1 WHERE key = ?", (key,))
        conn.commit()


def record_shadow_check(conn: sqlite3.Connection, *, run_id: str, node: str, key: str,
                         agreed: bool) -> None:
    with _lock:
        conn.execute(
            """
            INSERT INTO vault_shadow_log (run_id, node, key, agreed, checked_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(run_id, node) DO UPDATE SET
              key = excluded.key, agreed = excluded.agreed, checked_at = excluded.checked_at
            """,
            (run_id, node, key, 1 if agreed else 0, int(time.time())),
        )
        conn.commit()


def get_vault_shadow_stats(conn: sqlite3.Connection) -> dict:
    with _lock:
        cur = conn.execute("SELECT COUNT(*), COALESCE(SUM(agreed), 0) FROM vault_shadow_log")
        total, agreed = cur.fetchone()
        return {"total_checks": total, "agreed": agreed, "disagreed": total - agreed}


def get_vault_totals(conn: sqlite3.Connection) -> dict:
    with _lock:
        cur = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(hit_count), 0), COALESCE(SUM(fail_count), 0) "
            "FROM entries"
        )
        count, hits, fails = cur.fetchone()
        return {"entries": count, "hits": hits, "fails": fails}


def list_vault_entries_by_age(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """(key, patch_sha) pairs, oldest-effectively-used first — the LRU order
    meter/vault.py's `purge` evicts in."""
    with _lock:
        cur = conn.execute(
            "SELECT key, patch_sha FROM entries ORDER BY COALESCE(last_hit_at, created_at) ASC"
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


def delete_vault_entry(conn: sqlite3.Connection, key: str) -> None:
    with _lock:
        conn.execute("DELETE FROM entries WHERE key = ?", (key,))
        conn.commit()


def vault_patch_still_referenced(conn: sqlite3.Connection, patch_sha: str) -> bool:
    with _lock:
        cur = conn.execute("SELECT 1 FROM entries WHERE patch_sha = ? LIMIT 1", (patch_sha,))
        return cur.fetchone() is not None


def get_all_dossier_spans(conn: sqlite3.Connection, *, run_id: str, node: str) -> list[tuple[str, int, int]]:
    with _lock:
        cur = conn.execute(
            "SELECT file, start, end FROM dossier_spans WHERE run_id = ? AND node = ? "
            "ORDER BY file, start",
            (run_id, node),
        )
        return [(r[0], r[1], r[2]) for r in cur.fetchall()]


def increment_tool_call_count(conn: sqlite3.Connection, *, agent_id: str, run_id: str | None,
                               node: str | None) -> int:
    with _lock:
        conn.execute(
            """
            INSERT INTO tool_call_counts (agent_id, run_id, node, count, warned)
            VALUES (?, ?, ?, 1, 0)
            ON CONFLICT(agent_id) DO UPDATE SET count = count + 1,
              run_id = excluded.run_id, node = excluded.node
            """,
            (agent_id, run_id, node),
        )
        conn.commit()
        cur = conn.execute("SELECT count FROM tool_call_counts WHERE agent_id = ?", (agent_id,))
        row = cur.fetchone()
        return row[0] if row else 1


def get_tool_call_count(conn: sqlite3.Connection, agent_id: str) -> int:
    with _lock:
        cur = conn.execute("SELECT count FROM tool_call_counts WHERE agent_id = ?", (agent_id,))
        row = cur.fetchone()
        return row[0] if row else 0


def was_call_warned(conn: sqlite3.Connection, agent_id: str) -> bool:
    with _lock:
        cur = conn.execute("SELECT warned FROM tool_call_counts WHERE agent_id = ?", (agent_id,))
        row = cur.fetchone()
        return bool(row and row[0])


def mark_call_warned(conn: sqlite3.Connection, agent_id: str) -> None:
    with _lock:
        conn.execute("UPDATE tool_call_counts SET warned = 1 WHERE agent_id = ?", (agent_id,))
        conn.commit()


def get_throttle_denials(conn: sqlite3.Connection, *, agent_id: str, path: str) -> int:
    with _lock:
        cur = conn.execute(
            "SELECT denials FROM throttle_denials WHERE agent_id = ? AND path = ?",
            (agent_id, path),
        )
        row = cur.fetchone()
        return row[0] if row else 0


def increment_throttle_denial(conn: sqlite3.Connection, *, agent_id: str, path: str) -> int:
    with _lock:
        conn.execute(
            """
            INSERT INTO throttle_denials (agent_id, path, denials) VALUES (?, ?, 1)
            ON CONFLICT(agent_id, path) DO UPDATE SET denials = denials + 1
            """,
            (agent_id, path),
        )
        conn.commit()
        cur = conn.execute(
            "SELECT denials FROM throttle_denials WHERE agent_id = ? AND path = ?",
            (agent_id, path),
        )
        row = cur.fetchone()
        return row[0] if row else 1


def record_narration(conn: sqlite3.Connection, *, run_id: str, agent_id: str, agent_type: str,
                      node: str | None, prose_chars: int, artifact_chars: int,
                      ratio: float) -> None:
    with _lock:
        conn.execute(
            """
            INSERT INTO narration_stats (run_id, agent_id, agent_type, node, prose_chars,
                                          artifact_chars, ratio)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, agent_id) DO UPDATE SET
              agent_type = excluded.agent_type, node = excluded.node,
              prose_chars = excluded.prose_chars, artifact_chars = excluded.artifact_chars,
              ratio = excluded.ratio
            """,
            (run_id, agent_id, agent_type, node, prose_chars, artifact_chars, ratio),
        )
        conn.commit()


def get_narration_rows(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    with _lock:
        cur = conn.execute("SELECT * FROM narration_stats WHERE run_id = ?", (run_id,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def get_oracle_entry(conn: sqlite3.Connection, *, run_id: str, question_hash: str) -> dict | None:
    with _lock:
        cur = conn.execute(
            "SELECT * FROM oracle_cache WHERE run_id = ? AND question_hash = ?",
            (run_id, question_hash),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))


def upsert_oracle_entry(conn: sqlite3.Connection, *, run_id: str, question_hash: str,
                         answer: str, provenance: str, tree_state_hash: str,
                         created_at: int) -> None:
    with _lock:
        conn.execute(
            """
            INSERT INTO oracle_cache (run_id, question_hash, answer, provenance,
                                       tree_state_hash, hits, created_at)
            VALUES (?, ?, ?, ?, ?, 0, ?)
            ON CONFLICT(run_id, question_hash) DO UPDATE SET
              answer = excluded.answer, provenance = excluded.provenance,
              tree_state_hash = excluded.tree_state_hash, created_at = excluded.created_at
            """,
            (run_id, question_hash, answer, provenance, tree_state_hash, created_at),
        )
        conn.commit()


def bump_oracle_hit(conn: sqlite3.Connection, *, run_id: str, question_hash: str) -> None:
    with _lock:
        conn.execute(
            "UPDATE oracle_cache SET hits = hits + 1 WHERE run_id = ? AND question_hash = ?",
            (run_id, question_hash),
        )
        conn.commit()


def count_oracle_entries(conn: sqlite3.Connection, run_id: str) -> int:
    with _lock:
        cur = conn.execute("SELECT COUNT(*) FROM oracle_cache WHERE run_id = ?", (run_id,))
        return cur.fetchone()[0]


def invalidate_oracle_entries_for_file(conn: sqlite3.Connection, *, run_id: str,
                                        rel_path: str) -> int:
    """Deletes every cached answer whose provenance includes `rel_path`. A
    plain substring `LIKE` match on the JSON text is deliberately cheap
    rather than exact (JSON-parsing every row on every write): a false
    positive here just costs a needless cache miss later, never a stale
    answer being served — the tree_state_hash re-check at lookup is the
    correctness backstop regardless."""
    with _lock:
        cur = conn.execute(
            "DELETE FROM oracle_cache WHERE run_id = ? AND provenance LIKE ?",
            (run_id, f'%"{rel_path}"%'),
        )
        conn.commit()
        return cur.rowcount


def get_oracle_stats(conn: sqlite3.Connection, run_id: str) -> dict:
    with _lock:
        cur = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(hits), 0) FROM oracle_cache WHERE run_id = ?",
            (run_id,),
        )
        entries, hits = cur.fetchone()
        return {"entries": entries, "hits": hits}


def get_distill_brief(conn: sqlite3.Connection, spec_hash: str) -> dict | None:
    with _lock:
        cur = conn.execute("SELECT * FROM distill_cache WHERE spec_hash = ?", (spec_hash,))
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))


def store_distill_brief(conn: sqlite3.Connection, *, spec_hash: str, brief: str,
                         source_path: str | None) -> None:
    with _lock:
        conn.execute(
            """
            INSERT INTO distill_cache (spec_hash, brief, source_path, created_at,
                                        delivery_count, fetch_count)
            VALUES (?, ?, ?, ?, 0, 0)
            ON CONFLICT(spec_hash) DO UPDATE SET
              brief = excluded.brief, source_path = excluded.source_path,
              created_at = excluded.created_at
            """,
            (spec_hash, brief, source_path, int(time.time())),
        )
        conn.commit()


def bump_distill_delivery(conn: sqlite3.Connection, spec_hash: str) -> None:
    with _lock:
        conn.execute(
            "UPDATE distill_cache SET delivery_count = delivery_count + 1 WHERE spec_hash = ?",
            (spec_hash,),
        )
        conn.commit()


def bump_distill_fetch(conn: sqlite3.Connection, spec_hash: str) -> None:
    with _lock:
        conn.execute(
            "UPDATE distill_cache SET fetch_count = fetch_count + 1 WHERE spec_hash = ?",
            (spec_hash,),
        )
        conn.commit()


def record_attribution_exposures(conn: sqlite3.Connection, *, run_id: str, node: str,
                                  agent_type: str, exposures: list[dict]) -> None:
    if not exposures:
        return
    with _lock:
        conn.executemany(
            """
            INSERT INTO attribution_exposures (run_id, node, agent_type, unit_type,
                                                unit_label, token, referenced)
            VALUES (?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(run_id, node, unit_type, unit_label) DO NOTHING
            """,
            [(run_id, node, agent_type, e["unit_type"], e["unit_label"], e["token"])
             for e in exposures],
        )
        conn.commit()


def get_exposures_for_node(conn: sqlite3.Connection, *, run_id: str, node: str) -> list[dict]:
    with _lock:
        cur = conn.execute(
            "SELECT * FROM attribution_exposures WHERE run_id = ? AND node = ?", (run_id, node),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def mark_attribution_referenced(conn: sqlite3.Connection, *, run_id: str, node: str,
                                 unit_type: str, unit_label: str) -> None:
    with _lock:
        conn.execute(
            "UPDATE attribution_exposures SET referenced = 1 "
            "WHERE run_id = ? AND node = ? AND unit_type = ? AND unit_label = ?",
            (run_id, node, unit_type, unit_label),
        )
        conn.commit()


def get_attribution_rates(conn: sqlite3.Connection, min_exposures: int = 50) -> list[dict]:
    """Aggregated across every run recorded — the spec's "reference rate...
    over time." `min_exposures`-gated, matching the sample-size floor
    pruning itself is supposed to respect (meter.modules.attribution.min_exposures)."""
    with _lock:
        cur = conn.execute(
            """
            SELECT agent_type, unit_type, COUNT(*) AS exposures, SUM(referenced) AS referenced
            FROM attribution_exposures
            GROUP BY agent_type, unit_type
            HAVING COUNT(*) >= ?
            """,
            (min_exposures,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def get_attribution_rates_for_run(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    with _lock:
        cur = conn.execute(
            """
            SELECT agent_type, unit_type, COUNT(*) AS exposures, SUM(referenced) AS referenced
            FROM attribution_exposures WHERE run_id = ?
            GROUP BY agent_type, unit_type
            """,
            (run_id,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def record_pull_sections(conn: sqlite3.Connection, *, run_id: str, node: str, agent_type: str,
                          sections: list[dict]) -> None:
    if not sections:
        return
    with _lock:
        conn.executemany(
            """
            INSERT INTO pull_sections (run_id, node, agent_type, section_id, path,
                                        tokens_est, pulled)
            VALUES (?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(run_id, node, section_id) DO NOTHING
            """,
            [(run_id, node, agent_type, s["id"], s["path"], s["tokens_est"]) for s in sections],
        )
        conn.commit()


def mark_pull_section_fetched_by_path(conn: sqlite3.Connection, *, run_id: str, node: str,
                                       path: str) -> bool:
    with _lock:
        cur = conn.execute(
            "UPDATE pull_sections SET pulled = 1 WHERE run_id = ? AND node = ? AND path = ?",
            (run_id, node, path),
        )
        conn.commit()
        return cur.rowcount > 0


def get_pull_sections_for_node(conn: sqlite3.Connection, *, run_id: str, node: str) -> list[dict]:
    with _lock:
        cur = conn.execute(
            "SELECT * FROM pull_sections WHERE run_id = ? AND node = ?", (run_id, node),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def get_pull_stats(conn: sqlite3.Connection, min_samples: int = 20) -> list[dict]:
    with _lock:
        cur = conn.execute(
            """
            SELECT agent_type, section_id, COUNT(*) AS total, SUM(pulled) AS pulled,
                   AVG(tokens_est) AS avg_tokens_est
            FROM pull_sections
            GROUP BY agent_type, section_id
            HAVING COUNT(*) >= ?
            """,
            (min_samples,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def record_quorum_sample(conn: sqlite3.Connection, *, domain: str, unique_finding: bool) -> None:
    with _lock:
        conn.execute(
            """
            INSERT INTO quorum_domain_stats (domain, full_quorum_samples, unique_finding_samples)
            VALUES (?, 1, ?)
            ON CONFLICT(domain) DO UPDATE SET
              full_quorum_samples = full_quorum_samples + 1,
              unique_finding_samples = unique_finding_samples + excluded.unique_finding_samples
            """,
            (domain, 1 if unique_finding else 0),
        )
        conn.commit()


def get_quorum_domain_stats(conn: sqlite3.Connection, domain: str) -> dict | None:
    with _lock:
        cur = conn.execute("SELECT * FROM quorum_domain_stats WHERE domain = ?", (domain,))
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))


def get_all_quorum_domain_stats(conn: sqlite3.Connection) -> list[dict]:
    with _lock:
        cur = conn.execute("SELECT * FROM quorum_domain_stats")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def get_last_round(conn: sqlite3.Connection, run_id: str, agent_id: str) -> dict | None:
    with _lock:
        cur = conn.execute(
            "SELECT * FROM ledger_rounds WHERE run_id = ? AND agent_id = ? "
            "ORDER BY round_index DESC LIMIT 1",
            (run_id, agent_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))
