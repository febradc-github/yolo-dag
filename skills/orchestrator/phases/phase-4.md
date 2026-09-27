## Phase 4 — Decompose into a task DAG

**Distill's cache check comes first (inert if the `meter` subsystem isn't installed) — skip it
entirely in `micro` mode.** `micro`'s own definition is "no specialists, no spec review — the
request is the spec": by the time Phase 4 starts, `merged-spec.md` already holds nothing more than
the raw one-sentence request, copied verbatim by Phase 1's micro shortcut. There is nothing there
to compress, and running Distill anyway would spend 2-4 extra foreground spawns (compile, verify,
and on a miss a revise-and-re-verify round) on exactly the class of run `micro` exists to keep
cheap. In `micro` mode, skip straight to spawning `task-specialist` below with `merged-spec.md` as
its input, exactly as in `full`/`lite` when Distill isn't used. Everything below this paragraph
applies to `full` and `lite` only.

**Compiling a fresh brief is off by default, and a cache hit is what you are actually checking
for.** A hit is a local hash lookup: free, no spawn, and it hands you a brief already verified by
the gate below. A *miss* is a different question — whether compiling one is worth it — and at one
full-spec consumer it isn't: `meter/distill.py`'s docstring works the arithmetic, and compiling
costs several times what handing `task-specialist` a brief instead of the spec saves, on top of
2-4 serial foreground spawns stacked ahead of a step that is itself foreground and blocking. So
`distill.compile_on_miss` defaults to false and `check` reports that with its own exit code. This
costs no fidelity: the full spec is strictly the more faithful input, and steps 1-5 below are
unchanged for a deployment that turns compiling back on.

Every `dag-distill.py`
reference below means: locate it the same way `/dag-meter` does (`${CLAUDE_PLUGIN_ROOT}/scripts/
dag-distill.py`, falling back to searching `~/.claude/plugins` for a `yolo-dag` checkout, falling
back to `scripts/dag-distill.py` relative to the current directory) and run it with `python3` —
the plugin isn't necessarily under the current directory. Before spawning `task-specialist`, check
whether a compiled brief for this exact spec already exists: `dag-distill.py check
<run-dir>/merged-spec.md`. Branch on the exit code:

- **Exit `0`** (a brief is printed) — an identical spec was distilled before, this run or a prior
  one. Use the printed content in place of the full spec below and skip straight to spawning
  `task-specialist`.
- **Exit `3`** — a miss that is deliberately not worth compiling (the default). **Spawn no
  `spec-distiller` at all**, skip steps 1-5 entirely, and spawn `task-specialist` with the full
  `<run-dir>/merged-spec.md`. This is the common path, and it is not a degradation worth reporting
  — it's the configured default, so don't mention it in the summary.
- **Exit `1`** — a miss that *should* be compiled (`distill.compile_on_miss` is on). Compile
  fresh:

