## Phase 2 — Build + review loop (per specialist, independently)

Run this once per specialist, starting as soon as that specialist's first draft lands (don't
wait for all of them — they can be at different rounds simultaneously).

This is the most expensive phase in the pipeline: a constant 3 reviewers plus a consolidator, per
specialist, per round, is up to 96 spawns in a `full` run. Two of the steps below replace part of
that constant with a computation. Both fail toward the unmodified behaviour, never past it.

1. **Size the round (`dag-gauge.py`).** Before spawning reviewers, ask how many this round
   actually needs:

   ```
   python3 <plugin>/scripts/dag-gauge.py <run-dir>/specialists/<name>/round-<n>.md <name> \
       --mode <mode> --round <r> [--prior-findings <n>] [--all]
   ```

   Resolve `<plugin>` the way Phase 4 resolves `dag-distill.py`. It prints one line you parse:

   ```
   GAUGE: reviewers=2 angles=completeness,feasibility single_pass=no
   ```

   Spawn exactly that many `spec-reviewer` instances, on exactly those angles. It **always exits
   0** — on any failure it prints the unmodified constant (3 in `full`, 2 in `lite`), so a missing
   or broken script costs you nothing but the saving. Pass `--prior-findings` with the previous
   round's consolidated count from round 2 onward, and `--all` whenever the user passed it.

   `single_pass=yes` means one `spec-reviewer` covers the listed angles in a single spawn instead
   of one spawn each — give it all of them in its prompt and tell it to report each angle's
   findings separately in the same file. Gauge only says this for a small, low-risk deliverable,
   where three agents re-reading the same short document costs more than the independence buys.

   **Gauge never raises scrutiny and never overrides a floor**: a high-risk domain
   (`security-specialist`, `data-schema-specialist`) on its first round always gets the full
   panel, and `--all` always gets the full panel. If `meter` isn't installed, skip this step and
   use the mode's constant — that is exactly what the fallback line would have told you.

2. **Spawn the reviewers** in one batch, `run_in_background: true`, each given the path to the
   specialist's current deliverable (`<run-dir>/specialists/<name>/round-<n>.md` — it `Read`s this
   itself), the original request, its assigned angle from the three defined in the `spec-reviewer`
   agent file — **completeness/gaps**, **internal consistency**, **feasibility/risk** — and the
   exact path it must `Write` its findings to:
   `<run-dir>/specialists/<name>/round-<r>-findings-<angle>.md` (`r` is this round's number,
   starting at 1). **Meter's adaptive quorum (v2 M8, inert unless
   `meter.modules.quorum.enabled` is explicitly true, which it is not by default) may narrow
   this further for a domain the daemon has measured over 50+ full runs — see `meter/quorum.py`.
   Absent that measurement, Gauge's number above is the number.**

3. Each reviewer ends its final message with a short confirmation of what it wrote, then
   `REVIEW: CLEAN` or `REVIEW: FINDINGS <n>`. **Read that line, not a tool call** — reviewers do
   not call `ReportFindings` (they review prose, which has no file or line to anchor to). If all
   report `REVIEW: CLEAN`, the round closes early and this specialist is done; skip to step 6.
   **If a reviewer's final message has no parseable `REVIEW:` line at all, treat it as
   `REVIEW: FINDINGS 1`** — a reviewer that returned no verdict is not evidence the deliverable is
   clean. Note the gap (in `run.json`'s `degradations` or your own running notes) so it stays
   visible, and carry it into consolidation as one generic finding requiring the specialist's
   attention rather than silently treating the round as clean.

4. **Consolidate — script first, agent only on escalation (`dag-fold.py`).** Merging the finding
   files is a mechanical dedupe-and-rank of pre-scored, pre-structured records;
   `agents/spec-consolidator.md` says as much itself. Try the deterministic path:

   ```
   python3 <plugin>/scripts/dag-fold.py \
       <run-dir>/specialists/<name>/round-<r>-consolidated.md \
       <run-dir>/specialists/<name>/round-<r>-findings-*.md
   ```

   **Exit 0** — the consolidated file is written and the last stdout line is
   `CONSOLIDATED: <n>`, exactly the contract `spec-consolidator` would have emitted. Use it and
   **do not spawn the agent**. **Exit 2** — Fold declined; the reason is on stderr (a finding
   file it could not parse, or two findings about the same subject proposing opposite fixes,
   which is a genuine disagreement it refuses to adjudicate). Spawn `spec-consolidator` exactly
   as step 5 describes. Treat any other exit code as an exit 2.

5. **`spec-consolidator`, only if Fold escalated.** Spawn one, `run_in_background: true`, with
   the paths to the finding files (it `Read`s them itself) and the path it must `Write` its
   consolidated list to: `<run-dir>/specialists/<name>/round-<r>-consolidated.md`. It ends with a
   short confirmation and `CONSOLIDATED: <n>`. **If it returns no parseable `CONSOLIDATED:` line,
   treat it as `CONSOLIDATED: 1`** rather than closing the round early — a missing count is not
   evidence of an empty list — note the gap the same way, and resume the specialist with whatever
   raw finding text you do have.

   Either way: **if the count is `0`, the round closes** — deduplication can dissolve several
   near-findings into nothing, and an empty list is not worth a revision round.

6. `SendMessage` the specialist **by its Phase 1 spawn name** — never a fresh spawn — pointing it
   at the consolidated-findings path (it `Read`s that itself) and the path for its revised
   deliverable, `<run-dir>/specialists/<name>/round-<r>.md`, asking it to accept valid findings,
   push back with reasoning on the rest, and write a revised deliverable there.

7. That's one round. Repeat 1–6 up to the mode's round cap (3 in `full`, 1 in `lite`) — but stop
   early on **diminishing returns**: if the specialist's revision accepted *none* of the round's
   findings (it pushed back on everything), don't spend another round re-litigating — a further
   identical round rarely moves a considered pushback. Carry anything a reviewer would still
   stand behind forward as an Open Concern instead.

8. Each round's deliverable and consolidated findings are already on disk at the paths above —
   update that specialist's `rounds`/`status` in `run.json`, and re-read a file at the point of
   use rather than carrying its text forward.

9. **If the last round completes and a finding is still unresolved** — the specialist pushed back
   on something a reviewer would still stand by — don't loop another round and don't silently
   drop it. Take the specialist's last deliverable as final, and carry the unresolved finding
   into Phase 3 as a flagged **Open Concern**.

**Node ids for this phase's `DAG-NODE:` prefix** (see the spine's ground rules):
`P2-<specialist>-r<round>-<angle>` for a reviewer, `P2-<specialist>-r<round>-consolidator` for
the consolidator. A round consolidated by Fold spawns nothing and so has no node id and no
`run.json` `spawns` entry — that absence is the saving, not an omission to correct.
