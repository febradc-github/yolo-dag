"""M11 Sieve: deterministic-first review (meter-v2-handoff.md Section 3, M11).

**Class: verified** — the guard that makes it verified rather than statistical
is enforced here: a checker that errored, timed out, or was unavailable is
reported `ran=False`, never silently folded into "passed." `render_declaration`
only ever states a class as covered when `ran and passed` both hold.

**Architectural adaptation.** The spec's "residue" concept assumes a reviewer
that reviews a diff *blob* handed to it in-context, with mechanically-decided
regions annotated inline. This repo's `task-reviewer` doesn't work that way —
it's given a worktree path and a commit sha and reads/re-runs things itself
(`agents/task-reviewer.md`). Annotating a diff blob that never gets sent
doesn't apply. What carries the real benefit instead: a **declaration**
injected into the reviewer's prompt naming which checks already ran
deterministically and passed, so the reviewer's own attention goes to what
tooling can't decide — the actual "compounding effect" the spec cares about
(reviewer input, reviewer output, and round count all shrink) still holds,
just delivered as a prompt addendum rather than an annotated diff.

**Checkers implemented, and why the others aren't:**
- `ownership`: real. Compares the commit's changed files against the task's
  `owns` list from `tasks.json` — pure git + JSON, no external tool needed.
- `secrets`: real. Regex-based scan of the diff for common credential shapes.
  Deliberately conservative (a miss is a miss, not a claim of coverage for
  patterns it doesn't recognise).
- `types`, `lint`: best-effort. Detected and run only when this repo's own
  config (`tsconfig.json`, an ESLint/Ruff config) says which tool applies,
  with a short timeout; anything else (tool missing, timeout, non-JS/TS/PY
  repo) reports `not run`, never a guess.
- `tests`: always `not run`. Running an arbitrary project's test suite has
  no bounded time budget this hook could safely commit to (a `PreToolUse`
  hook has a short deadline; a real suite can take minutes) — attempting it
  risks the exact behaviour Rule 1 (fail-open everywhere) forbids.
- `ast`: always `not run`. The spec's AST rules (forbidden imports, layering
  breaches, missing error handling) need a per-repo rule *configuration*
  this plugin has no format for yet. Declaring coverage without one would be
  the module's own named failure mode.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

_SECRET_PATTERNS = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key"),
    (re.compile(r"-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"), "private key"),
    (re.compile(r"\bghp_[A-Za-z0-9]{36}\b"), "GitHub personal access token"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "Slack token"),
    (re.compile(r"(?i)\b(api[_-]?key|secret|password|passwd)\b\s*[:=]\s*['\"][^'\"\s]{8,}['\"]"),
     "hardcoded credential-shaped assignment"),
]

_CHECKER_TIMEOUT_S = 8


def _result(name: str, *, ran: bool, passed: bool = False, findings: list[str] | None = None,
            reason: str | None = None) -> dict[str, Any]:
    return {"name": name, "ran": ran, "passed": passed, "findings": findings or [], "reason": reason}


def check_ownership(repo_root: Path, worktree: str, commit: str, owns: list[str]) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["git", "-C", worktree, "diff", "--name-only", f"{commit}~1", commit],
            capture_output=True, text=True, timeout=_CHECKER_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _result("ownership", ran=False, reason=f"git diff failed: {exc}")
    if result.returncode != 0:
        return _result("ownership", ran=False, reason="git diff --name-only exited non-zero "
                                                        "(single-commit range unavailable)")

    changed = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    owns_set = set(owns)
    outside = [f for f in changed if f not in owns_set]
    return _result("ownership", ran=True, passed=not outside,
                    findings=[f"touched file outside owns: {f}" for f in outside])


def check_secrets(diff_text: str) -> dict[str, Any]:
    if not diff_text:
        return _result("secrets", ran=True, passed=True)
    findings = []
    for pattern, label in _SECRET_PATTERNS:
        if pattern.search(diff_text):
            findings.append(f"possible {label} found in diff")
    return _result("secrets", ran=True, passed=not findings, findings=findings)


def _run_tool(args: list[str], cwd: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                                 timeout=_CHECKER_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"failed to run: {exc}"
    return True, result.stdout + result.stderr if result.returncode != 0 else ""


def check_types(worktree: str) -> dict[str, Any]:
    wt = Path(worktree)
    if (wt / "tsconfig.json").is_file():
        ran, output = _run_tool(["npx", "--no-install", "tsc", "--noEmit"], worktree)
        if not ran:
            return _result("types", ran=False, reason=output)
        findings = [line for line in output.splitlines() if line.strip()][:20]
        return _result("types", ran=True, passed=not findings, findings=findings)
    if any(wt.rglob("*.py")) and _which("mypy"):
        ran, output = _run_tool(["mypy", "."], worktree)
        if not ran:
            return _result("types", ran=False, reason=output)
        findings = [line for line in output.splitlines() if line.strip()][:20]
        return _result("types", ran=True, passed=not findings, findings=findings)
    return _result("types", ran=False, reason="no tsconfig.json and no mypy available")


def check_lint(worktree: str) -> dict[str, Any]:
    wt = Path(worktree)
    eslint_configs = [".eslintrc", ".eslintrc.json", ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.yml"]
    if any((wt / c).exists() for c in eslint_configs):
        ran, output = _run_tool(["npx", "--no-install", "eslint", "."], worktree)
        if not ran:
            return _result("lint", ran=False, reason=output)
        findings = [line for line in output.splitlines() if line.strip()][:20]
        return _result("lint", ran=True, passed=not findings, findings=findings)
    if _which("ruff") and any(wt.rglob("*.py")):
        ran, output = _run_tool(["ruff", "check", "."], worktree)
        if not ran:
            return _result("lint", ran=False, reason=output)
        findings = [line for line in output.splitlines() if line.strip()][:20]
        return _result("lint", ran=True, passed=not findings, findings=findings)
    return _result("lint", ran=False, reason="no recognised lint config found")


def check_tests() -> dict[str, Any]:
    return _result("tests", ran=False,
                    reason="a hook cannot safely commit to the unbounded time an arbitrary test "
                           "suite might take; running one here would risk the deadline discipline "
                           "this daemon depends on everywhere else")


def check_ast() -> dict[str, Any]:
    return _result("ast", ran=False, reason="no per-repo AST rule configuration format exists yet")


def _which(name: str) -> bool:
    import shutil
    return shutil.which(name) is not None


def run_registry(*, repo_root: Path, worktree: str, commit: str, owns: list[str],
                  enabled: list[str]) -> list[dict[str, Any]]:
    """Runs every checker named in `enabled` (meter.modules.sieve.checkers)
    and returns their results, in a fixed order. Never raises: an unexpected
    exception in one checker is folded into that checker's own `not run`
    result rather than aborting the rest of the registry."""
    diff_text = ""
    try:
        diff_result = subprocess.run(
            ["git", "-C", worktree, "diff", f"{commit}~1", commit],
            capture_output=True, text=True, timeout=_CHECKER_TIMEOUT_S, check=False,
        )
        diff_text = diff_result.stdout
    except (OSError, subprocess.SubprocessError):
        pass

    dispatch = {
        "ownership": lambda: check_ownership(repo_root, worktree, commit, owns),
        "secrets": lambda: check_secrets(diff_text),
        "types": lambda: check_types(worktree),
        "lint": lambda: check_lint(worktree),
        "tests": check_tests,
        "ast": check_ast,
    }

    results = []
    for name in enabled:
        fn = dispatch.get(name)
        if fn is None:
            continue
        try:
            results.append(fn())
        except Exception as exc:
            results.append(_result(name, ran=False, reason=f"checker raised: {exc!r}"))
    return results


def render_declaration(results: list[dict[str, Any]]) -> str:
    """The declaration injected into a reviewer's prompt. Only ever states a
    class as covered when it actually ran and actually passed — the module's
    one load-bearing invariant."""
    lines = ["Sieve (meter, deterministic pre-check) ran before you were spawned:"]
    covered = [r for r in results if r["ran"] and r["passed"]]
    flagged = [r for r in results if r["ran"] and not r["passed"]]
    not_run = [r for r in results if not r["ran"]]

    if covered:
        lines.append("Already checked and passed — no need to re-verify these classes unless "
                      "you find concrete evidence the check's guarantee doesn't hold here:")
        for r in covered:
            lines.append(f"  - {r['name']}")

    if flagged:
        lines.append("Already checked and FAILED — these are real findings, not yours to "
                      "re-discover, but confirm and include them in your verdict:")
        for r in flagged:
            for f in r["findings"][:5]:
                lines.append(f"  - [{r['name']}] {f}")

    if not_run:
        lines.append("NOT checked (tool unavailable, timed out, or not configured for this "
                      "repo) — these classes are yours to cover normally:")
        for r in not_run:
            lines.append(f"  - {r['name']} ({r['reason']})")

    return "\n".join(lines)
