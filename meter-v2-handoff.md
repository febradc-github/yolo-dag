# METER v2: aggressive token reduction

**Status:** design handoff, ready for implementation
**Target repo:** `febradc-github/yolo-dag`
**Depends on:** METER v1.1 (`meter-handoff.md`), implemented
**Version:** 2.0
**Date:** 2026-09-17

---

## 0. How to read this document

v1 built the instrument: daemon, hooks, ledger, receipts, dossiers, clamp, echo index, vault, governor. Its job was to make spend visible and to remove obvious duplication.

v2 spends that instrument. Eight new modules, `M8` through `M15`, continuing v1's numbering so they nest under the same `meter.modules.*` config namespace. The target is a composite multiplier around 0.40 on a full run, meaning a run that costs 700 costs closer to 300.

Three rules carry over from v1 and one is new.

1. **Fail-open everywhere.** Unchanged.
2. **Measure before you cut.** Unchanged. Every module here declares what the ledger must show before it is allowed to ship.
3. **Do not break the invariants in v1 Section 3.2.** Unchanged, and M8 gets special scrutiny because it is the only module here that touches how many opinions a review gets.
4. **New: ship by risk class, not by size.** Section 2 defines three classes. The largest saving in this document is in the riskiest class and ships last, after the safe modules have earned the measurement confidence to evaluate it.

---

## 1. Where the remaining tokens are

v1 attacked duplication across agents. What is left is duplication *inside* each agent, plus work that never needed a model at all.

Assumed mix for a task-worker's input, stated as a hypothesis to be replaced by the ledger's actual breakdown:

| Class | Share | Why it survives v1 |
|---|---|---|
| Exploration and file content | 40% | The dossier made it cheaper to find, not smaller to carry. An agent still receives context it never reads |
| Pasted spec and contracts | 25% | The echo index moved it into a cached prefix. It is still transmitted in full, and is still mostly prose written for humans |
| System, tools, conventions | 15% | Effectively fixed |
| Instructions and templates | 10% | Effectively fixed |
| Tool results | 10% | Clamped already |

Review is roughly 55 percent of total run spend, driven by a quorum of three critics per round plus a consolidator, up to three rounds. That single structural constant is the largest remaining lever in the pipeline and no amount of per-prompt compression touches it.

Four observations shape everything below.

**Nobody optimises output.** Output bills at roughly 5x input. A worker that rewrites a whole file instead of editing it pays the file twice, once in and once out, and the second time at five times the rate.

**Pushing context assumes the sender can predict the receiver's needs.** It cannot. Every token of a 4,000-token dossier is billed whether or not it is read.

**Most review findings are decidable without a model.** Type errors, lint violations, dead code, missing tests, contract violations and a large share of convention breaches are all computable. They are currently being found by the most expensive reviewer available.

**The prompt cache is a place to put a compression dictionary.** Anything written once into a cached prefix and referenced thereafter costs roughly a tenth to reference. This is underused: v1 put *content* in the prefix, and the better use is putting a *codebook* there.

---

## 2. Risk classes

Every module is classified, and the class determines both the release it lands in and the evidence required to ship it.

| Class | Definition | Evidence required |
|---|---|---|
| **Lossless** | The agent provably receives the same information, encoded more compactly | A round-trip test: decode equals original. No quality measurement needed |
| **Verified** | The agent receives less information, but a deterministic gate proves the reduction was sufficient | The gate itself, plus a failure path that restores the full form |
| **Statistical** | Quality is traded for cost, and the trade is only knowable by sampling | A recall guard: periodically run the expensive path and measure what the cheap path missed |

**Never ship two statistical-class modules in the same release.** When quality moves, you need to know which one moved it.

| Module | Name | Class | Release |
|---|---|---|---|
| M9 | Intern | Lossless | R1 |
| M13 | Throttle | Lossless | R1 |
| M15 | Oracle | Lossless | R1 |
| M11 | Sieve | Verified | R2 |
| M12 | Distill | Verified | R2 |
| M14 | Attribution | Lossless (measurement only) | R3 |
| M10 | Pull | Verified | R3 |
| M8 | Quorum | Statistical | R4 |

