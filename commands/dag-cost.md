---
description: Show measured token spend for one yolo-dag run — meter's Ledger (M0), not the orchestrator's spawn-unit estimate. Reports "not available" plainly if meter never ran for this run.
argument-hint: A run id (e.g. 2026-08-21-a3f9). Omit to use the most recent run.
allowed-tools: ["Read", "Glob", "Bash"]
---

# /dag-cost — Measured token spend for one run

Target run: `$ARGUMENTS` — if empty, use the most recent directory under `.dag/runs/`.

meter is a separate plugin subsystem, on by default (see `meter-handoff.md`), that measures real
token spend per DAG node via Claude Code's hooks. As of this version, only its M0 milestone
(the Ledger) is implemented — it measures and changes nothing else about how the pipeline runs.
Nothing else in this command depends on meter being installed, enabled, or having run: if its
output isn't there, say so plainly and stop, rather than reconstructing numbers from anything
else.

1. **Look for `<run-dir>/meter/report.md`.** If it exists, read and print it verbatim — it
   already contains the measured totals, the cache-read ratio, the spawn-unit estimate for
   comparison, a per-phase breakdown, and the top nodes by spend.
2. **If it doesn't exist**, check whether `<run-dir>/meter/ledger.jsonl` exists instead (the
   report can be regenerated from it if something interrupted the write). If so, summarize it
   directly: total measured input/output tokens, how many node rows have `null` tokens (meter
   couldn't measure them — report the count, don't guess a number for them), and the top few
   nodes by `in_tok + out_tok`.
3. **If neither exists**, say plainly that meter produced no measurements for this run — either
   it wasn't enabled, the daemon never started, or the run predates this command — and point at
   `python3 scripts/dag-doctor.py` as the way to check why, rather than guessing a reason.
4. **Never fall back to the spawn-unit estimate as if it were a measurement.** `run.json`'s
   `spawns` array is the orchestrator's own cost-weighted guess, already visible via
   `/dag-status`; this command's entire purpose is the *measured* number, and blending the two
   without saying which is which defeats it. If you show both (`report.md` does), label them
   distinctly.
