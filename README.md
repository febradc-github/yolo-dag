# yolo-dag

A Claude Code plugin that turns an ambiguous request into a reviewed, executed, integrated branch
— structured as a directed graph of agent nodes rather than a linear script (see "Design: a DAG
with bounded loops" below).

![yolo-dag workflow: /brainstorm asks whether this is a plan-only run or a full run, sizes the run, and hands off to the orchestrator, which routes the request to the relevant domain specialists, runs a bounded adversarial review loop per specialist, merges and reconciles their deliverables into one build spec, decomposes it into independent tasks bound by shared contracts, then implements directly on a full run or asks first on a plan-only run — executing in user-paced passes, one number asked per pass and the orchestrator picking the tasks, merging each approved task onto the integration branch — then verifies the assembled branch with the full suite and an acceptance review against the original request](assets/workflow.jpeg)

A more literal, kept-current diagram of the same flow — including where `meter` (on by default)
sits — is in [`assets/workflow.svg`](assets/workflow.svg).

## Workflow

Run `/brainstorm <your request>`:

**Brainstorm (entrypoint)** — asks one question first: **plan-only run, or full run?** That single
answer sets everything about how the run treats you afterwards (see [Run type](#run-type)). Then
it resolves ambiguity in the request: decides autonomously by default and only asks when a
decision is genuinely high-stakes (irreversible, expensive, or touches security/data); everything
else gets a sensible default, stated explicitly so it's visible and correctable. Picks a run size,
then hands off to the orchestrator — the user is never blocked answering questions they don't
care about.

The orchestrator then runs six phases:

1. **Route & fan-out** — selects the specialists whose domains the request actually touches, from
   eight available: design, architecture, research & discovery, security, test planning, cost &
   resource estimation, UX copy, data & schema. Architecture, research, and test planning always
   run; the rest are routed in only when relevant. A CLI refactor doesn't need a UX copy spec, and
   producing one costs a full review loop to add noise to the build spec.
2. **Build + review, per specialist** — each specialist's deliverable goes through up to 3 rounds
   of adversarial review (3 independent reviewers + 1 consolidator per round); the specialist
   accepts valid findings and pushes back on the rest. The three reviewers work three genuinely
   different checklists — completeness/gaps, internal consistency (run on a different model tier,
   to decorrelate), feasibility/risk. A round closes early when all three come back clean, when
   consolidation dissolves the findings to zero, or when the specialist has pushed back on
   everything — another identical round rarely moves a considered pushback.
3. **Merge & reconcile** — combines the deliverables into one build spec, then runs
   `spec-reconciler` over the whole thing. Every review up to this point was *intra*-specialist
   and blind to its siblings, so nothing before this could catch two deliverables that are each
   internally excellent and mutually incompatible — the architecture spec and the data spec
   assuming different datastores, say. Contradictions go back to both specialists; anything still
   unresolved becomes an Open Concern.
4. **Decompose** — `task-specialist` breaks the merged spec into tasks designed to be
   **independent first**: where two tasks would touch the same interface, schema, or event shape,
   it writes that boundary out as a **shared contract** and gives both sides the same text
   verbatim, so they build against it in parallel instead of queueing behind each other. A real
   `depends_on` edge is the last resort, and carries a reason for why it couldn't be designed
   away. Every file is owned by exactly one task, so a task record now carries `id`, `title`,
   `description`, `acceptance_criteria`, `owns` (the files it alone creates or modifies),
   `contracts` (which shared contracts it owns or consumes), and `depends_on` (task ids, each with
   a reason, empty by default). The orchestrator validates all of that — unique
   ids, acyclicity, no shared file ownership, every contract defined and owned — and reports the
   graph's shape. It then reports the plan (routing, merged spec, graph, Open Concerns) and
   branches on the run type: a **full run** enters implementation immediately, a **plan-only run**
   stops here and asks. A "not yet" leaves the run parked, resumable later with `/dag-resume`.
5. **Execute, verify & merge** — user-paced passes, not a continuous ready-queue: the run's
   `dag/<run-id>` integration branch is created first, then before every pass the orchestrator
   asks **one number** — how many tasks to run in parallel — and picks the tasks itself, so a
   review pass covers one bounded batch of finished work instead of the whole DAG landing at once.
   Every task in a pass is fully independent of every other task in it — when the number you asked
   for would pull in a task that isn't, it runs the eligible subset and says why rather than asking
   again ("You asked for 3 tasks. Task 3 depends on Task 2, so only Tasks 1 and 2 can run in this
   pass"), and the same when fewer independent tasks remain than you asked for. Each worker
   implements in
   an isolated worktree cut from the branch's current tip, so a dependent's tree literally contains
   its dependencies' finished code, not a prose summary of it. Workers report where their work
   lives via a machine trailer (worktree, commit sha, blocker class); reviewers verify inside that
   worktree against the task's own acceptance criteria and contracts; a flagged task goes back to
   its own worker to rework and returns to the reviewer (2 reworks, then BLOCKED, dependents
   SKIPPED) — **a task is done only when a reviewer approves it**; passing tasks merge onto the
   branch immediately, with merge conflicts reworked by a fresh worker on top of the integrated
   code rather than resolved silently. The pass is only closed — and the next number asked — once
   every task dispatched into it, retries included, reaches a terminal state.
6. **Verify & hand off** — runs the suite once against the assembled branch (individually-passing
   tasks can still be collectively broken), then a final acceptance review compares the *whole
   diff* against the merged spec and the original request — the only point in the pipeline where
   anything checks the result against what was asked for. Findings go in the summary; the user
   reviews the branch.

`brainstorm` and `orchestrator` are separate skills — `brainstorm` is the only one the user talks
to directly, and hands off to `orchestrator` (via the `Skill` tool) once the request is fully
specified. Every agent below that point is spawned directly by `orchestrator` — no agent here
spawns further agents itself; multi-round specialist revisions are done by resuming the original
agent via `SendMessage`, not by re-spawning it.

## Before you run this

Three practical realities, stated here because discovering them mid-run is worse:

- **It needs git, and a clean tree.** The pipeline's product is a branch; it records the base
  commit up front, keeps the working tree checked out on `dag/<run-id>` during execution, and
  refuses to start over uncommitted changes (they'd be invisible to every worker and at risk on
  the branch switch). It never touches `main` or your base branch.
- **It spawns a lot of agents that edit files and run commands.** In a default-permission
  session that means a wall of permission prompts, which defeats the plugin's central promise
  that you aren't interrupted. Run it in a session whose permission mode you've chosen
  deliberately (e.g. auto-accept edits, or an allowlist covering `git`, your test runner, and
  file edits). Everything it writes lands in `.dag/`, isolated worktrees, and one `dag/<run-id>`
  branch.
- **What it costs.** A worst-case `full` run spawns north of a hundred agents. `micro` exists for
  the other end of the scale, and a plan-only run lets you see the plan before paying for the
  build.

## Run state

Every phase persists to `.dag/runs/<run-id>/` as it completes:

```
.dag/runs/2026-08-21-a3f9/
├── run.json              # machine-readable manifest: mode, run type (`plan_only`), whether the
│                         #   run is non-interactive (and its `tasks_per_pass`), base commit,
│                         #   phase status, spawn ledger (the budget is computed from this),
│                         #   degradations
├── request.md            # the restated request + stated assumptions
├── routing.md            # which specialists were selected, and why the rest weren't
├── specialists/<name>/round-{1,2,3}.md
├── merged-spec.md
├── reconcile.md          # cross-specialist contradictions and how each resolved
├── tasks.json            # the task graph and its shared contracts, with per-task status
│                         #   and attempt count
└── integration.md        # the integrated suite result + the acceptance review
```

`run.json` is the source of truth for code (commands and resume branch on its `phases` map — an
entry is only marked complete after its artifacts are fully written); the markdown files are the
human surface.

With that many agents in flight before a line of code is written, losing a run to a dropped
connection is not acceptable. Inspect a run with `/dag-status`, list them
with `/dag-runs`, pick an interrupted one back up with `/dag-resume <run-id>` — it re-enters at
the first phase not marked complete instead of starting over — stop an in-flight one cleanly
with `/dag-cancel`, and clear old debris with `/dag-clean`.

`.dag/` is gitignored; it's local working state, not repo content.

## Run type

The first thing `/brainstorm` asks — before clarifying questions, before reading any code — is
one question:

> **Is this a plan-only run, or should it continue into implementation?**

That answer holds for the whole run and sets two things:

| | **Full run** | **Plan-only run** |
|---|---|---|
| At the implementation boundary | enters implementation with no confirmation, never re-asks | stops and asks; starts nothing until you say go |
| Tasks in parallel per pass | you pick, up to **3** | you pick, **no maximum** |

Those are the only two interactive gates in the pipeline: the plan-only implementation boundary,
and the one-number question before each execution pass. Everything else — routing, design calls,
how Open Concerns resolve, how a tight budget degrades — the pipeline decides and reports. A
caller with no user attached pre-answers both rather than hanging on them; see
[Unattended runs](#unattended-runs).

Passing `--plan-only` or `--full-run` pre-answers the question, so it never gets asked. They are
mutually exclusive — passing both is an error, not a precedence puzzle.

The asymmetry on parallelism is deliberate: a plan-only run has already had the whole task graph
read and approved by a human, so there's nothing left for a ceiling to protect. A full run hasn't,
so its passes stay small enough to review as they land.

## Modes

The mode is a separate axis from the run type: it sizes how much scrutiny the request gets, and
therefore what it costs. A run is `full`, `lite`, or `micro`:

| | `full` | `lite` | `micro` |
|---|---|---|---|
| Phases | all six | all six | 4–6 only |
| Specialists | all routing selects | routing, capped at 4 | none — the request is the spec |
| Review rounds | up to 3 | 1 | none |
| Soft spawn budget | 120 units | 40 units | 8 units |

The mode does not set concurrency — how many tasks run at once is asked per pass, capped by the
run type.

### Passing a mode

Put the flag anywhere in the request — the start reads most clearly. It's stripped from the
request text before the specialists see it. The first line below is the argument hint the
slash-command picker shows; the rest are real invocations.

```
/brainstorm Request [mode=lite|full|micro] [--all] [--plan-only|--full-run]

/brainstorm mode=micro fix the typo in the onboarding email copy
/brainstorm mode=lite add a --json flag to the export command, matching the existing --csv one
/brainstorm mode=full add SSO to the admin console
/brainstorm --all rewrite the billing service
/brainstorm --plan-only migrate the session store to Redis
/brainstorm --full-run mode=lite add a --json flag to the export command
```

- **`mode=micro`** — no specialists, no spec review: straight to decomposition and execution, a
  handful of agents in total. For work that is fully specified by its own one-sentence statement
  — a typo, a rename, a config value. Before `micro` existed, the cheapest path still spent
  ~25 agents reviewing a typo fix.
- **`mode=lite`** — one review round, at most 4 specialists. For small, well-understood, low-risk
  work that still deserves a spec.
- **`mode=full`** — every routed specialist, up to 3 review rounds each. For real features,
  anything touching architecture or data, anything you'd want a second opinion on.
- **`--all`** — forces all eight specialists and skips the routing judgment entirely. Combines
  with `full` or `lite` (not `micro`, which has no specialists). Reach for it when you think
  routing will wrongly skip a domain that matters.
- **`--plan-only`** — sets the [run type](#run-type) without being asked for it. The orchestrator
  stops at the implementation boundary with the routing, the merged spec, and the task graph laid
  out for review, and starts nothing until you say go. Execution is the majority of the cost and
  the only part that changes files; answer "not yet" and `/dag-resume <run-id>` executes the plan
  later, once you've read it.
- **`--full-run`** — the counterpart: sets the run type to full without being asked, so the
  pipeline goes from the plan straight into implementation. Mutually exclusive with
  `--plan-only`; passing both is an error rather than a precedence rule.

### Unattended runs

An eval runner or a scripted caller has nobody to answer the two questions above, so it
pre-answers them instead. Both flags are required together, alongside a run type — including on a
plan-only run, which parks before it would ever ask for a number, because the number is run state
that `/dag-resume` picks up later rather than an answer to one prompt:

```
/brainstorm --full-run --non-interactive --tasks-per-pass 3 mode=lite add a --json flag to the export command
```

- **`--non-interactive`** — declares that no user is attached. It is always explicit, and is
  **never inferred** — not from a missing TTY, not from an unanswered question. It lands in
  `run.json` as `non_interactive`, so a resumed run behaves the same way.
- **`--tasks-per-pass N`** — answers the per-pass question with `N` for every pass in the run,
  recorded as `tasks_per_pass`. It is honored **only** in non-interactive mode: passing it to an
  ordinary interactive run is an error, because the per-pass pause is a review checkpoint rather
  than a formality. `N` is still bound by the run type's cap (3 on a full run, uncapped on a
  plan-only one) and passing a larger one fails at startup instead of being quietly clamped —
  though an `N` larger than the number of *eligible* tasks is fine, and runs the eligible subset
  exactly as a typed answer would.

Nothing else changes: the plan is still reported before implementation, dependency rules still
decide what is eligible, and a pass still has to finish completely before the next one starts. A
non-interactive plan-only run reports its plan and parks at the implementation boundary, where
`/dag-resume <run-id>` is the go-ahead — and the `tasks_per_pass` it was started with is the
number that resumed pass uses.

These three flags are documented here rather than in the slash-command hint, which stays short
enough to read at a glance and carries only what you'd type by hand.

### If you pass nothing

`/brainstorm <request>` sizes the run itself from the shape of the request and tells you which
mode it picked. That's the intended path — the flags exist for when you know something about the
stakes that the request text doesn't carry, and an explicit flag always beats the model's own
judgment.

There is no separate "default" mode to memorize: unflagged runs get whichever of the three fits.
(The one asymmetry: invoking the `orchestrator` skill directly with no mode at all falls back to
`full` — over-reviewing costs money, under-reviewing costs correctness.)

### Budget degradation

The budget is cost-weighted — a session-model spawn counts 1.0 units, Sonnet 0.5, Haiku 0.1 — and
computed from the spawn ledger in `run.json`, never from the orchestrator's memory of it (a
compacted context forgets spawns, and that drift is silent overspend). When a run crosses its
ceiling the orchestrator degrades rather than silently overspending or stopping halfway: fewer
review rounds first, then a narrower specialist set, then a lower parallelism cap at the per-pass
prompt (in a plan-only run, which has no cap, it states what a large pass will cost instead of
capping it) — and it says what it dropped. A `lite` run that turns out to be bigger than it looked degrades within `lite`;
it does not quietly become a `full` run.

Agents declare their own model tier: `spec-consolidator` runs on Haiku (it deduplicates and ranks
a list someone else wrote), `task-reviewer` on Sonnet (it checks explicit criteria against a
diff), and everything doing load-bearing reasoning inherits the session model. The orchestrator
passes no model overrides, with one documented exception: the internal-consistency reviewer in
each round runs on Sonnet, because three same-model reviewers converge on the same findings and
miss the same things — model diversity decorrelates them better than a different adjective in
the prompt.

## Design: a DAG with bounded loops

Structurally, this pipeline is a workflow **DAG** — a Directed Acyclic Graph: nodes connected by
one-way edges, with no path that loops back to where it started, so there's always a valid
execution order and nothing waits on itself. It's the same shape workflow engines like Airflow,
Dagster, and Temporal use, because it guarantees no deadlocks and makes it obvious what can run
in parallel (anything with no unresolved edges pointing into it).

- **Fan-out/fan-in** happens twice: the specialists converge at Merge (Phase 3), and within each
  specialist's review round, the 3 reviewers converge at the consolidator.
- **The task layer is a real DAG, but a deliberately flat one, scheduled in user-paced passes.**
  `task-specialist` optimizes for a graph with as few edges as possible: where two tasks share a
  boundary, it writes the boundary down as a contract both sides code against, rather than making
  one wait for the other. Edges that survive that carry a reason. The orchestrator validates
  acyclicity via topological levels ("waves" — still how a run's shape is reported), then executes
  in passes: before each one it asks how many tasks to run in parallel, selects an independent set
  itself, critical path first, and won't dispatch the next pass until every task in the current
  one — reworks included — has reached a terminal state. That keeps a review pass scoped to one
  batch of work instead of the whole DAG landing at once. Edges are still made real by continuous
  integration across passes — a dependent's worktree is cut from the integration branch after its
  dependencies landed, so it builds on their actual code.
- **The review loop (up to 3 rounds), the rework loop (up to 2 reworks per task), and contract
  revision are the one place this isn't a pure DAG.** A strict DAG can't have cycles — resuming a
  specialist, sending a flagged task back to its worker, or reopening every consumer of a revised
  contract is cyclic control flow. In practice it behaves like a *bounded, unrolled* DAG: round 1
  and round 2 are really distinct nodes, capped so the loop provably terminates instead of running
  forever. This is the same compromise most real workflow engines make, since a pure acyclic graph
  can't express "try again, up to N times" on its own. The same discipline bounds the other two
  feedback paths: a contract can be revised once, and when most task failures classify the *spec*
  as the problem, the graph goes back to `task-specialist` for correction exactly once per run.

## Meter (on by default, measurement only)

`meter` is a local, zero-network subsystem, shipped inside this plugin and **on by default**,
that measures real per-node token spend via Claude Code's hooks — see
[`meter-handoff.md`](meter-handoff.md) for the full design. **Only its first milestone is
implemented: the Ledger.** It measures and changes nothing else about how a run behaves — no tool
input or output is ever rewritten by it. Later milestones (context packs, output compression, a
patch cache, budget recalibration) don't ship until the Ledger has produced real numbers against
real runs.

It starts itself on `SessionStart` (a loopback-only daemon on `127.0.0.1:47615` by default) and
reports via `/dag-cost <run-id>`. Run `python3 scripts/dag-doctor.py` any time to see its
capability matrix, including which of its own design assumptions are still unverified against
your Claude Code install.

Its HTTP hooks authenticate with a token, but only advisorily: Claude Code has no documented way
for a `SessionStart` hook to hand a freshly generated secret to the hooks that fire afterward, so
an unauthenticated local request is still processed rather than rejected — safe here because this
milestone only ever *observes* (no hook response from it ever rewrites a tool's input or output).
`dag-doctor` reports the authenticated/unauthenticated split so you can see whether your
environment happens to wire the token through.

**To turn it off** (or back on), run `/dag-meter off` / `/dag-meter on` — `/dag-meter status`
reports whether it's currently running. It persists the choice to `.dag/meter.json` for every
future session, and also stops (or starts) this session's daemon immediately where it can. The
same switch by hand: `DAG_METER_DISABLE=1`, `{"enabled": false}` in `.dag/meter.json`, or Claude
Code's own `disableAllHooks`. With it off, this plugin is byte-for-byte its current self.

## Structure

```
yolo-dag/
├── skills/
│   ├── brainstorm/              # entrypoint — asks the run type, resolves ambiguity, hands off
│   └── orchestrator/            # route, review loops, merge+reconcile, decompose, execute, integrate
├── commands/
│   ├── dag-runs.md              # list past runs
│   ├── dag-status.md            # inspect one run in detail
│   ├── dag-resume.md            # resume an interrupted (or --plan-only) run
│   ├── dag-cancel.md            # stop an in-flight run, resumably
│   ├── dag-clean.md             # prune worktrees, run state, and (with confirmation) branches
│   ├── dag-cost.md              # measured token spend for one run, from meter's Ledger
│   └── dag-meter.md             # turn meter on/off, or check whether it's running
├── hooks/hooks.json              # meter's Claude Code hook registration (M0 subset)
├── meter/                        # meter's package — see meter-handoff.md; M0 (Ledger) only
├── agents/
│   ├── design-specialist.md
│   ├── architecture-specialist.md
│   ├── research-specialist.md
│   ├── security-specialist.md
│   ├── test-planning-specialist.md
│   ├── cost-estimation-specialist.md
│   ├── ux-copy-specialist.md
│   ├── data-schema-specialist.md
│   ├── spec-reviewer.md         # generic role, spawned 3x per review round, one named checklist each
│   ├── spec-consolidator.md     # generic role, spawned once per review round
│   ├── spec-reconciler.md       # spawned once in Phase 3; the only agent that sees every deliverable at once
│   ├── task-specialist.md       # decomposes the merged spec into a task DAG
│   ├── task-worker.md           # generic role, worktree-isolated, ends with a machine trailer
│   ├── task-reviewer.md         # generic role, verifies inside the worker's worktree
│   └── integration-reviewer.md  # spawned once in Phase 6; reviews the whole diff against the request
├── evals/                       # behavioural eval suite (see evals/README.md)
└── scripts/
    ├── validate.py               # structural validator, run in CI
    ├── dag-doctor.py             # meter's capability matrix and VERIFY probes
    ├── dag-meter.py              # meter's on/off/status CLI, behind /dag-meter
    ├── meter-boot.py             # meter's SessionStart hook (starts/refcounts the daemon)
    └── meter-hook.py             # meter's SessionEnd hook (main-thread accounting, refcounting)
```

## Development

```
python3 scripts/validate.py
```

Checks the invariants that hand-editing reliably breaks: manifests parse, agent frontmatter is
well-formed, every tool name is real, no agent has `Agent` in its tools (all fan-out is flat, from
the orchestrator), phase numbers agree between the orchestrator and every agent citing them, the
literal status-line contracts the orchestrator branches on — `VERDICT: PASS`, `REVIEW: CLEAN`,
`RECONCILE: CLEAN`, `INTEGRATION: PASS`, the worker's `WORKTREE:`/`COMMIT:` trailer — exist at
both ends, the mode table is identical in the three files that state it, every documented flag is
one the orchestrator parses, and the `run.json` keys the commands read are keys the orchestrator
writes. It runs on a bare Python 3 with no dependencies, in CI on every push.

`python3 scripts/dag-doctor.py` checks meter's own prerequisites separately (Python version,
loopback bind, and which of its design assumptions about Claude Code's hook payloads are still
unverified) — see [Meter](#meter-on-by-default-measurement-only) above.

The behavioural eval suite lives in [evals/](evals/) and runs with `claude plugin eval .`. It is
authored but **unexecuted** — `plugin eval` is gated behind early access — so treat its schema as
unverified. See [evals/README.md](evals/README.md).

## Local testing

```
cc --plugin-dir /path/to/yolo-dag
```

## Install

```
/plugin marketplace add febradc-github/yolo-dag
/plugin install yolo-dag@yolo-dag
```

## License

MIT — see [LICENSE](LICENSE).
