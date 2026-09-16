# METER: a token-efficiency wrapper for yolo-dag

**Status:** design handoff, ready for implementation
**Target repo:** `febradc-github/yolo-dag`
**Audience:** the coding agent implementing this, plus a human reviewing the plan
**Version:** 1.1 (decisions from Section 12 folded in; subsystem renamed from `bursar` to `meter`)
**Date:** 2026-09-16

---

## 0. How to read this document

Sections 1 to 3 are the argument: where the tokens go, what the idea is, what must not break.
Sections 4 to 7 are the specification: functional, architectural, technical, and per-phase integration.
Sections 8 to 12 are delivery: operations, implementation plan, measurement, risks, and the recorded decisions on what were open questions in the draft (Section 12 is now settled, not a list of choices to make).

Rules for the implementing agent:

1. **Milestone M0 ships alone and changes no behaviour.** Nothing else in this document is allowed to merge until M0 is producing real numbers. Every estimate below is a hypothesis; M0 exists to replace hypotheses with measurements.
2. **Every claim in this document marked `VERIFY` must be probed against a live Claude Code install before code depends on it.** Claude Code's hook surface moves fast. Probes are cheap. Assumptions are not.
3. **Fail-open is a hard requirement.** If the wrapper is broken, missing, misconfigured, or slow, yolo-dag must run exactly as it does today. There is no degraded-but-weird mode.
4. Do not change yolo-dag's documented behavioural contracts (Section 3.2). The wrapper changes what a run *costs*, not what it *does*.

---

## 1. Problem statement

### 1.1 The stated problem

yolo-dag is powerful, and expensive. A worst-case `full` run spawns north of a hundred agents. The README already flags cost as one of three things to know before running. The plugin has a budget system, but that system counts *spawns*, weighted by model tier (session model 1.0 units, Sonnet 0.5, Haiku 0.1), not tokens. Two spawns of the same agent type can differ by an order of magnitude in actual spend. The budget is therefore a proxy that can be systematically wrong in the same direction as the problem it is meant to control.

### 1.2 Where the tokens actually go

Four structural redundancies. All four follow from design decisions that are correct for reliability and expensive for cost. The wrapper's job is to keep the reliability and remove the cost.

| # | Redundancy | Mechanism in yolo-dag today | Why it is expensive |
|---|---|---|---|
| R1 | **Transmission** | Every Task prompt is fully self-contained: spec excerpts, contracts, conventions are pasted per agent, by design, because a subagent shares nothing with the parent | The same bytes are billed once per agent. With ~100 agents and a few thousand tokens of shared material each, this is hundreds of thousands of input tokens per run for content that never changed |
| R2 | **Discovery** | Each worker and reviewer starts cold in its own worktree and finds its way around with Glob, Grep and Read | N agents independently re-derive the same map of the repo. Search is a solved deterministic problem being paid for at model prices |
| R3 | **Reporting** | Agents return prose to the orchestrator; the orchestrator's context accumulates and is re-sent on every subsequent turn | Cost of the orchestrator's context grows with the number of completed nodes, and is paid again on every turn after that. Roughly quadratic in run length |
| R4 | **Execution** | Review rounds (up to 3), reworks (up to 2 per task), contract revision, and re-runs of the same run after a cancel all redo work from scratch | A rework repeats the entire task, not the delta. Two runs of a near-identical task share nothing |

A fifth, specific to multi-round review: specialists are resumed via `SendMessage` rather than re-spawned. That is the right call for coherence, and it means round 3 re-sends rounds 1 and 2 as history. Cost per round grows with round number.

### 1.3 What the wrapper cannot fix

Be honest about the floor. Adversarial review is expensive because three independent reviewers reading a deliverable is the product, not an inefficiency. Nothing here reduces the number of opinions in a review round. It reduces what each opinion costs to obtain, and it makes the cost of an extra opinion visible before it is spent.

---

## 2. The idea

### 2.1 One sentence

**Meter is a local, zero-network daemon that ships inside the plugin, attaches to Claude Code's hook lifecycle, measures every token the run actually spends, and then removes the four redundancies: it caches finished work, precomputes repository context deterministically, replaces agent prose with structured receipts, and detects text that is being re-transmitted across agents so it can be moved into a prompt-cacheable shared prefix.**

### 2.2 The part that is not obvious

Three pieces of this are, as far as I can tell, not standard practice anywhere in the agent-orchestration ecosystem, and they are the reason this is worth building rather than buying.

**(a) The Echo Index.** Hash every chunk of text that enters any agent's context during a run, at paragraph granularity, and count how many distinct agents received each chunk. The output is a ranked list: "this run transmitted this 1,800-token block of merged-spec to 41 agents." That number is the exact cost of R1, measured rather than guessed, and it is also the input to the fix: any chunk above a duplication threshold gets promoted into a byte-identical prompt prefix shared by every agent in a cohort. Anthropic's prompt cache bills a repeated prefix at roughly 0.1x the base input rate, and breaks even after a single reuse. A chunk sent to 41 agents is being billed at 41x when it could be billed at roughly 1.25x + 40 x 0.1x. The Echo Index turns the pathology into its own optimisation signal.

**(b) The Vault: content-addressed caching of *agent artifacts*, not agent reasoning.** Agent output is non-deterministic, which is why nobody caches it. But the *artifact* a task-worker produces is a patch, and a patch is verifiable: it either applies to the current tree or it does not, and the task's acceptance criteria either pass or they do not. So key a cache on a semantic hash of (normalised task spec, contract text, hashes of the exact input file spans, model tier, plugin version), store the resulting patch plus its receipt, and on a hit, replay the patch and verify it instead of spawning a worker. Two properties make this safe: replay skips the *worker*, never the *reviewer*, so yolo-dag's invariant that "a task is done only when a reviewer approves it" survives untouched; and a replay that fails to apply or fails verification falls through to a normal spawn, seeded with the stale patch as a warm start. This is ccache for coding agents. The immediate win is not across runs, it is *within* a run: reworks and review rounds are where near-identical work is repeated today.

**(c) Codemod promotion.** When the Vault sees several nodes in a run whose specs are near-duplicates (MinHash similarity over normalised spec text) and whose resulting patches are structurally similar, it asks one model call to write a deterministic transformation (comby, jscodeshift, Rector, libcst, or plain sed) that reproduces those patches, verifies it against the patches already produced, and stores the transformation rather than the diffs. The 30th "add a typed return to this controller method" node then costs approximately zero tokens, and the transformation is reusable in the next run and the next repo. The LLM is used once as a compiler-writer instead of N times as a compiler. This is the most speculative module here and it is gated behind everything else for that reason.

### 2.3 Why a hook-attached daemon rather than a CLI wrapper

The obvious shape would be a launcher that wraps the `claude` binary. That shape cannot see inside the run: it cannot intercept a subagent's Read, it cannot rewrite a Task prompt, it cannot compress a tool result before it enters context. Claude Code's hook system can do all three, and plugins can ship hooks. That is the whole reason this is implementable rather than hypothetical:

- `PreToolUse` can return `hookSpecificOutput.updatedInput`, which **replaces a tool's arguments before it runs**. A `Read` of a whole 2,000-line file becomes a `Read` of the 60 lines that matter.
- `PostToolUse` can return `hookSpecificOutput.updatedToolOutput`, which **replaces the tool's result before Claude sees it**. A 40KB test log becomes a 400-token digest. (Documented as honoured for all tools, not just MCP tools, since v2.1.121. `VERIFY` against the installed version.)
- `SubagentStart` can return `additionalContext`, injected at the start of the subagent's conversation, before its first prompt. That is the dossier delivery point.
- `SubagentStop` receives `agent_id`, `agent_type`, `agent_transcript_path` and `last_assistant_message`, and can block with exit code 2 to force one more turn. That is receipt validation and token accounting.
- Hooks fire inside subagents, carrying `agent_id` and `agent_type`, so every tool call in the entire run is attributable to a node.
- `${CLAUDE_PLUGIN_DATA}` is a plugin-scoped persistent directory that survives plugin updates and is removed on uninstall. That is where the Vault lives.

A wrapper built on hooks ships with the plugin, needs no change to how the user launches Claude Code, and degrades to a no-op if anything about it fails.

### 2.4 Thesis in one table

| Redundancy | Module | Mechanism | Expected effect |
|---|---|---|---|
| R1 transmission | **Echo Index** (M4) + **Governor** prefix discipline (M6) | Measure cross-agent chunk duplication, promote hot chunks into a byte-identical cacheable prefix, order prompts stable-first | Duplicated input billed at cache-read rates instead of full rates |
| R2 discovery | **Dossier** (M2) + **Clamp** (M3) | Deterministic per-node context packs from ripgrep, tree-sitter or ctags, and git; bounded reads via `updatedInput`; compressed tool results via `updatedToolOutput` | Exploration turns and oversized tool results collapse |
| R3 reporting | **Receipts** (M1) | Fixed-schema JSON return per agent, validated at `SubagentStop`; full transcript spooled to disk and queried on demand | Orchestrator context grows by a bounded amount per node |
| R4 execution | **Vault** (M5) + **Codemod promotion** (M7) | Content-addressed patch cache with verification-gated replay; promotion of repeated transformations to deterministic codemods | Reworks, retries and repeated node shapes stop costing full price |
| (all) | **Ledger** (M0) | Per-node measured token accounting from transcripts or OTEL | Makes every claim above falsifiable, and replaces the spawn-unit budget proxy |

---

## 3. Scope

