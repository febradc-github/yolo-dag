# yolo-dag — Hand-off & Next-Level Plan

**Status:** all five milestones implemented at `0.2.0`. This document was originally the plan;
it is now the record of what changed and what is still genuinely open.

Everything below is marked ✅ done, ⚠️ done-but-unverified, or ⬜ deliberately not done. The
"still open" section at the end is the honest list of what a reader should not assume works.

---

## What this was

`0.1.0` was 13 agents and 2 skills, two commits, zero tests, never run end to end. The design was
sound — a coherent five-phase pipeline, clean role separation, a genuinely good "you decide, the
user reviews the outcome" principle. The implementation had defects that would have bitten on the
first real run, and the execution layer was missing its final third.

## Tier 0 — Correctness bugs ✅

### 0.1 — Worker output never got merged back ✅

**The critical one.** Phase 5 spawned up to 10 workers with `isolation: "worktree"`, reviewed
them, retried the flagged ones, then declared the workflow complete. Nothing merged the worktrees
anywhere. The entire product of the pipeline lived in throwaway trees while the run reported
success.

Fixed by adding **Phase 6 — Integrate** to the orchestrator: create a `dag/<run-id>` branch,
merge each PASS worktree in dependency order (wave 0 first, which is also the order that
minimizes conflicts), rework any conflict through a fresh `task-worker` in *integration mode*
rather than resolving it silently, cap integration retries at 1 per task, then run the suite once
against the assembled result. `main` is never touched.

`task-specialist` now also declares each task's expected file footprint, and the orchestrator
warns when two tasks in the same wave will fight over the same file.

### 0.2 — `ReportFindings` couldn't express a spec finding ✅

`spec-reviewer` reviews an in-context prose deliverable, but `ReportFindings` requires `file` and
`line` on every finding — reviewers would have invented paths to satisfy the schema. The tool is
removed from `spec-reviewer`, which now returns structured markdown with per-finding confidence
and evidence. `task-reviewer` keeps it, where findings genuinely anchor to files.

### 0.3 — The PASS/FLAGGED signal travelled over the wrong channel ✅

The orchestrator branched its central control flow on a `ReportFindings` call, which renders to
the host UI — what a parent actually receives is the subagent's final message. Both reviewers now
emit literal status lines (`VERDICT: PASS` / `VERDICT: FLAGGED`, `REVIEW: CLEAN` /
`REVIEW: FINDINGS <n>`), the orchestrator branches on those, and `scripts/validate.py` asserts
the strings exist at both ends so they can't drift apart.

### 0.4 — Phase numbers were wrong in every agent file ✅

All 13 agents were written against an abandoned 6-phase numbering: every specialist's
`description` said "Phase 2 fan-out" while its body said Phase 1, and all eight claimed the merge
happened in Phase 4. Since `description` is what the model reads to decide whether an agent
applies, that was actively misleading.

Settled on six phases (Route & Fan-out, Build + Review, Merge & Reconcile, Decompose, Execute &
Verify, Integrate) and corrected every reference. The validator now pins each agent's declared
phase against a table and rejects any citation outside 1–6.

### 0.5 — Agents couldn't search the codebase ✅

Not one agent had `Grep` or `Glob`, despite nearly every body instructing it to check codebase
conventions. Added to all 13 that inspect code (`spec-consolidator` is deliberately `Read`-only —
it merges lists and never touches the repo). `task-worker` also gained `WebSearch`/`WebFetch`; it
previously had no way to look up an unfamiliar library's API.

### 0.6 — `task-specialist` could hang on `AskUserQuestion` ✅

Removed. It now surfaces high-stakes ambiguity in its final message and lets the orchestrator
decide whether to escalate. The orchestrator owns the user conversation; subagents don't.

---

## Tier 1 — Real DAG, survivable runs ✅

### 1.1 — Wave scheduling replaced the independent-set constraint ✅

The old rule forbade cross-task dependencies and folded ordered work into single oversized tasks,
because "nothing in the execution layer can express 'wait for task N.'" The orchestrator *is* the
execution layer, so now it does: `task-specialist` emits `depends_on` edges, the orchestrator
validates the graph is acyclic, computes topological waves, and passes each completed
dependency's worker report into its dependents' prompts.

A cyclic graph is bounced back to `task-specialist` rather than executed or silently de-edged. A
BLOCKED task marks its dependents SKIPPED rather than running them against work that never
landed.

This is the change that makes the project's name literally true — the task layer was an antichain
and is now a DAG.

