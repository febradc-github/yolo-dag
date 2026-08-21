# Grading criteria — decomposition

The spec has a deliberate shape: one foundational piece (storage/schema) and two independent
consumers of it (the endpoint, the nightly job). A correct decomposition is a DAG with a fan-out,
not a flat list and not one giant task.

## Must pass

1. **A fenced `json` block is present** and parses as JSON with a top-level `tasks` array.
2. **Every task has** `id`, `title`, `description`, `acceptance_criteria` (non-empty array),
   `depends_on` (array), and `files` (array).
3. **Ids are unique**, and every id appearing in any `depends_on` exists in the task list.
4. **The graph is acyclic.**
5. **At least one task has a non-empty `depends_on`.** This is the core of the change being
   tested: ordered work must be expressed as an edge. A graph where every task has
   `depends_on: []` means the model reverted to the old independent-set rule and folded the
   ordering away.
6. **The endpoint work and the nightly-job work are separate tasks.** They are genuinely
   independent of each other and folding them together is a decomposition failure.
7. **No task description references another task** by number, name, or ordinal ("after task 2",
   "using the schema from task 1"). The edge carries that relationship.

## Should pass

8. The storage/schema task sits at wave 0 (empty `depends_on`) and both consumers depend on it,
   directly or transitively.
9. `files` entries look like plausible repo paths consistent with the spec's `db/` and `server/`
   modules, not invented directories.
10. Acceptance criteria are checkable statements a reviewer with no other context could grade,
    not restatements of the task title.
11. Any fold the specialist made is called out explicitly in the prose section.

## Automatic failure

- A cycle exists in `depends_on`.
- A `depends_on` names an id that isn't in the list.
- The entire spec collapses into a single task.
- The JSON block is absent, or present but malformed.
