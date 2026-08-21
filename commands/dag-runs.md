---
description: List past yolo-dag pipeline runs found under .dag/runs/, newest first, with each one's mode, phase reached, and outcome.
argument-hint: (no arguments)
allowed-tools: ["Read", "Glob", "Bash"]
---

# /dag-runs — List pipeline runs

List every run recorded under `.dag/runs/` in this repository, newest first.

For each run directory, report one row: the run id, the mode, when it started (from the id and
the directory mtime), the furthest phase completed, and how it ended.

**Read `run.json` first** — its `phases` map is authoritative (a phase is only marked complete
after its artifacts are fully written, so it distinguishes "completed" from "died halfway
through writing the artifact"). Also pull `mode`, `integration_branch`, and any `degradations`
worth a note (a cancellation, a budget degradation).

**Fall back to artifact inference** only for a run with no readable `run.json` (older or
damaged), using which files exist:

| Artifact present | Phase reached |
|---|---|
| `request.md` only | 1 — routing/fan-out |
| `routing.md` | 1 — fanned out |
| `specialists/*/round-*.md` | 2 — review loops |
| `merged-spec.md` | 3 — merged |
| `reconcile.md` | 3 — reconciled |
| `tasks.json` | 4 — decomposed |
| `tasks.json` with task statuses set | 5 — executing |
| `integration.md` | 6 — integrated |

— and mark such rows as inferred.

Present it as a compact table. If `.dag/runs/` doesn't exist or is empty, say so plainly in one
line — that just means no pipeline has run in this repo yet, which is not an error.

Close with a reminder that any unfinished run can be picked up with `/dag-resume <run-id>`, but
only mention it if there actually is an unfinished run.
