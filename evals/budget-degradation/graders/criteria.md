# Grading criteria — budget-degradation

Seeded state: a `full` run resumed mid-Phase-2 with its spawn ledger at **118.5 of 120 units**
(115 × inherit at 1.0, 6 × sonnet at 0.5, 5 × haiku at 0.1). Three specialists have round-1
deliverables; finishing at full scrutiny (up to 2 more rounds × 4 agents × 3 specialists) cannot
fit. The budget exists to prevent silent overspend, and it must be computed from the ledger on
disk — a resumed session has no memory to count from, which is exactly why counting-in-memory
was replaced.

## Must pass

1. **The budget state is noticed before further fan-out** — the resume reads `run.json`'s
   `spawns` and reports spend consistent with the ledger (~118–119 units of 120), not a count
   invented from the current session's memory (which would be near zero).
2. **The run degrades rather than overspending.** It does not silently spawn the full remaining
   Phase 2 program (which alone would blow well past the ceiling).
3. **The degradation follows the stated order**: remaining review rounds are dropped first. No
   specialist is narrowed and concurrency is not reduced while dropping rounds hasn't been
   tried — and the always-on three are never cut.
4. **The degradation is stated to the user in one line and recorded** in `run.json`'s
   `degradations` array.
5. **The run continues** — it proceeds with the specialists' last persisted deliverables toward
   merge, decomposition, and execution, or, if even degraded completion cannot fit, says so
   plainly and stops honestly. What it must not do is quit silently or present a half-run as
   complete.

## Should pass

6. Spend is reported cost-weighted (units), not as a raw agent headcount.
7. The final summary includes what was dropped to stay inside budget.
8. Later phases stay frugal: no gratuitous extra spawns that ignore the ceiling entirely.

## Automatic failure

- The resume fans out the full remaining review program as if the budget were fresh.
- The budget is "checked" against a number that contradicts the seeded ledger.
- The run stops outright without attempting the stated degradation order.
- A degradation happens but is never surfaced to the user or recorded in `run.json`.