---

## 3. Modules

### M9. Intern (path and symbol interning)

**Class: lossless. Default: enabled. Release R1.**

`src/modules/auth/services/AuthService.ts` costs roughly 12 tokens and appears dozens of times in a single agent's context and output. The dossier already knows every file, symbol, contract and acceptance criterion a node touches, which means a codebook can be built deterministically before the agent starts.

**Mechanism.** The codebook goes into the cached prefix, where it is written once and read at roughly a tenth of the rate thereafter. Every reference after that is 2 tokens instead of 12 to 15.

```
FILES   f1 src/modules/auth/services/AuthService.ts
        f2 src/modules/auth/guards/SessionGuard.ts
SYMBOLS s1 AuthService.refresh (f1:120-168)
        s2 SessionGuard.validate (f2:44-71)
CONTRACTS c1 C-AUTH   AC a1 AC-02
```

References elsewhere become `f1:120-168`, `s1`, `c1`, `a1`.

**Where it applies.** Dossiers, task prompts, receipts, review findings, contract text, and the Vault's stored input-span lists. Not tool results, which are clamped by v1 and arrive in raw form from outside.

**De-interning is mandatory at every human boundary.** `/dag-cost`, `report.md`, the merged spec, commit messages and anything a person reads gets expanded back. Interning is a wire format, never a storage format for human-facing artifacts. The daemon owns both directions and the codebook is stored per node so a later run can decode an old receipt.

**The real risk is comprehension, not correctness.** Opaque identifiers can degrade a model's reasoning about code it has never seen. Two mitigations, in order:

1. Semi-mnemonic codes: `f1` in the table, but references written as `f1` only after the table has appeared in the same prompt, never across a message boundary where the table might be absent.
2. If the A/B shows any quality regression, fall back to **basename compression**, which is guaranteed safe: strip the common directory prefix shared by all files in the node and state it once. That captures roughly 60 percent of the saving with no comprehension risk at all.

**Exit criteria.** Round-trip test passes on 100 percent of generated codebooks. No measurable change in rework count or review findings across the A/B harness. If the second condition fails, ship basename compression instead and close the module.

---

### M13. Throttle (output-side reduction)

**Class: lossless. Default: enabled. Release R1.**

Output bills at roughly 5x input and has received no attention. Three enforcements.

**13a. Deny `Write` on an existing file.** A full-file rewrite pays the entire file in output tokens; an `Edit` pays the hunk. `PreToolUse` on `Write` where the path exists and is tracked by git returns a deny with a reason naming `Edit`. Exceptions, all checked deterministically: the file is new, the file is generated (matches a generated-file pattern list), the diff would exceed `throttle.rewrite_ratio` (default 0.6) of the file, or the agent has already been denied twice for this path. The last exception matters: an agent that genuinely needs a rewrite must be able to get one.

**13b. Narration accounting.** Log the ratio of prose output to artifact output per agent type, per node. This is not enforced, it is reported, because a hook cannot tell a useful explanation from padding. What it can do is make the number visible per agent template, which turns template tuning into an evidence-based activity instead of a taste-based one.

**13c. Effort routing.** `VERIFY` whether the installed Claude Code exposes per-node reasoning effort to a hook, via the `effort` field or the `$CLAUDE_EFFORT` environment variable. If it does, mechanical nodes (classified deterministically: pure rename, codemod-shaped change, single-file edit under N lines, no new symbols) get routed down. If it does not, close 13c and note it.

**Exit criteria.** Rewrite denials do not increase rework count. Output tokens per completed node measurably lower. No agent blocked from a genuinely needed rewrite, verified by auditing every denial in the first 20 runs.

---

### M15. Oracle (within-run question cache)

**Class: lossless. Default: enabled. Release R1.**

Agents ask the codebase the same questions repeatedly, both within a single agent and across siblings in a pass. "Where is the session token validated?" is asked once per reviewer and once per worker touching auth.

