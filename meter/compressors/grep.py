"""`Grep` output compression (meter-handoff.md Section 4/M3): group by file, cap
hits per file, and state the count that was elided. Lines that don't match the
`path:line:content` shape (headers, separators, a trailing match count) pass
through unchanged in place — this only ever caps *repetition* within one file,
never removes a line it can't attribute to a file.
"""

from __future__ import annotations

import re

_MATCH_RE = re.compile(r"^([^\n:]+):(\d+):(.*)$")


def compress(output: str, *, byte_threshold: int = 8000, max_per_file: int = 20) -> str | None:
    data = output.encode("utf-8", errors="replace")
    if len(data) <= byte_threshold:
        return None

    lines = output.splitlines()
    per_file_count: dict[str, int] = {}
    out: list[str] = []
    elided_total = 0
    pending_elided: dict[str, int] = {}

    def flush_pending() -> None:
        nonlocal pending_elided
        for path, count in pending_elided.items():
            out.append(f"  … [meter: {count} more match(es) in {path} elided] …")
        pending_elided = {}

    for line in lines:
        m = _MATCH_RE.match(line)
        if not m:
            flush_pending()
            out.append(line)
            continue
        path = m.group(1)
        per_file_count[path] = per_file_count.get(path, 0) + 1
        if per_file_count[path] <= max_per_file:
            out.append(line)
        else:
            pending_elided[path] = pending_elided.get(path, 0) + 1
            elided_total += 1

    flush_pending()

    if elided_total == 0:
        return None
    return "\n".join(out)
