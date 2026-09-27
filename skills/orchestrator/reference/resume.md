# Resuming an interrupted run

Read this **only** when this skill was invoked by `/dag-resume` with an existing run id.
A fresh run never needs it.

Read `run.json` first and re-enter at the first phase not marked `"complete"` (or `"skipped"`),
reading that phase's own file from `phases/` as you enter it. **Never redo a phase whose entry is
complete.** Three extra rules apply on a resume and nowhere else:

- **Re-validate `tasks.json`** — uniqueness, edges, acyclicity — before executing anything. State
  on disk can have been hand-edited or corrupted since it was written.
- **Treat the integration branch as authoritative for what has merged.** If `tasks.json` claims a
  task is MERGED but the branch is missing it, flag the inconsistency and stop rather than
  guessing which record is right.
- **Downgrade Phase 4's file-ownership and contract checks to warnings.** The `task-specialist`
  that could fix them is a dead spawn from another session, and refusing to resume over a graph
  you can't get corrected helps nobody. Never run two tasks with overlapping ownership in the
  same pass regardless — that rule does not relax.

A plan-only run parked at the implementation boundary resumes straight into Phase 5: its
`tasks_per_pass`, if the run was started `--non-interactive`, is already in `run.json` and is
what that pass uses.
