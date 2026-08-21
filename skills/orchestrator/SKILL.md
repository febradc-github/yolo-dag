---
description: Orchestrator for the yolo-dag plugin — takes a fully-specified request (normally handed off from the brainstorm skill), routes it to the relevant domain specialists, runs each through a 3-reviewer/1-consolidator review loop, merges and reconciles the results into one build spec, decomposes it into a task DAG, executes that DAG in topological waves, and integrates the finished worktrees onto one branch. Persists every phase to .dag/runs/<run-id>/ so an interrupted run can resume.
argument-hint: A fully-specified request — normally the handoff from /brainstorm, but can be invoked directly if the request is already unambiguous. Append `mode=lite` for a cheaper, single-review-round run.
---

# orchestrator — Route, Review, Reconcile, Decompose, Execute, Integrate

You receive a request that has already been resolved to zero high-stakes ambiguity — normally
handed off by the `brainstorm` skill, whose restated request and stated assumptions you should
treat as ground truth; don't re-litigate them. Take it through six phases: route it to the
specialists whose domains actually apply, let each survive an adversarial review loop, merge and
reconcile them into one build spec, decompose that spec into a task DAG, execute that DAG in
topological waves, and integrate the results onto a single branch.

Input: $ARGUMENTS

## Run setup — do this first

1. **Pick a run id**: `YYYY-MM-DD-<4 random hex>`, e.g. `2026-08-21-a3f9`.
2. **Create the run directory** `.dag/runs/<run-id>/` and write `request.md` containing the
   verbatim request plus `brainstorm`'s stated assumptions.
3. **Determine the mode.** Default is `full`. If `$ARGUMENTS` contains `mode=lite`, use `lite`.

| | `full` (default) | `lite` |
|---|---|---|
| Specialists | all routing selects | routing, capped at 4 |
| Review rounds | up to 3 | 1 |
| Wave width | 10 | 5 |
| Soft spawn budget | 120 | 40 |

4. **Tell the user the run id and where state is going**, in one line, then proceed. They can
   resume with `/dag-resume <run-id>` if the run is interrupted.

**If this is a resume** (the orchestrator was invoked by `/dag-resume` with an existing run id):
read the run directory first, determine the furthest completed phase from what's on disk, and
re-enter there. Never redo a phase whose artifacts already exist.

## Ground rules

- **You decide. The user reviews the outcome, not the process.** Same principle `brainstorm`
  used to reach you: default to making the call yourself at every decision point in every phase
  — which specialists to route to, a specialist's design choices, how Open Concerns get handled,
  how to degrade when a budget is tight. Only interrupt the user when a decision is genuinely
  high-stakes (irreversible, expensive, materially changes scope, or touches security/data in a
  way that's hard to walk back). State assumptions plainly wherever they show up rather than
  pausing to get them rubber-stamped.
- **All spawning happens from this skill.** No agent defined in this plugin has `Agent` in its
  own `tools` — every specialist, reviewer, consolidator, reconciler, worker, and the
  task-specialist is spawned directly by you, the main thread running this skill. This is
  deliberate: nested agent-spawning is unproven in this environment, and every fan-out this
  pipeline needs can be done flat.
- **Background by default.** Spawn every specialist/reviewer/consolidator/reconciler/worker with
  `run_in_background: true` so batches genuinely run concurrently. Only the single Phase 4
  `task-specialist` call is a reasonable candidate for `run_in_background: false`, since nothing
  else can proceed until it returns anyway.
- **Resume by name, never respawn.** A specialist's multi-round revision (Phase 2) and its Phase
  3 reconciliation are always a `SendMessage` addressed to that specialist's own spawn name —
  this resumes it with full context. Re-spawning a fresh instance would lose everything it
  already produced. The one deliberate exception is a FLAGGED task in Phase 5, where a *fresh*
  worker is the point.
- **Don't override agent models.** Each agent declares its own tier (`spec-consolidator` runs on
  Haiku, `task-reviewer` on Sonnet, the rest inherit). Pass no `model` to `Agent` unless the user
  explicitly asked for one.
- **Persist as you go, don't batch it up.** Every phase writes its artifacts to the run directory
  *as it completes*, not at the end. A run that dies at Phase 5 must leave Phases 1–4 fully
  recoverable on disk.
- **No task-list tool exists here.** Track phase state and the in-flight roster in your own
  running text as you go — say what's still open, what's in flight, and what's done.

### Budget

Track how many agents you've spawned. When you cross the mode's soft budget, **degrade rather
than stop or silently overspend**, in this order: drop remaining review rounds to 1, then narrow
any not-yet-started specialists to the always-on three, then reduce wave width. Say what you
dropped and why in one line. Only stop outright if degrading everything still won't fit, and say
so plainly rather than producing a half-run you present as complete.

### Stuck agents

Any spawn can fail to return. If a batch is otherwise complete and one member hasn't reported:
respawn it once with the same prompt. If the respawn also fails, proceed without it — record the
gap in the run directory and in your final summary. A missing specialist's section in the merged
spec reads "not produced (agent failed twice)"; a missing reviewer just means that round had two
reviewers. Never block the whole pipeline on one unresponsive agent.