### 3.1 In scope

- A Python package shipped inside the plugin, stdlib only, no `pip install` at any point.
- A `hooks/hooks.json` that registers the wrapper with Claude Code.
- A loopback-only daemon, started on `SessionStart`, stopped on `SessionEnd`.
- Two new slash commands: `/dag-cost` and `/dag-vault`.
- Extensions to `scripts/validate.py` covering the new invariants.
- Config file, kill switch, purge command, docs.

### 3.2 Invariants that must not break

These are yolo-dag's existing contracts. The implementing agent must treat each as a test, not a guideline.

1. A task is done only when a **reviewer approves it**. Vault replay skips workers, never reviewers.
2. All fan-out is flat, from the orchestrator. No agent spawns agents. The wrapper spawns nothing.
3. Every file is owned by exactly one task. The wrapper never writes to the repo working tree, only to `.dag/` and `${CLAUDE_PLUGIN_DATA}`.
4. The pipeline refuses to start over a dirty tree, never touches `main`, and puts all output on `dag/<run-id>`. Unchanged.
5. `run.json` remains the source of truth for resume. The wrapper adds keys, never removes or repurposes them.
6. The literal status-line contracts (`VERDICT: PASS`, `REVIEW: CLEAN`, `RECONCILE: CLEAN`, `INTEGRATION: PASS`, `WORKTREE:`, `COMMIT:`) keep working byte for byte. Receipts are **additive**.
7. The two interactive gates (plan-only implementation boundary, per-pass number) stay exactly as they are. The wrapper never adds a prompt.
8. Mode table (`full` / `lite` / `micro`) stays identical in all three files that state it, as `validate.py` enforces.

### 3.3 Out of scope

- Any change to routing, specialist selection, review-round counts, or the DAG semantics.
- Any network egress. The daemon binds loopback and makes no outbound calls.
- Any proxying of the Anthropic API or manipulation of `ANTHROPIC_BASE_URL`.
- Sharing a cache between users or machines.
- Replacing the spawn-unit budget in the first release. M0 runs alongside it; the Governor replaces it only after M0 has produced a baseline.

---

## 4. Functional specification

Each module below is independently shippable and independently disableable. `meter.modules.<name>.enabled` is a config boolean for each. Default states are given per module.

### M0. Ledger (measurement substrate)

**Default: enabled. Ships alone. No behavioural change.**

Purpose: attribute every token the run spends to a DAG node, and write it down.

**Probe zero, before any code.** Determine whether yolo-dag's agents are Task subagents or agent-team teammates. The README says specialists are resumed via `SendMessage`, which is the agent-teams mailbox tool, so this is genuinely ambiguous and it decides the whole attribution design. Task subagents carry `agent_id` and `agent_type` in every hook payload. Teammates are separate Claude Code sessions with their own `session_id` and their own transcript, and there is a reported gap where the `PreToolUse` payload identifies no teammate. If the agents are teammates, per-node attribution comes from `session_id` plus worktree path, and the transcript-per-teammate structure is actually easier to sum but harder to map. Nothing else in M0 can be designed until this is answered.

Behaviour:
- On `SubagentStart`, record `agent_id`, `agent_type`, wall-clock start, and the run/node correlation (see 6.4) into the run ledger.
- On `SubagentStop`, read `agent_transcript_path` and sum the per-message usage fields (`input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`). `VERIFY` the exact field names against a live transcript before depending on them; ship a schema probe that fails loudly and degrades to "unknown" rather than reporting a wrong number.
- On `Stop` and `SessionEnd`, sum the main-thread usage the same way and write `.dag/runs/<run-id>/meter/ledger.jsonl` plus a rendered `report.md`.
- Compute derived metrics: tokens per node, per agent type, per phase, per review round, cache-read ratio, and the ratio of measured cost to the spawn-unit estimate the orchestrator used.

Outputs:
- `ledger.jsonl`: one record per node, appendable and crash-safe.
- `report.md`: human summary, printed by `/dag-cost`.
- `baseline.json`: aggregate stats retained in `${CLAUDE_PLUGIN_DATA}` across runs, keyed by repo fingerprint and mode, for the Governor's cost model.
- **Round-over-round growth for resumed specialists.** Specialists are resumed rather than re-spawned, so each review round re-sends the accumulated history. Record, per specialist, per round: input tokens, cache-read tokens, cache-write tokens, and the wall-clock gap since its previous turn. Gap against cache-read ratio is the whole diagnosis: a gap longer than the cache TTL means the entire history was re-billed at full rate. This is the single most informative measurement M0 produces, because the fix for it (Section 8.7) is a settings key rather than new machinery.

Failure mode: if the transcript cannot be read or parsed, record the node with `tokens: null` and a `reason`. Never block, never guess.

Optional high-fidelity mode: Claude Code emits OpenTelemetry metrics including token usage, correlated by `prompt_id`, which also appears in hook input. If the user launches through the optional wrapper script (Section 8.5), the Ledger consumes OTEL instead of transcripts. This is strictly an accuracy upgrade, never a requirement.

### M1. Receipts

**Default: enabled.**

Purpose: cap R3. The orchestrator ingests a bounded structured record per node instead of unbounded prose.

Behaviour:
- Agent templates gain a required trailing fenced block:

````
```dag-receipt
{ "v": 1, "node": "T-003", "status": "done|blocked|partial",
  "files": [{"path": "src/a.ts", "spans": [[10,48]], "action": "modify"}],
  "symbols_touched": ["AuthService.refresh"],
  "ac": [{"id": "AC-02", "met": true, "evidence": "tests/auth.spec.ts:44"}],
  "verification": {"cmd": "npm test -- auth", "exit": 0, "digest": "sha256:…"},
  "contracts": {"owned": ["C-01"], "consumed": []},
  "risks": ["refresh path untested under clock skew"],
  "worktree": "…", "commit": "…" }
```
````

- On `SubagentStop`, the daemon parses the receipt. If it is missing or fails schema validation, the hook exits 2 with a precise message naming the invalid field. Exit 2 on `SubagentStop` prevents the subagent from stopping, so it gets exactly one repair turn. A second failure is recorded and allowed through, so a malformed receipt can never deadlock a node.
- Existing status lines (`VERDICT: PASS` and friends) stay in the prose. The receipt is additive and the validator checks both ends.
- Full transcripts are spooled to `.dag/runs/<run-id>/meter/transcripts/<node>.jsonl`. They are never re-injected into the orchestrator automatically. `/dag-status <run-id> --explain <node>` runs a Haiku-tier pass over the spooled transcript only when a human asks.

Why this is bounded and safe: a receipt is on the order of 300 to 600 tokens. Prose returns today are frequently 20x that, and the orchestrator pays for them on every subsequent turn.

### M2. Dossier

**Default: enabled for workers and reviewers. Disabled for specialists** (specialists reason about a spec, not a repo).

Purpose: kill R2. Replace agent exploration with deterministic precomputation.

Behaviour:
- Before a pass dispatches, for each node in the pass, the daemon builds a dossier with **zero model calls**:
  - Seed terms from the task record: `title`, `owns`, `contracts`, acceptance criteria, symbol-shaped tokens in the description.
  - Symbol index: tree-sitter if available, else ctags, else a language-specific regex table. Maps symbol to `file:start-end`, kind, and signature.
  - Import graph: 1-hop neighbours of the owned files. Python via `ast`, TS/JS via import-statement extraction, PHP via `use` statements and the Composer autoload map.
  - Text search: ripgrep if on PATH, else a stdlib walker, over seed terms.
  - Git signal: recent commits touching the owned files, and their diffs, capped.
  - Sibling context: receipts of already-completed nodes that touched the same files in this run.
