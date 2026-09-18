"""Test-runner output compression (meter-handoff.md Section 4/M3, item 3a of "3b").

Framework-agnostic on purpose: rather than parsing pytest/jest/go-test/etc.
output structurally (which breaks the moment a project's runner or config
changes), this looks for cross-framework textual signals — a trailing summary
line, failure-block openers, and a tail — and falls back to doing nothing the
moment it isn't confident those signals are present. A pass-through on an
unrecognised format costs tokens; a wrongly-dropped failure block costs a
rework loop, which is far more expensive (see the module's absolute rule).
"""

from __future__ import annotations

import re

_SUMMARY_RE = re.compile(
    r"^\s*(\d+\s+(passed|failed|error|errors|skipped|pending)\b.*|"
    r"(tests?|suites?)\s*:\s*\d+.*|"
    r"(ok|FAIL)\s+\S+\s+[\d.]+s.*)",
    re.IGNORECASE,
)

_FAILURE_START_RE = re.compile(
    r"^\s*(FAIL\b|FAILED\b|Error\b|ERROR\b|AssertionError|Exception\b|"
    r"Traceback \(most recent call last\)|✗|×|--- FAIL)",
)

# A line that plausibly starts a *new*, unrelated block — used to bound how far
# a failure block's "context" extends, so one failure doesn't swallow the rest
# of the log.
_NEW_BLOCK_RE = re.compile(r"^\s*(PASS\b|ok\s+\S|✓|---\s+PASS|\S.*::\S)")

_TAIL_LINES = 40
_FAILURE_BLOCK_MAX_LINES = 60


def compress(output: str, *, byte_threshold: int = 8000) -> str | None:
    data = output.encode("utf-8", errors="replace")
    if len(data) <= byte_threshold:
        return None  # under threshold: never touched, regardless of exit code

    lines = output.splitlines()
    if len(lines) < 5:
        return None  # too short to be worth the risk of misparsing

    keep = set()

    summary_lines = [i for i, line in enumerate(lines) if _SUMMARY_RE.match(line)]
    keep.update(summary_lines)

    failure_starts = [i for i, line in enumerate(lines) if _FAILURE_START_RE.match(line)]
    for start in failure_starts:
        end = min(len(lines), start + _FAILURE_BLOCK_MAX_LINES)
        for j in range(start + 1, end):
            if _NEW_BLOCK_RE.match(lines[j]):
                end = j
                break
        keep.update(range(start, end))

    keep.update(range(max(0, len(lines) - _TAIL_LINES), len(lines)))

    if not failure_starts and not summary_lines:
        # No recognisable structure at all — don't guess at what's noise.
        return None

    kept_fraction = len(keep) / len(lines)
    if kept_fraction > 0.8:
        return None  # nothing meaningful would be saved; not worth the risk

    return _render(lines, keep)


def _render(lines: list[str], keep: set[int]) -> str:
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
        elided = i - start
        out.append(f"… [meter: {elided} line(s) elided — passing-test output] …")
    return "\n".join(out)
