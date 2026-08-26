# yolo-dag evals

Behavioural eval suite for the plugin, run with:

```
claude plugin eval . --allow-tools Bash Write Edit
```

Filter to one case with `--case routing`, or to a group with `--tag cheap`.

## Status

**These cases are authored but have not been executed.** `claude plugin eval` is currently
gated behind early access, and it refused to run on the machine where the suite was written:

```
$ claude plugin eval init --bare probe
`plugin eval` is currently in early access
```

So the case layout follows what `claude plugin eval --help` documents — `evals/**/case.yaml`, or
`evals/**/prompt.md` plus `graders/*.md` — and the `case.yaml` files use only fields that help
text names explicitly (`runs`, `tags`, `max_turns`, `timeout_seconds`). Expect to fix schema
details on the first real run. Treat a green suite as unproven until someone with access has
actually run it.

**How these cases answer the pipeline's questions.** The pipeline asks via `AskUserQuestion` in up
to three places — `brainstorm`'s opening plan-only/full-run question, the implementation boundary
on a plan-only run, and the "how many tasks in parallel" prompt before every Phase 5 pass. An
unattended run used to hang on all of them; as of `0.6.0` it pre-answers them with flags instead:

- cases that drive the pipeline from a prompt pass `--full-run --non-interactive
  --tasks-per-pass 3` (`routing`, `integration-conflict`);
- the four seeded cases carry `plan_only`, `non_interactive`, and `tasks_per_pass` in the
  `run.json` their scaffold writes, so the resume takes both answers from disk;
- `review-loop` and `decomposition` invoke a single agent directly and never reach a question.

**`smoke` is deliberately left prompting** — it passes neither unattended flag, so it is the one
case that exercises the opening question, the plan-only implementation boundary, and the per-pass
cap for real (its criterion 11 grades whichever branch the answer takes). It needs a human or a
scripted answer, which is why it is tagged `interactive` and was never a CI case. If it ever gets
the unattended flags too, nothing in the suite verifies the gates at all.

## The cases

| Case | Tags | What it pins down |
|---|---|---|
| `routing` | cheap | Phase 1 skips specialists whose domain the request doesn't touch. |
| `decomposition` | cheap | Phase 4 turns a shared boundary into a contract, not an edge, with sole file ownership. |
| `review-loop` | medium | Phase 2 reviewers find a planted flaw and the specialist revises. |
| `cycle-rejection` | cheap, negative | A cyclic `tasks.json` is bounced to `task-specialist`, never executed, never silently de-edged. |
| `blocked-propagation` | cheap, negative | A capped task stays BLOCKED, its dependent is SKIPPED unrun, unrelated work still merges. |
| `resume` | medium | A run killed mid-Phase-5 resumes without redoing phases or re-executing merged tasks. |
| `budget-degradation` | medium, negative | A near-ceiling spawn ledger forces degradation in the stated order, said and recorded. |
| `integration-conflict` | medium | Two settings in one file both survive onto the branch instead of losing a side. |
| `smoke` | expensive, interactive | A small real request survives all six phases onto a branch, answering the run-type and per-pass questions for real. |

`smoke` is the one that matters and the one most likely to be flaky — it spawns real agents and
writes real code. Run it deliberately, not on every push. The cheap cases are cheap enough for
CI once the runner is available.

### The seeding technique

The four negative-path cases (`cycle-rejection`, `blocked-propagation`, `resume`,
`budget-degradation`) all work the same way: the scaffold writes a `.dag/runs/<id>/` directory
in a known partial state (non-interactive, with a `tasks_per_pass`) and the prompt is just
`/dag-resume <id>`. Failure paths that are
expensive or nondeterministic to *provoke* (a task genuinely failing three reviews, a ledger
crossing its ceiling) are cheap and deterministic to *seed* — the resume path is exercised for
free as a side effect, and each run starts from an identical, hand-auditable state.

## Why these

Each targets a defect class that actually shipped, or a rule whose silent failure would be
invisible until it mattered:

- `routing` — in `0.1.0`, every request fanned out to all 8 specialists regardless of relevance.
- `decomposition` — tasks were once forbidden from having dependencies, so ordered work got
  folded into oversized single tasks; the opposite failure is an edge for every shared boundary,
  serializing work a written contract could have run in parallel.
- `review-loop` — reviewers were told to report findings through a tool whose schema they could
  not satisfy.
- `cycle-rejection` — a cycle deadlocks the candidate set; the tempting "fix" (drop an edge)
  silently changes the plan's meaning.
- `blocked-propagation` — running a dependent against work that never landed produces confident
  nonsense; skipping it silently produces a dishonest summary.
- `resume` — persistence exists so a hundred-agent run survives a dropped connection; a resume
  that redoes phases or re-executes merged tasks makes it pointless.
- `budget-degradation` — in-memory spawn counting drifts downward across compaction, which is
  silent overspend, the exact thing the ceiling exists to prevent.
- `integration-conflict` — through `0.1.0` there was no integration at all; worker output was
  stranded in worktrees while the run reported success.
- `smoke` — nothing had ever been run end to end.

A case that doesn't correspond to a way this pipeline has actually broken (or provably would)
is probably not worth its runtime.
