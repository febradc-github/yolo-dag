## Phase 6 — Verify & hand off

Every merged task was only ever reviewed against its *own* acceptance criteria, by design.
Nothing before this point has run the assembled result, and nothing before this point has
compared it to what was actually asked for. This phase does both. Do not skip it.

1. **Run the suite** the test plan named in Phase 3 (in `micro` mode: the repo's own test/build
   command) against `dag/<run-id>`, once, and record the verbatim outcome in
   `<run-dir>/integration.md`. Individually-merged tasks can still be collectively broken.
2. **Acceptance review.** Spawn one `integration-reviewer` with the original request, the merged
   spec's path, the `base_commit`, and the branch name. It reviews the *whole diff*
   (`git diff <base_commit>..dag/<run-id>`) against the spec and the request — the only agent in
   the pipeline that ever does — and ends with `INTEGRATION: PASS` or `INTEGRATION: FLAGGED <n>`.
   Its findings go into the final summary; they do **not** trigger another build loop. Fixing
   them is a fresh request, not a silent extra loop here.
3. **If the integrated suite failed, report the failure honestly with its output.** Do not
   describe the run as successful.
4. **Clean up and hand back**: `git worktree prune`; keep (and name) only the worktrees of
   UNMERGED tasks; check the main working tree back out to `base_branch`, leaving `dag/<run-id>`
   intact for the user to review. Mark phase 6 complete in `run.json`.