### 1.2 — Run state persists; runs resume ✅

Every phase writes to `.dag/runs/<run-id>/` as it completes (`request.md`, `routing.md`,
`specialists/<name>/round-<n>.md`, `merged-spec.md`, `reconcile.md`, `tasks.json`,
`integration.md`). `/dag-resume` re-enters at the furthest completed phase.

One real limit is documented rather than papered over: `SendMessage` only reaches agents spawned
in the current session, so a resume landing mid-Phase-2 must re-spawn that specialist with its
last persisted deliverable as context rather than addressing a dead spawn name.

### 1.3 — Routing ✅

All 8 specialists fired on every request. Now architecture, research, and test-planning always
run, and the other five are selected on stated conditions. The decision is announced, not asked.

### 1.4 — Cross-specialist reconciliation ✅

Phase 3 was a literal concatenation, and every review loop was intra-specialist, so nothing ever
checked whether the architecture spec and the data spec picked the same database. New
`spec-reconciler` agent reads all deliverables together, reports only cross-cutting
contradictions at confidence ≥ 80, and the orchestrator routes each back to both named
specialists. One exchange, then anything unresolved becomes an Open Concern.

---

## Tier 2 — Economics ✅

- **2.1 Model tiering** ✅ — `spec-consolidator` → Haiku, `task-reviewer` → Sonnet, everything
  doing load-bearing reasoning inherits. The orchestrator is told not to pass `model` overrides.
- **2.2 Budget ceilings and lite mode** ✅ — soft spawn budgets (120 `full` / 40 `lite`) with a
  defined degradation order: drop review rounds, then narrow specialists, then narrow waves, and
  say what was dropped.
- **2.3 Stuck agents** ✅ — respawn once, then proceed without and record the gap. A missing
  specialist's section reads "not produced (agent failed twice)" rather than blocking the run.

---

## Tier 3 — Confidence and adoption

- **3.1 Eval suite** ⚠️ — five cases authored under `evals/`, each targeting a defect class that
  actually shipped in `0.1.0`. **Not executed** — see "Still open" below.
- **3.2 CI** ✅ — `scripts/validate.py` plus a GitHub Actions workflow. The validator was
  negative-tested: breaking a tool name, re-adding `ReportFindings` to `spec-reviewer`, removing
  `Grep`/`Glob`, changing a declared phase, and altering a status-line contract each produce a
  distinct failure.
- **3.3 Operator surface** ✅ — `/dag-runs`, `/dag-status`, `/dag-resume`.
- **3.4 Packaging** ✅ (mostly) — MIT `LICENSE`, `license: "MIT"` and a `homepage` in
  `plugin.json`, version `0.2.0`, `.gitignore` for `.dag/`. The directory rename is ⬜ — see below.

---

## Still open

Four things a reader should not assume are done.

1. **The evals have never run.** `claude plugin eval` is gated behind early access and refused to
   run on the machine where the suite was written (`plugin eval` is currently in early access).
   The case layout follows what `--help` documents, and `case.yaml` uses only fields that help
   text names explicitly, but **the schema is unverified** and should be expected to need fixing
   on first contact. A green suite means nothing until someone with access has actually run it.

2. **The pipeline has still never run end to end.** Every defect above was found by reading, and
   every fix is a prompt-level change validated structurally, not behaviourally. `scripts/validate.py`
   proves the pieces are internally consistent; it cannot prove the orchestrator actually
   integrates a worktree correctly. **The `smoke` eval is the thing that would prove it, and it
   is exactly the thing that hasn't run.** Treat `0.2.0` as unproven until it has.

3. **The local directory is still `agent-dag` while the plugin and remote are `yolo-dag`.**
   Deliberately not renamed — doing so mid-session would have broken every path in flight for no
   functional gain. Harmless, but worth fixing before anyone else clones it.

4. **Resume across sessions is partially degraded by design.** Specialists can't be resumed once
   their session is gone, so a Phase-2 resume re-spawns them from persisted state. That loses
   whatever reasoning wasn't written down in the deliverable itself. Acceptable, but it means a
   resumed run is not bit-identical to an uninterrupted one.

## The one thing not to lose

The strongest idea here is *"you decide, the user reviews the outcome."* It's what makes this
different from every other multi-agent pipeline that turns into a twenty-question interrogation
before it does any work. Routing decisions, budget degradation, and conflict resolution are all
**stated** to the user and never **asked** of them — the only exceptions are genuinely
high-stakes Open Concerns, where proceeding on a guess is worse than interrupting. Keep it that
way.
