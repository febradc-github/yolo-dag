Use the `spec-reviewer` agent to review the deliverable below. Its assigned angle is
**completeness/gaps**. The original request was:

> Design the storage for a user subscription feature. Each user may hold at most one
> subscription per plan. Expired subscriptions must be retained for a full audit trail — we are
> required to be able to reconstruct who held what, and when, for the past seven years.

Return the reviewer's output verbatim, including its final status line.

---

## Data & Schema Spec

Storage engine: PostgreSQL, matching the existing `db/` module.

Entity `subscription`:

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | primary key |
| `user_id` | uuid | foreign key → `users.id` |
| `plan` | text | plan identifier |
| `status` | text | `active` or `expired` |

When a subscription expires, the row is deleted and the user may re-subscribe to the same plan.

Indexes: primary key on `id`, index on `user_id` for lookup.
