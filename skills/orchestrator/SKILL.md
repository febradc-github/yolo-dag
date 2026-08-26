---
description: Orchestrator for the yolo-dag plugin — takes a fully-specified request (normally handed off from the brainstorm skill), routes it to the relevant domain specialists, runs each through a 3-reviewer/1-consolidator review loop, merges and reconciles the results into one build spec, decomposes it into a task DAG of independent tasks bound by shared contracts, then enters implementation directly on a full run, or stops at the implementation boundary and asks first on a plan-only run — executing that DAG in user-paced passes of worktree-isolated workers, asking only how many tasks to run in parallel each pass and picking the tasks itself, waiting for every task in a pass to be reviewed and done before asking again — then verifies and acceptance-reviews the assembled branch. Persists every phase to .dag/runs/<run-id>/ (including a machine-readable run.json) so an interrupted run can resume.
argument-hint: Request [mode=lite|full|micro] [--all] [--plan-only]
---

# orchestrator — Route, Review, Reconcile, Decompose, Execute, Integrate

You receive a request that has already been resolved to zero high-stakes ambiguity — normally
handed off by the `brainstorm` skill, whose restated request and stated assumptions you should
treat as ground truth; don't re-litigate them. Take it through six phases: route it to the
specialists whose domains actually apply, let each survive an adversarial review loop, merge and
reconcile them into one build spec, decompose that spec into a task DAG, then execute that DAG in
user-paced passes of worktree-isolated workers — asking before each pass only *how many* tasks to
run in parallel and choosing the tasks yourself, merging each passing task onto the run's
integration branch as its pass resolves — then verify and acceptance-review the assembled branch.

Whether the user is asked before implementation starts is set by the **run type**, decided in
`brainstorm` before this skill was invoked: a *full run* enters implementation with no
confirmation; a *plan-only* run stops at the implementation boundary and starts nothing until the
user says go.

Input: $ARGUMENTS

## Run setup — do this first

1. **Git preflight.** This pipeline's entire product is a git branch; establish the ground it
   stands on before spawning anything:
   - Assert this is a git repository (`git rev-parse --git-dir`). If it isn't, stop and say so —
     offer to `git init`, but don't do it unbidden.
   - Record the base: `base_branch` (`git branch --show-current`), `base_commit`
     (`git rev-parse HEAD`), and cleanliness (`git status --porcelain`).
   - **A dirty tree blocks the run.** Phase 5 checks out the integration branch in this working
     tree, and uncommitted changes are invisible to every worker (their worktrees are checkouts of
     a commit). Tell the user what's dirty and ask them to commit/stash it — or to explicitly
     accept starting anyway. This is one of the few genuinely blocking questions: proceeding on a
     guess here can clobber their uncommitted work. "Dirty" means modified or staged *tracked*
     files; ignore `.dag/` entirely (it's the pipeline's own state and would otherwise block
     every run on itself), and merely warn about other untracked files — they're invisible to
     workers but not at risk.
   - Refuse to start if the repo is mid-merge or mid-rebase.
2. **Pick a run id**: `YYYY-MM-DD-<4 random hex>`, e.g. `2026-08-21-a3f9`. If a branch
   `dag/<run-id>` somehow already exists, pick a new hex suffix.
3. **Create the run directory** `.dag/runs/<run-id>/` and write `request.md` containing the
   verbatim request plus `brainstorm`'s stated assumptions.