**Mechanism.** Normalise the question (lowercase, strip punctuation and pronouns, sort content words, drop stopwords), hash it, and key a cache on `(question_hash, tree_state_hash)` where `tree_state_hash` covers the files in the answer's provenance. The answer plus its provenance is stored in the v1 Vault's SQLite store under a separate table. Subsequent asks are served from the cache.

**Where the interception happens.** This cannot intercept a question a model asks itself internally. It intercepts the *observable* form: repeated `Grep` and `Glob` calls with semantically equivalent patterns, and repeated `Read` of the same spans. `PreToolUse` normalises the search and checks the cache; a hit returns the prior result through `updatedToolOutput` on a no-op call, or more simply, denies with the cached answer as the reason. Prefer the first shape if the installed version supports it cleanly, and `VERIFY`.

**Invalidation.** Any write to a file in an answer's provenance invalidates every cached answer derived from it. This is tracked in the store, not inferred. Cross-run reuse is off by default: the tree moves between runs and a stale answer is worse than no answer.

**Exit criteria.** Zero stale answers served, verified by replaying the invalidation log against the git history of the run. Measurable reduction in duplicate search calls per pass.

---

### M11. Sieve (deterministic-first review)

**Class: verified. Default: enabled. Release R2.**

A large share of review findings are decidable by tooling. Running a model to find them is paying the most expensive reviewer available to do a linter's job, and worse, those findings consume review *rounds*, which are the most expensive unit in the pipeline.

**Mechanism.** Before any reviewer spawns, run the checker registry against the node's diff:

| Checker | Finds |
|---|---|
| Type checker | type errors, signature mismatches |
| Linter and formatter | style, unused, shadowing, complexity thresholds |
| Test runner with coverage delta | untested new paths |
| AST rules | contract violations, forbidden imports, layering breaches, missing error handling on known-throwing calls |
| Dependency and ownership check | files touched outside the node's `owns`, new dependencies |
| Secret scan | credentials in the diff |

Everything found is fixed or reported before the reviewer runs. The reviewer then receives the **residue**: the diff with mechanically-decided regions annotated as already checked, plus an explicit declaration of which classes were covered and by what.

**The guard that makes this verified rather than statistical.** The declaration sent to the reviewer must list only checks that **actually ran and actually passed**. A checker that errored, timed out, or was unavailable is reported as *not run*, never silently omitted. Claiming coverage that did not happen converts a cheap saving into an undetected defect, and it is the one failure mode of this module that matters.

**Compounding effect.** Sieve cuts three things at once: reviewer input (smaller residue), reviewer output (fewer findings to write up), and round count (mechanical findings never trigger a round). The third is worth more than the first two combined.

**Exit criteria.** Across 20 runs, no finding that a checker claimed to cover appears in a later human or acceptance review. Review rounds per specialist measurably lower. Reviewer findings per round unchanged or higher in substance, meaning the reviewer spent its attention on things tooling cannot decide.

---

### M12. Distill (spec compilation)

**Class: verified. Default: enabled. Release R2.**

The merged spec is written for humans and transmitted to every agent. A brief written for agents is a fraction of the size and carries the same decision-relevant content.

**Mechanism.** One Haiku-tier pass compiles the merged spec into a dense brief: decisions, constraints, contracts, acceptance criteria, explicit non-goals. No narrative, no rationale, no alternatives considered. The brief is cached in the Vault keyed by the spec's content hash and reused across every agent in the run and across resumed runs.

**The fidelity gate.** This is what makes it verified. From the full spec, generate `distill.probe_count` (default 25) closed questions with unambiguous answers, covering every contract and acceptance criterion. Answer them from the brief alone. Any miss rejects the brief. On rejection, retry once with the missed topics named; on a second rejection, ship the full spec and log the failure. The probe set is generated once per spec and cached alongside the brief, so the gate costs one cheap pass, not one per agent.

**Full text stays one Read away.** The brief names its source path. An agent that needs rationale can fetch it. Log how often that happens: a high fetch rate means the brief is too thin and the compiler needs tuning.

**Exit criteria.** Fidelity gate passes on first attempt for at least 90 percent of specs. Full-spec fetch rate below 20 percent. No increase in reconciliation findings, which is the signal that would indicate the brief dropped something load-bearing.

