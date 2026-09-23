"""Shared git helpers used by more than one meter module.

`resolve_diff_range` centralises the "what's the true base of this task's
diff" problem that both Sieve (M11, meter/sieve.py) and Vault (M5,
meter/vault.py) independently ran into: task-worker's machine trailer names
only a final `COMMIT` sha, with no base sha recorded alongside it
(agents/task-worker.md), so a worker that made more than one commit for its
task has no reliable range to diff against without extra information.

The fix doesn't require changing that trailer contract: workers run in an
isolated worktree "cut from the branch's current tip" (README) and never
rebase onto a moving branch, so the merge-base between the worker's final
commit and the run's integration branch (`dag/<run-id>`) IS that tip.
"""

from __future__ import annotations

import subprocess

_GIT_TIMEOUT_S = 8


def resolve_diff_range(*, worktree: str, commit: str,
                        integration_branch: str | None) -> tuple[str, str] | None:
    """Returns `(base_sha, commit)` spanning every commit the worker made for
    this task, or `None` when that range can't be established (no
    `integration_branch` given, the branch is unreachable from this worktree,
    or a git failure). Callers must treat `None` as "the full range is
    unverified" — never silently narrow to a single commit and claim full
    coverage instead."""
    if not integration_branch:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", worktree, "merge-base", commit, integration_branch],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    base = result.stdout.strip()
    return (base, commit) if base else None
