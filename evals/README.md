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

## The cases

| Case | Tags | What it pins down |
|---|---|---|
| `routing` | cheap | Phase 1 skips specialists whose domain the request doesn't touch. |
| `decomposition` | cheap | Phase 4 emits a valid, acyclic graph with checkable criteria. |
| `review-loop` | medium | Phase 2 reviewers find a planted flaw and the specialist revises. |
| `integration-conflict` | medium | Phase 6 detects a two-tasks-one-file conflict instead of losing a side. |
| `smoke` | expensive | A small real request survives all six phases onto a branch. |

`smoke` is the one that matters and the one most likely to be flaky — it spawns real agents and
writes real code. Run it deliberately, not on every push. The other four are cheap enough for CI
once the runner is available.

## Why these five

Each targets a defect class that actually shipped in `0.1.0`:

- `routing` — every request fanned out to all 8 specialists regardless of relevance.
- `decomposition` — tasks were forbidden from having dependencies, so ordered work got folded
  into oversized single tasks.
- `review-loop` — reviewers were told to report findings through a tool whose schema they could
  not satisfy.
- `integration-conflict` — there was no integration phase at all; worker output was stranded in
  worktrees.
- `smoke` — nothing had ever been run end to end.

A case that doesn't correspond to a way this pipeline has actually broken is probably not worth
its runtime.