---

### M14. Attribution (context reference tracking)

**Class: lossless, measurement only. Default: enabled. Release R3.**

Every dossier budget, section ranking and inclusion rule in v1 is a guess. This replaces all of them with data, and it is the prerequisite for M10.

**Mechanism.** For each agent, record what it was given, broken into addressable units: file spans, dossier sections, spec sections, sibling receipts, contract clauses. Then record what it *referenced*, detected three ways:

1. An explicit mention in its output, by path, symbol, section ID or interned code (M9 makes this trivially detectable, which is a second reason to build M9 first).
2. A tool call targeting it.
3. An edit that touches it.

The output is a reference rate per unit type, per agent type, over time. Units below `attribution.prune_threshold` (default 0.15) across a minimum sample of 50 exposures stop being included.

**Why this matters beyond pruning.** It produces the first honest answer to "how much of what we send is ever used," and that number is the ceiling on what push-based context can ever be worth. If it comes back at 30 percent, M10 is worth building. If it comes back at 85 percent, M10 is not, and you should know that before building it.

**Exit criteria.** Reference rates stable across runs. Pruning applied only above the minimum sample. No increase in rework count after the first pruning round.

---

### M10. Pull (manifest-based context delivery)

**Class: verified. Default: manifest enabled, push fallback retained. Release R3.**

Front-loading context assumes the sender can predict the receiver's needs. Attribution will have quantified exactly how badly it does.

**Mechanism.** Instead of injecting a 4,000-token dossier at `SubagentStart`, inject a roughly 200-token manifest:

```
Context available for T-003, by id:
  d1 owned files and spans (7 files, ~900 tok)
  d2 contract C-AUTH full text (~400 tok)
  d3 call sites of AuthService.refresh (11 sites, ~600 tok)
  d4 prior sibling receipts touching these files (2, ~300 tok)
  d5 recent git history for owned files (~500 tok)
  ...
Fetch with: Read .dag/runs/<run>/meter/dossiers/T-003/<id>.md
```

The agent pulls what it needs. Typical pulls will be two or three sections.

**Cost accounting, honestly.** Pull costs one extra round trip per section fetched, and a round trip re-sends the agent's current context. If the context at that moment is large and uncached, a pull can cost more than the section saved. So:

- Manifest fetches happen **early**, when the agent's context is smallest and its prefix is warm.
- The manifest states approximate token sizes so the agent can batch its fetches into one call rather than three.
- The daemon measures realised pull cost, including the round-trip overhead, and reports net saving rather than gross.

**Promotion.** Sections pulled above `pull.promote_threshold` (default 0.8) of the time are moved back into the push path for that agent type. The dossier stops being a designer's guess and becomes a measured distribution. Sections never pulled are dropped by M14's pruning.

**Fallback.** Any agent type where net saving is negative reverts to full push. This is a per-agent-type setting written by the daemon from measurement, not a global switch.

**Exit criteria.** Net saving positive per agent type after round-trip costs. No increase in rework count. No increase in dossier-denial escalations from v1's M2.

---

### M8. Quorum (adaptive review depth)

**Class: statistical. Default: off. Release R4, alone.**

The largest remaining lever and the only one that trades quality. Three critics per round is a 3x multiplier on the most expensive phase in the pipeline. It buys variance reduction, and variance reduction has diminishing returns that are currently being paid for at full price on every node regardless of whether the node is contentious.

**Mechanism.** Run critic 1. Escalate to critics 2 and 3 only on a deterministic contention predicate:

- Critic 1 raised any finding at or above severity `quorum.escalate_severity`, or
- the Sieve's deterministic checkers disagree with critic 1's verdict, or
- the deliverable's domain has a historical split-verdict rate above `quorum.domain_threshold`, or
- the deliverable is above a size or novelty threshold, or
- this is a re-review after a rework, or
- the random recall sample selected this node.

Everything about the predicate is computable before critics 2 and 3 would spawn. No model call decides whether to escalate.

