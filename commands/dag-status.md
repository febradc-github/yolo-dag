---
description: Show the detailed status of one yolo-dag run — phase reached, specialist roster, task graph progress, spend against budget, open concerns, and integration outcome.
argument-hint: A run id (e.g. 2026-08-21-a3f9). Omit to use the most recent run.
allowed-tools: ["Read", "Glob", "Bash"]
---

# /dag-status — Inspect one run

Report the detailed state of a single pipeline run.

Target run: `$ARGUMENTS` — if empty, use the most recent directory under `.dag/runs/`.

Read `run.json` first — its `phases` map is the authority on what completed — then the markdown
artifacts for the substance. Report:

1. **Header** — run id, mode, whether `--plan-only` was set, the phase reached, and the
   `integration_branch` if one exists (with `base_branch`/`base_commit` it was cut from).
2. **Routing** — which specialists were selected and which were skipped, with the stated reason,
   from `routing.md`.
3. **Review loops** — per specialist, how many rounds ran and whether it closed clean or carried
   an Open Concern, from `specialists/<name>/round-*.md` and the `specialists` array in
   `run.json`.
4. **Reconciliation** — contradictions found and how each resolved, from `reconcile.md`.
5. **Task graph** — from `tasks.json`: total tasks, the level/wave shape, and a count by status
   (MERGED / BLOCKED / SKIPPED / UNMERGED / not yet attempted). List every task that is not
   MERGED individually, with its accumulated findings, its `BLOCKER:` class if one was reported,
   and — for UNMERGED tasks — the worktree path holding the only copy of the work. Those are the
   ones the user actually needs to see.
6. **Spend** — from `run.json`'s `spawns`: spawn counts by model tier, the cost-weighted unit
   total against the mode's soft budget, and anything in `degradations` (budget degradations,
   cancellations), verbatim.
7. **Integration** — the suite result verbatim and the acceptance review's
   `INTEGRATION: PASS`/`FLAGGED` outcome with its findings, from `integration.md`.

If the run is unfinished, end by naming the exact phase it would resume into and the command to
do it: `/dag-resume <run-id>`.

Report only what's actually on disk. If an artifact is missing, say the phase didn't reach it —
don't infer or reconstruct what a phase "would have" produced. If `run.json` and an artifact
disagree (a phase marked complete whose artifact is missing, or vice versa), report the
inconsistency itself — that's a real finding about the run, not a display problem to smooth
over.