- Ranked and truncated to a token budget (`meter.dossier.max_tokens`, default 4000, measured with the daemon's tokenizer estimate).
- Written to `.dag/runs/<run-id>/meter/dossiers/<node>.md`.
- Delivered at `SubagentStart` via `additionalContext`, as a **short pointer plus a summary**, not the whole dossier: hook output strings are capped at 10,000 characters, above which Claude Code writes them to a file and passes a path. Do not fight that; write the file yourself and pass the path deliberately.
- `additionalContext` must be phrased as **factual statements**, not imperative instructions. Claude Code's own guidance is explicit that out-of-band command phrasing can trip prompt-injection defences and get surfaced to the user instead of used. Correct form: "A precomputed context pack for this task is at `.dag/.../T-003.md`. It lists the 7 files this task owns with the relevant line spans, the 3 symbols they reference, and the contract text. It was generated from the repository index, not from a model."

**Enforcement, with an escape hatch.** A dossier that agents ignore saves nothing. So:
- `PreToolUse` on `Glob` and `Grep`, for an agent with a dossier, when the dossier has not yet been read: deny once with a reason pointing at the dossier path.
- After the dossier is read, allow everything.
- Denials are capped at `meter.dossier.max_denials` (default 2) per agent, ever. After the cap, the wrapper never denies again for that agent. An agent that genuinely needs to explore must always be able to.
- Denial events are logged with the query that was denied. A high denial-then-explore rate means the dossier is bad; that is a tuning signal, and the report surfaces it.

### M3. Clamp

**Default: enabled.**

Purpose: stop oversized tool payloads from entering context. Two mechanisms, both deterministic.

**3a. Bounded reads (`PreToolUse` + `updatedInput`).**
- A `Read` with no `offset`/`limit` on a file larger than `meter.clamp.full_read_threshold` lines (default 400), where the symbol index knows which spans this node needs, is rewritten to a bounded read of those spans plus a margin.
- `updatedInput` replaces the **entire** input object, so all fields must be re-emitted, not just the changed ones.
- A repeat `Read` of a span this agent has already read in this session returns a short note that it already has the content, instead of the content again.
- Hard exclusions: never rewrite reads of files under `owns` for the node (the agent is editing those), never rewrite when the index has no opinion, never rewrite binary or generated files.

**3b. Output compression (`PostToolUse` + `updatedToolOutput`).**
Per-tool compressors, each a pure function with a golden-file test:
- Test runners: keep the summary line, every failure block, and the last 40 lines of stderr. Drop passing-test noise. Always keep the full text when the exit code is non-zero and the output is under the threshold.
- `git diff` / `git status`: keep the stat summary and hunk headers past a size threshold, with a pointer to the full text on disk.
- Build and install logs: keep errors, warnings, and the tail.
- `Grep` with a large result set: group by file, cap per-file hits, state the count that was elided.
- Every compression writes the full original to `.dag/runs/<run-id>/meter/tool-output/<id>.txt` and names that path in the replacement, so nothing is lost and the agent can escalate.

**Absolute rule: a compressor must never remove an error message.** If a compressor is uncertain, it passes the output through unchanged. Uncertainty is cheap; a swallowed stack trace costs a rework loop, which costs far more than the tokens saved.

### M4. Echo Index

**Default: enabled in measure-only mode. Promotion is opt-in until validated.**

Purpose: measure and then eliminate R1.

Behaviour:
- Every text payload that enters an agent's context through a channel the wrapper can see (Task prompt, dossier, `additionalContext`, tool results) is split into normalised chunks (paragraph or fenced block, whitespace-collapsed, 64-bit hash).
- The index counts, per chunk: bytes, estimated tokens, number of distinct agents that received it, and where it came from.
- Report output: a ranked "echo table", the headline metric being **echoed tokens**, defined as `sum over chunks of (tokens x (recipients - 1))`. That number is the measured size of R1.
- **Promotion (phase 2):** chunks above `meter.echo.promote_threshold` recipients are written into a per-cohort **preamble file**. The orchestrator's Task prompt template is changed so that the preamble text appears first, byte-identical, in every Task prompt in the cohort, with volatile per-task content strictly after it. Prompt cache matching is a byte-for-byte prefix match, so ordering discipline is the entire game: tools, then stable preamble, then semi-stable cohort text, then the task-specific tail.
- The wrapper does not and cannot set `cache_control` markers itself; Claude Code manages caching internally. What the wrapper controls is whether a cacheable prefix *exists*. Making sibling prompts share a long identical head is the lever available.

### M5. Vault

**Default: `shadow`. Four modes, not a boolean.**

`vault.mode` takes `off`, `shadow`, `rework_only`, or `full`.

| Mode | Behaviour |
|---|---|
| `off` | No keys computed, no store written |
| `shadow` | Keys computed, candidate replays recorded, **the worker runs anyway**. Afterwards the would-be replay is diffed against what the worker actually produced |
| `rework_only` | Real replay, restricted to same-run rework and retry cases where the task is identical and the tree has barely moved |
| `full` | Real replay across runs and repeats |

The boolean was the wrong shape. An adversarial test set can only contain near-misses someone already thought of, and the failure mode here is the near-miss nobody thought of. Shadow mode gets precision and recall from live work at zero risk, which is strictly better evidence than a synthetic suite.

**Promotion path.** `shadow` until 50 runs show zero would-be-wrong replays, then `rework_only` because it has both the highest hit rate and the narrowest blast radius, then `full` only if `rework_only` stays clean for another 50. Each promotion is a config change and a release note, never automatic.

Purpose: eliminate R4.

**Key.**
```
key = sha256(
  norm(task.description) || norm(task.acceptance_criteria) ||
  norm(contract_text_for(task)) || sorted(hash(span) for span in dossier.input_spans) ||
  model_tier || agent_type || plugin_version || vault_schema_version
)
```
`norm()` lowercases, collapses whitespace, strips IDs, dates, run ids and ordinal markers, and sorts bullet lists. Normalisation is a pure function with an exhaustive unit-test table, because every false-positive collision is a wrong patch.

**Value.** `{ patch (unified diff), receipt, verification_cmd, verification_digest, model_tier, tokens_spent, created_at, hit_count }`. Patches are stored content-addressed under `${CLAUDE_PLUGIN_DATA}/meter/artifacts/`; the index is SQLite in the same directory.

**Replay path (exact hit).**
1. `git apply --3way --check` the stored patch against the node's worktree. Failure means fall through.
2. Apply it. Run the stored verification command. Non-zero exit means revert and fall through.
3. Write a receipt marked `source: "vault"` with the original node's receipt attached as provenance.
4. **Hand the node to its reviewer exactly as if a worker had produced it.** The reviewer does not know or care that it was a replay. Invariant 3.2.1 holds.

**Warm start (near hit).** MinHash similarity over the normalised spec above `meter.vault.warm_threshold` (default 0.85), or an exact key whose patch failed to apply: spawn the worker normally, and include the stale patch in its dossier as factual context ("a previous solution to a near-identical task is at `<path>`; it does not apply cleanly to the current tree"). This is where most of the value lands in practice, because reworks are near hits by construction.

**Where the hits come from, in order of expected value:**
1. Rework loops inside one run (same task, one review round later, tree barely changed).
2. A cancelled-and-resumed run.
3. Repeated node shapes within one run (the same mechanical change across N files or N endpoints).
4. Across runs in the same repo.
5. Across repos. Rare, and mostly the domain of M7.

**Invalidation.** Vault entries are invalidated by plugin version bump, vault schema bump, or any change to the hashed input spans. There is no time-based expiry; there is a size cap with LRU eviction and a `/dag-vault purge` command.

### M6. Governor

**Default: `advise`. It recalibrates and reports. It enforces exactly one thing, and only at 3x.**

Purpose: turn measurement into control.

**6a. Recalibrate the existing budget. Do not replace it.**

The spawn-unit system is not wrong in shape, it is wrong in calibration. The degradation ladder (fewer review rounds, then narrower specialists, then lower parallelism) is a **quality** policy expressed in cost terms, and those are correctness decisions the orchestrator should keep owning. What is broken is the input: `1.0 / 0.5 / 0.1` per model tier are guesses, and they are per *tier* when the real variance is per *agent type*. A `task-worker` and a `spec-consolidator` on the same tier are not the same cost, and nothing in the current weighting can express that.

So the Governor writes `weights.json` back from `baseline.json`: a measured cost coefficient per `(agent_type, mode)`, normalised so that the existing ceilings (120 / 40 / 8) keep their current meaning on an average run. The orchestrator reads the weights if present and falls back to the hardcoded tier weights if absent. The ceiling, the ladder, the degradation reporting and the per-pass prompt are all untouched and simply become accurate.

This is a much smaller change than replacing the budget, introduces no new failure mode, and preserves a design that already works. Prediction output stays advisory: the predicted cost of a pass is printed next to the existing one-number prompt as information, never as a gate.

**6b. Runaway containment.** A node that exceeds 2x its prediction gets one `additionalContext` note on the next `PostToolUse` stating the overrun as a fact and that a partial receipt is acceptable. At 3x, `PreToolUse` denies further tool calls for that node with a reason instructing it to emit its receipt now. This is the only place the wrapper deliberately stops work, and it is bounded, logged, and reported in the run summary as a degradation, using yolo-dag's existing degradation channel.

**6c. Model routing proposal.** yolo-dag already declares model tiers per agent (`spec-consolidator` on Haiku, `task-reviewer` on Sonnet, everything load-bearing on the session model, internal-consistency reviewer deliberately decorrelated onto Sonnet). The Governor does not override these. It measures outcomes per tier and produces a routing report: which agent types show no measurable quality difference at a lower tier, judged by review-pass rates and rework counts. A human decides. Automatic downgrade is explicitly not in scope, because the decorrelation rationale is a correctness argument, not a cost one.

**6d. Cache-cohort scheduling.** Sibling nodes sharing a promoted prefix are grouped into a cohort and dispatched contiguously. Anthropic's default prompt cache TTL is short (5 minutes, refreshed on each read) with a longer tier available; a cohort that stretches past the window loses the prefix and pays a fresh write. So: order the pass to keep a cohort contiguous, and never interleave two cohorts when a topological order permits keeping them apart. Dependency order always wins; cohort affinity is a tie-breaker only.

### M7. Codemod promotion

**Default: disabled, and `scope: "repo"` when enabled. Experimental. Ships last or not at all.**

Purpose: convert repeated transformations into deterministic code.

Behaviour:
- The Vault flags a cluster when N nodes (default 3) in a window have spec similarity above threshold and patches with matching AST-level shape.
- One model call is made, at Sonnet tier, with the cluster's patches as input, asked to produce a transformation script in whichever of comby, jscodeshift/ts-morph, Rector, libcst, or sed fits the language.
- The script is verified by running it against each cluster member's pre-state and diffing against the known-good post-state. **All members must match exactly, or the codemod is discarded.**
- **Scope.** Patches are always repo-scoped; a diff means nothing elsewhere. Codemods are global-eligible, because verification rather than scope is what makes them safe: every application runs the node's own acceptance command before a reviewer sees it. Global is opt-in and gated on a literal-scrub check that rejects any transformation carrying identifiers, paths or string literals from the source repo beyond the pattern itself.
- A verified codemod is stored in the Vault as a transformation, keyed by the cluster's normalised spec centroid. Future matching nodes run the codemod, verify with the node's own acceptance command, and hand off to the reviewer. Cost: approximately zero model tokens.
- A codemod that fails verification on a new node is never retried on that node; the node falls through to a normal spawn, and the codemod's confidence is decremented. Three failures retire it.

This module is where the largest theoretical saving lives and also the largest chance of subtle wrongness, which is exactly why it is gated behind everything above it and why verification is total rather than sampled.

---

## 5. Architecture

### 5.1 Components

```
Claude Code session
  │
  ├─ hooks (hooks/hooks.json, shipped by the plugin)
  │     ├─ SessionStart  ──► meter-boot.py      (command hook, exec form)
  │     ├─ SubagentStart ──► daemon /subagent/start
  │     ├─ PreToolUse    ──► daemon /tool/pre
  │     ├─ PostToolUse   ──► daemon /tool/post
  │     ├─ PostToolBatch ──► daemon /batch
  │     ├─ SubagentStop  ──► daemon /subagent/stop
  │     ├─ PreCompact    ──► meter-hook.py      (command hook)
  │     └─ SessionEnd    ──► meter-hook.py      (command hook)
  │
  └─ meter daemon (127.0.0.1, loopback only, one per machine, refcounted per session)
        ├─ router          request dispatch, 50ms soft deadline per hot-path call
        ├─ ledger    M0    token accounting
        ├─ receipts  M1    schema validation, transcript spooling
        ├─ dossier   M2    symbol index, import graph, context packs
        ├─ clamp     M3    read rewriting, output compression
        ├─ echo      M4    chunk hashing, duplication index, prefix promotion
        ├─ vault     M5    sqlite index + content-addressed patch store
        ├─ governor  M6    cost model, budget enforcement, cohort scheduling
        └─ codemod   M7    cluster detection, transformation synthesis and verification
```

### 5.2 Transport decision

Hot-path hooks (`PreToolUse`, `PostToolUse`) fire on **every tool call in the entire run**, including inside every subagent. A process spawn per call costs 30 to 60ms of interpreter startup, which across a hundred-agent run is minutes of pure overhead and defeats the purpose.

Therefore: **hot-path hooks use `type: "http"` against `http://127.0.0.1:<port>`**, which Claude Code POSTs to directly with no process spawn. Lifecycle hooks (`SessionStart`, `PreCompact`, `SessionEnd`), which fire rarely, use `type: "command"` in exec form.

Consequences to handle:
- Port is fixed by config (`meter.port`, default 47615) because `hooks.json` is static JSON. Expose it via plugin `user_config` so a user with a conflict can change it. On conflict, the daemon fails to bind and stays down; HTTP hooks then fail as non-blocking errors and **the run proceeds unchanged**. That is the fail-open path and it is by construction, not by handling.
- Every request carries a shared secret read from `${CLAUDE_PLUGIN_DATA}/meter/token`, mode 0600, regenerated per boot, so a stray local process cannot drive the hooks. `allowedEnvVars` plus a header is the documented mechanism for passing it.
- The daemon enforces its own deadline. Any hot-path handler that exceeds `meter.deadline_ms` (default 50) returns an empty decision immediately and completes its work asynchronously. Slow is worse than absent.
- Build a `spawn` transport mode as a config option for environments where HTTP hooks are unavailable or disallowed. Same handlers, different front door.

### 5.3 Runtime choice

**Python 3.11+, standard library only.**

Rationale, in order: the repo already ships `scripts/validate.py` on bare Python 3 with no dependencies and runs it in CI, so this adds no new class of dependency; `sqlite3`, `hashlib`, `difflib`, `http.server`, `json`, `ast` and `tokenize` are all stdlib and cover everything needed; and shipping zero third-party packages inside a plugin other people install is the correct posture for something that runs on every tool call.

If Python 3.11+ is absent, `meter-boot.py` cannot run, the daemon never starts, HTTP hooks fail non-blockingly, and yolo-dag behaves exactly as it does today. Detection and a one-line notice happen at `SessionStart`; there is no hard failure.

External binaries are optional accelerators, never requirements: `rg`, `ctags`, `git`, `tree-sitter`. Each has a stdlib fallback and a capability probe at boot, recorded in the run report.

### 5.4 Storage layout

```
${CLAUDE_PLUGIN_DATA}/meter/            # survives plugin updates, removed on uninstall
├── vault.db                             # sqlite, WAL mode
├── artifacts/<aa>/<sha256>.patch        # content-addressed patches
├── codemods/<id>/                       # M7
├── baseline.json                        # cost model, per repo fingerprint + mode
├── token                                # daemon auth secret, 0600
└── port                                 # actual bound port, for diagnostics

<repo>/.dag/runs/<run-id>/meter/        # gitignored, per run, disposable
├── ledger.jsonl
├── report.md
├── echo.json
├── dossiers/<node>.md
├── receipts/<node>.json
├── transcripts/<node>.jsonl
├── tool-output/<id>.txt
└── preamble/<cohort>.md
```

Repo fingerprint: `sha256(git remote origin url + first commit sha)`, falling back to the absolute repo path. Never the path alone, or worktrees fragment the cache.

### 5.5 New plugin file layout

```
yolo-dag/
├── .claude-plugin/plugin.json      MODIFIED  version bump, user_config for port + enable
├── hooks/hooks.json                NEW
├── meter/                         NEW       the package
│   ├── __init__.py  daemon.py  router.py  config.py  store.py
│   ├── ledger.py  receipts.py  dossier.py  clamp.py  echo.py  vault.py  governor.py  codemod.py
│   ├── index/  symbols.py  imports.py  repomap.py  tokens.py
│   ├── compressors/  tests.py  git.py  build.py  grep.py  __init__.py
│   └── schemas/  receipt.v1.json  ledger.v1.json  config.v1.json
├── scripts/
│   ├── meter-boot.py              NEW       SessionStart, starts/refcounts the daemon
│   ├── meter-hook.py              NEW       thin command-hook client
│   ├── dag-meter                   NEW       CLI entrypoint (report, vault, purge, doctor)
│   └── validate.py                 MODIFIED  new invariants
├── commands/
│   ├── dag-cost.md                 NEW
│   └── dag-vault.md                NEW
├── agents/*.md                     MODIFIED  receipt block appended to each template
└── skills/orchestrator/SKILL.md    MODIFIED  prompt ordering, receipt ingestion, vault gate
```

---

## 6. Technical specification

### 6.1 Hook registration

`hooks/hooks.json`. Every path uses `${CLAUDE_PLUGIN_ROOT}` and exec form (`args` present) so no shell re-parses anything.

```json
{
  "description": "meter: token accounting and redundancy elimination for yolo-dag",
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "python3",
        "args": ["${CLAUDE_PLUGIN_ROOT}/scripts/meter-boot.py"],
        "timeout": 20, "statusMessage": "meter: starting" } ] }
    ],
    "SubagentStart": [
      { "hooks": [ { "type": "http", "url": "http://127.0.0.1:47615/subagent/start",
        "timeout": 10, "headers": {"X-Dag-Meter-Token": "$DAG_METER_TOKEN"},
        "allowedEnvVars": ["DAG_METER_TOKEN"] } ] }
    ],
    "PreToolUse": [
      { "matcher": "Read|Grep|Glob", "hooks": [ { "type": "http",
        "url": "http://127.0.0.1:47615/tool/pre", "timeout": 5, "headers": {"X-Dag-Meter-Token": "$DAG_METER_TOKEN"}, "allowedEnvVars": ["DAG_METER_TOKEN"] } ] },
      { "matcher": "Task", "hooks": [ { "type": "http",
        "url": "http://127.0.0.1:47615/tool/pre-task", "timeout": 10, "headers": {"X-Dag-Meter-Token": "$DAG_METER_TOKEN"}, "allowedEnvVars": ["DAG_METER_TOKEN"] } ] }
    ],
    "PostToolUse": [
      { "matcher": "Bash|Grep|Read", "hooks": [ { "type": "http",
        "url": "http://127.0.0.1:47615/tool/post", "timeout": 5, "headers": {"X-Dag-Meter-Token": "$DAG_METER_TOKEN"}, "allowedEnvVars": ["DAG_METER_TOKEN"] } ] }
    ],
    "SubagentStop": [
      { "hooks": [ { "type": "http", "url": "http://127.0.0.1:47615/subagent/stop",
        "timeout": 20, "headers": {"X-Dag-Meter-Token": "$DAG_METER_TOKEN"}, "allowedEnvVars": ["DAG_METER_TOKEN"] } ] }
    ],
    "PreCompact": [
      { "hooks": [ { "type": "command", "command": "python3",
        "args": ["${CLAUDE_PLUGIN_ROOT}/scripts/meter-hook.py", "precompact"], "timeout": 15 } ] }
    ],
    "SessionEnd": [
      { "hooks": [ { "type": "command", "command": "python3",
        "args": ["${CLAUDE_PLUGIN_ROOT}/scripts/meter-hook.py", "sessionend"], "timeout": 10 } ] }
    ]
  }
}
```

`VERIFY` before implementation, in this order:
- **Whether the agents are Task subagents or agent-team teammates.** This decides whether `SubagentStart` and `SubagentStop` fire per node, whether `agent_id` and `agent_type` are present, and whether attribution must fall back to `session_id` plus worktree path. Everything else here assumes subagents.
- That the subagent-spawning tool is named `Task` in the installed version. yolo-dag also uses `SendMessage` to resume specialists; if that is a tool, it needs a matcher too, and it is the highest-value dedup target in the review loop.
- That `updatedToolOutput` is honoured for non-MCP tools in the installed version.
- That `additionalContext` from `SubagentStart` lands before the subagent's first prompt.
- The exact usage field names in the transcript JSONL.

Write these probes as `scripts/dag-doctor.py` and make it the first deliverable. It should print a capability matrix and exit non-zero if a module's prerequisite is missing, so a user can run it before enabling anything.

### 6.2 Hook response contracts

**PreToolUse, rewrite a read:**
```json
{ "hookSpecificOutput": { "hookEventName": "PreToolUse",
  "permissionDecision": "allow",
  "permissionDecisionReason": "meter: bounded to spans known relevant to T-003",
  "updatedInput": { "file_path": "/abs/src/auth.ts", "offset": 120, "limit": 90 } } }
```
`updatedInput` replaces the whole input object; re-emit every field. Note also that rewriting auto-allows the call, bypassing the interactive permission prompt for it. That is documented behaviour and acceptable here because the wrapper only ever narrows a read that was already going to happen, but it must be stated in the plugin README.

**Known hazard.** There is a reported issue where `updatedInput` is ignored when multiple `PreToolUse` hooks match the same event, since matching hooks run in parallel and the last writer wins. Mitigation: exactly one meter `PreToolUse` handler per tool name, no overlapping matchers, and a doctor check that warns when another `PreToolUse` hook from user or project settings matches the same tools. If a conflict is detected, disable M3a for the session and log it.

**PostToolUse, compress a result:**
```json
{ "hookSpecificOutput": { "hookEventName": "PostToolUse",
  "updatedToolOutput": "…digest…\n\n[meter: 38.2KB elided, full output at .dag/runs/…/tool-output/9f3.txt]" } }
```

**SubagentStart, deliver a dossier:**
```json
{ "hookSpecificOutput": { "hookEventName": "SubagentStart",
  "additionalContext": "A precomputed context pack for task T-003 is at .dag/runs/2026-08-21-a3f9/meter/dossiers/T-003.md. It contains the 7 files this task owns with relevant line spans, 12 referenced symbols with signatures, the text of contract C-01, and the receipts of 2 completed sibling tasks that touched the same files. It was built from the repository index by deterministic tooling." } }
```
Factual statements only. No imperatives.

**SubagentStop, reject a malformed receipt:** exit code 2, with the reason on stderr. Exit 2 blocks the stop and the message goes to the subagent. First failure only.

### 6.3 Vault schema

```sql
PRAGMA journal_mode=WAL;

CREATE TABLE entries (
  key            TEXT PRIMARY KEY,      -- semantic hash, Section 4/M5
  repo           TEXT NOT NULL,
  agent_type     TEXT NOT NULL,
  model_tier     TEXT NOT NULL,
  patch_sha      TEXT NOT NULL,         -- artifacts/<aa>/<sha>.patch
  receipt_json   TEXT NOT NULL,
  verify_cmd     TEXT,
  verify_digest  TEXT,
  tokens_spent   INTEGER,
  created_at     INTEGER NOT NULL,
  last_hit_at    INTEGER,
  hit_count      INTEGER DEFAULT 0,
  fail_count     INTEGER DEFAULT 0,
  schema_v        INTEGER NOT NULL
);
CREATE TABLE minhash (key TEXT, band INTEGER, sig TEXT, PRIMARY KEY (band, sig, key));
CREATE INDEX idx_entries_repo ON entries(repo, agent_type);

CREATE TABLE ledger (
  run_id TEXT, node TEXT, agent_id TEXT, agent_type TEXT, phase TEXT,
  model TEXT, in_tok INTEGER, out_tok INTEGER, cache_read INTEGER, cache_write INTEGER,
  wall_ms INTEGER, source TEXT, started_at INTEGER, ended_at INTEGER,
  PRIMARY KEY (run_id, agent_id)
);

CREATE TABLE echo (
  run_id TEXT, chunk_hash TEXT, tokens INTEGER, recipients INTEGER, origin TEXT,
  PRIMARY KEY (run_id, chunk_hash)
);
```

Concurrency: up to 3 workers run in parallel on a full run, and each has hooks firing. WAL mode plus a short busy timeout, single writer thread in the daemon, readers unblocked.

### 6.4 Run and node correlation

The hardest plumbing problem: a hook knows `session_id`, `agent_id`, `agent_type` and `cwd`, but not "this is task T-003 of run 2026-08-21-a3f9". Resolution, in priority order:

1. **Explicit marker.** The orchestrator's Task prompt template gains a single machine-readable line: `DAG-NODE: <run-id>/<node-id>`. The `PreToolUse` on `Task` handler reads it from `tool_input.prompt`, and maps the resulting `agent_id` seen at `SubagentStart` to that node. This is the primary path and it is exact. The line is stripped from nothing and harms nothing; it reads as metadata.
2. **Worktree path.** Workers run in worktrees whose path already encodes the run and task. `cwd` in the hook input gives it.
3. **Fallback.** Unattributed nodes are recorded as `node: null` with their `agent_type`. Aggregate stats stay correct; per-node stats degrade. Never guess an attribution.

`run.json` gains one additive key: `meter: { "version": "...", "modules": {...}, "measured_tokens": ..., "vault_hits": ... }`. Nothing existing is repurposed. `/dag-status` reads it if present and ignores it if absent, so an old run file still opens.

### 6.5 Token estimation

The daemon needs a token count for budgeting dossiers and chunks, offline and fast. Use a calibrated heuristic (characters divided by a per-language constant, calibrated once against measured ledger data) rather than a bundled tokenizer. Accuracy of plus or minus 10 percent is fine for budgeting. Do not call any network endpoint on the hot path for this, ever.

Ground truth for *reporting* always comes from the Ledger, never from the estimator. Keep the two clearly separated in the code, and never print an estimate where a measurement is expected.

### 6.6 Changes to `scripts/validate.py`

The existing validator is the thing that keeps this plugin honest. Extend it:

1. Every hook event name in `hooks/hooks.json` is a real Claude Code event (maintain an allowlist constant with a dated comment).
2. Every command-hook script path exists and is executable.
3. Every agent template that is expected to emit a receipt contains the `dag-receipt` fenced block, and the fields in the template match `schemas/receipt.v1.json`.
4. The receipt schema version in the template matches the one the daemon parses.
5. Existing status-line contracts still present in every agent that had them. This is a regression guard on Invariant 3.2.6.
6. `run.json` keys the new commands read are keys someone writes, extending the existing check to `/dag-cost` and `/dag-vault`.
7. Config keys named in `commands/*.md` and in the README exist in `schemas/config.v1.json`.
8. The port in `hooks/hooks.json` matches the default in `config.v1.json`.

Item 8 matters more than it looks: `hooks.json` is static and cannot read config, so the default port is duplicated in two places by necessity. The validator is what stops those two drifting.

---

## 7. Integration with yolo-dag's six phases

| Phase | Today | With meter |
|---|---|---|
| Brainstorm | Asks run type, resolves ambiguity | Unchanged. No new prompt, ever |
| 1. Route and fan-out | Selects specialists | Unchanged. Governor logs a predicted cost per selected specialist next to the existing routing report |
| 2. Build and review per specialist | Up to 3 rounds, 3 reviewers plus consolidator, specialists resumed via `SendMessage` | **Highest-value target.** Reviewer prompts share a large prefix by construction, so they are the first Echo cohort. Consolidator input becomes receipts rather than prose. Resumed-specialist context is where round-over-round growth lives: measure it in M0 before touching it |
| 3. Merge and reconcile | `spec-reconciler` sees every deliverable | Unchanged in behaviour. The reconciler is the one agent that legitimately needs everything; do not dossier it, do not clamp it |
| 4. Decompose | `task-specialist` writes the graph and contracts | Emits `DAG-NODE` markers. Dossiers for the whole pass are precomputed here, off the model path, while nothing is waiting |
| 5. Execute, verify, merge | Worker per task in a worktree, reviewer, up to 2 reworks, merge on approval | Dossiers to workers and reviewers. Clamp on test and git output. Vault consulted before spawning a worker, never before a reviewer. Reworks are the primary Vault warm-start case |
| 6. Verify and hand off | Suite plus acceptance review of the whole diff | Unchanged. The acceptance reviewer compares the whole diff against the request and must not be given a reduced view. Clamp the suite output only |

**Speculative prefetch.** Dossier construction is deterministic and uses no model tokens, so build the next pass's dossiers while the current pass is running. It is free and it removes the only latency this wrapper adds to the critical path.

---

## 8. Operations

### 8.1 Install

No change for the user. `/plugin install yolo-dag@yolo-dag` ships the wrapper. First `SessionStart` after install:
1. Probes capabilities (`python3`, `git`, `rg`, `ctags`, port availability).
2. Starts the daemon, writes the port and token files.
3. Prints one line: `meter: measuring (M0). /dag-cost after a run.`
4. Does nothing else.

After changing `hooks/hooks.json`, `/reload-plugins` or a restart is required; hooks are not hot-reloaded the way SKILL.md is. Say so in the README and in the release notes.

### 8.2 Configuration

Resolution order: package defaults, then `<repo>/.dag/meter.json`, then plugin `user_config`, then `DAG_METER_*` environment variables.

```json
{
  "enabled": true,
  "port": 47615,
  "transport": "http",
  "deadline_ms": 50,
  "modules": {
    "ledger":   { "enabled": true },
    "receipts": { "enabled": true,  "repair_turns": 1 },
    "dossier":  { "enabled": true,  "max_tokens": 4000, "max_denials": 2,
                  "apply_to": ["task-worker", "task-reviewer"] },
    "clamp":    { "enabled": true,  "full_read_threshold": 400, "output_threshold_bytes": 8000 },
    "echo":     { "enabled": true,  "mode": "measure", "promote_threshold": 4 },
    "vault":    { "mode": "shadow", "warm_threshold": 0.85, "max_gb": 2 },
    "governor": { "enabled": true,  "mode": "advise", "runaway_multiplier": 3.0,
                  "write_weights": false },
    "codemod":  { "enabled": false, "scope": "repo" }
  }
}
```

Kill switch: `DAG_METER_DISABLE=1`, or `"enabled": false`, or `/dag-clean --meter`. Any of them and the plugin is byte-for-byte its current self. Claude Code's own `disableAllHooks` also works and should be documented as the nuclear option.

### 8.3 New commands

**`/dag-cost [run-id] [--compare <run-id>]`** prints the measured report:

```
Run 2026-08-21-a3f9  mode=full  run_type=full  14m 22s
Measured: 1,284,430 in / 96,210 out   cache reads 41%   (spawn-unit estimate: 118/120)

By phase            in        out     agents
  2 review loops   742,110   51,880      64
  5 execution      388,240   33,410      22
  ...
Top 5 nodes by spend
  spec-reviewer/architecture/r2   61,220   ...
Echo: 214,880 echoed tokens across 37 chunks (17% of input)
  top chunk: merged-spec §3 "Data model", 1,840 tok x 41 agents
Vault: 6 hits, 3 warm starts, 0 failed replays. Est. avoided: 71,400 tok
Clamp: 2.1MB of tool output compressed to 84KB
Dossier: 18/22 nodes used, 3 denials, 1 escalation
```

**`/dag-vault [stats|purge|prune]`** manages the cache, shows hit rates, and purges. Purge must be trivially discoverable, because the vault stores diffs of the user's code.

### 8.4 Observability and debugging

- `scripts/dag-meter doctor`: capability matrix, hook conflict detection, daemon health, a synthetic hook round-trip.
- `DAG_METER_LOG=debug` writes to `.dag/meter.log`. Never to stdout; a hook that prints stray text on stdout breaks JSON parsing, which is a documented and common failure.
- Every decision the wrapper makes (a rewritten read, a compressed output, a denial, a vault hit) is logged with its input hash, so any behaviour can be reproduced from the log.
- The report always shows what the wrapper *did not* do: modules disabled, capabilities missing, deadlines exceeded.

### 8.5 Optional launcher

`scripts/dag-run` sets `CLAUDE_CODE_ENABLE_TELEMETRY` and OTEL exporter variables to a local collector inside the daemon, then execs `claude` with the user's arguments. This upgrades the Ledger from transcript parsing to first-party telemetry. Strictly optional, documented as such, and the plugin must never require it.

### 8.6 Security and privacy

- Daemon binds `127.0.0.1` only. No outbound network calls from any module except M7's single synthesis call, which goes through Claude Code itself, not through the daemon.
- Auth token file at 0600, regenerated each boot.
- The Vault stores patches of the user's source. Document this plainly in the README, keep it inside `${CLAUDE_PLUGIN_DATA}` (removed on uninstall), never sync it anywhere, and make purge one command.
- Dossiers and spooled transcripts live under `.dag/`, which is already gitignored. Verify the gitignore covers the new subdirectories.
- Secret hygiene: the dossier builder must respect `.gitignore` and skip `.env`-shaped files, keys, and anything matching a secret pattern list. A context pack that helpfully includes credentials is a serious bug, not a paper cut.

### 8.7 Cache TTL: the cheapest lever in this document

Claude Code exposes the prompt cache TTL directly, in two buckets. The main conversation is controlled by the `promptCacheTtl` setting or the `CLAUDE_CODE_PROMPT_CACHE_TTL` environment variable. Everything else, subagents included, is controlled by `subagentPromptCacheTtl` or `CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL`. Each accepts `5m` or `1h`, anything else is ignored, and both require Claude Code v2.1.242 or later. On a subscription plan, once usage draws on credits, Claude Code drops the main conversation to the cheaper 5-minute TTL unless a TTL is chosen explicitly.

This matters to yolo-dag specifically. A specialist resumed for review round 2 has been idle while three reviewers and a consolidator ran. If that gap exceeds five minutes, its entire accumulated history is re-billed at the full input rate instead of roughly a tenth of it. The same applies to any prefix shared across a cohort of sibling agents whose dispatch spans more than five minutes.

The arithmetic is not free, which is why this is an experiment rather than a default. A 1-hour cache write costs roughly 2x base input against 1.25x for the 5-minute tier, so a prefix written and never re-read is a 100 percent surcharge instead of a 25 percent one. Break-even is one reuse on the 5-minute tier and two on the 1-hour tier. Whether `subagentPromptCacheTtl: 1h` wins therefore depends entirely on the reuse distribution, which is exactly what M0 measures in its round-over-round output.

Implementation: **the wrapper does not set these.** It reads them, reports them in `/dag-cost`, and after M0 has data it prints a recommendation with the measured arithmetic behind it. A settings key that changes the user's billing is the user's decision, and the optional launcher (8.5) is where someone who wants it automated can opt in. Add a `dag-doctor` check that reports the current values and the Claude Code version.

---

## 9. Implementation plan

Six milestones. Each is independently mergeable and independently revertible. **M0 merges and runs on real work before M1 starts.**

### M0. Instrument (no behaviour change)
- `scripts/dag-doctor.py` with the full capability and `VERIFY` probe matrix.
- Daemon skeleton, HTTP transport, auth, boot and shutdown, refcounting.
- `SubagentStart` and `SubagentStop` attribution, transcript parsing, ledger.
- `DAG-NODE` marker in the orchestrator's Task template plus correlation.
- `/dag-cost` reading the ledger.
- Exit criteria: three real runs (one per mode) produce a ledger whose total is within 5 percent of the account's reported usage for the same window, and the run is otherwise indistinguishable from an unwrapped run.

### M1. Cheap wins
- Receipts: schema, agent template changes, `SubagentStop` validation, transcript spooling, orchestrator ingestion.
- Clamp 3b: output compressors with golden-file tests.
- Echo Index in measure-only mode.
- Exit criteria: measured reduction in orchestrator context growth per node, no change to review pass rates, no receipt deadlocks across 20 nodes.

### M2. Context packs
- Symbol index, import graph, dossier builder, `SubagentStart` delivery.
- Clamp 3a bounded reads with hazard detection for conflicting `PreToolUse` hooks.
- Denial policy with the escape hatch and the denial-rate metric.
- Exit criteria: dossier usage above 70 percent, escalation rate below 15 percent, no increase in rework counts. **Rework count is the quality guard for this milestone**: if starving agents of exploration makes them worse, reworks rise, and reworks cost more than exploration.

### M3. Prefix economics
- Echo promotion, cohort preamble files, prompt template reordering (stable, then cohort, then task tail).
- Governor cohort-contiguous scheduling as a tie-breaker under topological order.
- Exit criteria: cache-read ratio measurably higher in the ledger, dependency order provably unchanged (a property test over the scheduler).

### M4. The Vault
- Key normalisation with an exhaustive test table, SQLite store, patch storage.
- **Shadow mode first**: compute keys, record candidate replays, let the worker run, diff the two.
- Exact-hit replay gated on apply plus verification, reviewer path untouched. Ships behind `rework_only`.
- MinHash near-hit warm start.
- `/dag-vault`, including a shadow-agreement report.
- Exit criteria: **shadow-mode agreement over 50 real runs with zero would-be-wrong replays.** The adversarial test set stays as a unit test, not as the release gate, because it can only contain failures someone already imagined.

### M5. Governor: recalibration and containment
- Per `(agent_type, mode)` cost coefficients written to `weights.json` from measured baselines, normalised so the existing 120 / 40 / 8 ceilings keep their current meaning.
- Orchestrator reads `weights.json` when present, falls back to the hardcoded tier weights when absent.
- Runaway containment: one warning at 2x prediction, tool denial at 3x, reported through the existing degradation channel.
- Model-routing report and cache-TTL recommendation, both advisory.
- Exit criteria: the existing budget ceiling, ladder and prompts are byte-for-byte unchanged in behaviour; no run terminated incorrectly; every containment event explainable from the log.

### M6. Codemod promotion (experimental, optional)
- Cluster detection, synthesis, total verification, retirement policy.
- Exit criteria: no codemod ever applied that was not verified against every cluster member.

### 9.1 The plan as a yolo-dag task graph

Feed this to the plugin itself. The shape matches `tasks.json`.

```json
{
  "tasks": [
    {"id":"T-001","title":"dag-doctor capability and VERIFY probes",
     "acceptance_criteria":["FIRST determines whether agents are Task subagents or agent-team teammates, and which identity fields hook payloads actually carry","prints a capability matrix","exits non-zero on a missing prerequisite","probes updatedInput, updatedToolOutput, SubagentStart additionalContext, transcript usage fields, Task and SendMessage tool names","reports promptCacheTtl, subagentPromptCacheTtl and the Claude Code version"],
     "owns":["scripts/dag-doctor.py"],"contracts":["C-CFG"],"depends_on":[]},

    {"id":"T-002","title":"daemon skeleton, HTTP transport, auth, lifecycle",
     "acceptance_criteria":["binds loopback only","token file 0600 regenerated per boot","50ms soft deadline returns empty decision","refuses to start twice","fail-open verified by killing the daemon mid-run"],
     "owns":["meter/daemon.py","meter/router.py","meter/config.py","scripts/meter-boot.py","scripts/meter-hook.py"],"contracts":["C-CFG","C-HOOK"],"depends_on":[{"id":"T-001","reason":"transport choice depends on the probe result"}]},

    {"id":"T-003","title":"hooks.json registration and validator invariants",
     "acceptance_criteria":["all events real","exec form with CLAUDE_PLUGIN_ROOT","port matches config default","validate.py fails on drift"],
     "owns":["hooks/hooks.json","scripts/validate.py"],"contracts":["C-HOOK"],"depends_on":[]},

    {"id":"T-004","title":"node correlation and DAG-NODE marker",
     "acceptance_criteria":["agent_id maps to run/node for >95% of spawns","worktree fallback works","unattributed nodes recorded as null, never guessed"],
     "owns":["meter/ledger.py","skills/orchestrator/SKILL.md"],"contracts":["C-NODE"],"depends_on":[{"id":"T-002","reason":"needs the daemon's event stream"}]},

    {"id":"T-005","title":"ledger, store, and /dag-cost",
     "acceptance_criteria":["ledger.jsonl crash-safe","totals within 5% of reported usage","report renders with nulls present","records per-round input, cache-read and idle-gap for every resumed specialist"],
     "owns":["meter/store.py","commands/dag-cost.md","meter/schemas/ledger.v1.json"],"contracts":["C-NODE"],"depends_on":[{"id":"T-004","reason":"needs attribution"}]},

    {"id":"T-006","title":"receipts: schema, templates, validation, spooling",
     "acceptance_criteria":["one repair turn maximum","existing status lines untouched","malformed receipt never deadlocks a node","validator checks both ends"],
     "owns":["meter/receipts.py","meter/schemas/receipt.v1.json","agents/"],"contracts":["C-RECEIPT"],"depends_on":[{"id":"T-005","reason":"receipts are measured against the M0 baseline"}]},

    {"id":"T-007","title":"clamp: output compressors",
     "acceptance_criteria":["golden-file tests per compressor","never removes an error message","full output always written to disk and referenced","passes through when uncertain"],
     "owns":["meter/clamp.py","meter/compressors/"],"contracts":["C-HOOK"],"depends_on":[{"id":"T-002","reason":"needs PostToolUse plumbing"}]},

    {"id":"T-008","title":"symbol index, import graph, repo map",
     "acceptance_criteria":["works with rg and ctags absent","respects gitignore","never indexes secret-shaped files","incremental on git diff"],
     "owns":["meter/index/"],"contracts":["C-DOSSIER"],"depends_on":[]},

    {"id":"T-009","title":"dossier builder and SubagentStart delivery",
     "acceptance_criteria":["under max_tokens","factual phrasing, no imperatives","written to file, pointer injected","prefetched for the next pass"],
     "owns":["meter/dossier.py"],"contracts":["C-DOSSIER"],"depends_on":[{"id":"T-008","reason":"needs the index"},{"id":"T-006","reason":"sibling receipts are dossier input"}]},

    {"id":"T-010","title":"clamp: bounded reads with conflict detection",
     "acceptance_criteria":["updatedInput re-emits every field","never narrows a read of an owned file","disables itself on a detected PreToolUse conflict","denial cap honoured"],
     "owns":["meter/clamp.py"],"contracts":["C-DOSSIER","C-HOOK"],"depends_on":[{"id":"T-009","reason":"needs span knowledge"}]},

    {"id":"T-011","title":"echo index, measure mode",
     "acceptance_criteria":["chunk normalisation is a pure tested function","echoed-token metric in the report","no behaviour change"],
     "owns":["meter/echo.py"],"contracts":["C-NODE"],"depends_on":[{"id":"T-005","reason":"shares the ledger store"}]},

    {"id":"T-012","title":"prefix promotion and cohort scheduling",
     "acceptance_criteria":["preamble byte-identical across a cohort","stable-first ordering in the Task template","property test: topological order never violated","cache-read ratio improves"],
     "owns":["meter/governor.py","skills/orchestrator/SKILL.md"],"contracts":["C-PREFIX"],"depends_on":[{"id":"T-011","reason":"promotion needs the duplication data"}]},

    {"id":"T-013","title":"vault: key normalisation, store, and shadow mode",
     "acceptance_criteria":["exhaustive normalisation test table","adversarial near-miss set produces zero collisions","LRU eviction under max_gb","shadow mode records candidate replays and diffs them against real worker output without altering the run"],
     "owns":["meter/vault.py"],"contracts":["C-VAULT"],"depends_on":[{"id":"T-009","reason":"input spans come from the dossier"}]},

    {"id":"T-014","title":"vault: verified replay and warm start",
     "acceptance_criteria":["mode enum off|shadow|rework_only|full honoured, default shadow","replay gated on 3-way apply AND verification","reviewer always runs","failed replay reverts cleanly and falls through","warm start delivered as dossier context","/dag-vault reports shadow agreement rate"],
     "owns":["meter/vault.py","commands/dag-vault.md"],"contracts":["C-VAULT"],"depends_on":[{"id":"T-013","reason":"needs the store"}]},

    {"id":"T-015","title":"governor: weight recalibration, prediction, containment",
     "acceptance_criteria":["writes per agent_type coefficients to weights.json, normalised against the existing 120/40/8 ceilings","orchestrator falls back to hardcoded tier weights when weights.json is absent","existing ceiling, ladder and per-pass prompt unchanged in behaviour","prediction shown as information only, never as a gate","containment at 3x, one warning at 2x","every containment explainable from the log","degradation reported through the existing channel"],
     "owns":["meter/governor.py"],"contracts":["C-BUDGET"],"depends_on":[{"id":"T-005","reason":"needs baselines"}]},

    {"id":"T-016","title":"docs, README, release notes, purge UX",
     "acceptance_criteria":["vault contents and purge documented","reload-plugins requirement stated","kill switch documented three ways","auto-allow consequence of updatedInput stated"],
     "owns":["README.md"],"contracts":[],"depends_on":[{"id":"T-014","reason":"documents the vault"}]}
  ],
  "contracts": {
    "C-CFG": "config schema, resolution order, env var names",
    "C-HOOK": "hook event names, JSON request and response shapes, deadlines, fail-open semantics",
    "C-NODE": "run/node/agent correlation record and ledger row shape",
    "C-RECEIPT": "receipt v1 JSON schema and the fenced-block delimiter",
    "C-DOSSIER": "dossier file format, span record, token budget accounting",
    "C-PREFIX": "preamble file format and the exact Task prompt ordering",
    "C-VAULT": "vault key derivation, entry record, replay verification protocol",
    "C-BUDGET": "prediction record, containment thresholds, degradation reporting"
  }
}
```

---

## 10. Measurement

### 10.1 The A/B harness

Without this, none of the above is more than a story. Build `evals/meter/` alongside the existing eval suite:

- A fixed set of 5 request fixtures per mode against a pinned repo state, on a pinned plugin commit.
- Each fixture runs twice: `DAG_METER_DISABLE=1` and enabled. Same seed, same mode, same tasks-per-pass.
- Compare: total input and output tokens, cache-read ratio, wall time, review pass rate, rework count, acceptance-review findings count, and a diff of the final branch.
- The quality bar is the important half: **a token saving that raises rework counts is a loss, not a win**, because a rework costs more than any of these mechanisms saves.

### 10.2 Success criteria

| Milestone | Primary metric | Bar |
|---|---|---|
| M0 | Ledger accuracy against reported usage | within 5 percent |
| M1 | Orchestrator input tokens per completed node | measurably lower, rework count unchanged |
| M2 | Worker input tokens per task | measurably lower, rework count unchanged |
| M3 | Cache-read ratio across the run | measurably higher, DAG order unchanged |
| M4 | Vault hit rate inside rework loops | non-zero, with zero incorrect replays |
| M5 | Runs that overrun their budget | fewer, with no incorrect termination |

Deliberately no percentage targets before M0 exists. Setting a numeric goal against an unmeasured baseline is how you end up optimising the metric instead of the cost.

### 10.3 Order-of-magnitude reasoning (hypothesis only)

Purely to size the opportunity, not as a promise, and to be replaced by M0's output:

Let `A` be agents per run, `S` the shared bytes pasted per agent, `E` exploration tokens per repo-touching agent, `T` prose tokens returned per agent, and `Q` the orchestrator's re-send multiplier.

- Today, roughly: `input ≈ A·(S + E) + Q·Σ T`.
- R1 attack: the `A·S` term moves from `1.0x` billing to approximately `1.25x` once plus `0.1x` per reuse.
- R2 attack: `E` shrinks toward the dossier budget, and exploration turns disappear rather than just shrinking.
- R3 attack: `T` becomes a bounded receipt, so `Q·Σ T` stops compounding.
- R4 attack: the rework and retry multiplier on the whole expression drops toward 1.

With roughly 100 agents, the `A·S` term alone is plausibly a third of run input, and it is the term with the cleanest fix. That is why M3 (prefix economics) is scheduled before M4 (the Vault), even though the Vault is the more interesting idea.

---

## 11. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Agents turn out to be teammates, not subagents, and hook payloads carry no per-teammate identity | High | Probe zero in `dag-doctor` before any code; fallback attribution via `session_id` plus worktree path; `node: null` rather than a guess |
| Hook API changes under us | High | `meter-doctor` probe matrix, per-module prerequisites, fail-open by default, allowlist in the validator with a dated comment |
| `updatedInput` ignored with multiple `PreToolUse` hooks (reported issue) | High | One handler per tool, conflict detection at boot, auto-disable M3a on conflict |
| Dossier starves an agent, reworks rise | High | Denial cap of 2 and never again; rework count is the explicit exit gate for M2; instant revert path |
| A compressor eats an error message | High | Pass-through on uncertainty, full output always on disk, golden-file tests, never compress a non-zero exit under the threshold |
| Vault replays a patch that is subtly wrong | High | Verification gate plus the reviewer never being skipped; adversarial near-miss test set; conservative normalisation |
| Hook latency slows every tool call | Medium | HTTP transport with no process spawn, 50ms deadline returning an empty decision, async completion, latency in the report |
| Port conflict | Medium | Configurable, doctor check, fail-open when unbound |
| Vault grows unbounded on disk | Medium | `max_gb` with LRU eviction, `/dag-vault prune`, purge on uninstall via `CLAUDE_PLUGIN_DATA` |
| `additionalContext` tripping prompt-injection defences | Medium | Factual phrasing only, reviewed as a checklist item in T-009 |
| Complexity outgrows the plugin | Medium | Seven independently disableable modules, a single kill switch, and a standing rule that any module failing its exit criteria is deleted rather than tuned |
| Measurement itself costs tokens | Low | Zero model calls in M0 through M6. M7 makes exactly one, amortised over a cluster |

---

## 12. Decisions on the open questions

These five were open at draft. All are now closed. Each entry records the decision, the reasoning, and what it changed elsewhere in this document, so the implementing agent does not relitigate them.

### 12.1 Vault default: `shadow`, with a four-state mode

A boolean was the wrong shape. An adversarial test set can only contain near-misses someone already imagined, and the failure mode of a semantic cache is the near-miss nobody imagined. Shadow mode produces precision and recall from live work at zero risk, which is strictly better evidence than any synthetic suite.

Modes are `off`, `shadow`, `rework_only`, `full`. Default `shadow`. Promotion is manual and staged: 50 clean runs to reach `rework_only`, 50 more to reach `full`. `rework_only` comes before `full` because same-run rework has both the highest hit rate and the smallest blast radius.

Changed: M5 in Section 4, the config block in 8.2, milestone M4, tasks T-013 and T-014.

### 12.2 `SendMessage` and resumed specialists: earlier, but as measurement

Three findings settled this.

`SendMessage` is a real tool and `PreToolUse` can match it. But the lever available there is `updatedInput` only: `additionalContext` from `PreToolUse` and `SubagentStop` is injected into the **team lead's** context, and `SubagentStart` is the one event that injects directly into the sub-agent. So a hook on `SendMessage` can rewrite an outbound message and nothing more.

The cost of a resumed specialist is not the message, it is the re-sent history, and that is not a hook problem. It is a cache TTL problem with a documented settings fix (Section 8.7).

Outranking both: it is not yet established whether yolo-dag's agents are Task subagents or agent-team teammates. `SendMessage` is the agent-teams mailbox tool, which suggests teammates, and teammates are separate sessions whose hook payloads have been reported not to identify which teammate fired them. That determines the entire attribution design.

Decision: this moves earlier by being folded into M0 as measurement rather than becoming its own milestone. Probe zero answers the subagent-versus-teammate question before any code. The M0 ledger records per-round input, cache-read and idle-gap for every resumed specialist. The fix, if the data supports it, is mostly M1 (receipts shrink what accumulates) plus a settings recommendation, not new machinery.

Changed: M0 in Section 4, Section 6.1's VERIFY list, new Section 8.7, tasks T-001 and T-005, the risks table.

### 12.3 Cross-repo vault: patches stay repo-scoped, codemods are global-eligible and opt-in

Scope is a hit-rate question, not a safety question. Every codemod application already runs the node's own acceptance command before a reviewer sees it, so a bad global codemod fails exactly the same gate as a bad local one. Widening scope widens hit rate without widening blast radius.

The real objection is disclosure. In a plugin other people install, a global store means a transformation derived from one client's repo can surface in another's. So: `codemod.scope` defaults to `repo`; `global` is explicit opt-in and requires a literal-scrub check that rejects any transformation carrying identifiers, paths or string literals from the source repo beyond the pattern itself. Patches are always repo-scoped, because a diff is meaningless elsewhere.

Changed: M7 in Section 4, the config block in 8.2.

### 12.4 Budget: advise, and recalibrate rather than replace

The spawn-unit system is not wrong in shape. Its degradation ladder is a quality policy expressed in cost terms, and those are correctness decisions the orchestrator should keep owning. What is broken is the input: the tier weights are guesses, and they are per tier when the real variance is per agent type.

So the Governor writes measured `(agent_type, mode)` coefficients back as `weights.json`, normalised so the existing 120 / 40 / 8 ceilings keep their current meaning. The ceiling, the ladder, the reporting and the per-pass prompt are untouched and simply become accurate. Predictions stay advisory.

One exception stands: single-node runaway containment at 3x prediction. That is a safety valve, not budget policy, and it reports through the existing degradation channel.

Changed: M6a in Section 4, milestone M5, task T-015, the config block in 8.2.

### 12.5 Naming: drop the separate brand, call it `meter`

A wrapper that ships inside one plugin and is not separately installable does not need its own brand, and a second brand inside one repo makes every doc worse. The subsystem is `meter`, the package is `meter/`, the config key is `meter`, run state lives at `.dag/runs/<id>/meter/`, environment variables are `DAG_METER_*`, the receipt fence is `dag-receipt`, and the commands stay `/dag-cost` and `/dag-vault`. The M0 module is renamed `Ledger` to free the word.

Vault, Dossier, Echo Index, Clamp and Governor stay as internal module names. They are precise and they do not leak into user-facing surfaces.

If a standalone release for other plugins ever makes sense, give it a brand then. Renaming now is a substitution; renaming after M0 is a substitution across a receipt schema that other people's agent templates depend on.

Changed: every path, key and identifier in this document.

---

## Appendix A. Claude Code facts this design depends on

Verified against the Claude Code hooks reference and plugins reference as of 2026-09-16. Re-verify with `meter-doctor` before implementation.

- Plugins ship hooks in `hooks/hooks.json` at the plugin root. Only `plugin.json` lives in `.claude-plugin/`. Component directories inside `.claude-plugin/` load silently as nothing.
- `${CLAUDE_PLUGIN_ROOT}` is the install directory, `${CLAUDE_PLUGIN_DATA}` is persistent plugin storage that survives updates and is deleted when the plugin is uninstalled from its last scope.
- Hook handler types: `command`, `http`, `mcp_tool`, `prompt`, `agent`. Exec form (with `args`) spawns directly with no shell.
- Hooks from plugins also run inside subagents, with `agent_id` and `agent_type` in the input.
- `PreToolUse` supports `hookSpecificOutput.updatedInput`, replacing the entire tool input and auto-allowing the call.
- `PostToolUse` supports `hookSpecificOutput.updatedToolOutput`, replacing the tool result before Claude sees it (all tools, since v2.1.121).
- `SubagentStart` and `SessionStart` `additionalContext` is inserted at the start of the conversation, before the first prompt.
- `SubagentStop` input includes `agent_transcript_path` and `last_assistant_message`; exit 2 prevents the subagent from stopping and shows stderr to it.
- Hook output strings, including `additionalContext`, are capped at 10,000 characters; longer output is written to a file and replaced with a path and preview.
- Stray stdout from a shell profile breaks hook JSON parsing.
- Hook changes require `/reload-plugins` or a restart; SKILL.md changes take effect immediately.
- Prompt caching: cache reads bill at roughly 0.1x base input, 5-minute writes at 1.25x, 1-hour writes at about 2x; break-even is a single reuse on the 5-minute tier and two on the 1-hour tier; matching is a byte-for-byte prefix match and every read refreshes the TTL. Minimum cacheable prefix length varies by model. Confirm current numbers on Anthropic's pricing page before quoting them in the README.
- Claude Code exposes the TTL in two buckets: `promptCacheTtl` or `CLAUDE_CODE_PROMPT_CACHE_TTL` for the main conversation, `subagentPromptCacheTtl` or `CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL` for everything else. Each takes `5m` or `1h` and ignores other values. Both require v2.1.242 or later. On a subscription plan drawing on usage credits, Claude Code drops the main conversation to 5m unless a TTL is set explicitly. `DISABLE_PROMPT_CACHING` and `FORCE_PROMPT_CACHING_5M` also exist.
- `SendMessage` is the agent-teams mailbox tool and is matchable in tool events. Teammates are separate Claude Code sessions that inherit the lead's hooks; a reported gap is that the `PreToolUse` payload carries no teammate identity. `additionalContext` from `PreToolUse` and `SubagentStop` lands in the lead's context; `SubagentStart` is the exception that injects into the sub-agent.

## Appendix B. Reference

- Claude Code hooks reference: https://code.claude.com/docs/en/hooks
- Claude Code plugins reference: https://code.claude.com/docs/en/plugins-reference
- yolo-dag: https://github.com/febradc-github/yolo-dag
- Known issue, `updatedInput` with multiple `PreToolUse` hooks: https://github.com/anthropics/claude-code/issues/15897
