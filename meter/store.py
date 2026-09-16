"""Shared SQLite store for meter, at ${CLAUDE_PLUGIN_DATA}/meter/vault.db.

Schema is the one given in meter-handoff.md Section 6.3, verbatim, plus one
additive table not in that section: `ledger_rounds`. Section 6.3's `ledger` table
has a PRIMARY KEY of (run_id, agent_id) — one row per node — but Section 4's M0
spec requires per-round breakdowns for resumed specialists ("record, per
specialist, per round: input tokens, cache-read tokens, cache-write tokens, and
the wall-clock gap since its previous turn"). One row per node cannot hold that.
`ledger_rounds` is the supplement: `ledger` stays exactly as specified, cumulative
as of the most recent stop event, and `ledger_rounds` holds the per-stop-event
detail the M0 prose asks for. `entries`, `minhash`, and `echo` belong to later
milestones (M5, M4) and are created now only because they share this database
file; nothing in M0 writes to them.
"""

from __future__ import annotations

import sqlite3
import threading
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
  PRIMARY KEY (run_id, agent_id)
);

CREATE TABLE IF NOT EXISTS echo (
  run_id TEXT, chunk_hash TEXT, tokens INTEGER, recipients INTEGER, origin TEXT,
  PRIMARY KEY (run_id, chunk_hash)
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
"""

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
    with _lock:
        conn.execute(
            """
            INSERT INTO ledger (run_id, node, agent_id, agent_type, phase, model,
                                 in_tok, out_tok, cache_read, cache_write, wall_ms,
                                 source, started_at, ended_at)
            VALUES (:run_id, :node, :agent_id, :agent_type, :phase, :model,
                    :in_tok, :out_tok, :cache_read, :cache_write, :wall_ms,
                    :source, :started_at, :ended_at)
            ON CONFLICT(run_id, agent_id) DO UPDATE SET
              node=excluded.node, agent_type=excluded.agent_type, phase=excluded.phase,
              model=excluded.model, in_tok=excluded.in_tok, out_tok=excluded.out_tok,
              cache_read=excluded.cache_read, cache_write=excluded.cache_write,
              wall_ms=excluded.wall_ms, source=excluded.source,
              started_at=COALESCE(ledger.started_at, excluded.started_at),
              ended_at=excluded.ended_at
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
