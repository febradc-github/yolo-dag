---
description: Resume an interrupted yolo-dag run from its persisted state, re-entering at the first phase run.json does not mark complete instead of starting over. Also how a --plan-only run gets executed once the user has reviewed the plan.
argument-hint: A run id (e.g. 2026-08-21-a3f9). Omit to resume the most recent unfinished run.
allowed-tools: ["Read", "Glob", "Bash", "Write", "Skill", "Agent", "SendMessage"]
---

# /dag-resume — Resume an interrupted run

Pick up a pipeline run that stopped partway through — whether it was interrupted, cancelled via
`/dag-cancel`, or deliberately parked by `--plan-only` after the task graph.

Target run: `$ARGUMENTS` — if empty, use the most recent unfinished directory under
`.dag/runs/`.

## Before resuming

1. Confirm the run directory exists. If it doesn't, say so and list what's available rather than
   silently starting a new run — a typo'd run id must not turn into a fresh 100-agent pipeline.
2. Read `run.json` and determine the first phase not marked `"complete"` (or `"skipped"`). The
   `phases` map is authoritative — entries are only marked complete after their artifacts are
   fully written. If `run.json` is missing or unreadable, fall back to inferring from artifacts,
   and treat a truncated artifact as its phase not having completed.
3. State in one line what you found and where you're re-entering ("Run 2026-08-21-a3f9: Phases
   1–4 complete, 14 tasks, 5 merged — resuming Phase 5 dispatch").

## Resuming

Invoke the `orchestrator` skill via the `Skill` tool, passing the original request from
`request.md`, the run id, and the phase to re-enter at.

The orchestrator's own run-setup section handles re-entry: it reads `run.json`, skips every
completed phase, and continues. The rules that matter on a resume:

- **Never redo a completed phase.** The artifacts on disk are authoritative.
- **Specialists cannot be resumed across sessions.** `SendMessage` only reaches agents spawned in
  the current session, so a resume that lands mid-Phase-2 must re-spawn that specialist with its
  last persisted deliverable as context, rather than trying to address the dead spawn name.
- **Re-validate `tasks.json` before executing anything** — uniqueness, edges, acyclicity. State
  on disk can have been hand-edited or corrupted since it was written, and a cycle must be
  bounced back to `task-specialist`, never executed and never silently de-edged.
- **Tasks already MERGED do not re-run.** The integration branch is authoritative for what has
  landed: a resume into Phase 5 checks out `dag/<run-id>` and continues dispatching from the
  ready-queue. If `tasks.json` claims MERGED work but the branch is missing, report the
  inconsistency and stop rather than guessing.
- Tasks marked `running` by a dead or cancelled session are re-dispatched from `pending` — a
  worker that never reported produced nothing mergeable.
- **A `--plan-only` run resumes into Phase 5.** The resume *is* the user's approval to execute
  the plan they reviewed; don't ask again.
