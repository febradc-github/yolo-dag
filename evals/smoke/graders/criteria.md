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
6. **A run directory exists** at `.dag/runs/<run-id>/` containing at minimum `request.md`,
   `routing.md`, `merged-spec.md`, and `tasks.json`.
7. **`tasks.json` parses**, and every task in it carries a terminal status.
8. **`main` is unmodified.**

## Should pass

9. `lite` mode was honoured: at most 4 specialists, one review round each.
10. The task graph has more than one task, and at least one dependency edge if the work warranted
    one. A single monolithic task suggests the decomposition rules were skipped.
11. The user was not asked to approve intermediate artifacts. Nothing here is high-stakes, so a
    pause for sign-off on the merged spec contradicts the pipeline's stated principle.
12. The final summary reports task counts by status, the suite result, and where run state lives.

## Automatic failure

- The run reports success while `npm test` fails on the integration branch.
- Work exists only in worktrees with no integration branch.
- `src/strings.js` on the branch is missing `titleCase`.
- `main` was modified or the run committed directly to it.
