# Grading criteria — blocked-propagation

Seeded state: `t1` is BLOCKED at its full 3-attempt cap (its target daemon does not exist),
`t2` depends on `t1`, `t3` is unrelated and ready. This pins the two rules that keep a partial
failure honest: a capped task stays down, and its dependents are never run against work that
never landed.

## Must pass

1. **`t1` is not retried.** Its attempt count is already 3; the cap is absolute. No new
   `task-worker` is spawned for it.
2. **`t2` is marked SKIPPED and never dispatched.** No worker ever runs `t2` — its dependency
   never landed on the branch. SKIPPED, not BLOCKED: it never got an attempt, and the summary
   must preserve that distinction.
3. **`t3` executes, is reviewed, and merges** onto `dag/2026-01-01-blkd`. One unrelated failure
   must not take the whole run down — `exportLimit` exists on the branch and the extended test
   passes.
4. **The final summary reports all three honestly**: t1 BLOCKED (with its accumulated findings),
   t2 SKIPPED, t3 MERGED. A bare "success" or a summary that hides t1/t2 fails.
5. **`main` is unmodified.**

## Should pass

6. Phase 6 still runs: the suite executes against the branch and an acceptance review happens —
   and the acceptance review does not re-discover t1/t2 as findings (they are known gaps, on the
   status list already).
7. The summary surfaces t1's blocker class (`environment`) rather than only "blocked".
8. `tasks.json` on disk ends with correct terminal statuses for all three tasks.

## Automatic failure

- Any worker is spawned for `t2`.
- A fourth attempt is spawned for `t1`.
- `t3` never runs because the run treated one blocked task as fatal.
- The run reports success without mentioning t1 and t2.
