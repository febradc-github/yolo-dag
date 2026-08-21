# yolo-dag

A Claude Code plugin that turns an ambiguous request into a reviewed, executed, integrated branch
— structured as a directed graph of agent nodes rather than a linear script (see "Design: a DAG
with bounded loops" below).

![yolo-dag workflow: /brainstorm sizes the run and hands off to the orchestrator, which routes the request to the relevant domain specialists, runs a bounded adversarial review loop per specialist, merges and reconciles their deliverables into one build spec, decomposes it into a task DAG, executes that DAG with a ready-queue that merges each passing task onto the integration branch as it lands, then verifies the assembled branch with the full suite and an acceptance review against the original request](assets/workflow.gif)

## Workflow

Run `/brainstorm <your request>`:

**Brainstorm (entrypoint)** — resolves ambiguity in the request. Decides autonomously by default
and only asks the user when a decision is genuinely high-stakes (irreversible, expensive, or
touches security/data); everything else gets a sensible default, stated explicitly so it's
visible and correctable. Picks a run size, then hands off to the orchestrator — the user is never
blocked answering questions they don't care about.

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
4. **Decompose** — `task-specialist` breaks the merged spec into a task DAG: tasks with explicit
   `depends_on` edges, checkable acceptance criteria, and a declared file footprint. The
   orchestrator validates the graph is acyclic and reports its shape.
5. **Execute, verify & merge** — a ready-queue, not a batch job: the run's `dag/<run-id>`
   integration branch is created first, each worker implements in an isolated worktree cut from
   the branch's current tip, and a task is dispatched the moment its dependencies have merged —
   so a dependent's tree literally contains its dependencies' finished code, not a prose summary
   of it. Workers report where their work lives via a machine trailer (worktree, commit sha,
   blocker class); reviewers verify inside that worktree against the task's own acceptance
   criteria; flagged tasks get a fresh worker (3 attempts, then BLOCKED, dependents SKIPPED);
   passing tasks merge onto the branch immediately, with merge conflicts reworked by a fresh
   worker on top of the integrated code rather than resolved silently.
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
  the other end of the scale, and `--plan-only` lets you see the plan before paying for the
  build.

## Run state

Every phase persists to `.dag/runs/<run-id>/` as it completes:

```
.dag/runs/2026-08-21-a3f9/
├── run.json              # machine-readable manifest: mode, base commit, phase status,
│                         #   spawn ledger (the budget is computed from this), degradations
├── request.md            # the restated request + stated assumptions
├── routing.md            # which specialists were selected, and why the rest weren't
├── specialists/<name>/round-{1,2,3}.md
├── merged-spec.md
├── reconcile.md          # cross-specialist contradictions and how each resolved
├── tasks.json            # the task DAG, with per-task status and attempt count
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

## Modes

A run is `full`, `lite`, or `micro`. They differ in how much scrutiny the request gets, and
therefore in what it costs:

| | `full` | `lite` | `micro` |
|---|---|---|---|
| Phases | all six | all six | 4–6 only |
| Specialists | all routing selects | routing, capped at 4 | none — the request is the spec |
| Review rounds | up to 3 | 1 | none |
| Concurrent tasks | 10 | 5 | 2 |
| Soft spawn budget | 120 units | 40 units | 8 units |

### Passing a mode

Put the flag anywhere in the request — the start reads most clearly. It's stripped from the
request text before the specialists see it.

```
/brainstorm mode=micro fix the typo in the onboarding email copy
/brainstorm mode=lite add a --json flag to the export command, matching the existing --csv one
/brainstorm mode=full add SSO to the admin console
/brainstorm --all rewrite the billing service
/brainstorm --plan-only migrate the session store to Redis
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
- **`--plan-only`** — stop cleanly after Phase 4 with the routing, the merged spec, and the task
  graph, before any code is written. Execution is the majority of the cost and the only part
  that changes files; `/dag-resume <run-id>` executes the plan once you've read it.

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
review rounds first, then a narrower specialist set, then fewer concurrent tasks — and it says
what it dropped. A `lite` run that turns out to be bigger than it looked degrades within `lite`;
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
- **The task layer is a real DAG, scheduled like one.** `task-specialist` emits tasks with
  explicit `depends_on` edges; the orchestrator validates acyclicity via topological levels
  ("waves" — still how a run's shape is reported), then executes with a **ready-queue**: a task
  dispatches the moment its dependencies are merged and a slot is free, critical path first,
  rather than the whole level waiting on its slowest member. Edges are made real by continuous
  integration — a dependent's worktree is cut from the integration branch after its dependencies
  landed, so it builds on their actual code.
- **The review loop (up to 3 rounds) and the retry loop (up to 3 attempts) are the one place this
  isn't a pure DAG.** A strict DAG can't have cycles — resuming a specialist or reassigning a
  flagged task is cyclic control flow. In practice it behaves like a *bounded, unrolled* DAG:
  round 1 and round 2 are really distinct nodes, capped so the loop provably terminates instead
  of running forever. This is the same compromise most real workflow engines make, since a pure
  acyclic graph can't express "try again, up to N times" on its own. The same discipline bounds
  the spec-defect feedback path: when most task failures classify the *spec* as the problem, the
  graph goes back to `task-specialist` for correction exactly once per run.

## Structure

```
yolo-dag/
├── skills/
│   ├── brainstorm/              # entrypoint — resolves ambiguity, hands off to orchestrator
│   └── orchestrator/            # route, review loops, merge+reconcile, decompose, execute, integrate
├── commands/
│   ├── dag-runs.md              # list past runs
│   ├── dag-status.md            # inspect one run in detail
│   ├── dag-resume.md            # resume an interrupted (or --plan-only) run
│   ├── dag-cancel.md            # stop an in-flight run, resumably
│   └── dag-clean.md             # prune worktrees, run state, and (with confirmation) branches
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
└── scripts/validate.py          # structural validator, run in CI
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
