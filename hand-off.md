# yolo-dag — Hand-off & Next-Level Plan

**Status:** the `0.3.0` plan below is implemented. This document was the plan; it is now the
record of what changed and what is still genuinely open. Same convention as before: ✅ done,
⚠️ done-but-unverified, ⬜ deliberately not done.

The short version of what `0.3.0` was about: `0.2.0` fixed the *design* of the execution layer
and never fixed its *plumbing* — Phase 6 existed, was well specified in prose, and had nothing
to operate on, because no artifact anywhere told the orchestrator where a worker's output
physically was. Tier 0 was that class of defect. Everything after it was real improvement.

---

## Where 0.2.0 landed (condensed record)

`0.1.0` was 13 agents and 2 skills, never run. `0.2.0` added a sixth phase, a real task DAG, run
persistence, routing, cross-specialist reconciliation, model tiering, budgets, an operator
surface, a structural validator, and CI — all verified by reading, none by running. Read the git
history for detail.

---

## Tier 0 — The execution layer does not plumb ✅

### 0.1 — Nothing captured where a worker's output lives ✅

**The critical one, same shape as `0.1.0`'s critical one.** Phase 6 said "merge each PASS
task's worktree"; the orchestrator only ever receives an agent's final message, and
`task-worker`'s defined report was prose — no path, no branch, no SHA. Phase 6 had no argument.

Fixed with a **machine trailer** in the same style as the verdict lines that already worked:
`task-worker` now ends every report with literal `WORKTREE:` / `BRANCH:` / `COMMIT:` /
`BLOCKER:` lines, `scripts/validate.py` pins the contract at both ends (negative-tested), and
the orchestrator merges the `COMMIT` SHA, not "the worktree". `COMMIT: none` is defined as the
honest empty value and treated as FLAGGED-with-attempt, never merged as air.

⚠️ One assumption still unverified empirically: that an `Agent`-tool worktree's commits are
reachable from the parent repo after the agent exits (true for `git worktree`, false for a full
copy). The orchestrator is written to be robust to both — `git cat-file -e <sha>` first,
`git fetch <worktree-path> <sha>` as the fallback — but nobody has watched it happen.

### 0.2 — `task-reviewer` reviewed a tree that didn't contain the work ✅

It was spawned without isolation and told to "read the changed files" — in the main working
tree, where the changes don't exist. It now receives the worker's `WORKTREE`/`COMMIT` and is
explicitly instructed to verify there (`git -C`), with "the path/sha doesn't exist" defined as
a FLAGGED verdict rather than something to improvise around.

### 0.3 — Dependents received prose, not code ✅ (dissolved by 1.2)

Workers received English descriptions of their dependencies' output and wrote code against a
paraphrase. Continuous integration (1.2) dissolves this outright: a dependent is dispatched only
after its dependencies merge, and its worktree is cut from the branch tip — the dependency's
actual code is in its tree. The reports are still passed, demoted to context.

### 0.4 — No git preflight, no recorded base commit ✅

Run setup now asserts a git repo, records `base_branch` / `base_commit` / cleanliness into
`run.json`, refuses to start mid-merge/mid-rebase, and blocks on a dirty tree (modified tracked
files — one of the few genuinely blocking questions, since Phase 5 switches branches in the
main tree). `.dag/` itself is excluded from the dirtiness check so the pipeline doesn't block
on its own state directory. The integration branch is cut from the recorded SHA, not from
wherever `HEAD` drifted to.

### 0.5 — Worktrees were never cleaned up ✅

Cleanup is continuous: a merged task's worktree is removed the moment its merge lands, Phase 6
prunes, and UNMERGED tasks' worktrees are deliberately kept *and named* — they're the only copy
of that work. `/dag-clean` handles what crashed sessions leave behind.

---

## Tier 1 — Make it a real scheduler ✅

### 1.1 — Ready-queue replaced waves ✅

A task now dispatches the moment its dependencies are MERGED and a slot is free, critical path
(longest remaining dependency chain) first — one slow task retrying no longer stalls unrelated
ready work. Topological levels ("waves") survive as the acyclicity proof and the reporting
shape, so `task-specialist`, the README, and the evals needed almost no change.

