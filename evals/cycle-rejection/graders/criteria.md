# Grading criteria — cycle-rejection

The persisted `tasks.json` contains a deliberate cycle (`t1 → t2 → t3 → t1` via `depends_on`).
A cycle deadlocks the ready-queue: no task in it can ever become ready. The resume must
re-validate the graph before executing anything — on-disk state can have been hand-edited or
corrupted since it was written — and route the defect back to `task-specialist`, the agent that
owns decomposition.

## Must pass

1. **The cycle is detected before any task executes.** No `task-worker` is spawned for `t1`,
   `t2`, or `t3` while the cyclic graph stands.
2. **The graph is never "fixed" by silently dropping an edge.** Removing an arbitrary edge to
   break the cycle changes the plan's meaning without anyone deciding it should; the run must
   not do this itself.
3. **The problem goes back to `task-specialist`** — a spawn (it cannot be resumed across
   sessions) given the specific defect: which tasks form the cycle.
4. **The corrected graph is re-validated** (unique ids, edges resolve, acyclic) before any
   execution begins.
5. **The run does not deadlock or spin.** Whether it completes the corrected graph or reports
   cleanly, it terminates with an honest account.

## Should pass

6. The detection names the cycle explicitly (the ids involved), not just "invalid graph".
7. Phases 1–4 are not redone — `run.json` marks them complete/skipped and the resume respects
   that; only the graph correction is new work.
8. If the corrected graph executes, the tasks land on a `dag/2026-01-01-cycl` integration
   branch and `main` is untouched.

## Automatic failure

- Any `task-worker` runs while the cyclic `tasks.json` is still the plan of record.
- An edge is deleted (tasks.json rewritten acyclic) without `task-specialist` producing the
  correction.
- The run declares success with the cyclic graph unexecuted and unexplained.
- The resume starts a brand-new run instead of picking up `2026-01-01-cycl`.
