"""M3 Clamp: bounded reads (3a) and output-side compression (3b) — meter-handoff.md
Section 4/M3.

`decide` (3b, `PostToolUse` + `updatedToolOutput`) and `decide_read` (3a,
`PreToolUse` + `updatedInput`) are the two entry points the router calls.
Neither raises: any exception is treated exactly like "decline to touch this"
(fail open — see the module's absolute rule about never removing an error
message, and 3a's own hard exclusions below).
"""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Any

from . import dossier as dossier_mod
from . import store
from .compressors import build as build_compressor
from .compressors import git as git_compressor
from .compressors import grep as grep_compressor
from .compressors import tests as tests_compressor

_GENERATED_PATH_RE = re.compile(
    r"(^|/)(dist|build|generated|out|\.next|\.nuxt|vendor|node_modules|target)(/|$)"
)
_GENERATED_SUFFIX_RE = re.compile(r"\.(min\.js|min\.css|generated\.\w+|pb2\.py|pb\.go)$")

_DEFAULT_MARGIN = 20

_TEST_RUNNER_RE = re.compile(
    r"\b(pytest|py\.test|jest|mocha|go test|cargo test|rspec|phpunit|"
    r"npm (run )?test|yarn test|pnpm test|dotnet test|gradle test|mvn test)\b",
    re.IGNORECASE,
)
_BUILD_RE = re.compile(
    r"\b(npm (ci|install)|yarn install|pnpm install|pip install|make\b|"
    r"cargo build|docker build|tsc\b|webpack|mvn (install|package)|"
    r"gradle build|go build)\b",
    re.IGNORECASE,
)


def _select_compressor(tool_name: str, command: str):
    if tool_name == "Grep":
        return grep_compressor.compress
    if tool_name != "Bash":
        return None
    if git_compressor.is_diff_command(command):
        return git_compressor.compress
    if _TEST_RUNNER_RE.search(command or ""):
        return tests_compressor.compress
    if _BUILD_RE.search(command or ""):
        return build_compressor.compress
    return None


def decide(*, tool_name: str, tool_input: dict, output: str, byte_threshold: int,
           run_dir: Path | None, tool_use_id: str | None) -> str | None:
    """Returns a replacement string for `updatedToolOutput`, or None to leave
    the tool's output untouched."""
    if not output:
        return None

    command = str(tool_input.get("command") or "") if isinstance(tool_input, dict) else ""
    compressor = _select_compressor(tool_name, command)
    if compressor is None:
        return None

    try:
        compressed = compressor(output, byte_threshold=byte_threshold)
    except Exception:
        return None  # fail open: a compressor bug must never touch the output

    if compressed is None:
        return None

    pointer = _spool_original(output, run_dir, tool_use_id)
    if pointer:
        compressed = compressed + f"\n\n[meter: full output ({len(output.encode('utf-8', errors='replace')):,} bytes) at {pointer}]"
    return compressed


def _spool_original(output: str, run_dir: Path | None, tool_use_id: str | None) -> str | None:
    if run_dir is None:
        return None
    try:
        tool_output_dir = run_dir / "meter" / "tool-output"
        tool_output_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(output.encode("utf-8", errors="replace")).hexdigest()[:12]
        name = f"{tool_use_id or int(time.time() * 1000)}-{digest}.txt"
        path = tool_output_dir / name
        path.write_text(output, encoding="utf-8")
        return str(path)
    except OSError:
        return None


def _is_generated_shaped(rel_path: str) -> bool:
    return bool(_GENERATED_PATH_RE.search(rel_path)) or bool(_GENERATED_SUFFIX_RE.search(rel_path))


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[list[int]]:
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def _fully_covered(target: tuple[int, int], existing: list[tuple[int, int]]) -> bool:
    t_start, t_end = target
    for m_start, m_end in _merge_intervals(existing):
        if m_start <= t_start and m_end >= t_end:
            return True
    return False


def decide_read(*, conn: Any, repo_root: Path, run_dir: Path, run_id: str, node_id: str,
                 agent_id: str, rel_path: str, requested_offset: int | None,
                 requested_limit: int | None, full_read_threshold: int,
                 margin: int = _DEFAULT_MARGIN) -> dict | None:
    """Returns `{"kind": "rewrite", "offset": int, "limit": int, "reason": str}`,
    `{"kind": "deny", "reason": str}`, or `None` to leave the Read untouched.

    Hard exclusions, per meter-handoff.md Section 4/M3a, checked in order —
    each one is a reason to return `None` immediately, never a maybe:
    """
    if requested_offset is not None or requested_limit is not None:
        return None  # the agent already asked for a bounded read; not ours to touch

    if dossier_mod.is_binary_shaped(rel_path) or _is_generated_shaped(rel_path):
        return None

    task = dossier_mod.load_task(run_dir, node_id)
    owns = set((task or {}).get("owns") or []) if task else set()
    if rel_path in owns:
        return None  # never narrow a read of a file this node owns — it's editing that file

    abs_path = repo_root / rel_path
    try:
        if not abs_path.is_file():
            return None
        with abs_path.open("r", encoding="utf-8", errors="replace") as fh:
            line_count = sum(1 for _ in fh)
    except OSError:
        return None
    if line_count <= full_read_threshold:
        return None

    spans = store.get_dossier_spans(conn, run_id=run_id, node=node_id, file=rel_path)
    if not spans:
        return None  # the index has no opinion on this file

    merged_start = max(1, min(s for s, _ in spans) - margin)
    merged_end = min(line_count, max(e for _, e in spans) + margin)

    already = store.get_read_spans(conn, agent_id=agent_id, file=rel_path)
    if already and _fully_covered((merged_start, merged_end), already):
        return {"kind": "deny",
                "reason": f"meter: you already read {rel_path} lines {merged_start}-{merged_end} "
                          f"earlier in this session — re-reading it won't show anything new."}

    try:
        store.record_read_span(conn, agent_id=agent_id, file=rel_path, start=merged_start,
                                end=merged_end)
    except Exception:
        return None  # fail open: don't hand out a rewrite we failed to record as delivered

    return {"kind": "rewrite", "offset": merged_start, "limit": merged_end - merged_start + 1,
            "reason": f"meter: bounded to the {merged_end - merged_start + 1} line(s) "
                      f"({merged_start}-{merged_end}) the index found relevant to {node_id}"}
