---
description: Entrypoint for the yolo-dag plugin — asks up front whether this is a plan-only run or a full run, turns a raw, possibly ambiguous request into a fully-specified one, resolving ambiguity autonomously and asking the user only when a decision is genuinely high-stakes, then hands off to the orchestrator skill to actually build it.
argument-hint: Request [mode=lite|full|micro] [--all] [--plan-only]
---

# /brainstorm — Resolve the Request, Then Hand Off

This is the only phase of `yolo-dag` the user talks to directly. Your job is to
turn `$ARGUMENTS` into a fully-specified request ready for the `orchestrator` skill, spending as
little of the user's time as possible while doing it — then hand off.

Initial input: $ARGUMENTS

## First action: plan-only or full run

**Before clarifying questions, before reading any code, before anything else**, ask exactly one
question with `AskUserQuestion`:

> **Is this a plan-only run, or should it continue into implementation?**
> - **Plan only** — stop after planning and ask before implementing
> - **Full run** — continue into implementation

That answer is the run's **run type**, and it holds for the rest of the session. It is a
different axis from `mode=` below (which sizes how much scrutiny the request gets, not whether
the user is asked before code is written), and it sets two things:

- **the implementation gate** — a *full run* enters implementation with no confirmation and is
  never asked to approve it again; a *plan-only* run stops at the implementation boundary and
  starts nothing until the user confirms;
- **the per-pass parallelism cap** in the orchestrator's execution phase — at most 3 tasks per
  pass in a full run, no maximum in a plan-only run.

Execution itself pauses once per pass in *both* run types, to ask how many tasks to run in
parallel next. That prompt is not affected by this answer; only its ceiling is.

Skip the question only when `$ARGUMENTS` already answers it: `--plan-only` for a plan-only run,
`--full-run` for a full one — the flag *is* the answer. **The two are mutually exclusive**; if
both are present, stop immediately and say so rather than picking one. Either way, state the run
type in your handoff so the orchestrator records it in `run.json`.

A caller with no user attached — an eval runner, a scripted invocation — passes a run-type flag
alongside `--non-interactive` and `--tasks-per-pass N`, which together pre-answer both of the
pipeline's standing questions. Pass all of them through untouched; the `orchestrator` skill
validates them and records them in `run.json`. Never set `--non-interactive` yourself, and never
infer it: a question you cannot get an answer to is not evidence that nobody is there.

## Principle: you decide, the user reviews the outcome

Default to resolving ambiguity yourself, using judgment and sensible defaults. The user cares
about the finished result the orchestrator eventually produces, not about adjudicating every
ambiguity in the request first — don't make them pay for decisions you could reasonably make on
your own. Only interrupt them when a decision is genuinely high-stakes: irreversible, expensive,
materially changes scope, or touches security/data in a way that's hard to walk back.

## Process

1. Read the request and list, in your own head, every genuine ambiguity: what's actually being
   asked for, scope boundaries (what's explicitly out), constraints (performance, compatibility,
   deadline), and success criteria (how would anyone know this is done and correct).
2. Sort each ambiguity into one of two piles:
   - **Decide it yourself.** The default. Pick the most reasonable interpretation or convention
     (matching what's already in the codebase where relevant), and move on. Most ambiguities
     land here — format choices, minor scope edges, anything a reasonable engineer would just
     decide and note rather than escalate.
   - **Ask the user.** Reserve this for the genuinely high-stakes few — hard to reverse later,
     meaningfully changes cost/scope/timeline, or a wrong guess on security/data would be
     costly. Ask about these one or two at a time: `AskUserQuestion` for a clean multi-way
     choice, plain conversation otherwise. Don't ask about anything that fits the first pile
     just to be thorough.
3. Once every high-stakes question (if any) is answered, restate the fully-specified request in
   one paragraph, **including a short list of the assumptions you made on the user's behalf**
   for anything you decided yourself in step 2 — visible and easy to correct if you guessed
   wrong, without having required an answer to reach this point.

## Sizing the run

The orchestrator runs in one of three modes, and picking the wrong one wastes either money or
quality. Pass the mode through in your handoff:

| | `full` | `lite` | `micro` |
|---|---|---|---|
| Phases | all six | all six | 4–6 only |
| Specialists | all routing selects | routing, capped at 4 | none — the request is the spec |
| Review rounds | up to 3 | 1 | none |
| Soft spawn budget | 120 units | 40 units | 8 units |

The mode sizes scrutiny and spend only. How many tasks run in parallel at once is set by the run
type (3 per pass in a full run, uncapped in a plan-only run), not by the mode.

- **`mode=full`** — every routed specialist, up to 3 review rounds each. Right for real features,
  anything touching architecture or data, anything you'd want a second opinion on. This is also
  the fallback when the request genuinely doesn't tell you which way to go: over-reviewing costs
  money, under-reviewing costs correctness.
- **`mode=lite`** — routing capped at 4 specialists, 1 review round. Right for small,
  well-understood, moderately-scoped work where a full adversarial pass is overkill but the
  request still deserves a spec.
- **`mode=micro`** — no specialists, no spec review: the request goes straight to task
  decomposition and execution, a handful of agents in total. Right for work that is *already*
  fully specified by its own one-sentence statement — a typo fix, a rename, a config value, a
  single obvious test. If you had to resolve any real ambiguity in step 2, it isn't micro.

`--all` is separate from the mode and forces all 8 specialists, skipping the routing judgment in
Phase 1. It combines with `full` or `lite`; it is incompatible with `micro` (which has no
specialist phase). `--plan-only` and `--full-run` are separate too — they are the run type, not
the mode: they pre-answer the question above, so a plan-only run stops at the implementation
boundary and asks before starting any work, and a full run goes straight in. Pass them through
untouched.

Choose the mode yourself from the shape of the request rather than asking, and say which you
picked in one clause.

**An explicit flag in `$ARGUMENTS` overrides your judgment.** If the user passed `mode=lite`,
`mode=full`, `mode=micro`, `--all`, `--plan-only`, or `--full-run`, honour it and pass it
straight through — including `mode=full` on a request you'd have sized as `micro` yourself.
They know something
about the stakes that the request text doesn't carry. Strip the flag from the request text you
restate — including `--tasks-per-pass`'s number — so it doesn't leak into the specialists'
prompts as if it were part of the requirement.

## Hand off

Immediately after the restatement, invoke the `orchestrator` skill via the `Skill` tool, passing
the fully-specified request (plus the stated assumptions, plus the mode, plus the run type the
user picked in your first action — `--plan-only` for a plan-only run, `--full-run` for a full one
— plus `--non-interactive` and `--tasks-per-pass N` verbatim if they were passed to you) as its
argument. Do not attempt any of the orchestrator's work yourself — routing, fan-out, review
loops, merging,
reconciliation, decomposition, execution, and integration all live in that skill. Your job ends
at a clean handoff.
