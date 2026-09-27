---
description: Orchestrator for the yolo-dag plugin — takes a fully-specified request (normally from the brainstorm skill), routes it to the specialists whose domains apply, closes each with an adversarial review loop, reconciles them into one build spec, decomposes that into a task DAG, then executes it in user-paced passes of worktree-isolated workers — entering implementation directly on a full run, or stopping to ask on a plan-only run — and finally verifies and acceptance-reviews the assembled branch. Phase bodies load on demand from phases/; every phase persists to .dag/runs/<run-id>/ so an interrupted run resumes.
argument-hint: Request [mode=auto|lite|full|micro] [--all] [--plan-only]
---

# orchestrator — Route, Review, Reconcile, Decompose, Execute, Integrate

You receive a request already resolved to zero high-stakes ambiguity — normally handed off by
the `brainstorm` skill, whose restated request and stated assumptions are ground truth; don't
re-litigate them. Take it through six phases: route it to the specialists whose domains apply,
let each survive an adversarial review loop, merge and reconcile them into one build spec,
decompose that into a task DAG, then execute that DAG in user-paced passes of worktree-isolated
workers — asking before each pass only *how many* tasks to run in parallel and choosing the tasks
yourself, merging each passing task onto the run's integration branch — then verify and
acceptance-review the assembled branch.

Whether the user is asked before implementation starts is the **run type**, decided in
`brainstorm`: a *full run* enters implementation with no confirmation; a *plan-only* run stops at
the implementation boundary until the user says go.

Input: $ARGUMENTS

## This file is the spine; the phases load on demand

**The phase bodies are not in this file.** Each lives in `phases/phase-<n>.md` beside it. Read a
phase's file at the moment you enter that phase, and not before — the six of them together are
four times the size of this spine, and a run that skips phases (every `micro` run skips 1–3)
or dies at Phase 2 should never have paid for the rest.

Resolve `<skill-dir>` the same way Phase 4 resolves `dag-distill.py`:
`${CLAUDE_PLUGIN_ROOT}/skills/orchestrator/`, falling back to searching `~/.claude/plugins` for a
`yolo-dag` checkout, falling back to `skills/orchestrator/` relative to the current directory.

| Entering | Read first | Skipped when |
|---|---|---|
| Phase 1 — Route & fan out | `<skill-dir>/phases/phase-1.md` | `micro` |
| Phase 2 — Build + review loop | `<skill-dir>/phases/phase-2.md` | `micro` |
| Phase 3 — Merge & reconcile | `<skill-dir>/phases/phase-3.md` | `micro` |
| Phase 4 — Decompose into a task DAG | `<skill-dir>/phases/phase-4.md` | never |
| Phase 5 — Execute, verify & merge | `<skill-dir>/phases/phase-5.md` | plan-only run, until resumed |
| Phase 6 — Verify & hand off | `<skill-dir>/phases/phase-6.md` | never |

**`micro` mode never opens phases 1–3, so their work is stated here in full**: write
`routing.md` noting "micro — no specialists; the request is the spec", copy the request verbatim
into `merged-spec.md`, mark phases 1–3 `"skipped"` in `run.json`, and go straight to Phase 4.
That is the entirety of what those three phases do in `micro` mode — there is nothing in their
files you are missing.

Read each phase file exactly once, when you reach it. If a phase file can't be found, say so and
stop rather than improvising that phase from this summary — the summaries above are an index, not
a specification.

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
4. **Determine the mode, the run type, and the flags.** Parse `$ARGUMENTS` for `mode=auto`,
   `mode=full`, `mode=lite`, `mode=micro`, `--all`, `--plan-only`, `--full-run`,
   `--non-interactive`, and `--tasks-per-pass N`. With no mode flag, use whatever mode
   `brainstorm` chose in its handoff, and fall back to **`auto`** if you were invoked directly
   with no mode at all. `--all` forces all 8 specialists and is incompatible with `micro` (which
   has no specialists) — if both are passed, run as `lite` and say so in one line.

   **`auto` is a selector, not a fourth tier.** It resolves to one of the three real modes before
   anything else happens, using only the request in front of you — no model call, no extra spawn:

   - **`micro`** — a single, self-contained change whose shape is already fully determined by the
     request: one file or one obvious pair of files, no new dependency, no persistence or auth
     surface, nothing another specialist would have an opinion about. A typo, a version bump, a
     flag rename, "add a `--json` flag to this command".
   - **`full`** — the request touches auth, user or sensitive data, secrets, money, or untrusted
     input; *or* it spans enough of the system that two specialists could reasonably contradict
     each other. Scrutiny is cheap next to the cost of getting these wrong.
   - **`lite`** — everything else, which is most requests.

   Resolve it, record the resolved mode in `run.json` (never the literal `auto`), and **say which
   way it resolved and why, in one line** — "auto → lite: multi-file feature, no auth or
   persistence surface". If the user disagrees they can re-run with an explicit mode; don't ask.

   The **run type** is a separate axis: `--plan-only` (or a handoff saying so) sets
   `plan_only: true`; `--full-run` sets `plan_only: false`. **The two flags are mutually
   exclusive** — if both are passed, stop immediately and say so plainly; do not pick a
   precedence. Never ask the user for the run type here — `brainstorm` already did, as its first
   action, and re-asking is exactly the checkpoint this pipeline removed.

   **`run.json`'s `plan_only` is where the run type lives from this point on.** Both places that
   branch on it — the Phase 4 implementation boundary and Phase 5's per-pass cap — re-read it from
   disk rather than from memory, for the same reason the budget is: a `full` run outlives several
   context compactions, and a forgotten run type silently turns a plan-only run into an unattended
   build.

   **If, and only if, `--non-interactive` or `--tasks-per-pass` is present**, read
   `<skill-dir>/reference/unattended.md` and apply its validation rules before creating a branch
   or spawning anything. Neither flag is ever inferred — not from a missing TTY, not from how the
   request reads, not from a question going unanswered — so a run without them never needs that
   file.

