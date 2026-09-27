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
   array — the *current* version, not the version quoted in an older report), and, **for each task
   in its `depends_on`, that task's `dag-receipt` block rather than its full prose report**.

   The receipt is the bounded form of the same information — files and spans touched, symbols,
   which criteria were met with what evidence, the verification command and exit code, contracts
   owned/consumed, risks — and a dependent needs no more than that, because its worktree already
   *contains* the dependency's code. Pasting several full reports instead re-pays each one as
   input for every dependent, to restate what the tree already holds; that is exactly the
   "give it the path, not the text" rule in the spine's ground rules, applied to the one place in
   this pipeline that was still restating in full. Say plainly that the dependencies' code is
   already present and the receipts are context, not the source of truth. **If a dependency's
   receipt is missing or unusable** — no `dag-receipt` block in its final message, or one so
   incomplete a dependent couldn't act on it — fall back to that dependency's prose report, for
   that dependency alone; never drop a dependency's context entirely. Note that `meter` being
   absent is *not* that case: every worker emits the block because its own template says to, and
   `meter` only validates what the template already produces.

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
   the changes do not exist. Restate those two values as their own literal lines,
   `WORKTREE: <path>` and `COMMIT: <sha>`, somewhere in the reviewer's prompt (same "inert if
   `meter` isn't installed" pattern as the `DAG-NODE:` line above) — this is the only way a local
   measurement tool can find the worktree/commit to run deterministic checks against before the
   reviewer starts; without a fixed format, it has no reliable way to locate them in free-form
   prose. Tasks in a pass move through their own cycles independently; the barrier is at the end
   of the pass, not between its stages.
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
    - **This reopen does not draw against the task's ordinary rework budget** (the 2-rework/
      3-review cap from step 9) — it is a separate, independently bounded loop, capped instead by
      the one-revision-per-contract rule below. A task can in the worst case see both its full
      normal rework budget *and* one contract-triggered reopen in the same run without either
      budget starving the other.
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

