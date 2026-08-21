# yolo-dag

A Claude Code plugin that turns an ambiguous request into a reviewed, executed, integrated branch
— structured as a directed graph of agent nodes rather than a linear script (see "Design: a DAG
with bounded loops" below).

![yolo-dag workflow: /brainstorm hands off to the orchestrator, which routes the request to the relevant domain specialists, runs a bounded adversarial review loop per specialist, merges and reconciles their deliverables into one build spec, decomposes it into a task DAG, executes that DAG in topological waves, and integrates the finished worktrees onto one branch](assets/workflow.svg)

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
   accepts valid findings and pushes back on the rest. A round closes early when all three
   reviewers come back clean.
3. **Merge & reconcile** — combines the deliverables into one build spec, then runs
   `spec-reconciler` over the whole thing. Every review up to this point was *intra*-specialist
   and blind to its siblings, so nothing before this could catch two deliverables that are each
   internally excellent and mutually incompatible — the architecture spec and the data spec
   assuming different datastores, say. Contradictions go back to both specialists; anything still
   unresolved becomes an Open Concern.
4. **Decompose** — `task-specialist` breaks the merged spec into a task DAG: tasks with explicit
   `depends_on` edges, checkable acceptance criteria, and a declared file footprint. The
   orchestrator validates the graph is acyclic and computes execution waves.
5. **Execute & verify** — wave by wave, up to 10 tasks at a time: workers implement in isolated
   worktrees, reviewers check each against its own acceptance criteria, flagged tasks get
   reassigned to a fresh worker (capped at 3 attempts), and a task's dependents get its report as
   context.
6. **Integrate** — merges each passing worktree onto a `dag/<run-id>` branch in dependency order,
   reworks any conflict through a fresh worker rather than resolving it silently, then runs the
   suite once against the assembled result. Individually-passing tasks can still be collectively
   broken, and nothing before this point has run the whole thing.

`brainstorm` and `orchestrator` are separate skills — `brainstorm` is the only one the user talks
to directly, and hands off to `orchestrator` (via the `Skill` tool) once the request is fully
specified. Every agent below that point is spawned directly by `orchestrator` — no agent here
spawns further agents itself; multi-round specialist revisions are done by resuming the original
agent via `SendMessage`, not by re-spawning it.

## Run state

Every phase persists to `.dag/runs/<run-id>/` as it completes:

```
.dag/runs/2026-08-21-a3f9/
├── request.md            # the restated request + stated assumptions
├── routing.md            # which specialists were selected, and why the rest weren't
├── specialists/<name>/round-{1,2,3}.md
├── merged-spec.md
├── reconcile.md          # cross-specialist contradictions and how each resolved
├── tasks.json            # the task DAG, with per-task status and attempt count
└── integration.md        # per-task merge outcome + the integrated suite result
```

A worst-case `full` run spawns north of a hundred agents before a line of code is written, so
losing one to a dropped connection is not acceptable. Inspect a run with `/dag-status`, list them
with `/dag-runs`, and pick an interrupted one back up with `/dag-resume <run-id>` — it re-enters
at the furthest completed phase instead of starting over.

`.dag/` is gitignored; it's local working state, not repo content.

## Modes

A run is either `full` or `lite`. They differ in how much scrutiny the request gets, and
therefore in what it costs:

| | `full` | `lite` |
|---|---|---|
| Specialists | all routing selects | routing, capped at 4 |
| Review rounds | up to 3 | 1 |
| Wave width | 10 tasks | 5 tasks |
| Soft spawn budget | 120 agents | 40 agents |

### Passing a mode

Put the flag anywhere in the request — the start reads most clearly. It's stripped from the
request text before the specialists see it.

```
/brainstorm mode=lite fix the typo in the onboarding email copy
/brainstorm mode=full add SSO to the admin console
/brainstorm --all rewrite the billing service
```

- **`mode=lite`** — one review round, at most 4 specialists. For small, well-understood, low-risk
  work where a full adversarial pass is overkill.
- **`mode=full`** — every routed specialist, up to 3 review rounds each. For real features,
  anything touching architecture or data, anything you'd want a second opinion on.
- **`--all`** — forces all eight specialists and skips the routing judgment entirely. Combines
  with either mode. Reach for it when you think routing will wrongly skip a domain that matters.

### If you pass nothing

`/brainstorm <request>` sizes the run itself from the shape of the request and tells you which
mode it picked. That's the intended path — the flags exist for when you know something about the
stakes that the request text doesn't carry, and an explicit flag always beats the model's own
judgment.

There is no separate "default" mode to memorize: unflagged runs get whichever of the two fits.

### Budget degradation

When a run crosses its spawn budget the orchestrator degrades rather than silently overspending
or stopping halfway: fewer review rounds first, then a narrower specialist set, then narrower
waves — and it says what it dropped. A `lite` run that turns out to be bigger than it looked
degrades within `lite`; it does not quietly become a `full` run.

Agents declare their own model tier: `spec-consolidator` runs on Haiku (it deduplicates and ranks
a list someone else wrote), `task-reviewer` on Sonnet (it checks explicit criteria against a
diff), and everything doing load-bearing reasoning inherits the session model.

## Design: a DAG with bounded loops

Structurally, this pipeline is a workflow **DAG** — a Directed Acyclic Graph: nodes connected by
one-way edges, with no path that loops back to where it started, so there's always a valid
execution order and nothing waits on itself. It's the same shape workflow engines like Airflow,
Dagster, and Temporal use, because it guarantees no deadlocks and makes it obvious what can run
in parallel (anything with no unresolved edges pointing into it).

- **Fan-out/fan-in** happens twice: the specialists converge at Merge (Phase 3), and within each
  specialist's review round, the 3 reviewers converge at the consolidator.
- **The task layer is a real DAG.** `task-specialist` emits tasks with explicit `depends_on`
  edges, and the orchestrator executes them in topological waves — wave 0 is everything with no
  dependencies, wave *n* is everything whose dependencies landed earlier. An earlier version of
  this plugin forbade cross-task dependencies entirely and folded ordered work into single
  oversized tasks, because the execution layer couldn't express "wait for task N." It can now, so
  "define the schema" and "write the migration" are two tasks with an edge rather than one task
  that's hard to review.
- **The review loop (up to 3 rounds) and the retry loop (up to 3 attempts) are the one place this
  isn't a pure DAG.** A strict DAG can't have cycles — resuming a specialist or reassigning a
  flagged task is cyclic control flow. In practice it behaves like a *bounded, unrolled* DAG:
  round 1 and round 2 are really distinct nodes, capped so the loop provably terminates instead
  of running forever. This is the same compromise most real workflow engines make, since a pure
  acyclic graph can't express "try again, up to N times" on its own.

## Structure

```
yolo-dag/
├── skills/
│   ├── brainstorm/              # entrypoint — resolves ambiguity, hands off to orchestrator
│   └── orchestrator/            # route, review loops, merge+reconcile, decompose, execute, integrate
├── commands/
│   ├── dag-runs.md              # list past runs
│   ├── dag-status.md            # inspect one run in detail
│   └── dag-resume.md            # resume an interrupted run
├── agents/
│   ├── design-specialist.md
│   ├── architecture-specialist.md
│   ├── research-specialist.md
│   ├── security-specialist.md
│   ├── test-planning-specialist.md
│   ├── cost-estimation-specialist.md
│   ├── ux-copy-specialist.md
│   ├── data-schema-specialist.md
│   ├── spec-reviewer.md         # generic role, spawned 3x per review round with a distinct angle each time
│   ├── spec-consolidator.md     # generic role, spawned once per review round
│   ├── spec-reconciler.md       # spawned once in Phase 3; the only agent that sees every deliverable at once
│   ├── task-specialist.md       # decomposes the merged spec into a task DAG
│   ├── task-worker.md           # generic role, spawned up to 10x per wave, worktree-isolated
│   └── task-reviewer.md         # generic role, spawned up to 10x per wave
├── evals/                       # behavioural eval suite (see evals/README.md)
└── scripts/validate.py          # structural validator, run in CI
```

## Development

```
python3 scripts/validate.py
```

Checks the invariants that hand-editing reliably breaks: manifests parse, agent frontmatter is
well-formed, every tool name is real, no agent has `Agent` in its tools (all fan-out is flat, from
the orchestrator), phase numbers agree between the orchestrator and every agent citing them, and
the literal status-line contracts the orchestrator branches on — `VERDICT: PASS`, `REVIEW: CLEAN`,
`RECONCILE: CLEAN` — exist at both ends. It runs on a bare Python 3 with no dependencies, in CI on
every push.

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
