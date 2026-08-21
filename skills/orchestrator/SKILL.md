---
description: Orchestrator for the yolo-dag plugin — takes a fully-specified request (normally handed off from the brainstorm skill), routes it to the relevant domain specialists, runs each through a 3-reviewer/1-consolidator review loop, merges and reconciles the results into one build spec, decomposes it into a task DAG, executes that DAG with a ready-queue of worktree-isolated workers — merging each passing task onto the run's integration branch as it lands — then verifies and acceptance-reviews the assembled branch. Persists every phase to .dag/runs/<run-id>/ (including a machine-readable run.json) so an interrupted run can resume.
argument-hint: A fully-specified request — normally the handoff from /brainstorm, but can be invoked directly if the request is already unambiguous. Add `mode=lite` for a cheaper single-review-round run, `mode=full` for the full 3-round pass, `mode=micro` to skip the specialist phases entirely (the request is the spec), `--all` to force all 8 specialists, or `--plan-only` to stop cleanly after the task graph and let /dag-resume execute it later.
---

# orchestrator — Route, Review, Reconcile, Decompose, Execute, Integrate

You receive a request that has already been resolved to zero high-stakes ambiguity — normally
handed off by the `brainstorm` skill, whose restated request and stated assumptions you should
treat as ground truth; don't re-litigate them. Take it through six phases: route it to the
specialists whose domains actually apply, let each survive an adversarial review loop, merge and
reconcile them into one build spec, decompose that spec into a task DAG, execute that DAG with a
ready-queue of worktree-isolated workers — merging each passing task onto the run's integration
branch the moment it passes review — then verify and acceptance-review the assembled branch.

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
4. **Determine the mode and flags.** Parse `$ARGUMENTS` for `mode=full`, `mode=lite`,
   `mode=micro`, `--all`, and `--plan-only`. With no mode flag, use whatever mode `brainstorm`
   chose in its handoff, and fall back to `full` if you were invoked directly with no mode at
   all. `--all` forces all 8 specialists and is incompatible with `micro` (which has no
   specialists) — if both are passed, run as `lite` and say so in one line.

| | `full` | `lite` | `micro` |
|---|---|---|---|
| Phases | all six | all six | 4–6 only |
| Specialists | all routing selects | routing, capped at 4 | none — the request is the spec |
| Review rounds | up to 3 | 1 | none |
| Concurrent tasks | 10 | 5 | 2 |
| Soft spawn budget | 120 units | 40 units | 8 units |

5. **Write `run.json`** to the run directory — the machine-readable manifest every command and
   every resume branches on. The markdown artifacts are the human surface; `run.json` is the
   source of truth for code. Keys:

