"""M5 Vault, shadow mode only (meter-handoff.md Section 4/M5).

**Scope.** `vault.mode` in the spec is `off | shadow | rework_only | full`.
This implements the key derivation, content-addressed patch store, and
`shadow` mode: every successfully completed node with a valid receipt is
checked against the vault by its semantic key, and — if a match already
exists — the match is diffed against what the node's own worker actually
produced, recorded, and reported. **The worker always runs regardless; this
module never applies a patch and never skips a spawn.**

`rework_only` and `full` (real replay) are NOT implemented. Real replay means
denying a `Task` spawn and handing back an already-authored patch "exactly as
if a worker had produced it" — that needs the orchestrator to understand a
vault-gate denial specially (meter-handoff.md's own file layout section
anticipates a `skills/orchestrator/SKILL.md` change for this), which is a
bigger and riskier change than anything else in this module. More to the
point: the spec's own promotion path is "shadow until 50 runs show zero
would-be-wrong replays, **then** rework_only" — building real replay before
shadow mode has actually produced that evidence would violate the spec's own
ordering, not just save engineering time. Ship shadow mode, let it run, look
at `/dag-vault shadow-agreement`, then build the rest.

**Key derivation** follows Section 4/M5's formula:

    key = sha256(norm(description) || norm(acceptance_criteria) ||
                 norm(contract_text) || sorted(input_span_hashes) ||
                 model_tier || agent_type || plugin_version || schema_version)

`norm()` is exhaustively unit-tested (tests/test_vault.py) because, per the
spec, "every false-positive collision is a wrong patch." `input_span_hashes`
approximates "sorted(hash(span) for span in dossier.input_spans)": a hash of
each owned file's full content (owned files are never bounded, so their
whole content is the input) plus an identity hash of each neighbour span
`meter/dossier.py` recorded (file, start, end — not the span's content, to
avoid re-reading every neighbour file a second time). This is an
approximation, documented as such: it will not detect a neighbour file's
*content* changing between two otherwise-identical tasks. The owned-file
whole-content hash is exact and covers the primary case.

**MinHash near-hit warm start (Section 4/M5's second mechanism) is NOT
implemented.** It's a separate, sizeable feature (shingling, banding, LSH)
whose entire value is downstream of shadow mode already proving the exact-key
path is trustworthy. `minhash` the table exists in the schema (Section 6.3)
but nothing here writes to it yet.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import config as config_mod
from . import dossier as dossier_mod
from . import gitutil
from . import store

VAULT_SCHEMA_VERSION = 1

_RUN_ID_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}-[0-9a-f]{4,}\b")
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_ID_RE = re.compile(r"\b[A-Z]{1,4}-\d+\b")  # T-003, AC-02, C-01 — uppercase only, so
                                             # lowercase words like "utf-8" never match
_ORDINAL_RE = re.compile(r"\b\d+(st|nd|rd|th)\b", re.IGNORECASE)
_BULLET_RE = re.compile(r"^\s*[-*]\s+")


def norm(text: str) -> str:
    """Lowercases, strips IDs/dates/run-ids/ordinal markers, sorts bullet
    lists, and collapses whitespace — a pure function with an exhaustive test
    table (tests/test_vault.py), because a false-positive collision here
    hands a node the wrong patch."""
    if not text:
        return ""

    t = _RUN_ID_RE.sub("<run>", text)
    t = _DATE_RE.sub("<date>", t)
    t = _ID_RE.sub("<id>", t)
    t = t.lower()
    t = _ORDINAL_RE.sub("<ord>", t)

    lines = t.split("\n")
    out_lines: list[str] = []
    i = 0
    while i < len(lines):
        if _BULLET_RE.match(lines[i]):
            group = []
            while i < len(lines) and (_BULLET_RE.match(lines[i]) or
                                       (group and not lines[i].strip())):
                if lines[i].strip():
                    group.append(_BULLET_RE.sub("", lines[i]).strip())
                i += 1
            out_lines.extend(sorted(group))
        else:
            out_lines.append(lines[i])
            i += 1

    return re.sub(r"\s+", " ", "\n".join(out_lines)).strip()


def compute_key(*, description: str, acceptance_criteria: list[str], contract_text: str,
                 input_span_hashes: list[str], model_tier: str, agent_type: str,
                 plugin_version: str) -> str:
    parts = [
        norm(description),
        norm("\n".join(acceptance_criteria)),
        norm(contract_text),
        "|".join(sorted(input_span_hashes)),
        model_tier or "",
        agent_type or "",
        plugin_version or "",
        str(VAULT_SCHEMA_VERSION),
    ]
    basis = "\x1f".join(parts)  # unit separator: won't plausibly appear in prose
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def artifact_path(plugin_data_dir: Path, patch_sha: str) -> Path:
    return plugin_data_dir / "artifacts" / patch_sha[:2] / f"{patch_sha}.patch"


def store_patch(plugin_data_dir: Path, patch_text: str) -> str:
    patch_sha = hashlib.sha256(patch_text.encode("utf-8", errors="replace")).hexdigest()
    path = artifact_path(plugin_data_dir, patch_sha)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(patch_text, encoding="utf-8")
    except OSError:
        pass
    return patch_sha


def load_patch(plugin_data_dir: Path, patch_sha: str) -> str | None:
    try:
        return artifact_path(plugin_data_dir, patch_sha).read_text(encoding="utf-8")
    except OSError:
        return None


def _plugin_version(repo_root: Path) -> str:
    try:
        data = json.loads((repo_root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        return str(data.get("version", ""))
    except (OSError, json.JSONDecodeError):
        return ""


def _input_span_hashes(conn: sqlite3.Connection, repo_root: Path, run_id: str, node_id: str,
                        task: dict) -> list[str]:
    hashes: list[str] = []
    for rel_path in (task.get("owns") or []):
        if not isinstance(rel_path, str):
            continue
        text = dossier_mod.read_file(repo_root, rel_path)
        if text is not None:
            hashes.append(hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest())
    for file, start, end in store.get_all_dossier_spans(conn, run_id=run_id, node=node_id):
        hashes.append(hashlib.sha256(f"{file}:{start}-{end}".encode("utf-8")).hexdigest())
    return hashes


def _git_show_patch(worktree: str, commit: str, integration_branch: str | None = None) -> str | None:
    """Prefers the task's full commit range via `gitutil.resolve_diff_range`
    (merge-base against the run's integration branch). Falls back to
    `git show <commit>` — the diff introduced by that one commit only — when
    the range can't be established (no `integration_branch`, or its
    merge-base doesn't resolve). The fallback's effect is bounded to
    shadow-mode measurement quality (an under-captured patch can only make a
    genuine match register as a disagreement, never the reverse) — it does
    not affect anything that changes pipeline behaviour, since shadow mode
    never applies a patch."""
    diff_range = gitutil.resolve_diff_range(worktree=worktree, commit=commit,
                                             integration_branch=integration_branch)
    if diff_range is not None:
        base, head = diff_range
        try:
            result = subprocess.run(
                ["git", "-C", worktree, "diff", base, head],
                capture_output=True, text=True, timeout=10, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            result = None
        if result is not None and result.returncode == 0 and result.stdout.strip():
            return result.stdout
        # range resolved but produced nothing usable — fall through below

    try:
        result = subprocess.run(
            ["git", "-C", worktree, "show", "--format=", commit],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 and result.stdout.strip() else None


def process_completed_receipt(conn: sqlite3.Connection, plugin_data_dir: Path, *,
                               repo_root: Path, run_dir: Path, run_id: str, node_id: str,
                               agent_type: str, receipt: dict,
                               model_tier: str | None = None) -> dict[str, Any] | None:
    """The shadow-mode entry point, called once per successfully-completed
    node with a schema-valid receipt (see meter/router.py). Never raises,
    never blocks, never applies anything — see the module docstring. Returns
    a summary dict for logging, or None when there was nothing to check
    (no task record, no worktree/commit, receipt not `done`).

    `model_tier` should be the node's actual model tier (e.g. from
    `ledger.tier_from_model`), distinct from `agent_type` — see the key
    formula's module docstring. Omitting it falls back to `agent_type`, the
    same value the key used before this distinction existed."""
    try:
        return _process(conn, plugin_data_dir, repo_root=repo_root, run_dir=run_dir,
                         run_id=run_id, node_id=node_id, agent_type=agent_type, receipt=receipt,
                         model_tier=model_tier)
    except Exception:
        return None


def _process(conn: sqlite3.Connection, plugin_data_dir: Path, *, repo_root: Path, run_dir: Path,
             run_id: str, node_id: str, agent_type: str, receipt: dict,
             model_tier: str | None = None) -> dict[str, Any] | None:
    if receipt.get("status") != "done":
        return None
    worktree = receipt.get("worktree")
    commit = receipt.get("commit")
    if not worktree or not commit or commit == "none":
        return None

    task = dossier_mod.load_task(run_dir, node_id)
    if task is None:
        return None

    description = str(task.get("title") or "")
    ac_list = [a if isinstance(a, str) else str(a.get("description", ""))
               for a in (task.get("acceptance_criteria") or [])]
    contracts_text = dossier_mod.load_contracts(run_dir, task.get("contracts") or [])
    contract_text = "\n".join(contracts_text.values())
    span_hashes = _input_span_hashes(conn, repo_root, run_id, node_id, task)
    plugin_version = _plugin_version(repo_root)

    resolved_model_tier = model_tier or agent_type

    key = compute_key(description=description, acceptance_criteria=ac_list,
                       contract_text=contract_text, input_span_hashes=span_hashes,
                       model_tier=resolved_model_tier, agent_type=agent_type,
                       plugin_version=plugin_version)

    patch_text = _git_show_patch(worktree, commit, integration_branch=f"dag/{run_id}")
    if patch_text is None:
        return None

    existing = store.get_vault_entry(conn, key)
    if existing is None:
        patch_sha = store_patch(plugin_data_dir, patch_text)
        verification = receipt.get("verification") or {}
        store.insert_vault_entry(conn, {
            "key": key, "repo": config_mod.repo_fingerprint(repo_root), "agent_type": agent_type,
            "model_tier": resolved_model_tier, "patch_sha": patch_sha, "receipt_json": json.dumps(receipt),
            "verify_cmd": verification.get("cmd"), "verify_digest": verification.get("digest"),
            "tokens_spent": None, "created_at": int(time.time()),
            "schema_v": VAULT_SCHEMA_VERSION,
        })
        return {"node": node_id, "outcome": "seeded", "key": key}

    stored_patch = load_patch(plugin_data_dir, existing["patch_sha"])
    agreed = stored_patch is not None and stored_patch == patch_text
    store.record_shadow_check(conn, run_id=run_id, node=node_id, key=key, agreed=agreed)
    store.bump_vault_hit(conn, key)
    return {"node": node_id, "outcome": "shadow_agreed" if agreed else "shadow_disagreed",
            "key": key}


def stats(conn: sqlite3.Connection, plugin_data_dir: Path) -> dict[str, Any]:
    """Backs `/dag-vault stats`. Disk usage is measured directly rather than
    inferred from row counts, since a purge can desync the two if it's ever
    interrupted partway."""
    totals = store.get_vault_totals(conn)
    shadow = store.get_vault_shadow_stats(conn)
    artifacts_dir = plugin_data_dir / "artifacts"
    disk_bytes = 0
    if artifacts_dir.is_dir():
        for path in artifacts_dir.rglob("*.patch"):
            try:
                disk_bytes += path.stat().st_size
            except OSError:
                pass
    return {**totals, **shadow, "disk_bytes": disk_bytes}


def purge(conn: sqlite3.Connection, plugin_data_dir: Path, *, max_gb: float) -> dict[str, Any]:
    """LRU eviction under `meter.modules.vault.max_gb` (meter-handoff.md
    Section 4/M5: "There is no time-based expiry; there is a size cap with
    LRU eviction and a `/dag-vault purge` command"). Evicts the entry with
    the oldest `COALESCE(last_hit_at, created_at)` first — an entry that's
    never been hit again since it was seeded is the correct thing to evict
    before one that keeps proving useful."""
    max_bytes = int(max_gb * (1024 ** 3))
    removed = 0

    def _current_size() -> int:
        artifacts_dir = plugin_data_dir / "artifacts"
        if not artifacts_dir.is_dir():
            return 0
        return sum(p.stat().st_size for p in artifacts_dir.rglob("*.patch") if p.is_file())

    size = _current_size()
    for key, patch_sha in store.list_vault_entries_by_age(conn):
        if size <= max_bytes:
            break
        store.delete_vault_entry(conn, key)
        if not store.vault_patch_still_referenced(conn, patch_sha):
            path = artifact_path(plugin_data_dir, patch_sha)
            try:
                file_size = path.stat().st_size
                path.unlink()
                size -= file_size
            except OSError:
                pass
        removed += 1

    return {"removed": removed, "remaining_bytes": _current_size()}
