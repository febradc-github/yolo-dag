"""`git diff` / `git status` output compression (meter-handoff.md Section 4/M3).

Keeps the stat summary and hunk headers, drops the hunk bodies — the actual
added/removed lines, which is where nearly all the bytes are on a large diff
and which the agent can re-fetch (`git show`, a bounded `Read`) if it turns
out to need them. Never touches anything under the byte threshold, and never
touches `git status` porcelain output, which is already line-per-file and
rarely large enough to be worth the risk.
"""

from __future__ import annotations

import re

_HUNK_HEADER_RE = re.compile(r"^@@ .* @@")
_FILE_HEADER_RE = re.compile(r"^(diff --git |index |--- |\+\+\+ )")
_STAT_LINE_RE = re.compile(r"^\s*\S.*\|\s*\d+\s*[+-]*\s*$")  # "path/file.py | 12 +++---"
_STAT_SUMMARY_RE = re.compile(r"^\s*\d+ files? changed")


def is_diff_command(command: str) -> bool:
    return bool(re.search(r"\bgit\s+diff\b", command or ""))


def compress(output: str, *, byte_threshold: int = 8000) -> str | None:
    data = output.encode("utf-8", errors="replace")
    if len(data) <= byte_threshold:
        return None

    lines = output.splitlines()
    kept: list[str] = []
    elided_hunk_lines = 0
    total_elided = 0
    in_hunk = False

    for line in lines:
        if _FILE_HEADER_RE.match(line) or _HUNK_HEADER_RE.match(line):
            if elided_hunk_lines:
                kept.append(f"  … [meter: {elided_hunk_lines} hunk line(s) elided] …")
                elided_hunk_lines = 0
            kept.append(line)
            in_hunk = _HUNK_HEADER_RE.match(line) is not None
            continue
        if _STAT_LINE_RE.match(line) or _STAT_SUMMARY_RE.match(line):
            kept.append(line)
            continue
        if in_hunk:
            elided_hunk_lines += 1
            total_elided += 1
            continue
        kept.append(line)

    if elided_hunk_lines:
        kept.append(f"  … [meter: {elided_hunk_lines} hunk line(s) elided] …")

    if total_elided == 0:
        return None  # nothing was actually a hunk body; don't claim a saving that isn't real

    return "\n".join(kept)
