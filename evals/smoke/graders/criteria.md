# Grading criteria — smoke

The one case that exercises all six phases against a real repo. It is deliberately small: the
point is that the machinery survives end to end, not that the work is hard.

## Must pass

1. **The run completes and reports a run id and an integration branch** named `dag/<run-id>`.
2. **Both helpers exist and work.** On the integration branch, `slugify` and `truncate` are
   exported from `src/strings.js` and behave as specified:
   - `slugify('Hello, World!')` → `hello-world`
   - `slugify('  spaced   out  ')` has no leading/trailing hyphen and no doubled hyphens
   - `truncate('hello', 10)` → `hello` (unchanged, already fits)
   - `truncate('hello world', 5)` is 5 characters plus an ellipsis
3. **The pre-existing `titleCase` still works** and its test still passes. The pipeline must not
   break what it found.
4. **`npm test` passes on the integration branch**, and the run reports the actual result. If the
   suite failed, the run must say so plainly rather than describing itself as successful — an
   honest failure report is a pass on *this* criterion but a fail on the one above.
5. **New tests were added** for both helpers, in the repo's existing `node --test` style rather
   than a newly introduced framework.
6. **A run directory exists** at `.dag/runs/<run-id>/` containing at minimum `run.json`,
   `request.md`, `routing.md`, `merged-spec.md`, and `tasks.json`.
7. **`tasks.json` parses**, and every task in it carries a terminal status
   (MERGED/BLOCKED/SKIPPED/UNMERGED).
8. **`main` is unmodified**, and the working tree is checked back out on it at the end.

## Should pass

9. `lite` mode was honoured: at most 4 specialists, one review round each.
10. The task graph has more than one task, each owning its own files, and no dependency edge that
    a shared contract could have removed. A single monolithic task suggests the decomposition
    rules were skipped.
11. **The only pauses are the standing ones**: `brainstorm`'s opening plan-only/full-run question,
    the implementation boundary *if and only if* the answer was plan-only, and Phase 5's per-pass
    prompt asking how many tasks to run in parallel. On a full run the implementation boundary
    must not pause at all — the plan is reported and execution starts. For `lite`'s small task
    count here, expect the opening question once and the per-pass prompt once or twice. Any
    *other* pause (sign-off on an intermediate artifact nothing here makes high-stakes, a check-in
    mid-review-round, a question about *which* tasks to run) contradicts the pipeline's stated
    principle and fails this criterion.

    This case passes neither `--non-interactive` nor `--tasks-per-pass`, so it is the one place
    the questions get exercised for real. Grade whichever branch the answer took:
    - **Answered "Full run"** → no pause at the implementation boundary, and the per-pass question
      states its maximum of 3 and never offers or accepts more.
    - **Answered "Plan only"** → the implementation boundary *does* stop and ask before any branch
      or worker exists, and the per-pass question names no maximum.

    In both branches the per-pass question asks for a number only, and the run picks the tasks
    itself.
12. The final summary reports task counts by status, the suite result, the acceptance review's
    `INTEGRATION:` verdict, and where run state lives.
13. No worktrees of merged tasks are left behind (`git worktree list` shows only the main tree).

## Automatic failure

- The run reports success while `npm test` fails on the integration branch.
- Work exists only in worktrees with no integration branch.
- `src/strings.js` on the branch is missing `titleCase`.
- `main` was modified or the run committed directly to it.
