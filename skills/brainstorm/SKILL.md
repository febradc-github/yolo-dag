---
description: Entrypoint for the yolo-dag plugin — asks up front whether this is a plan-only run or a full run, how much scrutiny it should get (mode), and whether to force all specialists, then turns a raw, possibly ambiguous request into a fully-specified one through a real back-and-forth with the user via AskUserQuestion, with zero tolerance for gaps in understanding the problem (implementation minutiae are still resolved autonomously), presents its finalized understanding in plain language with an optional generated HTML visual, then hands off to the orchestrator skill to actually build it.
argument-hint: Request [mode=lite|full|micro] [--all] [--plan-only]
---

# /brainstorm — Resolve the Request, Then Hand Off

This is the only phase of `yolo-dag` the user talks to directly. Your job is to turn
`$ARGUMENTS` into a fully-specified request ready for the `orchestrator` skill — through a real
back-and-forth with the user, not a guess. Resolve implementation minutiae yourself to keep the
conversation short, but never hand off while a gap remains in what's being asked for, why, or how
success will be judged — then hand off.

Initial input: $ARGUMENTS

## First action: run type, mode, and specialist coverage

**Before clarifying questions, before reading any code, before anything else**, resolve three
things about how this run will be shaped: the **run type**, the **mode**, and whether to **force
all specialists**. Each is skipped and taken straight from `$ARGUMENTS` when an explicit flag
already answers it; otherwise ask via `AskUserQuestion`.

1. **Run type and mode**, asked together in one `AskUserQuestion` call (batch both questions in
   the same call; skip either one whose flag is already present in `$ARGUMENTS`):

   > **Is this a plan-only run, or should it continue into implementation?**
   > - **Plan only** — stop after planning and ask before implementing
   > - **Full run** — continue into implementation

   > **How much scrutiny should this run get?**
   > - **Full** — every routed specialist, up to 3 review rounds each. For real features, anything
   >   touching architecture or data, anything you'd want a second opinion on.
   > - **Lite** — routing capped at 4 specialists, 1 review round. For small, well-understood work
   >   that still deserves a spec.
   > - **Micro** — no specialists, no review: straight to decomposition and execution. For work
   >   that's already fully specified by its own one-sentence statement — a typo, a rename, a
   >   config value.

   Skip the run-type question only when `--plan-only` or `--full-run` is present — the flag *is*
   the answer. **The two are mutually exclusive**; if both are present, stop immediately and say
   so rather than picking one. Skip the mode question only when `mode=full`, `mode=lite`, or
   `mode=micro` is present.

2. **Force all specialists**, asked next — only when the resolved mode (from the flag or the
   answer above) is `full` or `lite`, never `micro` (which has no specialist phase), and only when
   `--all` isn't already in `$ARGUMENTS`:

   > **Should this run force all 8 specialists (design, architecture, research, security,
   > test-planning, cost-estimation, ux-copy, data-schema), skipping the routing judgment?**
   > - **No, let routing decide** (recommended) — only the specialists whose domain the request
   >   actually touches
   > - **Yes, run all 8** — more thorough, but every extra specialist costs a full review loop

The **run type** holds for the rest of the session and is a different axis from **mode** (which
sizes how much scrutiny the request gets, not whether the user is asked before code is written).
It sets two things:

- **the implementation gate** — a *full run* enters implementation with no confirmation and is
  never asked to approve it again; a *plan-only* run stops at the implementation boundary and
  starts nothing until the user confirms;
- **the per-pass parallelism cap** in the orchestrator's execution phase — at most 3 tasks per
  pass in a full run, no maximum in a plan-only run.

Execution itself pauses once per pass in *both* run types, to ask how many tasks to run in
parallel next. That prompt is not affected by this answer; only its ceiling is.

State all three in your handoff so the orchestrator records them in `run.json`.

A caller with no user attached — an eval runner, a scripted invocation — cannot answer any of
these three questions. Resolve whichever isn't already flagged with your own best-effort judgment
from the shape of the request instead of asking, and state what you picked as a visible assumption
in the finalization summary. A scripted caller normally passes a run-type flag alongside
`--non-interactive` and `--tasks-per-pass N`, which together pre-answer the pipeline's two
standing questions downstream; pass all of them through untouched — the `orchestrator` skill
validates them and records them in `run.json`. Never set `--non-interactive` yourself, and never
infer it: a question you cannot get an answer to is not evidence that nobody is there.

## Principle: zero tolerance for gaps in understanding

Brainstorm exists so the pipeline never builds something blind. Treat any real gap in
understanding — what's being asked for, who it's for, why it matters, what "done and correct"
looks like — as disqualifying: the request does not move to the next phase carrying it. This is a
lower bar to ask than most workflows use, on purpose; a wrong shared understanding compounds
through every later phase, and asking now is cheap by comparison.

