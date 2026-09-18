"""M14 Attribution: context reference tracking (meter-v2-handoff.md Section 3,
M14).

**Class: lossless, measurement only.** This module never prunes what a
dossier includes — `meter.modules.attribution.prune` defaults `False`, and
there is no code path anywhere in this plugin that acts on a low reference
rate yet. That mirrors v1 Vault's own shadow-mode posture: measure for a full
release before anything is allowed to act on the measurement.

**Unit granularity: dossier sections.** The spec's own unit list ("file
spans, dossier sections, spec sections, sibling receipts, contract clauses")
doesn't map onto first-class concepts that all exist in this codebase — but
`meter/dossier.py`'s own section-building already produces exactly this kind
of addressable, labelled unit (owned-file symbols, neighbour-file symbols,
contract text, sibling receipts). Attribution tracks exposure and reference
at that same granularity rather than inventing a second, finer-grained unit
system with no natural boundary to key it on.

**Detection.** Each exposed unit carries a "detection token" fixed at
exposure time — a file path, a contract id, a sibling node id. `record_references`
scans an agent's full transcript (every tool-call input, JSON-serialised, and
every assistant text block) once, for a literal appearance of each token.
This covers all three of the spec's detection paths in one pass: a
`Read`/`Grep`/`Edit` targeting a file serialises that file's path into the
transcript's `tool_use` input, and an explicit mention in prose is a literal
string match — both surface as "the token appears somewhere in the
transcript." It cannot distinguish "the agent actually used this" from "the
agent happened to write this string for an unrelated reason" — a
conservative direction: it can only ever overstate a reference rate, never
understate one enough to make pruning look justified when it wasn't.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from . import store


def build_exposures(*, owns: list[str], contracts_text: dict[str, str],
                     neighbour_paths: list[str], sibling_receipts: list[dict]) -> list[dict]:
    """Returns the addressable units a node's dossier exposed it to, each
    with the token `record_references` will look for. Owned files aren't
    included: a node owns and edits those regardless of whether the dossier
    mentioned them, so "was it referenced" is not a meaningful question for
    them the way it is for a neighbour file, a contract, or a sibling's
    receipt it didn't already know about."""
    exposures = []
    for path in neighbour_paths:
        exposures.append({"unit_type": "neighbour_file", "unit_label": path, "token": path})
    for contract_id in contracts_text:
        exposures.append({"unit_type": "contract", "unit_label": contract_id, "token": contract_id})
    for receipt in sibling_receipts:
        node = receipt.get("node")
        if node:
            exposures.append({"unit_type": "sibling_receipt", "unit_label": node, "token": node})
    return exposures


def _transcript_text_blob(transcript_path: str) -> str | None:
    path = Path(transcript_path)
    if not path.exists():
        return None
    parts: list[str] = []
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
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "text":
                            parts.append(str(block.get("text", "")))
                        elif block.get("type") == "tool_use":
                            parts.append(json.dumps(block.get("input", {}), default=str))
    except OSError:
        return None
    return "\n".join(parts)


def record_exposures(conn: sqlite3.Connection, *, run_id: str, node: str, agent_type: str,
                      exposures: list[dict]) -> None:
    try:
        store.record_attribution_exposures(conn, run_id=run_id, node=node,
                                            agent_type=agent_type, exposures=exposures)
    except Exception:
        pass


def record_references(conn: sqlite3.Connection, *, run_id: str, node: str,
                       transcript_path: str | None) -> int:
    """Scans the transcript once and marks every exposed-but-unreferenced
    unit whose token appears in it. Returns the count newly marked. Never
    raises, and does nothing (returns 0) if there's nothing to check."""
    try:
        exposures = store.get_exposures_for_node(conn, run_id=run_id, node=node)
        if not exposures:
            return 0
        blob = _transcript_text_blob(transcript_path) if transcript_path else None
        if not blob:
            return 0
        count = 0
        for exp in exposures:
            if exp["referenced"]:
                continue
            if exp["token"] and exp["token"] in blob:
                store.mark_attribution_referenced(
                    conn, run_id=run_id, node=node,
                    unit_type=exp["unit_type"], unit_label=exp["unit_label"])
                count += 1
        return count
    except Exception:
        return 0


def render_report_section(conn: sqlite3.Connection, run_id: str) -> str | None:
    try:
        rows = store.get_attribution_rates_for_run(conn, run_id)
    except Exception:
        return None
    if not rows:
        return None
    lines = ["Attribution: dossier-section reference rates for this run "
             "(measurement only — nothing here is ever pruned automatically):"]
    for r in rows:
        exposures = r["exposures"] or 0
        referenced = r["referenced"] or 0
        pct = (referenced / exposures * 100) if exposures else 0.0
        lines.append(f"  {r['agent_type']:<24} {r['unit_type']:<16} "
                      f"{referenced}/{exposures} ({pct:.0f}%)")
    return "\n".join(lines)
