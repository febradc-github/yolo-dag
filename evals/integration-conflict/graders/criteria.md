# Grading criteria — integration-conflict

Two logically independent tasks (`retryLimit`, `logLevel`) both have to edit
`src/config.js`. They land in the same wave, run in separate worktrees, and collide at
integration. This case exists because `0.1.0` had no integration phase at all — worker output
was left stranded in worktrees while the run reported success.

## Must pass

1. **An integration branch named `dag/<run-id>` exists** at the end of the run, and the run
   reports its name.
2. **Both settings are present in `src/config.js` on that branch.** Losing one side of the
   conflict is the exact failure mode being tested. `retryLimit: 3` and `logLevel: "info"` must
   both survive.
3. **The original `timeoutMs` setting is still present.** Integration must compose with existing
   code, not replace the file.
4. **`npm test` was actually run against the integrated branch**, and its result is reported.
   Per-task reviews only ever check one task against its own criteria; the assembled result is
   verified only here.
5. **Nothing was merged to `main`.** The starting branch must be untouched — the run hands back a
   branch for the user to review.

## Should pass

6. The run explicitly notices the file overlap — either as a Phase 4 warning that two tasks in
   one wave declare overlapping `files`, or as a Phase 6 conflict it then resolves.
7. If a conflict occurred, it was resolved by spawning a `task-worker` in integration mode
   (rebuilding the task's intent on top of the integrated branch), not by hand-editing the
   conflict markers or by taking one side wholesale.
8. The integration retry cap of 1 per task is respected.
9. The final summary distinguishes task status (PASS/BLOCKED/SKIPPED/UNMERGED) rather than
   reporting a bare success.

## Automatic failure

- The run reports success while `src/config.js` on the integration branch contains only one of
  the two new settings.
- Conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`) are left in a committed file.
- No integration branch was created and the work exists only in worktrees.
- `main` was modified.
