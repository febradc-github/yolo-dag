---
description: Orchestrator for the yolo-dag plugin — takes a fully-specified request (normally handed off from the brainstorm skill) and fans it out to 8 parallel specialists, runs each through a 3-reviewer/1-consolidator review loop, merges the results into one build spec, decomposes it into dependency-free tasks, then executes and verifies them in batches of 10 until done.
argument-hint: A fully-specified request — normally the handoff from /brainstorm, but can be invoked directly if the request is already unambiguous
---

# orchestrator — Fan-out, Review, Merge, Decompose, Execute

You receive a request that has already been resolved to zero high-stakes ambiguity — normally
handed off by the `brainstorm` skill, whose restated request and stated assumptions you should
treat as ground truth; don't re-litigate them. Take it through five phases: fan out to eight
parallel domain specialists, let each survive an adversarial review loop, merge into one build
spec, decompose that spec into dependency-free tasks, then execute and verify those tasks in
fixed-size batches until the list is empty.

Input: $ARGUMENTS

## Ground rules

- **You decide. The user reviews the outcome, not the process.** Same principle `brainstorm`
  used to reach you: default to making the call yourself at every decision point in every phase
  — a specialist's design choices, how Open Concerns get handled, everything. Only interrupt the
  user when a decision is genuinely high-stakes (irreversible, expensive, materially changes
  scope, or touches security/data in a way that's hard to walk back). State assumptions plainly
  wherever they show up (a specialist's deliverable, the merge) rather than pausing to get them
  rubber-stamped.
- **All spawning happens from this skill.** No agent defined in this plugin has `Agent` in its
  own `tools` — every specialist, reviewer, consolidator, worker, and the task-specialist is
  spawned directly by you, the main thread running this skill. This is deliberate: nested
  agent-spawning (a subagent spawning further subagents) is unproven in this environment, and
  every fan-out this pipeline needs can be done flat.
- **Background by default.** Spawn every specialist/reviewer/consolidator/worker with
  `run_in_background: true` so batches genuinely run concurrently. Only the single Phase 4
  `task-specialist` call is a reasonable candidate for `run_in_background: false`, since nothing
  else can proceed until it returns anyway.
- **Resume by name, never respawn.** A specialist's multi-round revision (Phase 2) is always a
  `SendMessage` addressed to that specialist's own spawn name — this resumes it with full
  context. Re-spawning a fresh instance would lose everything it already produced.
- **No task-list tool exists here.** Track phase state and the in-flight roster in your own
  running text as you go — say what's still open, what's in flight, and what's done, rather than
  relying on a dedicated tracking tool.
- **Nothing in this pipeline persists to disk by default.** The merged build spec and the task
  list are in-context artifacts — consumed within the same run, no need to survive it. If the
  user wants any of it saved, do that as an explicit one-off `Write` call on request, not as a
  built-in step.

## Phase 1 — Fan-out (8 specialists, parallel, no cap)

In a single turn, issue 8 `Agent` calls, `run_in_background: true`, one per specialist type:
`design-specialist`, `architecture-specialist`, `research-specialist`, `security-specialist`,
`test-planning-specialist`, `cost-estimation-specialist`, `ux-copy-specialist`,
`data-schema-specialist`. Give each the fully-specified request verbatim, framed for its domain.
Note all 8 spawn names — you'll need them for Phase 2's `SendMessage` resumes.

Don't block waiting on them one at a time; let completion notifications arrive and track
against your list of 8.

## Phase 2 — Build + review loop (per specialist, independently, up to 3 rounds)

Run this once per specialist, starting as soon as that specialist's first draft lands (don't
wait for all 8 — they can be at different phase-2 rounds simultaneously):

1. Spawn 3 `spec-reviewer` agents in one batch, `run_in_background: true`, each given the
   specialist's current deliverable plus a distinct angle in its prompt. Reasonable angles:
   completeness/gaps, internal consistency, feasibility/risk — vary them each round if useful,
   or keep them fixed; both are fine.
2. Once all 3 return their `ReportFindings` results, spawn 1 `spec-consolidator`,
   `run_in_background: true`, with all 3 raw finding sets.
3. Once the consolidator returns its merged list, `SendMessage` it to the specialist **by its
   Phase 1 spawn name** — never a fresh spawn — asking it to accept valid findings, push back
   with reasoning on the rest, and return a revised deliverable.
4. That's one round. Repeat 1–3 up to 3 rounds total for this specialist. A round can close
   early if that round's `spec-reviewer` batch returns nothing at or above the confidence bar —
   treat that as "closed," no need to spend the remaining rounds.
5. **If round 3 completes and a reviewer's finding is still unresolved** — the specialist pushed
   back on something a reviewer would still stand by — don't loop a 4th round and don't silently
   drop it. Take the specialist's last deliverable as final for this specialist, and carry the
   unresolved finding forward into Phase 3 as a flagged "Open Concern" (this is a deliberate
   addition beyond the source spec, which doesn't define round-3-exhaustion behavior — the goal
   is to keep the pipeline moving while still surfacing the disagreement to a human).
6. Hold onto the specialist's finalized deliverable (and any Open Concern) for Phase 3.

## Phase 3 — Merge

You (not a spawned agent) assemble the combined build spec directly, once all 8 specialists have
finished their Phase 2 loops: concatenate each specialist's final deliverable under its own
heading (Design Spec, Architecture Spec, Research Notes, Security Spec, Test Plan, Cost &
Resource Estimate, UX Copy, Data & Schema Spec), followed by an "## Open Concerns" section
listing anything carried over from step 5 above, attributed to the specialist it came from.

Show the user the merged spec and move straight into Phase 4 — don't wait for a go/no-go by
default, same reasoning as the ground rules above: the user wants the finished tasks, not a
checkpoint on every intermediate artifact. The one exception is **Open Concerns that are
themselves high-stakes** (a genuine unresolved disagreement touching security, data, or
something costly to undo) — for those specifically, pause and ask before Phase 4, since
proceeding on a guess there is exactly the kind of decision this pipeline shouldn't make
unilaterally. Low-stakes Open Concerns get noted in the spec and carried forward, not gated on.

## Phase 4 — Task Specialist (decomposition)

Spawn one `task-specialist` with the full merged spec (foreground is fine here — nothing else
can proceed until it returns). It applies the decomposition rules in its own agent definition
(explicit acceptance criteria, no cross-task dependencies, no cross-task references, folded
ordering dependencies called out explicitly, one-worker-pass scope).

Hold the returned task list for Phase 5.

## Phase 5 — Execute & verify, batches of 10

Loop until the task list is empty:

1. Take up to 10 not-yet-attempted tasks. Spawn up to 10 `task-worker` agents in one batch,
   `run_in_background: true`, each with `isolation: "worktree"` so concurrent workers don't race
   on the same working tree, one task each.
2. As each worker finishes, spawn its matching `task-reviewer`, `run_in_background: true`, given
   that task's own acceptance criteria and the worker's report — batch these too rather than
   spawning them one at a time as workers trickle in.
3. Read each `task-reviewer`'s `ReportFindings` result: empty findings = PASS; non-empty =
   FLAGGED.
4. Any FLAGGED task is reassigned to a **new** `task-worker` instance — never resumed via
   `SendMessage`. The point of reassignment is a fresh, unbiased attempt, not a continuation
   that might inherit the same blind spot.
5. **Retry cap: 2 reassignments per task (3 attempts total)** — this is an explicit addition
   beyond the source spec, which doesn't define one; without a cap, one stuck task could loop
   this batch forever. Once a task hits its 3rd FLAGGED result, stop reassigning it: mark it
   BLOCKED, pull it out of the batch, and hold its accumulated `task-reviewer` findings to
   surface at the end rather than letting it stall the other tasks in the batch.
6. A batch is clear once every task in it is either PASS or BLOCKED-and-set-aside. Take the next
   up to 10 not-yet-attempted tasks and repeat from step 1.
7. **Workflow complete** once the task list is empty: report a summary — tasks completed, any
   BLOCKED tasks with their accumulated findings, and any Phase 2 Open Concerns carried through
   Phase 3. This is the end of the run.
