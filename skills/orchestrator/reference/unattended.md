# Unattended runs — flag validation

Read this **only** when `--non-interactive` or `--tasks-per-pass` appears in `$ARGUMENTS`.
Neither flag is ever inferred — not from a missing TTY, not from how the request reads, not
from a question going unanswered — so a run without them never needs this file.

**Unattended runs.** Two more flags let a caller with no user attached — an eval runner, a
scripted invocation — pre-answer this pipeline's two standing questions instead of hanging on
them:

- **`--non-interactive`** declares that no user is attached. It is **explicit and never
  inferred** — not from a missing TTY, not from how the request reads, not from a question
  going unanswered. Record it as `non_interactive: true` in `run.json`.
- **`--tasks-per-pass N`** pre-answers Phase 5's per-pass question with `N`, for every pass in
  the run. Record it as `tasks_per_pass: N`.

Validate these at startup, before creating a branch or spawning anything, and **stop with a
plain error** rather than proceeding:

- `--tasks-per-pass` without `--non-interactive` → refuse, and say why: in an interactive run
  the per-pass question is a review checkpoint the user is entitled to, and this flag is not a
  way around it.
- `--non-interactive` without a run type (`--plan-only` or `--full-run`) → refuse: the run
  would walk straight into the opening question with nobody there to answer it, which is the
  failure this mode exists to prevent.
- `--non-interactive` without `--tasks-per-pass` → refuse, **including on a plan-only run**,
  which parks before Phase 5 and never asks the question itself. The number is run state, not
  an answer to one prompt: `run.json` carries it into the pass that `/dag-resume` eventually
  runs, and requiring it here is what lets that resume proceed unattended without a second
  flag of its own. Set once at setup, used whenever the run reaches Phase 5.
- `N` above the run type's cap — **3 on a full run**; a plan-only run has no cap — → refuse and
  name the cap. Never silently clamp a pre-answered number. (`N` larger than the number of
  eligible tasks is not an error: Phase 5 runs the eligible subset exactly as it would for a
  typed answer.)
- `N` that is not a whole number ≥ 1 → refuse.

Non-interactive mode changes *where the two answers come from*, and nothing else. The plan is
still reported, the pass barrier is still a barrier, dependency rules still decide what is
eligible, and the per-pass structure is identical.
