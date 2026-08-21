---
description: Show the detailed status of one yolo-dag run — phase reached, specialist roster, task graph progress, open concerns, and integration outcome.
argument-hint: A run id (e.g. 2026-08-21-a3f9). Omit to use the most recent run.
allowed-tools: ["Read", "Glob", "Bash"]
---

# /dag-status — Inspect one run

Report the detailed state of a single pipeline run.

Target run: `$ARGUMENTS` — if empty, use the most recent directory under `.dag/runs/`.

Read that run directory and report:

1. **Header** — run id, mode (`full`/`lite` if recorded in `routing.md`), and the phase it
   reached.
2. **Routing** — which specialists were selected and which were skipped, with the stated reason,
   from `routing.md`.
3. **Review loops** — per specialist, how many rounds ran and whether it closed clean or carried
   an Open Concern, from `specialists/<name>/round-*.md`.
4. **Reconciliation** — contradictions found and how each resolved, from `reconcile.md`.
5. **Task graph** — from `tasks.json`: total tasks, the wave structure, and a count by status
   (PASS / BLOCKED / SKIPPED / UNMERGED / not yet attempted). List every task that is not PASS
   individually, with its accumulated findings — those are the ones the user actually needs to
   see.
6. **Integration** — branch name, which tasks merged cleanly, and the verbatim result of the
   integrated test run, from `integration.md`.

If the run is unfinished, end by naming the exact phase it would resume into and the command to
do it: `/dag-resume <run-id>`.

Report only what's actually on disk. If an artifact is missing, say the phase didn't reach it —
don't infer or reconstruct what a phase "would have" produced.
