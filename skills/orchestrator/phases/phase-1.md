## Phase 1 — Route & Fan-out

You only reach this file in `full` or `lite` mode — the spine handles `micro` without
opening it.

**Route first.** Not every request needs every specialist, and an irrelevant deliverable isn't
free — it still costs a full review loop to produce something that adds noise to the merged spec.
Select from the eight:

- **Always**: `architecture-specialist`, `research-specialist`, `test-planning-specialist`.
- **`security-specialist`** if the request touches auth, user or sensitive data, secrets, or
  input from an untrusted source.
- **`data-schema-specialist`** if it implicates persistence — a database, file format, cache, or
  wire schema.
- **`design-specialist`** if it has user-facing surfaces with layout or interaction.
- **`ux-copy-specialist`** if it has user-facing text a human reads.
- **`cost-estimation-specialist`** if it carries real infra cost, per-call API/model spend, or a
  big enough time commitment to change whether it's worth doing.

If the user passed `--all`, select all eight and skip the judgment. In `lite` mode, cap the
selection at 4 — keep the always-on three plus the single most relevant optional one. **When more
than one optional domain is deterministically triggered by its own hard rule above** (e.g. a
request that touches both auth and persistence triggers both `security-specialist` and
`data-schema-specialist`), don't guess which is "most relevant" — break the tie by a fixed risk
order: `security-specialist` > `data-schema-specialist` > `cost-estimation-specialist` >
`design-specialist` > `ux-copy-specialist`. Name every optional domain the tie-break dropped as an
explicit stated omission in `routing.md` ("also touches cost-estimation, dropped by the lite-mode
cap in favor of security") — never let it disappear silently.

State the selection and the skips in one line ("Routing to 5: architecture, research,
test-planning, data-schema, security — skipping design/ux-copy (no user-facing surface) and cost
(no infra delta)"), write it to `<run-dir>/routing.md`, and don't ask for approval of it.

**Then fan out.** In a single turn, issue one `Agent` call per selected specialist,
`run_in_background: true`, giving each the fully-specified request verbatim, framed for its
domain, plus the exact path it must `Write` its deliverable to:
`<run-dir>/specialists/<name>/round-0.md`. Note every spawn name — you need them for Phase 2's and
Phase 3's `SendMessage` resumes — and record each specialist in `run.json`'s `specialists` array
(`{"name": ..., "spawn": ..., "rounds": 0, "status": "drafting"}`).

Don't block waiting on them one at a time; let completion notifications arrive and track against
your roster.

