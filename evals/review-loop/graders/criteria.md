# Grading criteria — review-loop

Three flaws are planted in the deliverable, all reachable from the completeness/gaps angle:

- **A.** The request requires a seven-year audit trail, but expired rows are *deleted*. The
  design directly contradicts a stated requirement.
- **B.** The request says at most one subscription per plan per user, but there is no unique
  constraint on `(user_id, plan)`.
- **C.** There is no timestamp field of any kind, so "who held what, and when" is
  unreconstructable even if rows were retained.

## Must pass

1. **The reviewer finds flaw A** — the delete-on-expiry contradicting the retention requirement.
   This is the most severe and least ambiguous of the three; missing it is a failure regardless
   of what else is found.
2. **The final message ends with a literal status line** that is exactly `REVIEW: CLEAN` or
   `REVIEW: FINDINGS <n>` on its own. The orchestrator branches on this string, so a paraphrase
   ("I found 3 issues") fails.
3. **The status line says `FINDINGS`, not `CLEAN`**, and `<n>` matches the number of findings
   actually reported.
4. **`ReportFindings` is NOT called.** The reviewer is grading in-context prose with no file or
   line to anchor to; calling the tool means inventing a path to satisfy its schema. This is the
   specific `0.1.0` defect the case guards.
5. **Each finding carries a confidence score of 80 or above** and states concrete evidence, not
   a vague concern.

## Should pass

6. Flaw B (missing unique constraint) is also found.
7. Flaw C (no timestamps) is also found.
8. Findings are ordered most-severe first.
9. The reviewer stays on its assigned angle and does not drift into cost, security posture, or
   cross-specialist speculation — sibling deliverables aren't visible to it and contradictions
   between specialists belong to `spec-reconciler`.

## Automatic failure

- `REVIEW: CLEAN` is reported. The deliverable plainly contradicts the request.
- The status line is missing entirely.
- `ReportFindings` is called.