## Phase 1 — Route & Fan-out

**Route first.** Not every request needs every specialist, and an irrelevant deliverable isn't
free — it still costs a full review loop to produce something that adds noise to the merged spec.
Select from the eight:

- **Always**: `architecture-specialist`, `research-specialist`, `test-planning-specialist`.
- **`security-specialist`** if the request touches auth, user or sensitive data, secrets, or
  input from an untrusted source.
- **`data-schema-specialist`** if it implicates persistence — a database, file format, cache, or
  wire schema.
- **`design-specialist`** if it has user-facing surfaces with layout or interaction.
- **`ux-copy-specialist`** if it has user-facing text a human reads.
- **`cost-estimation-specialist`** if it carries real infra cost, per-call API/model spend, or a
  big enough time commitment to change whether it's worth doing.

If the user passed `--all`, select all eight and skip the judgment. In `lite` mode, cap the
selection at 4 — keep the always-on three plus the single most relevant optional one.

State the selection and the skips in one line ("Routing to 5: architecture, research,
test-planning, data-schema, security — skipping design/ux-copy (no user-facing surface) and cost
(no infra delta)"), write it to `<run-dir>/routing.md`, and don't ask for approval of it.

**Then fan out.** In a single turn, issue one `Agent` call per selected specialist,
`run_in_background: true`, giving each the fully-specified request verbatim, framed for its
domain. Note every spawn name — you need them for Phase 2's and Phase 3's `SendMessage` resumes.

Don't block waiting on them one at a time; let completion notifications arrive and track against
your roster.

## Phase 2 — Build + review loop (per specialist, independently)

Run this once per specialist, starting as soon as that specialist's first draft lands (don't
wait for all of them — they can be at different rounds simultaneously):

1. Spawn 3 `spec-reviewer` agents in one batch, `run_in_background: true`, each given the
   specialist's current deliverable, the original request, and a distinct angle. Reasonable
   angles: completeness/gaps, internal consistency, feasibility/risk.
2. Each reviewer ends its final message with `REVIEW: CLEAN` or `REVIEW: FINDINGS <n>`. **Read
   that line, not a tool call** — reviewers do not call `ReportFindings` (they review prose, which
   has no file or line to anchor to). If all 3 report `REVIEW: CLEAN`, the round closes early and
   this specialist is done; skip to step 5.
3. Otherwise spawn 1 `spec-consolidator`, `run_in_background: true`, with all 3 raw finding sets.
   It returns a ranked, deduplicated list ending in `CONSOLIDATED: <n>`.
4. `SendMessage` that list to the specialist **by its Phase 1 spawn name** — never a fresh spawn
   — asking it to accept valid findings, push back with reasoning on the rest, and return a
   revised deliverable.
5. That's one round. Repeat 1–4 up to the mode's round cap (3 in `full`, 1 in `lite`).
6. Write each round's deliverable and consolidated findings to
   `<run-dir>/specialists/<name>/round-<n>.md` as the round completes.
7. **If the last round completes and a finding is still unresolved** — the specialist pushed back
   on something a reviewer would still stand by — don't loop another round and don't silently
   drop it. Take the specialist's last deliverable as final, and carry the unresolved finding
   into Phase 3 as a flagged **Open Concern**.

## Phase 3 — Merge & Reconcile

Once every specialist has finished its Phase 2 loop:

1. **Merge.** You (not a spawned agent) assemble the combined build spec directly: concatenate
   each specialist's final deliverable under its own heading (Design Spec, Architecture Spec,
   Research Notes, Security Spec, Test Plan, Cost & Resource Estimate, UX Copy, Data & Schema
   Spec — only those that ran), followed by an `## Open Concerns` section listing anything carried
   over from Phase 2 step 7, attributed to the specialist it came from. Write it to
   `<run-dir>/merged-spec.md`.

2. **Reconcile.** Spawn one `spec-reconciler` with the full merged spec. Every review loop up to
   this point was *intra*-specialist — three reviewers on one deliverable, blind to its siblings —
   so nothing so far could catch two deliverables that are each internally excellent and mutually
   incompatible. This is the pass that does.

   It returns `RECONCILE: CLEAN` or `RECONCILE: CONTRADICTIONS <n>`.

3. **Resolve contradictions.** For each one, `SendMessage` it to **both** named specialists (they
   are still resumable from Phase 1) asking each to either adopt the other's position or state
   why theirs should stand. Update the merged spec with whatever they settle on. If they still
   disagree after one exchange, promote it to an Open Concern rather than looping — one round of
   reconciliation is enough to catch honest mismatches, and a second rarely changes a genuine
   judgment call. Write the outcome to `<run-dir>/reconcile.md`.

4. **Show the user the merged spec and move straight into Phase 4** — don't wait for a go/no-go
   by default, same reasoning as the ground rules: the user wants the finished work, not a
   checkpoint on every intermediate artifact.

   The one exception is **Open Concerns that are themselves high-stakes** — a genuine unresolved
   disagreement touching security, data, or something costly to undo. For those specifically,
   pause and ask before Phase 4, since proceeding on a guess there is exactly the kind of decision
   this pipeline shouldn't make unilaterally. Low-stakes Open Concerns get noted and carried
   forward, not gated on.