**The recall guard is not optional.** On `quorum.sample_rate` (default 0.15) of non-escalated nodes, run the full quorum anyway and record whether critics 2 and 3 produced a **unique accepted finding**: a finding critic 1 did not raise, that the parent accepted as valid. That metric is the entire justification for the module. If it is materially above zero for a domain, that domain reverts to a permanent quorum of 3.

**Per-domain quorum table.** The output is a learned table, per specialist domain, of how many critics that domain actually needs. Some domains will show critics 2 and 3 adding nothing over 50 runs. Those get a permanent quorum of 1. Others, probably security and data model, will show real unique findings and keep 3. That table is more valuable than the token saving: it tells you where adversarial review is earning its cost.

**Interaction with M11.** Sieve runs first and removes mechanical findings. That makes critic 1's findings more substantive, which makes the contention predicate more informative. Build Sieve first, then measure, then build Quorum.

**Exit criteria.** Across 100 nodes at full quorum, the unique-accepted-finding rate for critics 2 and 3 is measured per domain and published in the release notes. Adaptive quorum ships only for domains where that rate is below `quorum.unique_finding_threshold` (default 0.05). Acceptance-review findings per run do not increase. If they do, the module is reverted, not tuned.

---

## 4. Interactions between modules

These are not independent and the overlaps must be accounted for or the savings will be double-counted.

| Pair | Interaction |
|---|---|
| M11 Sieve and M8 Quorum | Sieve reduces reviewer input; Quorum reduces reviewer count. They multiply, but Sieve also makes Quorum's predicate more informative, so the ordering is forced: Sieve first |
| M12 Distill and M10 Pull | Both target the spec. Distill shrinks it; Pull makes it optional. Apply Distill first, then let Pull operate on the brief, or the measured saving of the second will look smaller than it is |
| M9 Intern and M14 Attribution | Interning makes references machine-detectable, which is what Attribution needs. M9 before M14 |
| M9 Intern and v1 Echo | Interning changes the byte content of prefixes, which invalidates Echo's promoted prefixes on first run. Expected, one-time, must not be read as a regression |
| M10 Pull and v1 M2 Dossier enforcement | Pull inverts the delivery model, so v1's denial rule ("read the dossier before you grep") becomes "read the manifest before you grep." Update the rule or it will deny correct behaviour |
| M13 Throttle and M15 Oracle | Independent. No overlap |

---

## 5. Configuration

Extends v1's `.dag/meter.json` under the same `modules` key.

```json
{
  "modules": {
    "intern":      { "enabled": true,  "mode": "codebook",
                     "fallback": "basename", "min_refs": 3 },
    "throttle":    { "enabled": true,  "deny_write_existing": true,
                     "rewrite_ratio": 0.6, "max_denials_per_path": 2,
                     "effort_routing": false },
    "oracle":      { "enabled": true,  "scope": "run", "max_entries": 5000 },
    "sieve":       { "enabled": true,  "checkers": ["types","lint","tests","ast","ownership","secrets"],
                     "declare_only_passed": true },
    "distill":     { "enabled": true,  "probe_count": 25, "retry": 1,
                     "fetch_rate_alarm": 0.2 },
    "attribution": { "enabled": true,  "prune_threshold": 0.15, "min_exposures": 50,
                     "prune": false },
    "pull":        { "enabled": false, "promote_threshold": 0.8,
                     "per_agent_override": {} },
    "quorum":      { "enabled": false, "escalate_severity": "major",
                     "sample_rate": 0.15, "unique_finding_threshold": 0.05,
                     "domain_table": {} }
  }
}
```

Note `attribution.prune` defaults false: it measures for a full release before it is allowed to act. Same pattern as v1's vault shadow mode, for the same reason.

---

## 6. Implementation plan

Four releases. Lossless first, statistical last. The ordering deliberately inverts the size of the savings: you take the small safe wins first because they buy the measurement confidence needed to evaluate the large risky one.

### R1: lossless (M9, M13, M15)
No quality risk, no new decisions, no gates beyond round-trip tests. Ship together.
Exit: composite token reduction visible in the ledger, rework count and review findings unchanged.

