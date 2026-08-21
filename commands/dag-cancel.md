---
description: Stop an in-flight yolo-dag run — halt further dispatch, stop this session's running pipeline agents, and leave the run directory in a cleanly resumable state.
argument-hint: A run id (e.g. 2026-08-21-a3f9). Omit to target the most recent run.
allowed-tools: ["Read", "Glob", "Bash", "Write", "ListAgents", "TaskStop"]
---

# /dag-cancel — Stop an in-flight run

Stop a pipeline run without corrupting its state, so `/dag-resume` can pick it up later.

Target run: `$ARGUMENTS` — if empty, use the most recent directory under `.dag/runs/`.

## Process

1. **Confirm the run directory exists** and read its `run.json`. If the run's phases are all
   complete, say so — there is nothing to cancel.
2. **Stop the in-flight agents.** Use `ListAgents` to find this session's running pipeline
   spawns (specialists, reviewers, workers) and `TaskStop` each one. Only agents spawned in
   *this* session can be stopped here — a run started in a dead session has no running agents
   left to stop, and saying so is the correct outcome, not a failure.
3. **Leave the state resumable, not lying.** In `tasks.json`, reset any task marked `running`
   back to `pending` (a stopped worker produced nothing mergeable; the resume must re-dispatch
   it). Do not touch tasks already MERGED / BLOCKED / SKIPPED / UNMERGED, and do not mark any
   phase complete that wasn't.
4. **Record the cancellation** in `run.json`'s `degradations` array ("cancelled by user at
   phase N") so `/dag-status` and a later resume can see it.
5. **Report** in a few lines: what was stopped, what had already finished (tasks merged so far,
   the integration branch if one exists), and that `/dag-resume <run-id>` continues from here.

Do not delete anything — worktrees, run state, and the integration branch all survive a cancel.
Cleanup is `/dag-clean`'s job, and only when the user asks for it.
