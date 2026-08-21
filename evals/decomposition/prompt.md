Use the `task-specialist` agent to decompose the following merged build spec into a task graph.
Return its output verbatim, including the JSON block.

---

## Architecture Spec

A `subscriptions` feature with three parts: a storage layer, a REST endpoint that reads it, and a
nightly job that expires stale rows. The endpoint and the nightly job both depend on the storage
layer existing. They do not depend on each other.

**Decisions others depend on:** PostgreSQL via the existing `db/` module; HTTP layer is the
existing Express app in `server/`.

## Data & Schema Spec

Storage engine: PostgreSQL (matches the existing `db/` module).

Entity `subscription`: `id` (uuid, pk), `user_id` (uuid, fk → users), `plan` (text),
`status` (text, one of active/expired), `expires_at` (timestamptz). Unique index on
`(user_id, plan)`.

A migration is required to create the table, and it must reference the schema above.

## Test Plan

Suite command: `npm test`.

- Unit tests for the expiry predicate.
- Integration test that the endpoint returns only `active` rows.
- Integration test that the nightly job flips `active` → `expired` past `expires_at`.

## Open Concerns

None.