### 1.2 — Continuous integration ✅

The highest-leverage change in the plan. `dag/<run-id>` is created at Phase 5 *start*, the main
tree stays checked out on it (which is the mechanism making every new worktree base on the
current tip), and each task merges the moment it passes review. Conflicts spawn an
integration-mode worker whose fresh worktree already contains the integrated code; cap 1, then
UNMERGED. Phase 6 became **Verify & hand off**: whole-suite run, acceptance review, cleanup,
checkout back to `base_branch`. The stated trade — a task that passes review and later proves
wrong is on the branch — is the same trade every CI system makes.

### 1.3 — Workers verify before reporting ✅

`task-worker` runs the project's test/build command in its own worktree before declaring done
and includes the verbatim result; the reviewer re-runs rather than trusts it. The cheapest
point in the pipeline that can catch a red suite now does.

---

## Tier 2 — State the orchestrator can trust ✅

- **2.1 `run.json`** ✅ — one machine-readable manifest per run: mode, flags, base commit,
  per-phase status (marked complete only after artifacts are fully written), specialist roster,
  spawn ledger, degradations. Commands and resume branch on it; markdown stays the human
  surface. The validator pins the key schema against both the orchestrator and the commands.
- **2.2 Budget from disk, cost-weighted** ✅ — every spawn appends to the ledger; spend is a
  weighted read (inherit 1.0 / Sonnet 0.5 / Haiku 0.1) against 120/40/8-unit ceilings, never a
  remembered count (which drifts downward across compaction — silent overspend). The summary
  reports spend by tier and units.
- **2.3 Context discipline** ✅ — ground rule: once an artifact is written, the orchestrator's
  memory of it is its path; re-read at point of use. The `TodoWrite` contradiction is resolved
  ("use it if available, otherwise running text" — it stays in `VALID_TOOLS`).
- **2.4 Resume fidelity** ✅ — resume branches on `run.json`, re-validates `tasks.json` before
  executing (seeded-corruption is now an eval), treats the integration branch as authoritative
  for MERGED, re-dispatches `running` tasks from `pending`, and a `--plan-only` run resumes
  into Phase 5 without re-asking. Cross-session specialist loss is still by design (see Still
  open).

---

## Tier 3 — Close the loop on the request ✅

- **3.1 Acceptance review** ✅ — new `integration-reviewer` agent (Phase 6 **step**, not a
  Phase 7 — renumbering is the defect this project already ate once): reviews
  `git diff base_commit..dag/<run-id>` against the merged spec and the original request, emits
  `INTEGRATION: PASS` / `INTEGRATION: FLAGGED <n>` (validator-pinned), findings go to the
  summary and never trigger another build loop.
- **3.2 Execution tells the spec it was wrong** ✅ — `BLOCKER:` classification
  (`spec-defect` / `environment` / `criteria-conflict` / `unknown`) in the worker trailer; one
  confirming fresh worker before a blocker sticks; the summary aggregates classes and says
  loudly when most failures are `spec-defect`. The bounded re-decompose also landed: >50% of
  attempted tasks blocked as `spec-defect` sends the failures back to `task-specialist`,
  strictly once per run.

---

## Tier 4 — Economics and ergonomics ✅

- **4.1 `micro` mode** ✅ — no specialists, no spec review; phases 4–6 only, 2 concurrent
  tasks, 8-unit budget. The request is the spec. `brainstorm` selects it automatically for
  already-unambiguous small work; the README's "fix the typo" example now costs a handful of
  agents instead of ~25–30.
- **4.2 `--plan-only`** ✅ — stop cleanly after Phase 4 with routing, spec, and graph;
  `/dag-resume` executes once the user has read the plan.
- **4.3 Review-round exits** ✅ — a round also closes on `CONSOLIDATED: 0` and on full
  pushback (a further identical round rarely moves a considered pushback; survivors become
  Open Concerns).