1. Spawn `spec-distiller` in **compile mode**, foreground, with the path to the merged spec (it
   `Read`s it itself), `meter.modules.distill.probe_count` (default 25, use 25 if `meter` config
   isn't readable) as the number of verification questions to generate, and the two paths it must
   `Write` to: `<run-dir>/brief.md` and `<run-dir>/distill-answer-key.md`.
2. Spawn `spec-distiller` again — a **fresh spawn**, not a resume — in **verify mode**, foreground,
   with only the path to the brief (`<run-dir>/brief.md` — it `Read`s it itself) and the bare
   questions (strip the answers out of `distill-answer-key.md` yourself before passing them —
   never the answers, never the full spec).
3. **Grade it yourself.** `Read` `<run-dir>/distill-answer-key.md` for the stored answers and
   compare the verify pass's answers against them, question by question. Any answer that's wrong,
   or "not answerable from the brief," is a miss. **Any miss at all rejects the brief** — this is
   what makes Distill "verified" rather than a hopeful compression, so don't round up a near-miss.
4. **On a miss**, `SendMessage` the compile-mode spawn (resume it, don't respawn) naming exactly
   which questions it missed and why, and ask it to revise `brief.md` in place to cover those gaps.
   Re-run the verify pass (a fresh spawn again) once, against the same path. If it passes this
   second attempt, proceed with the revised brief. **If it fails again, give up on distillation for
   this spec**: use the full spec (`<run-dir>/merged-spec.md`) for `task-specialist` below, and
   note the failure in one line in your final report (this is a real degradation worth surfacing,
   same spirit as the existing budget degradation channel).
5. **On a full pass** (first or second attempt), cache it: `python3 scripts/dag-distill.py store
   <run-dir>/merged-spec.md <run-dir>/brief.md` — the brief is already on disk at that path, so no
   temp file needed. Use `<run-dir>/brief.md` in place of the full spec below.

Spawn one `task-specialist` with the path to the full merged and reconciled spec
(`<run-dir>/merged-spec.md`), or — if Distill produced a brief that passed its gate — the path to
the brief instead (`<run-dir>/brief.md`), stating plainly that the full spec is at
`<run-dir>/merged-spec.md` if `task-specialist` needs something the brief doesn't cover (if it
says it does, tell it to Read that path, and afterward run `python3 scripts/dag-distill.py
record-fetch <run-dir>/merged-spec.md` — this is the fetch-rate signal that tells a future
tuning pass the brief was too thin), plus the path it must `Write` the task graph to:
`<run-dir>/tasks.json`. Foreground either way — nothing else can proceed until it returns. It
applies the decomposition rules in its own agent definition — **independence first, shared
contracts where a boundary is unavoidable, a real `depends_on` edge only as a last resort** —
writes the graph directly to `tasks.json`, and returns a short human-readable summary (contracts,
a one-line-per-task list, folds/assumptions made) rather than pasting the graph inline.

**Validate the graph before executing it.** `Read` `<run-dir>/tasks.json` — written directly by
`task-specialist` — then check:

- every id is unique;
- every `depends_on` entry names a task that exists, and carries a `reason`;
- the graph is **acyclic**;
- **no file is owned by two tasks.** Every path in a task's `owns` list belongs to exactly one
  task. This is a hard check, not a warning: shared ownership means the split is wrong, and the
  fix is the specialist's (re-split, or move the shared surface into a contract), not yours;
- every contract named in a task's `contracts` list is defined in the top-level `contracts`
  array, and each contract has exactly one owner task.

If any check fails, `SendMessage` the specific problem back to `task-specialist` and ask for a
corrected graph. A cycle will deadlock Phase 5 — never try to execute one, and never break it
yourself by dropping an edge at random.

A `depends_on` entry may also arrive as a bare id string rather than an `{id, reason}` object —
graphs written by an older version of this pipeline, and hand-edited state, both look like that.
Read it as an edge with an unstated reason rather than failing the run over it, and read a legacy
`files` key as `owns`.

Then compute the **topological levels** ("waves") — level 0 is every task with
`depends_on: []`; level *n* is every task whose dependencies all sit in levels `< n`. Levels are
the proof of acyclicity and the reporting shape ("14 tasks in 4 waves: 6, 5, 2, 1" — report that
line to the user); the actual scheduling in Phase 5 is a user-paced pass loop over the candidate
set, not a strict wave-by-wave walk. A well-decomposed graph is mostly level 0 — that is the
point of the independence-first rules, not a sign the specialist under-thought the ordering.

**Report the plan, then branch on the run type** — read `plan_only` from `run.json`, not from
memory. Either way, first report the routing decision,
where the merged spec lives, the task graph and its shape, the shared contracts and who owns each,
and any Open Concerns.

- **Full run** → **enter Phase 5 immediately, with no confirmation.** Do not ask whether to
  proceed, do not re-ask anything `brainstorm` already settled. The user chose a full run at the
  start precisely so this boundary wouldn't stop them.
- **Plan-only run** (`plan_only: true`) → **stop at this boundary and ask**, via
  `AskUserQuestion`, whether to proceed with implementation now. Start no work — no branch, no
  worker — until they confirm.
  - **In a non-interactive run** (`non_interactive: true`) there is nobody to ask, so stop here
    cleanly instead: leave phases 5–6 `"pending"` in `run.json` and report that
    `/dag-resume <run-id>` executes the plan. The confirmation still gates the work; it just
    arrives as a later invocation rather than an answer.
  - **Proceed** → move into Phase 5.
  - **Not yet** → leave phases 5–6 `"pending"` in `run.json` and tell them `/dag-resume <run-id>`
    executes the plan once they're ready.
  - **Substantive feedback on the spec or graph itself** (not just "not yet") → route it to
    where it actually belongs — a task-shape complaint back to `task-specialist` (resumable
    in-session via `SendMessage`), a spec-level complaint back to the relevant Phase 1
    specialist — re-validate whatever comes back, then re-offer this same gate. Don't
    reinterpret their feedback yourself and proceed on a guess.

