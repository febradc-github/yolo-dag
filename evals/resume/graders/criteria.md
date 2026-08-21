# Grading criteria — resume

Seeded state: phases 1–4 complete, `t1` MERGED on `dag/2026-01-01-resm` (its commit is on the
branch), `t2` depends on `t1` and is pending, `t3` (depends on `t2`) was `running` when the
session died. Resume fidelity is the whole point of persisting run state; this case pins it.

## Must pass

1. **No completed phase is redone.** No specialist, reviewer, consolidator, or reconciler is
   spawned, and no new `tasks.json` is generated — `run.json` marks phases 1–4
   complete/skipped and the artifacts on disk are authoritative.
2. **`t1` is not re-executed.** No worker runs for it, and its commit on the branch is
   untouched (no revert, no duplicate implementation of `retryLimit`).
3. **`t2` builds on `t1`'s actual code.** Its worker's tree contains `retryLimit` already (the
   worktree is cut from the branch tip), and the final `src/config.js` on the branch has
   `timeoutMs`, `retryLimit`, and `logLevel` together.
4. **`t3` is re-dispatched from pending.** A task marked `running` by a dead session produced
   nothing mergeable; it must get a fresh worker, not be assumed done.
5. **The run finishes through Phase 6**: suite run against the branch, acceptance review,
   honest summary, working tree back on `main`, `main` unmodified.

## Should pass

6. The resume states, in one line, what it found and where it re-entered before doing anything.
7. `tasks.json` re-validation happens before dispatch (unique ids, edges resolve, acyclic).
8. The final `run.json` marks phases 5 and 6 complete and every task terminal.

## Automatic failure

- Any Phase 1–4 work is redone (a specialist spawn, a regenerated task graph).
- A second implementation of `retryLimit` appears (t1 re-run), or its commit is reverted.
- `t2` is implemented without `t1`'s code in its tree (e.g. a `src/config.js` that lost
  `retryLimit`).
- The resume starts a brand-new run id instead of picking up `2026-01-01-resm`.
