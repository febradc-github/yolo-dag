---
description: Entrypoint for the yolo-dag plugin — turns a raw, possibly ambiguous request into a fully-specified one, resolving ambiguity autonomously and asking the user only when a decision is genuinely high-stakes, then hands off to the orchestrator skill to actually build it.
argument-hint: The request to run through the pipeline
---

# /brainstorm — Resolve the Request, Then Hand Off

This is the only phase of `yolo-dag` the user talks to directly. Your job is to
turn `$ARGUMENTS` into a fully-specified request ready for the `orchestrator` skill, spending as
little of the user's time as possible while doing it — then hand off.

Initial input: $ARGUMENTS

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

## Hand off

Immediately after the restatement, invoke the `orchestrator` skill via the `Skill` tool, passing
the fully-specified request (plus the stated assumptions) as its argument. Do not attempt any of
the orchestrator's work yourself — fan-out, review loops, merging, decomposition, and execution
all live in that skill. Your job ends at a clean handoff.
