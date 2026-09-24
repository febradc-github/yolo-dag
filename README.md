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
├── specialists/<name>/
│   ├── round-0.md                      # initial draft — written directly by the specialist
│   ├── round-<n>-findings-<angle>.md   # each reviewer's findings for round n — written by it
│   ├── round-<n>-consolidated.md       # round n's merged findings — written by the consolidator
│   └── round-<n>.md                    # the specialist's revision after round n
├── merged-spec.md        # every specialist's final round file, concatenated by the orchestrator
├── reconcile.md          # cross-specialist contradictions and rulings — written directly by
│                         #   spec-reconciler
├── brief.md, distill-answer-key.md   # meter's Distill brief + its verification Q/A, if it ran
├── tasks.json            # the task graph and its shared contracts — written directly by
│                         #   task-specialist, with per-task status and attempt count added as
│                         #   execution proceeds
└── integration.md        # the integrated suite result + the acceptance review
```

Every file above that a spawned agent produces is written **directly by that agent**, to the exact
path the orchestrator hands it — never pasted through a chat message and re-written by the
orchestrator. See [Design: agents write their own deliverables](#design-agents-write-their-own-deliverables)
below for why.

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

Agents declare their own model tier: `spec-consolidator` and `spec-distiller` run on Haiku (both
are mechanical — deduplicating and ranking a list someone else wrote, or compiling/verifying a
brief against closed questions), and every other agent — including `task-reviewer` and the three
`spec-reviewer` instances per round — runs on Sonnet. The orchestrator passes no model overrides;
the three `spec-reviewer` angles (completeness/gaps, internal consistency, feasibility/risk)
decorrelate by checklist, not by model.

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

## Design: agents write their own deliverables

Every agent that produces a document-sized artifact — a specialist's deliverable, a reviewer's
findings, a consolidator's merged list, the reconciler's cross-specialist rulings, a distilled
brief, the task graph — carries `Write`, scoped to the one path the orchestrator hands it in its
prompt, and writes there directly. Its final chat message is a short confirmation (the path, plus
a one-line summary or the literal status line its role requires), not the deliverable itself.

This is a deliberate performance choice, not a formatting preference. Earlier, every one of those
agents had no `Write` tool at all and had to return its full deliverable inline, in one final
message, for the orchestrator to persist on its behalf. A single chat message has its own
output-token ceiling, and a document this pipeline produces routinely — a merged spec assembled
from several specialists, a reconciler's full set of cross-specialist rulings — can exceed it
outright. Hitting that ceiling doesn't fail loudly: the agent's message truncates or comes back as
a bare "done" with none of the actual content, and recovery means noticing the truncation and
spending a whole extra resume-and-re-emit round just to get the real output out. Observed on a
single real run: a reconciler hit that ceiling twice on one document and needed a human to
manually chunk its output before the run could continue, and several reviewers/consolidators
returned truncated finals that each cost a resume. None of that is inherent to the work being
reviewed — it's an artifact of the transport, and a `Write` call has no equivalent ceiling.

It also compounds going the other direction: giving the *next* agent a path to `Read` instead of
pasting a large upstream artifact into its prompt means nothing gets restated in full at every
handoff — a reviewer's findings aren't retyped into the consolidator's prompt, the consolidator's
merged list isn't retyped into the `SendMessage` that resumes the specialist, and so on. Smaller
prompts at every hop mean faster responses at every hop.

None of this changes what gets decided — the same number of specialists, review rounds, and tasks
run, at the same depth. It only changes how a deliverable travels once a decision has been made.
Deliverable depth is still expected to track the request's actual complexity (a single-file,
client-only page doesn't need enterprise-scale documentation regardless of how it's transported) —
that's a separate, complementary discipline, not something this mechanism enforces on its own.

## Meter (on by default, measurement only)

`meter` is a local, zero-network subsystem, shipped inside this plugin and **on by default**,
that measures real per-node token spend via Claude Code's hooks — see
[`meter-handoff.md`](meter-handoff.md) for the full v1 design and
[`meter-v2-handoff.md`](meter-v2-handoff.md) for the v2 token-reduction modules that build on it.
**All of v1's milestones except Codemod (M7, which the spec itself says "ships last or not at
all") are implemented: the Ledger (M0), Receipts (M1), Dossier (M2), Clamp (M3, both halves), Echo
(M4, measure-only), Vault (M5, shadow mode only), and Governor (M6, with one deliberate deviation —
see below).** Ledger measures and changes nothing about how a run behaves — no tool input or
output is ever rewritten by it. Receipts is the first milestone with a
real (if narrow) behavioural effect: `task-worker` and `task-reviewer` now emit a trailing
machine-readable `dag-receipt` fenced block, and a missing/invalid one gets exactly one repair turn
(`meter.modules.receipts.repair_turns`, default 1) before the daemon lets it through
unconditionally — a malformed receipt can never deadlock a node. Dossier precomputes a per-task
context pack — owned files' indexed symbols (Python via `ast`, other languages via a regex table —
see `meter/index/symbols.py`), 1-hop import neighbours, relevant contract text, sibling task
receipts, recent git history, and a bounded text search — with zero model calls, and delivers it
via `additionalContext` at `SubagentStart`; a `Glob`/`Grep` before the dossier has been read gets
denied once (up to `meter.modules.dossier.max_denials`, default 2) with a pointer to it, never more
than that, so an agent that genuinely needs to explore always can. Clamp has two halves: **3a**
rewrites an unbounded `Read` of a large file the index has an opinion about (a 1-hop import
neighbour of the task's owned files) down to just the relevant spans plus a margin — never an owned
file, never a file the index has no opinion on, never a file the agent already asked to read
narrowly — and turns a *repeat* unbounded read of a span it's already delivered into a denial
("you already have this") rather than sending the content twice; **3b** compresses oversized `Bash`
(test runs, `git diff`, build/install logs) and `Grep` output before it reaches an agent's context,
per-tool, always preserving every error/warning line and the full original on disk
(`.dag/runs/<run-id>/meter/tool-output/`). Both halves decline outright rather than guess when
they aren't confident — 3a's rewriting also disables itself for an entire repo's sessions if
`dag-doctor` or `SessionStart` detects a conflicting `PreToolUse`-on-`Read` hook in user/project
settings, the documented hazard where Claude Code lets the last of several matching hooks silently
win (`meter-handoff.md` Section 6.2). Echo measures (never rewrites) how much of the Task-prompt
text handed to each spawned agent is byte-identical to text another sibling agent already received
in the same run — `/dag-cost` reports the resulting "echoed tokens" figure, the measured size of
the duplication this plugin's later milestones exist to eliminate; it currently only watches the
Task-prompt channel (see `meter/echo.py` for why). Vault computes a semantic key for every
successfully completed task (its title, acceptance criteria, contract text, and the content/spans
it was given, normalised so IDs, dates, and run ids never cause a false-positive collision — see
`meter/vault.py`'s exhaustively unit-tested `norm()`) and, in **shadow mode** (the default and
currently the only implemented mode), records what a cached patch for that key *would* have looked
like and diffs it against what the task's own worker actually produced — `/dag-vault stats` reports
the agreement rate. **The worker always runs regardless; shadow mode never applies a patch or skips
a spawn.** Real replay (`rework_only`/`full` modes, which would actually substitute a cached patch
for a worker) isn't built yet — the spec's own promotion path requires shadow mode to run clean for
50 real runs before real replay is even allowed to ship, so building it now would jump the queue on
the evidence it's gated on. Governor recalibrates the orchestrator's per-spawn budget weights from
real measured cost per agent type (`skills/orchestrator/SKILL.md` reads
`${CLAUDE_PLUGIN_DATA}/meter/weights.json` if present, falling back to the existing hardcoded tier
weights otherwise — the ceiling and degradation ladder are unchanged either way), prints a
cost-by-agent-type routing report and a prompt-cache-TTL recommendation in `/dag-cost`, and contains
runaway nodes — but **not** on the spec's own token-based signal. Live per-tool-call token
tracking isn't possible on this plugin's transcript-only accounting (tokens are only known once, in
full, at `SubagentStop` — by which point there's nothing left to contain), so containment instead
uses **tool-call count** as a live proxy: cheap to track, correlated with real spend in practice,
but a genuinely different metric than the spec names, not a hidden implementation detail — see
`meter/governor.py`'s module docstring for the full reasoning. Warn at 2x and deny further tool
calls at 3x a measured (or default) call-count prediction, same thresholds the spec asks for, just
applied to a different quantity.

**v2** ([`meter-v2-handoff.md`](meter-v2-handoff.md)) adds further token-reduction modules under
the same `meter.modules.*` namespace, shipped by risk class (lossless first). **R1 (lossless) is
complete: Intern, Throttle, and Oracle.** **Intern** (M9) gives every file path mentioned in a
dossier a shorter form, stated once, so the dozens of later references cost far less — default
mode is `basename` (strip the directory prefix every file in the node shares, "guaranteed safe...
no comprehension risk at all" per the spec), with a full `codebook` mode (short `f1`/`f2`/... codes
for files, plus symbol/contract/AC codes) implemented and available but deliberately not the
default: the spec's own two comprehension-risk mitigations require an A/B harness this repo doesn't
have to confirm "no measurable change in rework count or review findings" before it's safe to turn
on for real work. Every codebook round-trips exactly (see `meter/intern.py`'s exit-criterion test)
and is stored alongside its dossier so a human can decode it later. Scoped to the dossier's own
file-path mentions only — task prompts, receipts, review findings, and contract text authored by
agents or the orchestrator are not interned, since that needs either new daemon-side rewriting
hooks or a shared codebook protocol taught to every agent template, both bigger and riskier than
anything else in this module. **Throttle**
(M13, lossless) denies a `Write` to an existing, git-tracked file with a reason naming `Edit` —
output bills at roughly 5x input, so a full-file rewrite is the most expensive way to make a small
change — with four exceptions (new file, generated-shaped file, a rewrite large enough that
denying it wouldn't help, or the agent's already been denied twice for this exact path) so a
genuinely needed rewrite always gets through; it also reports (never enforces) a prose-to-artifact
output ratio per agent type in `/dag-cost`. Its third piece, 13c (routing mechanical nodes to a
lower reasoning effort), is deliberately not implemented — it needs a hook capability
(`meter-v2-handoff.md` VERIFY item) nothing here can confirm exists yet, and the spec's own
instruction for exactly that situation is to close it and say so, which `meter/throttle.py` does.
**Oracle** (M15, lossless) caches a `Grep` answer, keyed by a normalised hash of the question
(lowercased, punctuation/pronouns/stopwords stripped, remaining words sorted — so "where is the
session token validated" and "session token validation location" hash identically) — a repeated
or semantically-equivalent search within the same run gets denied with the prior answer instead of
re-running. Any `Write`/`Edit` to a file an answer's content came from invalidates that answer
immediately; a content hash of the same files is also re-checked on every lookup as a backstop.
Scoped to `Grep` only — `Glob` has no existing output hook to record an answer from and is a
lower-value target anyway (cheap filename matches, not the expensive full-text search this
targets), and repeated-`Read` caching is already covered by Clamp 3a's own read-span tracking, so
building a second version of the same idea here would just be duplicated bookkeeping. Cross-run
reuse is off by default — the cache never outlives the run it was built in.

**R2 (verified) is in progress: Sieve is done, Distill is next.** **Sieve** (M11) runs a
deterministic checker registry against a task's diff before its `task-reviewer` even starts, then
injects a declaration into the reviewer's prompt naming which classes are already checked and
passed — so the reviewer's attention goes to what tooling can't decide, and mechanical findings
never consume a review round. Two checkers are fully real (`ownership`: does the commit touch
anything outside the task's `owns`; `secrets`: regex-scanned credential shapes in the diff);
`types`/`lint` run best-effort against whatever this repo's own config says applies (`tsconfig.json`
→ `tsc`, an ESLint/Ruff config → that linter) and report `not run` rather than guess otherwise;
`tests`/`ast` always report `not run` — a hook can't safely commit to an arbitrary test suite's
unbounded runtime, and no per-repo AST rule format exists yet. **The one invariant that makes this
"verified" rather than "statistical":** the declaration only ever states a class as covered when it
both ran and passed — never omitted, never assumed. This module also carries this plugin's single
highest-blast-radius rewrite: it's the only place `updatedInput` rewrites an `Agent` spawn's prompt
rather than a `Read`/`Grep`/`Write`'s input, and a bug here would corrupt every review in the
pipeline, not just narrow one file read — so `meter/router.py`'s `_maybe_run_sieve` is deliberately
conservative, returning untouched on any uncertainty (missing `WORKTREE:`/`COMMIT:` lines, an
unresolvable task, a checker exception) rather than ever guessing. It required one small
`skills/orchestrator/SKILL.md` change: a `task-reviewer` spawn now restates its worktree/commit as
literal lines (the same "inert if `meter` isn't installed" pattern as the existing `DAG-NODE:`
line), since there was previously no fixed format to parse them from.

**Distill** (M12) is also implemented — with a real architectural split worth calling out. Its
mechanism needs an actual model call (a Haiku-tier pass to compile a spec into a dense brief, plus
more model calls for a fidelity gate: generate closed questions from the full spec, answer them
from the brief alone, reject on any miss), and this daemon is a credential-less local process with
no way to invoke a model. So the daemon owns only what it safely can — a content-hash-keyed brief
cache (`scripts/dag-distill.py check`/`store`/`record-fetch`/`stats`) that survives across runs and
repos with an identical spec — and a new agent, `spec-distiller`, plus a Phase 4 addition to
`skills/orchestrator/SKILL.md`, do the actual compile-and-verify work: one spawn compiles the brief
and a set of verification questions from the full spec, a second **fresh** spawn (blind to the full
spec and to the first spawn's reasoning) answers those questions from the brief alone, and the
Orchestrator itself grades the answers — no extra spawn needed for that. Worth being honest about
scope: checking this repo's own `skills/orchestrator/SKILL.md`, its existing design doesn't
actually pass the merged spec into "every agent" the way the spec assumes — Phase 5's
`task-worker`/`task-reviewer` only ever receive their own task's slice, never the whole spec. The
one spawn that does receive the full spec, once per run, is `task-specialist` in Phase 4 — so
that's where Distill is wired in, and its practical payoff here is narrower than the spec's own
framing (cross-run/cross-repo reuse of an identical spec, not "every agent in the run" — there's
only one full-spec consumer per run in this codebase). Distill is skipped entirely in `micro`
mode — there `merged-spec.md` already holds nothing beyond the raw request, and compiling a brief
from a spec that's already one sentence would add pure round-trip overhead to the exact class of
run `micro` exists to keep cheap. In `full`/`lite`, Distill's compile-verify(-revise-reverify)
sequence is fully serial and foreground by construction (each step gates the next), which is a
deliberate correctness-over-latency trade documented alongside its Phase 4 usage in
`skills/orchestrator/SKILL.md`, not an oversight.

**R3's measurement half, M14 Attribution, is also done** (M14 ships and measures alone before M10
Pull is even considered, per the spec's own release plan — see below). It records, for each node,
which dossier sections it was exposed to beyond its own owned files (a 1-hop import neighbour, a
contract's text, a sibling task's receipt) and whether it ever actually touched that content
afterward — a `Read`/`Grep`/`Edit` targeting it, or a literal mention in its final report, both
show up as the same signal: the exposed file path or id appearing anywhere in its transcript.
Never enforced, never prunes anything — `meter.modules.attribution.prune` stays `False`, same
posture as Vault's shadow mode: measure through a full release before anything is allowed to act
on the measurement. `/dag-cost` reports the resulting reference rate per `(agent_type, unit_type)`,
persisted across every run so the numbers actually accumulate into something meaningful over time.

**M10 Pull is implemented and tested, but ships DISABLED by default** (`meter.modules.pull.enabled:
false`, deliberately overriding the spec's own sample config, which shows it on). The spec's
release plan says Pull "ships only if M14's measured reference rate justifies it" — that
measurement needs a full release of real run data, which doesn't exist yet. When enabled, a
dossier's sections are each written to their own file under
`.dag/runs/<run-id>/meter/dossiers/<node>/` instead of being pushed inline, and a short (~200 tok)
manifest — one line per section, its id and approximate size — is injected via `additionalContext`
instead; the agent pulls what it actually needs with an ordinary `Read`, tracked per section. The
build (at `PreToolUse` on the spawning `Agent` call) and the delivery (a separate, later
`SubagentStart` request that shares no Python state with the build — only what landed on disk) are
two different hook invocations, which is exactly the kind of handoff a unit test on either half
alone wouldn't catch if it broke; a dedicated integration test exercises the full
build-then-deliver-then-track sequence. Per-agent-type promotion (push a section pulled >80% of
the time) and reversion (force full push where the round-trip overhead outweighs the saving) are
implemented as a `recommend()` function ready to run once real pull-rate data exists, but nothing
currently calls it automatically — turning any of this on before that evidence exists would be
jumping the queue on the spec's own gate.

**R4's M8 Quorum completes every module in the v2 handoff doc** (the only one left unbuilt is v1's
M7 Codemod, which that spec itself says ships last or not at all). Quorum is the doc's own largest
lever and its only quality-trading module — "three critics per round is a 3x multiplier on the
most expensive phase in the pipeline," and in this codebase that's Phase 2's 3-`spec-reviewer`
loop, not Phase 5's already-single `task-reviewer`. **Ships disabled by default**
(`meter.modules.quorum.enabled: false`), same reasoning as Pull: the spec's own gate needs 50+
full-quorum samples per specialist domain, measuring whether critics 2 and 3 ever produce a
finding critic 1 missed that the parent accepted as valid, before any domain may narrow below 3 —
and that measurement needs real runs this repo hasn't had. The contention predicate (the
deterministic escalate/don't-escalate decision — no model call ever makes it, per the spec's own
hard requirement) and the recall-guard sampling are fully implemented and tested, with one
disclosed adaptation: this repo's `spec-reviewer` reports a bare finding *count*
(`REVIEW: FINDINGS <n>`), not per-finding severities, so "critic 1 found anything at all"
substitutes for the spec's severity-threshold check — a stricter, not silently different, signal.
The `skills/orchestrator/SKILL.md` addition is one sentence, inert today: with no domain ever
reaching 50 samples, Phase 2 always spawns all 3 reviewers, byte-for-byte the same as before this
module existed.

It starts itself on `SessionStart` (a loopback-only daemon on `127.0.0.1:47615` by default) and
reports via `/dag-cost <run-id>`. Run `python3 scripts/dag-doctor.py` any time to see its
capability matrix, including which of its own design assumptions are still unverified against
your Claude Code install — the SubagentStop block/repair mechanism Receipts relies on is one of
them; see the VERIFY item it prints.

Its HTTP hooks authenticate with a token, but only advisorily: Claude Code has no documented way
for a `SessionStart` hook to hand a freshly generated secret to the hooks that fire afterward, so
an unauthenticated local request is still processed rather than rejected. This is safe for Ledger
because it only ever *observes*; safe for Receipts because an unauthenticated repair-turn request
is still just asking the same subagent to re-emit its own already-required receipt; safe for Clamp
3b because every compressor only ever shortens a result the tool already legitimately produced —
it can't inject content, and the full original is always kept on disk regardless of which path
requested the compression; safe for Dossier's `Glob`/`Grep` enforcement because its worst case is
one denied call with a pointer to a context pack the agent was always going to receive anyway,
capped at `max_denials` regardless of how many requests arrive; safe for Clamp 3a's read
rewriting because `updatedInput` can only ever *narrow* a `Read` to a sub-range of the same file at
the same path — it cannot redirect the read elsewhere or fabricate content, so the worst case of an
unauthenticated request is the same file read more narrowly than intended, never a different file
or invented text; safe for Governor's containment because it only ever denies a tool call this
same subagent was already about to make (never rewrites input, never substitutes a different tool),
capped by the same `runaway_multiplier`-derived threshold every legitimate request would also hit;
safe for Throttle because a denied `Write` costs the agent one extra `Edit` call at most — the
file, the path, and the agent's own intended content are all unchanged, and the worst case of an
unauthenticated request is a slightly less efficient tool choice, never a different outcome; safe
for Oracle because a served cache hit is always an answer this exact run already produced
for an equivalent search, re-verified against the current file content on every lookup — the
worst case of an unauthenticated request is a legitimate prior answer returned again, never a
fabricated one; safe for Sieve because the rewrite only ever *appends* a declaration built
from checks it ran itself against the real worktree/commit named in the prompt already — it
can't inject arbitrary content or redirect the spawn, so the worst case of an unauthenticated
request is a factual, checker-derived note the reviewer would have been correct to receive
anyway; safe for Attribution because it only ever records whether a token already present in the
dossier it built appeared in a transcript that already exists — pure bookkeeping, nothing it
records can change what any tool does; and safe for Pull (disabled by default regardless) because
its manifest names only files this same dossier build already wrote to disk for this exact node.
(Quorum has no hook surface at all yet — its measurement table and the orchestrator's read of it
are the only moving parts, so this auth question doesn't apply to it.) `dag-doctor` reports the
authenticated/unauthenticated split so you can see whether your environment happens to wire the
token through.

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
│   ├── dag-meter.md             # turn meter on/off, or check whether it's running
│   └── dag-vault.md             # Vault (M5) stats and purge, shadow-mode agreement rate
├── hooks/hooks.json              # meter's Claude Code hook registration (v1 M0-M6 + v2 R1 + M11)
├── meter/                        # meter's package — v1: M0 (Ledger) + M1 (Receipts) + M2 (Dossier) + M3 (Clamp, both halves) + M4 (Echo, measure-only) + M5 (Vault, shadow mode only) + M6 (Governor, 6b via a call-count proxy); v2 (meter-v2-handoff.md, ALL of R1-R4 implemented): M9 (Intern) + M13 (Throttle) + M15 (Oracle) + M11 (Sieve) + M12 (Distill, cache only) + M14 (Attribution, measure-only) + M10 (Pull, disabled by default) + M8 (Quorum, disabled by default)
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
│   ├── spec-distiller.md        # meter's M12 Distill — compiles/verifies a dense brief before Phase 4
│   ├── task-specialist.md       # decomposes the merged spec into a task DAG
│   ├── task-worker.md           # generic role, worktree-isolated, ends with a machine trailer
│   ├── task-reviewer.md         # generic role, verifies inside the worker's worktree
│   └── integration-reviewer.md  # spawned once in Phase 6; reviews the whole diff against the request
├── evals/                       # behavioural eval suite (see evals/README.md)
└── scripts/
    ├── validate.py               # structural validator, run in CI
    ├── dag-doctor.py             # meter's capability matrix and VERIFY probes
    ├── dag-meter.py              # meter's on/off/status CLI, behind /dag-meter
    ├── dag-vault.py              # meter's Vault stats/purge CLI, behind /dag-vault
    ├── dag-distill.py            # meter's M12 Distill brief cache CLI, called from Phase 4 (no slash command — orchestrator-internal)
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