```json
{
  "run_id": "2026-08-21-a3f9",
  "mode": "full",
  "plan_only": false,
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
Never redo a phase whose entry is complete. Two extra rules on resume: re-validate `tasks.json`
(uniqueness, edges, acyclicity) before executing anything — state on disk can have been
hand-edited or corrupted since it was written — and treat the integration branch as authoritative
for what has merged: if `tasks.json` claims a task is MERGED but the branch is missing, flag the
inconsistency and stop rather than guessing.

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
the ceiling exists to prevent. Append an entry to `run.json`'s `spawns` array as each batch goes
out (`{"agent": ..., "model": ..., "phase": ...}`), and compute spend by re-reading it.

Unit weights: session-model (`inherit`) spawn = **1.0**, `sonnet` = **0.5**, `haiku` = **0.1**.

When a run crosses the mode's soft budget, **degrade rather than stop or silently overspend**,
in this order: drop remaining review rounds to 1, then narrow any not-yet-started specialists to
the always-on three, then reduce concurrent tasks. Record each degradation in `run.json`'s
`degradations` array and say what you dropped and why in one line. Only stop outright if
degrading everything still won't fit, and say so plainly rather than producing a half-run you
present as complete.

### Stuck agents

Any spawn can fail to return. If a batch is otherwise complete and one member hasn't reported:
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
definition and returns both a human-readable task list and a fenced `json` block.

**Validate the graph before executing it.** Persist the JSON to `<run-dir>/tasks.json`, then check:

- every id is unique;
- every id in a `depends_on` exists in the task list;
- the graph is **acyclic**.

If any check fails, `SendMessage` the specific problem back to `task-specialist` and ask for a
corrected graph. A cycle will deadlock Phase 5 — never try to execute one, and never break it
yourself by dropping an edge at random.

Then compute the **topological levels** ("waves") — level 0 is every task with
`depends_on: []`; level *n* is every task whose dependencies all sit in levels `< n`. Levels are
the proof of acyclicity and the reporting shape ("14 tasks in 4 waves: 6, 5, 2, 1" — report that
line to the user); the actual scheduling in Phase 5 is a ready-queue, which is strictly better.

Warn — don't block — if two tasks that can be ready at the same time declare overlapping `files`.

**If `--plan-only` was passed, stop here — cleanly.** Report the routing decision, the merged
spec's location, the task graph and its shape, and any Open Concerns; leave phases 5–6
`"pending"` in `run.json`; and tell the user `/dag-resume <run-id>` executes the plan when
they're ready. Execution is the majority of the cost and the only part that writes code — this
flag exists so the user can see the plan without paying for the build.

## Phase 5 — Execute, verify & merge (continuously)

Execution is continuous, not big-bang: each task merges onto the integration branch the moment
it passes review. That is what makes dependency edges real — a dependent's worktree is created
from a branch tip that already *contains* its dependencies' code, not from a prose summary of it.

1. **Create the integration branch first**: `git checkout -b dag/<run-id> <base_commit>` in the
   main working tree, and record `integration_branch` in `run.json`. The main tree stays on this
   branch for the whole phase — that is the mechanism by which every worker worktree spawned from
   here is based on the branch's current tip. Never integrate onto `main` (or the repo's default
   branch) directly, and never without the user asking.
2. **Schedule with a ready-queue.** A task is *ready* when every id in its `depends_on` is
   MERGED. Keep up to the mode's concurrent-task cap of workers in flight; when one resolves,
   refill from the ready set immediately. Never hold a whole-level barrier — one slow task
   retrying must not stall unrelated ready work. When more tasks are ready than there are free
   slots, dispatch **longest-remaining-dependency-chain first** (critical path first).
3. **Spawn one `task-worker` per dispatched task**, `run_in_background: true`, with
   `isolation: "worktree"`. Give each worker its task, its acceptance criteria, its declared
   `files` footprint, and the final reports of every task in its `depends_on` — noting that the
   dependencies' actual code is already present in its tree, and the reports are context, not the
   source of truth.
4. **Read each worker's machine trailer** — the literal `WORKTREE:`, `BRANCH:`, `COMMIT:`, and
   `BLOCKER:` lines at the end of its final message. That trailer is the only channel through
   which the pipeline learns where the work physically is; a report without it is a report of
   nothing.
   - `COMMIT: none` alongside a claim of success means the worker produced nothing (an unchanged
     worktree is auto-cleaned) — treat it as FLAGGED and reassign; it counts as an attempt.
   - `BLOCKER: <class>` other than `none` means the worker judged the task unsatisfiable
     (`spec-defect` / `environment` / `criteria-conflict` / `unknown`). Spawn **one** fresh worker
     to independently confirm; if it reports the same blocker class, mark the task BLOCKED with
     that class — don't burn the third attempt re-confirming.
   - **Verify the commit is reachable** from the main repo (`git cat-file -e <sha>`). If it
     isn't — the isolation mechanism gave the worker a detached copy rather than a
     shared-object worktree — fetch it in: `git fetch <worktree-path> <sha>`. If both fail, treat
     the spawn as failed under the stuck-agent rule.
5. **Spawn each finished worker's `task-reviewer`**, `run_in_background: true`, with the task's
   acceptance criteria, the worker's report, and — critically — the worker's `WORKTREE` path and
   `COMMIT` sha, so it verifies the actual work (`git -C <worktree> ...`) rather than the main
   tree, where the changes do not exist. Batch these rather than spawning one at a time as
   workers trickle in.
6. **Read each reviewer's verdict from the literal `VERDICT: PASS` / `VERDICT: FLAGGED` line at
   the end of its final message.** Do not branch on a `ReportFindings` tool call — that renders to
   the host UI and is not the channel you receive. If a reviewer somehow returns no verdict line,
   treat it as FLAGGED and note it.
7. **On `VERDICT: FLAGGED`**: reassign to a **new** `task-worker` instance — never resumed via
   `SendMessage`. The point of reassignment is a fresh, unbiased attempt, not a continuation that
   might inherit the same blind spot. Pass the reviewer's findings to the new worker; its fresh
   worktree comes off the branch's current tip. **Retry cap: 2 reassignments per task (3 attempts
   total).** On the 3rd FLAGGED result, mark it BLOCKED and hold its accumulated findings for the
   final summary.
8. **On `VERDICT: PASS`, merge immediately**: `git merge --no-ff <commit>` onto `dag/<run-id>`
   in the main tree.
   - **Clean merge** → mark the task MERGED, then remove its worktree
     (`git worktree remove <path>` and `git worktree prune`) — the work is on the branch; the
     worktree is done. Cleanup is continuous, not a Phase 6 afterthought.
   - **Conflict** → `git merge --abort`, then spawn a fresh `task-worker` in **integration
     mode**: give it the task, its acceptance criteria, and the conflict, and have it redo the
     task's intent on top of the branch's current tip (its fresh worktree already contains the
     integrated code). This is a distinct retry reason from a FLAGGED review and gets its own
     cap: **1 integration retry per task.** A second conflict → mark the task UNMERGED, *keep its
     worktree*, and report the path — that worktree is the only copy of the work.
9. **A BLOCKED task blocks its dependents.** Any task whose `depends_on` includes a BLOCKED (or
   UNMERGED) task cannot run — mark it SKIPPED (not BLOCKED; it never got an attempt) and carry
   it to the summary. Do not run a task whose dependency never landed on the branch.
10. **Spec-defect circuit breaker.** If at any point more than half of all *attempted* tasks are
    BLOCKED with `BLOCKER: spec-defect`, the spec is the problem, not the workers. Pause
    dispatch and `SendMessage` the accumulated blocker reports to `task-specialist` (its Phase 4
    spawn is resumable in-session) for a corrected graph of the not-yet-merged remainder;
    re-validate it, then resume dispatch. **Strictly once per run** — the bounded-loop property
    is what keeps this pipeline a DAG.
11. Update each task's status and attempt count in `<run-dir>/tasks.json` as it resolves
    (`pending` → `running` → `pass` → `merged`, or `blocked` / `skipped` / `unmerged`), and
    append every spawn to `run.json`'s `spawns` as batches go out.
12. The phase is done when every task is MERGED, BLOCKED, SKIPPED, or UNMERGED.

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
- any Open Concerns carried from Phase 2 or Phase 3;
- anything dropped to stay inside budget, any agent that failed twice, and the spend: spawn
  counts by model tier and the unit total against the mode's budget;
- where the run state lives (`.dag/runs/<run-id>/`).

The user reviews the branch. Merging it anywhere is their call, not yours.