- **4.4 Decorrelated reviewers** ✅ — the three angles are three explicit checklists in
  `spec-reviewer.md`, and the internal-consistency instance runs on Sonnet — the one documented
  exception to "no model overrides".
- **4.5 `/dag-cancel`, `/dag-clean`** ✅ — stop an in-flight run resumably (TaskStop, reset
  `running`→`pending`, record the cancellation); clean debris with confirmation gates on
  anything that's someone's only copy.
- **4.6 Permissions reality** ✅ — README "Before you run this": git preconditions, what the
  pipeline touches, and why a default-permission session turns the no-questions promise into a
  wall of prompts.

---

## Tier 5 — Validation

- **5.1 New invariants** ✅ — `scripts/validate.py` now also pins: the worker trailer and
  `INTEGRATION:` contracts at both ends; the mode table identical across README / brainstorm /
  orchestrator (this check caught the README drift live, mid-implementation); every
  argument-hint flag parsed by the orchestrator; eval case names matching their directories
  (and declared scaffolds existing); `run.json` keys present in the orchestrator and command
  key references inside the schema. New checks negative-tested. Both `0.2.0` contradictions
  fixed: no more "(default)" in the mode table vs "no default mode" (README now states the
  direct-invocation `full` fallback explicitly), and the `TodoWrite` line no longer denies the
  tool exists.
- **5.2 Negative-path evals** ✅ authored — `cycle-rejection`, `blocked-propagation`, `resume`,
  `budget-degradation`, all using one technique: the scaffold seeds `.dag/runs/<id>/` in a
  known partial state and the prompt is just `/dag-resume <id>` — failure paths that are
  expensive to *provoke* are cheap and deterministic to *seed*. All six scaffolds were actually
  executed against a scratch directory: they run clean, their JSON parses, the seeded ledger
  sums to exactly 118.5/120 units, and the resume fixture's branch layout is as the criteria
  describe.
- **5.3 Run the evals** ⬜ — still blocked on `plugin eval` early access. Unchanged, still the
  single highest-value item in this document.

---

## Still open

1. **The pipeline has still never run end to end.** Every `0.3.0` change is prompt-level and
   structurally validated; the scaffolds have run, the pipeline has not. The `smoke` eval
   remains the thing that would prove it and the thing that hasn't run. Treat `0.3.0` as
   unproven until it has.
2. **The evals have never run** (`plugin eval` early-access gate). The `case.yaml` schema is
   still unverified against the real runner.
3. **The worktree-reachability assumption** (Tier 0.1 ⚠️) needs one empirical check the first
   time anyone runs Phase 5: does `git cat-file -e <worker-sha>` succeed from the main repo? If
   not, the `git fetch <worktree-path>` fallback is load-bearing and should be promoted to the
   primary path.
4. **The local directory is still `agent-dag`; the plugin and remote are `yolo-dag`.**
   Deliberately not renamed mid-session again — this session's tooling and the open IDE window
   are rooted in the old path. It is one command from a fresh shell:
   `mv /Volumes/blackbox/Projects/agent-dag /Volumes/blackbox/Projects/yolo-dag`. Do it before
   anyone else clones.
5. **Resume across sessions still loses specialist context by design.** Specialists can't be
   `SendMessage`d once their session is gone; a Phase-2 resume re-spawns from persisted
   deliverables. Now explicit in `run.json` and exercised by the `budget-degradation` and
   `resume` evals, but still a real fidelity gap.

---

## The one thing not to lose

Unchanged through two releases, and it survived contact with this one too:

*"You decide, the user reviews the outcome."* Routing decisions, budget degradation, and
conflict resolution are **stated**, never **asked** — the exceptions are genuinely high-stakes
Open Concerns and the dirty-tree preflight, where guessing is worse than interrupting.

`0.3.0`'s addition to the principle: stating an outcome confidently is only a virtue when the
outcome is real. The machine trailer, the in-worktree review, the acceptance review, and the
disk-backed ledger all exist to make the confident report *true* — the pipeline now establishes
internally what it tells the user externally.
