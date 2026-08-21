---
description: List past yolo-dag pipeline runs found under .dag/runs/, newest first, with each one's phase reached and outcome.
argument-hint: (no arguments)
allowed-tools: ["Read", "Glob", "Bash"]
---

# /dag-runs — List pipeline runs

List every run recorded under `.dag/runs/` in this repository, newest first.

For each run directory, read enough to report one row: the run id, when it started (from the id
and the directory mtime), the furthest phase it reached, and how it ended. Infer the phase from
which artifacts exist:

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

Present it as a compact table. If `.dag/runs/` doesn't exist or is empty, say so plainly in one
line — that just means no pipeline has run in this repo yet, which is not an error.

Close with a reminder that any unfinished run can be picked up with `/dag-resume <run-id>`, but
only mention it if there actually is an unfinished run.