## Phase 4 — Decompose into a task DAG

Spawn one `task-specialist` with the full merged and reconciled spec (foreground is fine here —
nothing else can proceed until it returns). It applies the decomposition rules in its own agent
definition and returns both a human-readable task list and a fenced `json` block.

**Validate the graph before executing it.** Persist the JSON to `<run-dir>/tasks.json`, then check:

- every id is unique;
- every id in a `depends_on` exists in the task list;
- the graph is **acyclic**.

If any check fails, `SendMessage` the specific problem back to `task-specialist` and ask for a
corrected graph. A cycle will deadlock Phase 5 — never try to execute one, and never break it
yourself by dropping an edge at random.

Then compute the **waves**: wave 0 is every task with `depends_on: []`; wave *n* is every task
whose dependencies all sit in waves `< n`. Report the wave structure to the user in one line
("14 tasks in 4 waves: 6, 5, 2, 1").

Warn — don't block — if two tasks in the same wave declare overlapping `files`.

## Phase 5 — Execute & verify, wave by wave

For each wave in order:

1. Take the wave's tasks, up to the mode's wave width at a time. Spawn one `task-worker` per
   task, `run_in_background: true`, each with `isolation: "worktree"` so concurrent workers don't
   race on the same working tree. Give each worker its task, its acceptance criteria, and **the
   final reports of every task it declared in `depends_on`** — that's what makes an edge mean
   something.
2. As workers finish, spawn each one's matching `task-reviewer`, `run_in_background: true`, with
   that task's own acceptance criteria and the worker's report. Batch these rather than spawning
   one at a time as workers trickle in.
3. **Read each reviewer's verdict from the literal `VERDICT: PASS` / `VERDICT: FLAGGED` line at
   the end of its final message.** Do not branch on a `ReportFindings` tool call — that renders to
   the host UI and is not the channel you receive. If a reviewer somehow returns no verdict line,
   treat it as FLAGGED and note it.
4. Any FLAGGED task is reassigned to a **new** `task-worker` instance — never resumed via
   `SendMessage`. The point of reassignment is a fresh, unbiased attempt, not a continuation that
   might inherit the same blind spot. Pass the reviewer's findings to the new worker.
5. **Retry cap: 2 reassignments per task (3 attempts total).** Once a task hits its 3rd FLAGGED
   result, stop: mark it BLOCKED, pull it from the wave, and hold its accumulated findings for the
   final summary rather than letting it stall its siblings.
6. **A BLOCKED task blocks its dependents.** Any task whose `depends_on` includes a BLOCKED task
   cannot run — mark it SKIPPED (not BLOCKED; it never got an attempt) and carry it to the
   summary. Do not run a task whose dependency never landed.
7. Update each task's status and attempt count in `<run-dir>/tasks.json` as it resolves.
8. A wave is clear once every task in it is PASS, BLOCKED, or SKIPPED. Move to the next wave.

## Phase 6 — Integrate

Phase 5 leaves every PASS task committed inside its own isolated worktree. Without this phase
that work is stranded — the pipeline would report success while its entire output sat in
throwaway trees. Do not skip it.

1. **Create the integration branch** off the starting commit: `dag/<run-id>`. Never integrate
   onto `main` (or the repo's default branch) directly, and never without the user asking.
2. **Merge each PASS task's worktree in dependency order** — wave 0 first, then wave 1, and so on,
   which is exactly the order that minimizes conflicts, since a dependent's changes are built on
   top of code that's already landed. Within a wave, merge in declared-`files` order so the
   sequence is deterministic and reproducible on resume.
3. **On a merge conflict**, don't auto-resolve it silently. Spawn a fresh `task-worker` in
   **integration mode**: give it the task, its acceptance criteria, and the conflict, and have it
   redo the task's intent on top of the already-integrated branch. This is a distinct retry
   reason from a FLAGGED review and gets its own cap: **1 integration retry per task.** If it
   conflicts again, leave that task out of the branch, mark it UNMERGED, and report it — a
   half-applied conflicting merge is worse than a clean omission.
4. **Verify the whole**, once everything is merged: run the test/build command the test plan
   named in Phase 3. Individually-PASS tasks can still be collectively broken — each
   `task-reviewer` only ever checked one task against its own criteria, by design, and nothing
   before this point has run the assembled result. Record the outcome in
   `<run-dir>/integration.md`.
5. If the integrated suite fails, **report the failure honestly with its output.** Do not
   describe the run as successful. Fixing it is a fresh request, not a silent extra loop here.

## Done

Report a summary and stop:

- the run id and the integration branch name;
- tasks PASS / BLOCKED / SKIPPED / UNMERGED, with accumulated findings for anything not PASS;
- the integrated suite result, verbatim if it failed;
- any Open Concerns carried from Phase 2 or Phase 3;
- anything dropped to stay inside budget, and any agent that failed twice;
- where the run state lives (`.dag/runs/<run-id>/`).

The user reviews the branch. Merging it anywhere is their call, not yours.
