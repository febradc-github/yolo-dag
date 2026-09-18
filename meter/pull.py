"""M10 Pull: manifest-based context delivery (meter-v2-handoff.md Section 3,
M10).

**Class: verified. Default: DISABLED** (`meter.modules.pull.enabled = False`,
overriding the spec's own sample config, which shows it enabled).

**Why disabled by default.** The spec's own release plan (Section 6, R3)
says Pull "ships only if M14's measured reference rate justifies it" — that
measurement needs a full release of real run data against M14 Attribution,
which does not exist yet (no live runs have happened against this
implementation). Building the mechanism now and leaving it off is the same
posture already applied to Vault's real-replay modes and Intern's codebook
mode: available the moment the gating evidence exists, never defaulted on
before it does.

**Mechanism.** Instead of injecting a dossier's full body at `SubagentStart`,
write each of its sections to its own file under
`.dag/runs/<run>/meter/dossiers/<node>/<section-id>.md` and inject a short
manifest — each section's id, approximate size, and a one-line description —
in place of the content. An agent pulls what it needs with an ordinary
`Read`; `meter/router.py` tracks which sections actually get read.

**Cost accounting, honestly — this is why net-saving takes measured inputs,
not constants.** The spec is explicit that a pull's true cost is the
round-trip's *entire re-sent context* at that moment, which varies per node
and per turn and cannot be estimated analytically from here. `net_saving`
therefore takes `avg_roundtrip_tokens` as a parameter the caller must supply
from real measured data (e.g. the resumed agent's per-round input tokens
around a pull event, from `meter/ledger.py`'s existing round tracking) —
this module never invents a plausible-looking constant for it.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from . import store
from .index import tokens as tokens_mod

_SECTION_ID_RE = re.compile(r"[^a-z0-9]+")


def section_id_for(label: str) -> str:
    return _SECTION_ID_RE.sub("_", label.lower()).strip("_") or "section"


def build_manifest_sections(sections: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """`sections` is the same (label, markdown) list meter/dossier.py's
    `_assemble_sections` already produces. Returns one entry per section
    (skipping the header, which is never optional — see `write_sections`)
    with a stable id, its text, and an estimated token size."""
    out = []
    seen_ids: set[str] = set()
    for label, text in sections:
        if label == "header":
            continue
        base_id = section_id_for(label)
        section_id = base_id
        n = 1
        while section_id in seen_ids:
            n += 1
            section_id = f"{base_id}_{n}"
        seen_ids.add(section_id)
        out.append({"id": section_id, "label": label, "text": text,
                    "tokens_est": tokens_mod.estimate_tokens(text)})
    return out


def write_sections(run_dir: Path, node_id: str, sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Writes each section to its own file, returns the same list with a
    `path` key added. Never raises: a section that fails to write is
    dropped from the manifest rather than referencing a file that isn't
    there — a dangling manifest entry is worse than a shorter one."""
    safe_node = node_id.replace("/", "_")
    section_dir = run_dir / "meter" / "dossiers" / safe_node
    written = []
    try:
        section_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return []
    for section in sections:
        path = section_dir / f"{section['id']}.md"
        try:
            path.write_text(section["text"], encoding="utf-8")
        except OSError:
            continue
        written.append({**section, "path": str(path)})
    return written


def render_manifest(node_id: str, sections: list[dict[str, Any]]) -> str:
    lines = [f"Context available for {node_id}, by id:"]
    for s in sections:
        lines.append(f"  {s['id']}  {s['label']} (~{s['tokens_est']:,} tok)")
    if sections:
        section_dir = Path(sections[0]["path"]).parent
        lines.append(f"Fetch with: Read {section_dir}/<id>.md")
    return "\n".join(lines)


def net_saving(*, section_tokens: int, total_exposures: int, pulled_count: int,
                avg_roundtrip_tokens: float) -> float:
    """Gross tokens saved by never pushing a section inline, minus the
    round-trip overhead for every pull that did happen. A never-pulled
    section costs ~0 beyond its one manifest line; a pulled one costs its
    own content plus the round-trip overhead, instead of the push
    baseline's flat per-exposure cost."""
    push_baseline = section_tokens * total_exposures
    pull_cost = (section_tokens + avg_roundtrip_tokens) * pulled_count
    return push_baseline - pull_cost


def recommend(conn: sqlite3.Connection, *, promote_threshold: float = 0.8,
              min_samples: int = 20) -> list[dict[str, Any]]:
    """Per (agent_type, section_id) recommendation from accumulated
    `pull_sections` data: `promote` (pulled often enough it should just be
    pushed), `revert_to_push` (net saving isn't positive — the round-trip
    overhead is eating the benefit), or `keep_pull` (working as intended).
    `avg_roundtrip_tokens` isn't in this table yet (see module docstring —
    it needs real Ledger round data this repo has no live runs to supply),
    so this recommends purely on pull *rate* against `promote_threshold` for
    now; the net-saving formula above is ready to fold in once that data
    exists, deliberately not before."""
    try:
        rows = store.get_pull_stats(conn, min_samples=min_samples)
    except Exception:
        return []
    out = []
    for row in rows:
        total = row["total"] or 0
        pulled = row["pulled"] or 0
        rate = (pulled / total) if total else 0.0
        if rate >= promote_threshold:
            action = "promote"
        else:
            action = "keep_pull"
        out.append({"agent_type": row["agent_type"], "section_id": row["section_id"],
                    "pull_rate": round(rate, 3), "samples": total, "action": action})
    return out
