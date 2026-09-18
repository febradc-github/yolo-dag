"""Unit tests for the parts of meter.store not already exercised indirectly by
test_receipts.py / test_echo.py: dossier_state bookkeeping."""

from __future__ import annotations

import sqlite3
import unittest

from meter import store


class DossierStateTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(store._SCHEMA)

    def tearDown(self):
        self.conn.close()

    def test_unknown_agent_returns_none(self):
        self.assertIsNone(store.get_dossier_state(self.conn, "agent-1"))

    def test_set_and_get_round_trips(self):
        store.set_dossier_state(self.conn, agent_id="agent-1", run_id="run-1", node="T-001",
                                 dossier_path="/tmp/T-001.md")
        state = store.get_dossier_state(self.conn, "agent-1")
        self.assertEqual(state["dossier_path"], "/tmp/T-001.md")
        self.assertEqual(state["read"], 0)
        self.assertEqual(state["denials"], 0)

    def test_mark_read(self):
        store.set_dossier_state(self.conn, agent_id="agent-1", run_id="run-1", node="T-001",
                                 dossier_path="/tmp/T-001.md")
        store.mark_dossier_read(self.conn, "agent-1")
        state = store.get_dossier_state(self.conn, "agent-1")
        self.assertEqual(state["read"], 1)

    def test_denial_increments_and_is_capped_by_caller(self):
        store.set_dossier_state(self.conn, agent_id="agent-1", run_id="run-1", node="T-001",
                                 dossier_path="/tmp/T-001.md")
        self.assertEqual(store.increment_dossier_denial(self.conn, "agent-1"), 1)
        self.assertEqual(store.increment_dossier_denial(self.conn, "agent-1"), 2)
        state = store.get_dossier_state(self.conn, "agent-1")
        self.assertEqual(state["denials"], 2)

    def test_upsert_preserves_read_and_denial_state(self):
        store.set_dossier_state(self.conn, agent_id="agent-1", run_id="run-1", node="T-001",
                                 dossier_path="/tmp/T-001.md")
        store.mark_dossier_read(self.conn, "agent-1")
        store.increment_dossier_denial(self.conn, "agent-1")
        # A second SubagentStart for the same agent_id (shouldn't normally
        # happen, but the upsert must not silently reset progress if it does).
        store.set_dossier_state(self.conn, agent_id="agent-1", run_id="run-1", node="T-001",
                                 dossier_path="/tmp/T-001.md")
        state = store.get_dossier_state(self.conn, "agent-1")
        self.assertEqual(state["read"], 1)
        self.assertEqual(state["denials"], 1)


if __name__ == "__main__":
    unittest.main()