| | `full` | `lite` | `micro` |
|---|---|---|---|
| Phases | all six | all six | 4–6 only |
| Specialists | all routing selects | routing, capped at 4 | none — the request is the spec |
| Review rounds | up to 3 | 1 | none |
| Soft spawn budget | 90 units | 30 units | 6 units |

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

   `run.json` may also carry a `meter` key, written and read only by the `meter` subsystem if
   it's installed (see `meter-handoff.md`) — never write it yourself, and never treat its absence
   as an error; it's additive state for a separate, optional tool, not part of this skill's own
   schema.

   In `micro` mode, phases 1–3 start as `"skipped"`. Mark each phase `"complete"` in `run.json`
   only after its artifacts are fully written — a phase whose entry still says `"pending"` or
   `"in_progress"` did not complete, however plausible its half-written markdown looks.
6. **Tell the user the run id and where state is going**, in one line, then proceed. They can
   resume with `/dag-resume <run-id>` if the run is interrupted.

**If this is a resume** (invoked by `/dag-resume` with an existing run id), read
`<skill-dir>/reference/resume.md` and follow it — it carries the three rules that apply only on a
resume. A fresh run skips that file entirely.

## Ground rules

- **You decide. The user reviews the outcome, not the process.** Default to making the call
  yourself at every decision point in every phase — routing, a specialist's design choices, how
  Open Concerns get handled, how to degrade on a tight budget. Interrupt only when a decision is
  genuinely high-stakes (irreversible, expensive, materially changes scope, or touches
  security/data in a way that's hard to walk back). State assumptions plainly rather than pausing
  to get them rubber-stamped. **There are exactly two standing interactive gates**, and no
  others: on a *plan-only* run, the implementation boundary at the end of Phase 4; and, in both
  run types, Phase 5's per-pass prompt asking how many tasks to run in parallel. A full run does
  not pause at the implementation boundary at all. In a **non-interactive** run those two answers
  come from `run.json` instead, and nothing else about the gates changes.
- **All spawning happens from this skill.** No agent in this plugin has `Agent` in its `tools` —
  every specialist, reviewer, consolidator, reconciler, worker and task-specialist is spawned by
  you, the main thread. Nested spawning is unproven here, and every fan-out this pipeline needs
  can be done flat.
- **Background by default.** Spawn every specialist/reviewer/consolidator/reconciler/worker with
  `run_in_background: true` so batches genuinely run concurrently. Only Phase 4's single
  `task-specialist` call is a reasonable candidate for `run_in_background: false`, since nothing
  else can proceed until it returns.
- **Prefer a script to a spawn.** Where this skill offers a deterministic path for a mechanical
  step — Phase 2's `dag-fold.py`, Phase 4's `dag-distill.py` — take it, and fall back to the
  agent only on the documented escalation signal. Each of those scripts replaces a model call
  with computed output and is written to fail toward the agent, never past it.
- **Meter attribution line (inert if `meter` isn't installed).** Prefix every `Agent` spawn's
  `prompt` with one line, `DAG-NODE: <run-id>/<node-id>`, before the task content — `<node-id>` is
  `P1-<specialist>` in Phase 1, `P2-<specialist>-r<round>-<angle>` for a reviewer or
  `P2-<specialist>-r<round>-consolidator` for the consolidator in Phase 2, `P3-reconciler`,
  `P4-task-specialist`, `<task-id>-worker` / `<task-id>-reviewer` in Phase 5, and
  `P6-integration-reviewer` in Phase 6. Plain metadata a local measurement tool may read; nothing
  about this skill's behavior depends on it.
- **Resume by name, never respawn.** A specialist's multi-round revision (Phase 2), its Phase 3
  reconciliation, and a Phase 5 worker's rework after a FLAGGED review are always a `SendMessage`
  to that agent's own spawn name — this resumes it with full context, and in the worker's case
  with the worktree its work already lives in. Re-spawning would lose everything it produced.
  Spawn fresh only where the tree itself has to be fresh: a merge-conflict retry in integration
  mode, a reopened contract consumer, or an agent whose spawn is no longer reachable.
- **Don't override agent models.** Each agent declares its own tier in frontmatter —
  `spec-consolidator` and `spec-distiller` on Haiku (both mechanical), every other agent on
  Sonnet. Pass no `model` to `Agent`; Phase 2's reviewers decorrelate by assigned angle, not by
  model.
- **Persist as you go, don't batch it up.** Every phase writes its artifacts to the run directory
  *as it completes*. A run that dies at Phase 5 must leave Phases 1–4 fully recoverable on disk.
- **Spec-producing agents write their own deliverables.** Every agent producing a document-sized
  artifact — a specialist deliverable, reviewer findings, a consolidated list, the reconciler's
  rulings, a distilled brief, the task graph — carries `Write` scoped to the exact path you give
  it, and writes there directly instead of returning the document in its final chat message. Give
  it that path every time you spawn or resume it; it must never write anywhere else (project
  files are `task-worker`'s alone). This is not a formatting nicety: forcing a large document
  through one final message has caused this pipeline's worst observed stalls — a reconciler
  hitting the output-token ceiling mid-document twice on one run, reviewers returning truncated
  "receipt-only" finals needing a resume just to emit the real content. A `Write` call has no
  such ceiling. The same applies to what *you* hand an agent: give it the path to its input
  rather than pasting a large upstream artifact inline. **Nothing is restated in full at any hop
  of this pipeline.**
- **Depth proportional to the request.** A deliverable's length should track what this request
  actually needs decided, not a template's checklist. A single-file, client-only page needs a
  short architecture note, not an enterprise document — say so when you spawn a specialist if the
  scope is obviously small. Padding isn't thoroughness.
- **Once an artifact is written, your memory of it is its path, not its text.** Re-read
  `merged-spec.md`, `tasks.json`, or a specialist's round file from `.dag/runs/<run-id>/` at the
  point of use instead of carrying artifact text forward. A `full` run outlives several context
  compactions; the run directory is what survives them, and this rule is what makes the run
  survivable.
- **Roster tracking.** If `TodoWrite` is available, use it for the in-flight roster; otherwise
  track phase state and the roster in your own running text.

### Budget

The budget is **cost-weighted and read from disk, never from memory** — a compacted context
forgets spawns, and the drift is always downward, which means silent overspend, the exact thing
the ceiling exists to prevent. Append an entry to `run.json`'s `spawns` array as each batch of
spawns goes out (`{"agent": ..., "model": ..., "phase": ...}`), and compute spend by re-reading it.

Unit weights: session-model (`inherit`) spawn = **1.0**, `sonnet` = **0.5**, `haiku` = **0.1**.
These are per-*tier* guesses, and `meter` (if installed and enabled — see [Meter](../../README.md#meter-on-by-default-measurement-only))
recalibrates them into per-*agent-type* measured coefficients from real runs. Before weighting a
spawn, check whether `${CLAUDE_PLUGIN_DATA}/meter/weights.json` exists (it won't on a fresh
install, or if meter is off — that's normal, not an error). If it does, look up
`weights[<mode>][<agent_type>]`; use that number in place of the tier weight for that spawn. If
the file is absent, or has no entry for this exact `(mode, agent_type)` pair, fall back to the
tier weight above — never block on a missing or malformed weights.json, and never invent a number
for an agent type it doesn't cover.

**A step that runs as a script costs nothing against the budget.** A `dag-fold.py` consolidation
or a `dag-distill.py` cache hit spawns no agent, so it gets no `spawns` entry and no weight. That
is the point of taking those paths, and it is why the ceilings above are lower than they were
before those paths existed.

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
member hasn't reported: respawn it once with the same prompt. If the respawn also fails, proceed
without it — record the gap in the run directory and in your final summary. A missing
specialist's section in the merged spec reads "not produced (agent failed twice)"; a missing
reviewer just means that round had one fewer. Never block the whole pipeline on one unresponsive
agent.
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