4. **Determine the mode, the run type, and the flags.** Parse `$ARGUMENTS` for `mode=full`,
   `mode=lite`, `mode=micro`, `--all`, `--plan-only`, `--full-run`, `--non-interactive`, and
   `--tasks-per-pass N`. With no mode flag, use whatever mode `brainstorm` chose in its handoff,
   and fall back to `full` if you were invoked directly with no mode at all. `--all` forces all
   8 specialists and is incompatible with `micro` (which has no specialists) — if both are
   passed, run as `lite` and say so in one line.

   The **run type** is a separate axis from the mode: `--plan-only` (or a handoff saying the user
   picked a plan-only run) sets `plan_only: true`; `--full-run` (or a handoff saying the user
   picked a full run) sets `plan_only: false`. **The two flags are mutually exclusive** — if both
   are passed, stop immediately and say so plainly; do not pick a precedence. Never ask the user
   for the run type here — `brainstorm` already did, as its first action, and re-asking is exactly
   the checkpoint this pipeline removed.

   **`run.json`'s `plan_only` is where the run type lives from this point on.** Both places that
   branch on it — the Phase 4 implementation boundary and Phase 5's per-pass cap — re-read it from
   disk rather than from memory, for the same reason the budget is: a `full` run outlives several
   context compactions, and a forgotten run type silently turns a plan-only run into an unattended
   build.

   **Unattended runs.** Two more flags let a caller with no user attached — an eval runner, a
   scripted invocation — pre-answer this pipeline's two standing questions instead of hanging on
   them:

   - **`--non-interactive`** declares that no user is attached. It is **explicit and never
     inferred** — not from a missing TTY, not from how the request reads, not from a question
     going unanswered. Record it as `non_interactive: true` in `run.json`.
   - **`--tasks-per-pass N`** pre-answers Phase 5's per-pass question with `N`, for every pass in
     the run. Record it as `tasks_per_pass: N`.

   Validate these at startup, before creating a branch or spawning anything, and **stop with a
   plain error** rather than proceeding:

   - `--tasks-per-pass` without `--non-interactive` → refuse, and say why: in an interactive run
     the per-pass question is a review checkpoint the user is entitled to, and this flag is not a
     way around it.
   - `--non-interactive` without a run type (`--plan-only` or `--full-run`) → refuse: the run
     would walk straight into the opening question with nobody there to answer it, which is the
     failure this mode exists to prevent.
   - `--non-interactive` without `--tasks-per-pass` → refuse, **including on a plan-only run**,
     which parks before Phase 5 and never asks the question itself. The number is run state, not
     an answer to one prompt: `run.json` carries it into the pass that `/dag-resume` eventually
     runs, and requiring it here is what lets that resume proceed unattended without a second
     flag of its own. Set once at setup, used whenever the run reaches Phase 5.
   - `N` above the run type's cap — **3 on a full run**; a plan-only run has no cap — → refuse and
     name the cap. Never silently clamp a pre-answered number. (`N` larger than the number of
     eligible tasks is not an error: Phase 5 runs the eligible subset exactly as it would for a
     typed answer.)
   - `N` that is not a whole number ≥ 1 → refuse.

   Non-interactive mode changes *where the two answers come from*, and nothing else. The plan is
   still reported, the pass barrier is still a barrier, dependency rules still decide what is
   eligible, and the per-pass structure is identical.

| | `full` | `lite` | `micro` |
|---|---|---|---|
| Phases | all six | all six | 4–6 only |
| Specialists | all routing selects | routing, capped at 4 | none — the request is the spec |
| Review rounds | up to 3 | 1 | none |
| Soft spawn budget | 120 units | 40 units | 8 units |

The mode sizes scrutiny and spend. It does *not* set concurrency: how many tasks run in parallel
is asked per pass in Phase 5, capped at 3 in a full run and uncapped in a plan-only run.

5. **Write `run.json`** to the run directory — the machine-readable manifest every command and
   every resume branches on. The markdown artifacts are the human surface; `run.json` is the
   source of truth for code. Keys:

```json
{
  "run_id": "2026-08-21-a3f9",
  "mode": "full",
  "plan_only": false,
  "non_interactive": false,
  "tasks_per_pass": null,
  "base_branch": "main",
  "base_commit": "<sha>",
  "clean_start": true,
  "integration_branch": null,
  "phases": { "1": "pending", "2": "pending", "3": "pending", "4": "pending", "5": "pending", "6": "pending" },
  "specialists": [],
  "spawns": [],
  "degradations": []
}
```

   In `micro` mode, phases 1–3 start as `"skipped"`. Mark each phase `"complete"` in `run.json`
   only after its artifacts are fully written — a phase whose entry still says `"pending"` or
   `"in_progress"` did not complete, however plausible its half-written markdown looks.
6. **Tell the user the run id and where state is going**, in one line, then proceed. They can
   resume with `/dag-resume <run-id>` if the run is interrupted.

