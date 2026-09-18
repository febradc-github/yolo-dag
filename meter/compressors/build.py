"""Build/install log compression (meter-handoff.md Section 4/M3).

Keeps every line that looks like an error or a warning, plus the tail (where
the final result usually is), and drops the rest — dependency-resolution
chatter, progress bars, "up to date" noise. If nothing matches the
error/warning patterns, this is conservative: the tail alone is very likely
insufficient to represent a large build log, so it declines to compress
rather than risk hiding the one line that mattered.
"""

from __future__ import annotations

import re

_SIGNAL_RE = re.compile(r"\b(error|err!|warning|warn\b|fatal|failed|failure)\b", re.IGNORECASE)
_TAIL_LINES = 40


def compress(output: str, *, byte_threshold: int = 8000) -> str | None:
    data = output.encode("utf-8", errors="replace")
    if len(data) <= byte_threshold:
        return None

    lines = output.splitlines()
    if len(lines) < 5:
        return None

    signal_idx = {i for i, line in enumerate(lines) if _SIGNAL_RE.search(line)}
    if not signal_idx:
        return None  # no recognisable signal; don't guess at what else matters

    keep = set(signal_idx)
    keep.update(range(max(0, len(lines) - _TAIL_LINES), len(lines)))

    if len(keep) / len(lines) > 0.8:
        return None

    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if i in keep:
            out.append(lines[i])
            i += 1
            continue
        start = i
        while i < n and i not in keep:
            i += 1
        out.append(f"… [meter: {i - start} line(s) elided — no error/warning signal] …")
    return "\n".join(out)
