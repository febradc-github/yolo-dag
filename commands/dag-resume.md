---
description: Resume an interrupted yolo-dag run from its persisted state, re-entering at the furthest phase it completed instead of starting over.
argument-hint: A run id (e.g. 2026-08-21-a3f9). Omit to resume the most recent unfinished run.
allowed-tools: ["Read", "Glob", "Bash", "Write", "Skill", "Agent", "SendMessage"]
---

# /dag-resume — Resume an interrupted run

Pick up a pipeline run that stopped partway through.

Target run: `$ARGUMENTS` — if empty, use the most recent unfinished directory under
`.dag/runs/`.

## Before resuming

1. Confirm the run directory exists. If it doesn't, say so and list what's available rather than
   silently starting a new run — a typo'd run id must not turn into a fresh 100-agent pipeline.
2. Read the run's artifacts and determine the furthest **completed** phase. A phase counts as
   complete only if its output artifact exists and is well-formed; a truncated `merged-spec.md`
   means Phase 3 did not complete.
3. State in one line what you found and where you're re-entering ("Run 2026-08-21-a3f9: Phases
   1–4 complete, 14 tasks in 4 waves, wave 2 partially executed — resuming Phase 5 at wave 2").

## Resuming

Invoke the `orchestrator` skill via the `Skill` tool, passing the original request from
`request.md`, the run id, and the phase to re-enter at.

The orchestrator's own run-setup section handles re-entry: it reads the run directory, skips
every completed phase, and continues. The rules that matter on a resume:

- **Never redo a completed phase.** The artifacts on disk are authoritative.
- **Specialists cannot be resumed across sessions.** `SendMessage` only reaches agents spawned in
  the current session, so a resume that lands mid-Phase-2 must re-spawn that specialist with its
  last persisted deliverable as context, rather than trying to address the dead spawn name.
- **Tasks already marked PASS in `tasks.json` do not re-run.** Resume execution at the first wave
  containing unresolved tasks.
- **A resume into Phase 6 re-merges from scratch** onto a fresh `dag/<run-id>` branch, since
  partial integration state isn't reliably recoverable from a dead session.