**If this is a resume** (the orchestrator was invoked by `/dag-resume` with an existing run id):
read `run.json` first and re-enter at the first phase not marked `"complete"` (or `"skipped"`).
Never redo a phase whose entry is complete. Three extra rules on resume: re-validate `tasks.json`
(uniqueness, edges, acyclicity) before executing anything — state on disk can have been
hand-edited or corrupted since it was written; treat the integration branch as authoritative
for what has merged, so if `tasks.json` claims a task is MERGED but the branch is missing, flag
the inconsistency and stop rather than guessing; and downgrade Phase 4's file-ownership and
contract checks to warnings here — the `task-specialist` that could fix them is a dead spawn from
another session, and refusing to resume over a graph you can't get corrected helps nobody. Never
run two tasks with overlapping ownership in the same pass regardless.

## Ground rules

- **You decide. The user reviews the outcome, not the process.** Same principle `brainstorm`
  used to reach you: default to making the call yourself at every decision point in every phase
  — which specialists to route to, a specialist's design choices, how Open Concerns get handled,
  how to degrade when a budget is tight. Only interrupt the user when a decision is genuinely
  high-stakes (irreversible, expensive, materially changes scope, or touches security/data in a
  way that's hard to walk back). State assumptions plainly wherever they show up rather than
  pausing to get them rubber-stamped. **There are exactly two standing interactive gates in this
  skill**, and no others: on a *plan-only* run, the implementation boundary at the end of Phase 4;
  and, in both run types, Phase 5's per-pass prompt asking how many tasks to run in parallel. A
  full run does not pause at the implementation boundary at all — report the plan and keep going.
  In a **non-interactive** run those two answers come from `run.json` instead of from a question,
  and nothing else about the gates changes: the plan is still reported before implementation, and
  the pass barrier still holds until every task in the pass is done.
- **All spawning happens from this skill.** No agent defined in this plugin has `Agent` in its
  own `tools` — every specialist, reviewer, consolidator, reconciler, worker, and the
  task-specialist is spawned directly by you, the main thread running this skill. This is
  deliberate: nested agent-spawning is unproven in this environment, and every fan-out this
  pipeline needs can be done flat.
- **Background by default.** Spawn every specialist/reviewer/consolidator/reconciler/worker with
  `run_in_background: true` so batches genuinely run concurrently. Only the single Phase 4
  `task-specialist` call is a reasonable candidate for `run_in_background: false`, since nothing
  else can proceed until it returns anyway.
- **Resume by name, never respawn.** A specialist's multi-round revision (Phase 2), its Phase 3
  reconciliation, and a Phase 5 worker's rework after a FLAGGED review are always a `SendMessage`
  addressed to that agent's own spawn name — this resumes it with full context, and in the
  worker's case with the worktree its work already lives in. Re-spawning a fresh instance would
  lose everything it already produced. Spawn fresh only where the tree itself has to be fresh: a
  merge-conflict retry in integration mode, a reopened contract consumer, or an agent whose spawn
  is no longer reachable.
- **Don't override agent models — with one documented exception.** Each agent declares its own
  tier (`spec-consolidator` runs on Haiku, `task-reviewer` on Sonnet, the rest inherit). Pass no
  `model` to `Agent`, except: in each Phase 2 review round, spawn the **internal-consistency**
  `spec-reviewer` with `model: "sonnet"`. Three same-model instances of one agent converge on the
  same findings and miss the same things; genuine model diversity decorrelates them better than a
  different adjective in the prompt does.
- **Persist as you go, don't batch it up.** Every phase writes its artifacts to the run directory
  *as it completes*, not at the end. A run that dies at Phase 5 must leave Phases 1–4 fully
  recoverable on disk.
- **Once an artifact is written, your memory of it is its path, not its text.** Re-read
  `merged-spec.md`, `tasks.json`, or a specialist's round file from `.dag/runs/<run-id>/` at the
  point of use instead of carrying full artifact text forward in context. A `full` run outlives
  several context compactions; the run directory is what survives them, and this rule is what
  makes the run survivable.
- **Roster tracking.** If a task-list tool (`TodoWrite`) is available in your session, use it for
  the in-flight roster; otherwise track phase state and the roster in your own running text —
  say what's still open, what's in flight, and what's done.

### Budget

The budget is **cost-weighted and read from disk, never from memory** — a compacted context
forgets spawns, and the drift is always downward, which means silent overspend, the exact thing
the ceiling exists to prevent. Append an entry to `run.json`'s `spawns` array as each batch of
spawns goes out (`{"agent": ..., "model": ..., "phase": ...}`), and compute spend by re-reading it.

Unit weights: session-model (`inherit`) spawn = **1.0**, `sonnet` = **0.5**, `haiku` = **0.1**.

When a run crosses the mode's soft budget, **degrade rather than stop or silently overspend**,
in this order: drop remaining review rounds to 1, then narrow any not-yet-started specialists to
the always-on three, then lower the parallelism cap offered at Phase 5's per-pass prompt (in a
plan-only run, which has no cap, don't invent one — state the spend a large pass implies before
dispatching it and let the user's number stand). Record each degradation in `run.json`'s
`degradations` array and say what you dropped and why in one line. Only stop outright if
degrading everything still won't fit, and say so plainly rather than producing a half-run you
present as complete.

### Stuck agents

Any spawn can fail to return. If a review round or execution pass is otherwise complete and one
member hasn't reported:
respawn it once with the same prompt. If the respawn also fails, proceed without it — record the
gap in the run directory and in your final summary. A missing specialist's section in the merged
spec reads "not produced (agent failed twice)"; a missing reviewer just means that round had two
reviewers. Never block the whole pipeline on one unresponsive agent.

## Phase 1 — Route & Fan-out

**In `micro` mode, skip Phases 1–3 entirely**: write `routing.md` noting "micro — no
specialists; the request is the spec", copy the request into `merged-spec.md`, mark phases 1–3
`"skipped"` in `run.json`, and go straight to Phase 4.

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
domain. Note every spawn name — you need them for Phase 2's and Phase 3's `SendMessage` resumes —
and record each specialist in `run.json`'s `specialists` array (`{"name": ..., "spawn": ...,
"rounds": 0, "status": "drafting"}`).

Don't block waiting on them one at a time; let completion notifications arrive and track against
your roster.

## Phase 2 — Build + review loop (per specialist, independently)

Run this once per specialist, starting as soon as that specialist's first draft lands (don't
wait for all of them — they can be at different rounds simultaneously):

1. Spawn 3 `spec-reviewer` agents in one batch, `run_in_background: true`, each given the
   specialist's current deliverable, the original request, and one of the three named angles
   defined in the `spec-reviewer` agent file: **completeness/gaps**, **internal consistency**
   (spawn this one with `model: "sonnet"` — the one model override this pipeline makes, for
   decorrelation), and **feasibility/risk**.
2. Each reviewer ends its final message with `REVIEW: CLEAN` or `REVIEW: FINDINGS <n>`. **Read
   that line, not a tool call** — reviewers do not call `ReportFindings` (they review prose, which
   has no file or line to anchor to). If all 3 report `REVIEW: CLEAN`, the round closes early and
   this specialist is done; skip to step 5.
3. Otherwise spawn 1 `spec-consolidator`, `run_in_background: true`, with all 3 raw finding sets.
   It returns a ranked, deduplicated list ending in `CONSOLIDATED: <n>`. If the count is `0`,
   the round closes — deduplication can dissolve three near-findings into nothing, and an empty
   list is not worth a revision round.
4. `SendMessage` that list to the specialist **by its Phase 1 spawn name** — never a fresh spawn
   — asking it to accept valid findings, push back with reasoning on the rest, and return a
   revised deliverable.
5. That's one round. Repeat 1–4 up to the mode's round cap (3 in `full`, 1 in `lite`) — but stop
   early on **diminishing returns**: if the specialist's revision accepted *none* of the round's
   findings (it pushed back on everything), don't spend another round re-litigating — a further
   identical round rarely moves a considered pushback. Carry anything a reviewer would still
   stand behind forward as an Open Concern instead.
6. Write each round's deliverable and consolidated findings to
   `<run-dir>/specialists/<name>/round-<n>.md` as the round completes, and update that
   specialist's `rounds`/`status` in `run.json`.
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
definition — **independence first, shared contracts where a boundary is unavoidable, a real
`depends_on` edge only as a last resort** — and returns both a human-readable task list and a
fenced `json` block carrying `contracts` and `tasks`.

**Validate the graph before executing it.** Persist the JSON to `<run-dir>/tasks.json`, then check:

- every id is unique;
- every `depends_on` entry names a task that exists, and carries a `reason`;
- the graph is **acyclic**;
- **no file is owned by two tasks.** Every path in a task's `owns` list belongs to exactly one
  task. This is a hard check, not a warning: shared ownership means the split is wrong, and the
  fix is the specialist's (re-split, or move the shared surface into a contract), not yours;
- every contract named in a task's `contracts` list is defined in the top-level `contracts`
  array, and each contract has exactly one owner task.

If any check fails, `SendMessage` the specific problem back to `task-specialist` and ask for a
corrected graph. A cycle will deadlock Phase 5 — never try to execute one, and never break it
yourself by dropping an edge at random.

A `depends_on` entry may also arrive as a bare id string rather than an `{id, reason}` object —
graphs written by an older version of this pipeline, and hand-edited state, both look like that.
Read it as an edge with an unstated reason rather than failing the run over it, and read a legacy
`files` key as `owns`.

Then compute the **topological levels** ("waves") — level 0 is every task with
`depends_on: []`; level *n* is every task whose dependencies all sit in levels `< n`. Levels are
the proof of acyclicity and the reporting shape ("14 tasks in 4 waves: 6, 5, 2, 1" — report that
line to the user); the actual scheduling in Phase 5 is a user-paced pass loop over the candidate
set, not a strict wave-by-wave walk. A well-decomposed graph is mostly level 0 — that is the
point of the independence-first rules, not a sign the specialist under-thought the ordering.

**Report the plan, then branch on the run type** — read `plan_only` from `run.json`, not from
memory. Either way, first report the routing decision,
where the merged spec lives, the task graph and its shape, the shared contracts and who owns each,
and any Open Concerns.

- **Full run** → **enter Phase 5 immediately, with no confirmation.** Do not ask whether to
  proceed, do not re-ask anything `brainstorm` already settled. The user chose a full run at the
  start precisely so this boundary wouldn't stop them.
- **Plan-only run** (`plan_only: true`) → **stop at this boundary and ask**, via
  `AskUserQuestion`, whether to proceed with implementation now. Start no work — no branch, no
  worker — until they confirm.
  - **In a non-interactive run** (`non_interactive: true`) there is nobody to ask, so stop here
    cleanly instead: leave phases 5–6 `"pending"` in `run.json` and report that
    `/dag-resume <run-id>` executes the plan. The confirmation still gates the work; it just
    arrives as a later invocation rather than an answer.
  - **Proceed** → move into Phase 5.
  - **Not yet** → leave phases 5–6 `"pending"` in `run.json` and tell them `/dag-resume <run-id>`
    executes the plan once they're ready.
  - **Substantive feedback on the spec or graph itself** (not just "not yet") → route it to
    where it actually belongs — a task-shape complaint back to `task-specialist` (resumable
    in-session via `SendMessage`), a spec-level complaint back to the relevant Phase 1
    specialist — re-validate whatever comes back, then re-offer this same gate. Don't
    reinterpret their feedback yourself and proceed on a guess.

## Phase 5 — Execute, verify & merge (in user-paced passes)

Execution proceeds one **pass** at a time. Before each pass you take a single number — how many
tasks to run in parallel — then choose the tasks yourself and drive every one of them to *done*
before taking another. A task is done only when a `task-reviewer` has approved it. The number
comes from the user, asked once per pass; in a non-interactive run it comes from `run.json`'s
`tasks_per_pass` instead.

The pause between passes is the point: the user reviews finished work in small batches instead of
the whole DAG landing at once. It happens in both run types, plan-only and full alike.

1. **Create the integration branch first**: `git checkout -b dag/<run-id> <base_commit>` in the
   main working tree, and record `integration_branch` in `run.json`. The main tree stays on this
   branch for the whole phase — that is the mechanism by which every worker worktree spawned from
   here is based on the branch's current tip. Never integrate onto `main` (or the repo's default
   branch) directly, and never without the user asking.
2. **Compute the candidate set.** A task is a *candidate* when it has not run and every id in its
   `depends_on` is MERGED. Recompute this fresh at the start of every pass — it typically grows
   with whatever the last pass unlocked.
3. **Ask how many tasks to run in parallel this pass**, via `AskUserQuestion`. Which cap applies
   comes from `run.json`'s `plan_only`, re-read at each pass rather than remembered.

   **In a non-interactive run** (`non_interactive: true` in `run.json`) don't ask: take
   `tasks_per_pass` from `run.json` as this pass's number, say in one line that you're using it,
   and go to step 4. It was validated against the cap at startup. Everything else about the pass
   is unchanged — you still select the tasks, still enforce independence, still run the eligible
   subset when fewer qualify, and still hold the barrier at the end of the pass.

   Otherwise ask for a **number, and nothing else**:
   - **Never ask which tasks.** Selection is yours (step 4). A question naming tasks is the wrong
     question however carefully it's phrased.
   - **Full run: the maximum is 3 per pass, and you state the maximum in the question** ("How many
     tasks should run in parallel this pass? (max 3)"). Offer whichever of `3, 2, 1` are ≤ the
     candidate count; if the user types a larger number anyway, clamp to 3 and say so.
   - **Plan-only run: there is no maximum.** Offer a few sensible numbers up to the candidate
     count and let the user type any number they like; you spawn one worker per selected task.
     The user reviewed the whole plan before approving implementation, so the ceiling that
     protects a full run has already been paid for here.
   - If exactly one candidate remains there is no choice to offer — say so in one line, dispatch
     it, and skip the prompt.
   - If the answer exceeds what's available, clamp per step 4 and say what you clamped to. Never
     re-ask because the number didn't fit.
4. **Select the tasks yourself** — the best next candidates by readiness and dependency order,
   longest-remaining-dependency-chain first (critical path first), so the pass unlocks as much of
   the graph as it can. Two hard constraints on the selection:
   - **Every task in a pass must be fully independent of every other task in it.** No task in the
     pass may appear in another's `depends_on`, transitively included, and no two may own the same
     file.
   - **When the requested count would pull in a task that isn't independent of the rest, don't run
     them together.** Say which tasks are eligible and why, then proceed with the eligible subset
     — **without re-asking**: *"You asked for 3 tasks. Task 3 depends on Task 2, so only Tasks 1
     and 2 can run in this pass."* The same applies when fewer independent candidates remain than
     the user asked for: take what's eligible, say so, and go.
5. **Spawn one `task-worker` per selected task**, `run_in_background: true`, with
   `isolation: "worktree"`. Give each worker its task, its acceptance criteria, its `owns` list,
   the verbatim text of every contract it implements or consumes (from `tasks.json`'s `contracts`
   array — the *current* version, not the version quoted in an older report), and the final
   reports of every task in its `depends_on` — noting that the dependencies' actual code is
   already present in its tree, and the reports are context, not the source of truth.

   **The number is a target, not a guarantee.** If the runtime won't spawn that many agents at
   once, spawn as many as it allows and queue the remainder *as part of this same pass*,
   dispatching each queued task as a slot frees up. A runtime limit is not a reason to ask the
   user a second question: don't prompt again until the whole pass is done.
6. **Read each worker's machine trailer as it reports** — the literal `WORKTREE:`, `BRANCH:`,
   `COMMIT:`, and `BLOCKER:` lines at the end of its final message. That trailer is the only
   channel through which the pipeline learns where the work physically is; a report without it is
   a report of nothing.
   - `COMMIT: none` alongside a claim of success means the worker produced nothing (an unchanged
     worktree is auto-cleaned). There is nothing to rework, so spawn a fresh worker for the task
     within this same pass; it consumes one of the task's rework rounds.
   - `BLOCKER: contract-conflict` means a shared contract can't be implemented as written — go to
     step 12. It is not a worker failure and does not consume a rework round.
   - `BLOCKER: <class>` otherwise (`spec-defect` / `environment` / `criteria-conflict` /
     `unknown`) means the worker judged the task unsatisfiable. Spawn **one** fresh worker to
     independently confirm; if it reports the same blocker class, mark the task BLOCKED with that
     class — don't burn the remaining rework rounds re-confirming.
   - **Verify the commit is reachable** from the main repo (`git cat-file -e <sha>`). If it
     isn't — the isolation mechanism gave the worker a detached copy rather than a
     shared-object worktree — fetch it in: `git fetch <worktree-path> <sha>`. If both fail, treat
     the spawn as failed under the stuck-agent rule.
7. **Review each task as soon as its own worker reports** — one `task-reviewer` per finished task,
   `run_in_background: true`, with the task's acceptance criteria, the current text of its
   contracts, the worker's report, and — critically — the worker's `WORKTREE` path and `COMMIT`
   sha, so it verifies the actual work (`git -C <worktree> ...`) rather than the main tree, where
   the changes do not exist. Tasks in a pass move through their own cycles independently; the
   barrier is at the end of the pass, not between its stages.
8. **Read each reviewer's verdict from the literal `VERDICT: PASS` / `VERDICT: FLAGGED` line at
   the end of its final message.** Do not branch on a `ReportFindings` tool call — that renders to
   the host UI and is not the channel you receive. If a reviewer somehow returns no verdict line,
   treat it as FLAGGED and note it.
9. **On `VERDICT: FLAGGED`, the task goes back to its own worker to rework** — `SendMessage` the
   reviewer's findings to that worker's spawn name, so it fixes the defect in the worktree it
   already owns and re-commits. Then re-review: `SendMessage` the new commit back to the same
   reviewer (it holds the findings it raised and is the cheapest correct check that they were
   actually addressed). The task is done only when a reviewer returns `VERDICT: PASS`.
   - **If the worker's spawn is unreachable** — a resumed run, a dead agent — spawn a fresh
     `task-worker` with the findings instead; its worktree comes off the branch's current tip.
     Same for an unreachable reviewer: spawn a fresh one with the findings and the new commit.
   - This all happens **inside the same pass**: the pass is not closed until this task also
     reaches a terminal state.
   - **Cap: 2 rework rounds per task (3 reviews total).** On the 3rd `FLAGGED`, mark it BLOCKED
     and hold its accumulated findings for the final summary.
10. **On `VERDICT: PASS`, merge immediately**: `git merge --no-ff <commit>` onto `dag/<run-id>`
    in the main tree.
    - **Clean merge** → mark the task MERGED, then remove its worktree
      (`git worktree remove <path>` and `git worktree prune`) — the work is on the branch; the
      worktree is done. Cleanup happens as each task lands, not deferred to Phase 6.
    - **Conflict** → `git merge --abort`, then spawn a fresh `task-worker` in **integration
      mode**: give it the task, its acceptance criteria, and the conflict, and have it redo the
      task's intent on top of the branch's current tip (its fresh worktree already contains the
      integrated code). This is a distinct retry reason from a FLAGGED review, also resolved
      **within the same pass**, and gets its own cap: **1 integration retry per task.** A second
      conflict → mark the task UNMERGED, *keep its worktree*, and report the path — that worktree
      is the only copy of the work. Two tasks in one pass conflicting on a file is also a
      decomposition bug (they should not have owned the same file); say so when it happens.
11. **A BLOCKED task blocks its dependents.** Any task whose `depends_on` includes a BLOCKED (or
    UNMERGED) task cannot run — mark it SKIPPED (not BLOCKED; it never got an attempt) and carry
    it to the summary. Do not run a task whose dependency never landed on the branch.
12. **When a shared contract turns out to be wrong** (`BLOCKER: contract-conflict`), the worker
    stops rather than improvising a different shape, and the fix is yours, not its:
    - Read the reported mismatch against the contract's current text in `tasks.json`. If it's the
      task that's wrong rather than the contract, send that back as a rework (step 9) and say so.
    - Otherwise **revise the contract** — resuming `task-specialist` via `SendMessage` when the
      right shape isn't obvious — and write the revision into `tasks.json`: the `contracts` entry
      plus the verbatim copy carried in each bound task's description.
    - **Reopen every task that consumes the revised contract, including tasks already marked
      MERGED**, and return each to the worker/reviewer cycle with the new contract text. A merged
      task reopens as a fresh worker off the branch's current tip (which contains its own earlier
      work); a merged-then-revised task is not exempt because it already passed once — it passed
      against a contract that no longer exists.
    - Reviewers verify against the **current** contract, never the version a task was written
      against. Pass the current text in every reviewer prompt.
    - Reopened tasks join the current pass and it does not close until they are done.
    - **Cap: one revision per contract.** If the same contract breaks a second time, the spec is
      wrong at a level this loop can't fix — mark the affected tasks BLOCKED with `spec-defect`
      and carry it to the summary. The bounded-loop property is what keeps this pipeline a DAG.
13. **Spec-defect circuit breaker.** If at any point more than half of all *attempted* tasks are
    BLOCKED with `BLOCKER: spec-defect`, the spec is the problem, not the workers. Pause
    dispatch and `SendMessage` the accumulated blocker reports to `task-specialist` (its Phase 4
    spawn is resumable in-session) for a corrected graph of the not-yet-merged remainder;
    re-validate it, then resume dispatch. **Strictly once per run** — the bounded-loop property
    is what keeps this pipeline a DAG.
14. Update each task's status and attempt count in `<run-dir>/tasks.json` as it resolves
    (`pending` → `running` → `pass` → `merged`, or `blocked` / `skipped` / `unmerged`), and
    append every spawn to `run.json`'s `spawns` as the pass goes out.
15. **Close the pass, report, and loop.** The pass is closed only once every task dispatched into
    it — including every rework, integration retry, and contract reopening spawned to resolve it —
    has reached a terminal status (MERGED, BLOCKED, UNMERGED, or SKIPPED). **Start no task from
    the next pass early.** Then report what finished, in one short block: each task's title and
    outcome, anything not merged and why, and how many candidates remain. Then go back to step 2.
    The phase itself is done when the candidate set is empty and every task is MERGED, BLOCKED,
    SKIPPED, or UNMERGED.

## Phase 6 — Verify & hand off

Every merged task was only ever reviewed against its *own* acceptance criteria, by design.
Nothing before this point has run the assembled result, and nothing before this point has
compared it to what was actually asked for. This phase does both. Do not skip it.

1. **Run the suite** the test plan named in Phase 3 (in `micro` mode: the repo's own test/build
   command) against `dag/<run-id>`, once, and record the verbatim outcome in
   `<run-dir>/integration.md`. Individually-merged tasks can still be collectively broken.
2. **Acceptance review.** Spawn one `integration-reviewer` with the original request, the merged
   spec's path, the `base_commit`, and the branch name. It reviews the *whole diff*
   (`git diff <base_commit>..dag/<run-id>`) against the spec and the request — the only agent in
   the pipeline that ever does — and ends with `INTEGRATION: PASS` or `INTEGRATION: FLAGGED <n>`.
   Its findings go into the final summary; they do **not** trigger another build loop. Fixing
   them is a fresh request, not a silent extra loop here.
3. **If the integrated suite failed, report the failure honestly with its output.** Do not
   describe the run as successful.
4. **Clean up and hand back**: `git worktree prune`; keep (and name) only the worktrees of
   UNMERGED tasks; check the main working tree back out to `base_branch`, leaving `dag/<run-id>`
   intact for the user to review. Mark phase 6 complete in `run.json`.

## Done

Report a summary and stop:

- the run id and the integration branch name;
- tasks MERGED / BLOCKED / SKIPPED / UNMERGED, with accumulated findings for anything not
  MERGED, and the worktree path of anything UNMERGED;
- the integrated suite result, verbatim if it failed;
- the acceptance review's verdict and findings;
- blocker classes aggregated — **if most failures are `spec-defect`, say so loudly**: it means
  the spec was the problem, and that sentence is worth more to the user than another retry round
  would have been;
- any shared contract that had to be revised mid-run, what changed, and which tasks were reopened
  because of it — a contract that broke under a worker is the clearest evidence available about
  where the spec was wrong;
- any Open Concerns carried from Phase 2 or Phase 3;
- anything dropped to stay inside budget, any agent that failed twice, and the spend: spawn
  counts by model tier and the unit total against the mode's budget;
- where the run state lives (`.dag/runs/<run-id>/`).

The user reviews the branch. Merging it anywhere is their call, not yours.