This is not license to interrogate the user about everything. Two different kinds of unknowns get
two different responses:

- **Gaps in the problem itself** — what's actually being built, for whom, why, and how success is
  judged. Never guess these. Ask, every time, no matter how small the gap looks.
- **Implementation minutiae** — format choices, naming, minor scope edges, anything a reasonable
  engineer would just pick and note rather than escalate, where getting it "wrong" costs a small
  revision later, not a wrongly-built feature. Decide these yourself and say what you decided in
  the finalization summary.

When in doubt which pile something belongs to, ask — the failure mode this guards against is a
confidently wrong assumption about the problem, not an occasional question that turns out to have
been decidable.

## Process

Run this as a loop, not a single pass — answers routinely surface new gaps.

1. Read the request and list every place your understanding of the problem is incomplete: what's
   actually being asked for, who it's for, scope boundaries (what's explicitly out), constraints
   (performance, compatibility, deadline), and success criteria (how would anyone — technical or
   not — know this is done and correct).
2. Sort each unknown into one of the two piles from the principle above. Implementation minutiae:
   decide it, note it, move on. Anything about the problem itself: it goes to the user, no matter
   how minor it looks.
3. Ask the problem-understanding questions through the `AskUserQuestion` tool — it's the default
   for every question this skill asks, batched a few at a time (up to the tool's per-call limit),
   using its free-text "Other" option for anything that doesn't reduce to a clean choice. Fall
   back to plain conversational text only when a question genuinely needs an open-ended answer no
   option list could capture.
4. Re-read the answers against the same list. Answers routinely open new sub-questions — "the
   export is for auditors" invites "audited against what standard?" — and if any surface, loop
   back to step 3. Do not proceed while a genuine gap remains.
5. Only once no gap in the problem itself remains, move to Finalization below.

A caller with no user attached (`--non-interactive`) cannot hold this conversation. In that case,
resolve every gap — problem-level included — with your best-effort judgment, and make each one a
maximally visible stated assumption in the finalization summary instead of blocking on an answer
nobody can give.

## Finalization

Once every gap is closed, tell the user what you now understand — in a form a technical and a
non-technical reader can both follow. Skip jargon and internal plumbing (modes, specialists,
phases, run types); describe the problem, not the pipeline that will solve it.

Cover, in plain prose:

- **The problem** — what's wrong or missing today, in the user's own terms.
- **Who it's for** — the person or system that benefits.
- **What will be built** — the shape of the solution, not its implementation.
- **What's explicitly out of scope** — so "done" has an edge.
- **How success will be judged** — the test anyone could apply after the fact.
- **Assumptions made on your behalf** — the implementation-minutiae decisions from step 2 of the
  Process loop, listed briefly so they're easy to correct if wrong.

Ask the user to confirm this understanding is right before moving on. Treat a correction here the
same as a gap found in the Process loop: resolve it and re-check, don't patch around it.

Then ask, via `AskUserQuestion`:

> **Would you like a visual (HTML) representation of this understanding?**
> - **Yes** — generate one
> - **No** — continue without one

If yes, load the `artifact-design` skill, then build one simple, self-contained HTML page that
mirrors the plain-prose summary above (problem, scope, success criteria) and publish it with the
`Artifact` tool. This is a one-time understanding check for the user, not a polished deliverable
— keep it modest. Share the link and continue; a "no" here doesn't block the handoff.

## Sizing the run

Run type, mode, and `--all` are already resolved in First Action above — this table is what each
mode means, for reference while asking that question and while stating assumptions in a
non-interactive fallback:

| | `full` | `lite` | `micro` |
|---|---|---|---|
| Phases | all six | all six | 4–6 only |
| Specialists | all routing selects | routing, capped at 4 | none — the request is the spec |
| Review rounds | up to 3 | 1 | none |
| Soft spawn budget | 120 units | 40 units | 8 units |

The mode sizes scrutiny and spend only. How many tasks run in parallel at once is set by the run
type (3 per pass in a full run, uncapped in a plan-only run), not by the mode.

Whichever way mode and `--all` were resolved — flag or answer — strip them from the request text
you restate, including `--tasks-per-pass`'s number, so none of it leaks into the specialists'
prompts as if it were part of the requirement.

## Hand off

Immediately after finalization, invoke the `orchestrator` skill via the `Skill` tool, passing
the fully-specified request (plus the confirmed understanding and stated assumptions, plus the
mode, plus `--all` if it was resolved to yes, plus the run type the
user picked in your first action — `--plan-only` for a plan-only run, `--full-run` for a full one
— plus `--non-interactive` and `--tasks-per-pass N` verbatim if they were passed to you) as its
argument. Do not attempt any of the orchestrator's work yourself — routing, fan-out, review
loops, merging,
reconciliation, decomposition, execution, and integration all live in that skill. Your job ends
at a clean handoff.