### R2: verified (M11, M12)
Both reduce information behind a deterministic gate. Ship together; their gates are independent.
Exit: Sieve's coverage claims hold across 20 runs, Distill's fidelity gate passes above 90 percent first-attempt.

### R3: measurement then delivery (M14, then M10)
M14 ships alone and measures for one release. M10 ships only if M14's reference rate justifies it.
Exit: net-positive pull saving per agent type after round-trip costs.

### R4: statistical (M8, alone)
Nothing else changes in this release. The recall guard runs from day one, in measure-only mode, before adaptive quorum is enabled for any domain.
Exit: per-domain unique-finding rates published, adaptive quorum enabled only where the rate is below threshold.

### 6.1 Task graph

```json
{
  "tasks": [
    {"id":"T-101","title":"intern: codebook builder and de-interner",
     "acceptance_criteria":["codebook built from the dossier with zero model calls","round-trip decode equals original for 100% of generated codebooks","de-interning applied at every human-facing boundary","codebook stored per node so old receipts decode","basename fallback implemented and switchable"],
     "owns":["meter/intern.py"],"contracts":["C-INTERN"],"depends_on":[]},

    {"id":"T-102","title":"intern: wire format across dossiers, prompts, receipts",
     "acceptance_criteria":["references only emitted where the codebook is present in the same prompt","receipt schema version bumped","vault input-span lists interned","validate.py checks codebook presence"],
     "owns":["meter/dossier.py","meter/receipts.py","meter/schemas/receipt.v2.json"],"contracts":["C-INTERN","C-RECEIPT"],"depends_on":[{"id":"T-101","reason":"needs the codebook"}]},

    {"id":"T-103","title":"throttle: deny Write on existing tracked files",
     "acceptance_criteria":["all four exceptions implemented deterministically","denial capped at 2 per path","every denial logged with the path and reason","audit of first 20 runs shows no wrongly blocked rewrite"],
     "owns":["meter/throttle.py"],"contracts":["C-THROTTLE"],"depends_on":[]},

    {"id":"T-104","title":"throttle: narration accounting and effort routing probe",
     "acceptance_criteria":["prose-to-artifact ratio logged per agent type and shown in /dag-cost","dag-doctor reports whether per-node effort is settable from a hook","effort routing implemented only if the probe passes, otherwise closed with a note"],
     "owns":["meter/throttle.py","scripts/dag-doctor.py"],"contracts":["C-THROTTLE"],"depends_on":[{"id":"T-103","reason":"same module"}]},

    {"id":"T-105","title":"oracle: question normalisation and answer cache",
     "acceptance_criteria":["normalisation is a pure tested function","key covers tree state and provenance","invalidation on any write to a provenance file","zero stale answers when the invalidation log is replayed against run git history","scope defaults to run"],
     "owns":["meter/oracle.py"],"contracts":["C-ORACLE"],"depends_on":[]},

    {"id":"T-106","title":"sieve: checker registry and residue format",
     "acceptance_criteria":["each checker isolated, timeout-bounded, and reports ran/passed/errored separately","a checker that errored is declared as not run","residue annotates mechanically-decided regions","declaration lists only checks that ran and passed"],
     "owns":["meter/sieve.py","meter/checkers/"],"contracts":["C-SIEVE"],"depends_on":[]},

    {"id":"T-107","title":"sieve: reviewer integration and round accounting",
     "acceptance_criteria":["reviewer receives residue plus declaration","mechanical findings never consume a review round","review rounds per specialist tracked before and after in the ledger"],
     "owns":["meter/sieve.py","agents/"],"contracts":["C-SIEVE"],"depends_on":[{"id":"T-106","reason":"needs the registry"}]},

    {"id":"T-108","title":"distill: spec compiler and brief cache",
     "acceptance_criteria":["one Haiku-tier pass per spec content hash","brief carries decisions, constraints, contracts, acceptance criteria and non-goals only","brief cached in the vault and reused across agents and resumed runs","full spec path named in the brief"],
     "owns":["meter/distill.py"],"contracts":["C-DISTILL"],"depends_on":[]},

    {"id":"T-109","title":"distill: fidelity gate",
     "acceptance_criteria":["probe set generated once per spec and cached","every contract and acceptance criterion covered by at least one probe","any miss rejects the brief","one retry naming missed topics, then fall back to full spec and log","full-spec fetch rate reported"],
     "owns":["meter/distill.py"],"contracts":["C-DISTILL"],"depends_on":[{"id":"T-108","reason":"gates the compiler"}]},

    {"id":"T-110","title":"attribution: exposure and reference tracking",
     "acceptance_criteria":["exposures recorded per addressable unit","references detected via output mention, tool call, and edit","reference rates per unit type and agent type in /dag-cost","prune disabled by default"],
     "owns":["meter/attribution.py"],"contracts":["C-ATTR"],"depends_on":[{"id":"T-102","reason":"interned refs make mentions detectable"}]},

    {"id":"T-111","title":"attribution: pruning with minimum sample",
     "acceptance_criteria":["pruning only above min_exposures","pruned units logged","rework count unchanged after the first pruning round","pruning reversible by config"],
     "owns":["meter/attribution.py"],"contracts":["C-ATTR"],"depends_on":[{"id":"T-110","reason":"needs the rates"}]},

    {"id":"T-112","title":"pull: manifest generation and section files",
     "acceptance_criteria":["manifest under 250 tokens","approximate section sizes stated","sections written as individually readable files","v1 dossier denial rule updated to reference the manifest"],
     "owns":["meter/pull.py","meter/dossier.py"],"contracts":["C-PULL"],"depends_on":[{"id":"T-111","reason":"section set comes from attribution data"}]},

    {"id":"T-113","title":"pull: net saving accounting and promotion",
     "acceptance_criteria":["round-trip overhead included in the measured saving","net saving reported per agent type","sections above promote_threshold moved back to push","agent types with negative net saving revert to full push automatically"],
     "owns":["meter/pull.py"],"contracts":["C-PULL"],"depends_on":[{"id":"T-112","reason":"needs the manifest"}]},

    {"id":"T-114","title":"quorum: contention predicate and recall guard, measure-only",
     "acceptance_criteria":["predicate fully deterministic, no model call","recall guard samples at configured rate","unique accepted finding defined and measured per domain","adaptive quorum disabled in this task, measurement only"],
     "owns":["meter/quorum.py"],"contracts":["C-QUORUM"],"depends_on":[{"id":"T-107","reason":"sieve must run first to make critic findings substantive"}]},

    {"id":"T-115","title":"quorum: per-domain adaptive depth",
     "acceptance_criteria":["enabled only for domains measured below unique_finding_threshold","domain table written from measurement, not hand-edited","escalation always available","acceptance-review findings per run do not increase","revert path is a single config change"],
     "owns":["meter/quorum.py"],"contracts":["C-QUORUM"],"depends_on":[{"id":"T-114","reason":"needs the measurement"}]},

    {"id":"T-116","title":"validator, docs and release notes for v2",
     "acceptance_criteria":["validate.py covers the new config keys, receipt v2 schema and checker registry","risk class of each module stated in the README","per-domain quorum table published in release notes","every module independently disableable and documented as such"],
     "owns":["scripts/validate.py","README.md"],"contracts":[],"depends_on":[{"id":"T-115","reason":"documents the final state"}]}
  ],
  "contracts": {
    "C-INTERN":  "codebook format, reference syntax, de-interning boundaries, storage per node",
    "C-THROTTLE":"write-denial predicate and exception list, narration metric shape",
    "C-ORACLE":  "question normalisation, cache key, provenance and invalidation rules",
    "C-SIEVE":   "checker registry interface, ran/passed/errored reporting, residue and declaration format",
    "C-DISTILL": "brief schema, probe generation rule, gate thresholds and fallback",
    "C-ATTR":    "addressable unit ids, exposure and reference records, prune rule",
    "C-PULL":    "manifest schema, section file layout, net saving accounting, promotion rule",
    "C-QUORUM":  "contention predicate, recall sampling, unique-accepted-finding definition, domain table"
  }
}
```

