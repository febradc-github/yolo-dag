## Phase 3 — Merge & Reconcile

Once every specialist has finished its Phase 2 loop:

1. **Merge.** You (not a spawned agent) assemble the combined build spec directly: `Read` each
   specialist's final deliverable from its own path on disk (never carry it forward from context —
   it was written there as Phase 2 completed) and concatenate them under their own headings
   (Design Spec, Architecture Spec, Research Notes, Security Spec, Test Plan, Cost & Resource
   Estimate, UX Copy, Data & Schema Spec — only those that ran), followed by an `## Open Concerns`
   section listing anything carried over from Phase 2 step 7, attributed to the specialist it came
   from. Write it to `<run-dir>/merged-spec.md`.

2. **Reconcile.** Spawn one `spec-reconciler` with the path to the merged spec (it `Read`s it
   itself) and the path it must `Write` its rulings to, `<run-dir>/reconcile.md`. Every review loop
   up to this point was *intra*-specialist — three reviewers on one deliverable, blind to its
   siblings — so nothing so far could catch two deliverables that are each internally excellent
   and mutually incompatible. This is the pass that does.

   It ends with a short confirmation, then `RECONCILE: CLEAN` or `RECONCILE: CONTRADICTIONS <n>`.
   **If it returns no parseable `RECONCILE:` line, treat it as `RECONCILE: CONTRADICTIONS 1`**
   rather than `CLEAN` — a missing verdict is not evidence the specs agree. Note the gap, and since
   there is no actual contradiction text to route back to two named specialists, carry it forward
   as an Open Concern instead of trying to resolve an unspecified one.

3. **Resolve contradictions.** For each one, `SendMessage` it to **both** named specialists (they
   are still resumable from Phase 1), pointing at the same `round-<n>.md` path each already owns,
   asking each to either adopt the other's position and overwrite that file in place, or state why
   theirs should stand and leave it unchanged. If they still disagree after one exchange, promote
   it to an Open Concern rather than looping — one round of reconciliation is enough to catch
   honest mismatches, and a second rarely changes a genuine judgment call. `reconcile.md` is
   already on disk, written directly by the reconciler; append the resolution outcome to it rather
   than rewriting the whole file. **If any specialist's file actually changed**, redo the Merge
   step above (re-`Read` and re-concatenate) so `merged-spec.md` reflects it — it does not update
   itself.

4. **Show the user the merged spec and move straight into Phase 4** — don't wait for a go/no-go
   by default, same reasoning as the ground rules: the user wants the finished work, not a
   checkpoint on every intermediate artifact.

   The one exception is **Open Concerns that are themselves high-stakes** — a genuine unresolved
   disagreement touching security, data, or something costly to undo. For those specifically,
   pause and ask before Phase 4, since proceeding on a guess there is exactly the kind of decision
   this pipeline shouldn't make unilaterally. Low-stakes Open Concerns get noted and carried
   forward, not gated on.

