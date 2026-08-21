# Grading criteria — integration-conflict

Two logically independent tasks (`retryLimit`, `logLevel`) both have to edit
`src/config.js`. They can be in flight at the same time, run in separate worktrees, and their
merges onto the integration branch collide. This case exists because `0.1.0` had no integration
at all — worker output was left stranded in worktrees while the run reported success — and
because losing one side of a conflict is the quietest way integration can fail.

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
   verified only in Phase 6.
5. **Nothing was merged to `main`.** The starting branch must be untouched — the run hands back a
   branch for the user to review. (The working tree may be checked out on `dag/<run-id>` during
   the run; it should be back on the base branch at the end.)

## Should pass

6. The run explicitly notices the file overlap — either as a Phase 4 warning that two
   concurrently-runnable tasks declare overlapping `files`, or as a merge conflict it then
   resolves. (Under continuous integration the conflict may not materialize at all — if the
   second task's worktree was cut after the first merged, both settings simply compose; that is
   the design working, not a miss.)
7. If a conflict occurred, it was resolved by spawning a `task-worker` in integration mode
   (redoing the task's intent on top of the integrated branch), not by hand-editing the conflict
   markers or by taking one side wholesale.
8. The integration retry cap of 1 per task is respected.
9. Both workers ended their reports with the machine trailer (`WORKTREE:` / `BRANCH:` /
   `COMMIT:` / `BLOCKER:`), and the run merged commits it learned about from those trailers.
10. The final summary distinguishes task status (MERGED/BLOCKED/SKIPPED/UNMERGED) rather than
    reporting a bare success.

## Automatic failure

- The run reports success while `src/config.js` on the integration branch contains only one of
  the two new settings.
- Conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`) are left in a committed file.
- No integration branch was created and the work exists only in worktrees.
- `main` was modified.