T-102 also consumes `C-RECEIPT`, which is owned by v1 and is being revised here, not defined. Interning changes the receipt wire format, so the schema version goes to v2 and v1's validator check on receipt-schema agreement has to move with it.

---

## 7. Measurement

### 7.1 Per-module attribution

Reuse v1's A/B harness. Each module is measured with everything else held at its current release state, one module toggled. A composite number with all eight enabled is not sufficient, because the overlaps in Section 4 will otherwise be double-counted.

### 7.2 Expected multipliers

Hypotheses, stated so they can be falsified, against the input mix in Section 1.

| Module | Class hit | Multiplier on that class |
|---|---|---|
| M8 Quorum | review phase spend | 0.47 |
| M10 Pull | exploration and dossier | 0.45 |
| M11 Sieve | reviewer input and round count | 0.65 |
| M12 Distill | pasted spec | 0.32 |
| M13 Throttle | output tokens | 0.60 |
| M9 Intern | everything referencing code | 0.90 |
| M14 Attribution | dossier residue | 0.80 |
| M15 Oracle | duplicate search and read | 0.85 |

Composite, accounting for the overlaps: roughly **0.35 to 0.45** of current spend. A run at 700 lands between 245 and 315.

Two caveats that must appear in the release notes.

**M8 is more than half the saving and is the only quality trade.** Without it, the composite is roughly 0.65. Everything else combined is a 35 percent cut. Quorum is what turns that into a 60 percent cut, and it is the one you cannot ship without a recall guard.

