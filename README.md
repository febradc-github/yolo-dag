# yolo-dag

A Claude Code plugin that turns an ambiguous request into reviewed, independent, executable
tasks — structured as a directed graph of agent nodes rather than a linear script (see
"Design: a DAG with bounded loops" below).

![yolo-dag workflow: /brainstorm hands off to the orchestrator, which fans out to 8 domain specialists, runs a bounded adversarial review loop per specialist, merges into one build spec, decomposes it into independent tasks, then executes and verifies them in a bounded retry loop](assets/workflow.svg)

## Workflow

Run `/brainstorm <your request>`:

1. **Brainstorm (entrypoint)** — resolves ambiguity in the request. Decides autonomously by
   default and only asks the user when a decision is genuinely high-stakes (irreversible,
   expensive, or touches security/data); everything else gets a sensible default, stated
   explicitly so it's visible and correctable. Hands off to the orchestrator once done — the
   user is never blocked answering questions they don't care about.
2. **Orchestrator: fan-out** — spins up 8 domain specialists in parallel against the
   fully-specified request: design, architecture, research & discovery, security, test
   planning, cost & resource estimation, UX copy, data & schema.
3. **Orchestrator: build + review, per specialist** — each specialist's deliverable goes
   through up to 3 rounds of adversarial review (3 independent reviewers + 1 consolidator per
   round); the specialist accepts valid findings and pushes back on the rest.
4. **Orchestrator: merge** — combines all 8 specialists' final deliverables into one build
   spec, with any unresolved review findings called out as Open Concerns (only paused on if
   high-stakes).
5. **Orchestrator: task decomposition** — a task specialist breaks the merged spec into tasks
   with no dependencies on each other.
6. **Orchestrator: execute & verify** — up to 10 tasks at a time: workers implement, reviewers
   check each against its own acceptance criteria, flagged tasks get reassigned to a fresh
   worker (capped at 3 attempts total), and the loop repeats until the task list is empty.

`brainstorm` and `orchestrator` are separate skills — `brainstorm` is the only one the user
talks to directly, and hands off to `orchestrator` (via the `Skill` tool) once the request is
fully specified. Every agent below that point is spawned directly by `orchestrator` — no agent
here spawns further agents itself; multi-round specialist revisions are done by resuming the
original agent via `SendMessage`, not by re-spawning it.

## Design: a DAG with bounded loops

Structurally, this pipeline is a workflow **DAG** — a Directed Acyclic Graph: nodes connected by
one-way edges, with no path that loops back to where it started, so there's always a valid
execution order and nothing waits on itself. It's the same shape workflow engines like Airflow,
Dagster, and Temporal use, because it guarantees no deadlocks and makes it obvious what can run
in parallel (anything with no unresolved edges pointing into it).

- **Fan-out/fan-in** happens twice: the 8 specialists converge at Merge (Phase 3), and within
  each specialist's review round, the 3 reviewers converge at the consolidator.
- **Phase 5's "no dependencies on each other" rule is an edge constraint.** `task-specialist` is
  asked to decompose into a maximally parallel independent set — a graph with no edges among
  sibling task-nodes. Where a real dependency exists, it collapses the two nodes into one
  instead of emitting an edge, since nothing in the execution layer can express "wait for task
  N."
- **The review loop (up to 3 rounds) and the retry loop (up to 3 attempts) are the one place
  this isn't a pure DAG.** A strict DAG can't have cycles — resuming a specialist or reassigning
  a flagged task is cyclic control flow. In practice it behaves like a *bounded, unrolled* DAG:
  round 1 and round 2 are really distinct nodes, capped so the loop provably terminates instead
  of running forever. This is the same compromise most real workflow engines make, since a pure
  acyclic graph can't express "try again, up to N times" on its own.

## Structure

```
yolo-dag/
├── skills/
│   ├── brainstorm/              # entrypoint — resolves ambiguity, hands off to orchestrator
│   └── orchestrator/            # fan-out, review loops, merge, decompose, execute & verify
└── agents/
    ├── design-specialist.md
    ├── architecture-specialist.md
    ├── research-specialist.md
    ├── security-specialist.md
    ├── test-planning-specialist.md
    ├── cost-estimation-specialist.md
    ├── ux-copy-specialist.md
    ├── data-schema-specialist.md
    ├── spec-reviewer.md        # generic role, spawned 3x per review round with a distinct angle each time
    ├── spec-consolidator.md    # generic role, spawned once per review round
    ├── task-specialist.md      # decomposes the merged spec into independent tasks
    ├── task-worker.md          # generic role, spawned up to 10x per batch, worktree-isolated
    └── task-reviewer.md        # generic role, spawned up to 10x per batch
```

## Local testing

```
cc --plugin-dir /path/to/yolo-dag
```

## Install

```
/plugin marketplace add febradc-github/yolo-dag
/plugin install yolo-dag@yolo-dag
```