**Tokens and billed cost diverge.** Cache reads bill at roughly a tenth. Any module that moves content into a cached prefix cuts cost far more than it cuts tokens; any module that reduces round trips can cut tokens while raising the cache-miss rate. Report both curves separately in `/dag-cost` or you will misattribute which mechanism worked.

### 7.3 Quality metrics, tracked on every release

Token savings mean nothing without these held constant. Regression on any one reverts the release.

- Rework count per task
- Review rounds per specialist
- Acceptance-review findings per run
- Integration failures per run
- Unique accepted findings from critics 2 and 3, per domain

---

## 8. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Adaptive quorum silently lowers review quality | High | Recall guard from day one, per-domain gating, revert rather than tune, acceptance findings as the tripwire |
| Sieve declares coverage for a check that did not run | High | Ran, passed and errored reported separately; only passed checks are declared; a failed checker is stated as not run |
| Distilled brief drops something load-bearing | High | Fidelity gate over every contract and acceptance criterion, retry, fall back to full spec, reconciliation findings as the tripwire |
| Interning degrades model comprehension | Medium | Round-trip test plus quality A/B; basename-compression fallback retains most of the saving with no comprehension risk |
| Pull costs more than it saves via round trips | Medium | Net accounting including overhead, per-agent-type auto-revert, early fetch while the prefix is warm |
| Oracle serves a stale answer | Medium | Provenance-tracked invalidation, run-scoped by default, invalidation log replayed against git history as a test |
| Throttle blocks a genuinely needed rewrite | Medium | Four deterministic exceptions, denial cap of 2 per path, audit of every denial in the first 20 runs |
| Attribution prunes something rarely but critically needed | Medium | Minimum sample of 50 exposures, prune off by default for a full release, rework count as the tripwire |
| Savings double-counted across overlapping modules | Medium | Per-module A/B with everything else held fixed; Section 4 overlap table |
| Eight more modules make the plugin unmaintainable | Medium | Each independently disableable, risk class stated, and the v1 standing rule holds: a module failing its exit criteria is deleted, not tuned |

---

## 9. What is deliberately not here

- **Cross-run quorum learning shared between repos.** Split-verdict rates are repo-specific and a shared table would import another codebase's risk profile.
- **Model-decided escalation in M8.** A model call to decide whether to make model calls is the wrong shape. The predicate stays deterministic.
- **Compressing agent instructions and templates.** Roughly 10 percent of input, already near-fixed, and the highest-blast-radius thing to touch.
- **Anything that reduces the number of *acceptance* reviews.** The final gate stays at full strength regardless of every optimisation above it.
