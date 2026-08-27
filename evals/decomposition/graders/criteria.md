# Grading criteria — decomposition

The spec has a deliberate shape: one foundational piece (storage/schema) and two consumers of it
(the endpoint, the nightly job). The interesting question is not whether the specialist notices
the ordering — it's whether it *removes* it. The schema is exactly the kind of shared boundary
that should become a written contract both consumers code against in parallel, rather than an
edge they queue behind.

## Must pass

1. **A fenced `json` block is present** and parses as JSON with top-level `tasks` and `contracts`
   arrays.
2. **Every task has** `id`, `title`, `description`, `acceptance_criteria` (non-empty array),
   `owns` (array), `contracts` (array), and `depends_on` (array).
3. **Ids are unique**, and every id appearing in any `depends_on` exists in the task list.
4. **The graph is acyclic.**
5. **No two tasks own the same file.** Every path in any `owns` list appears exactly once across
   the whole graph.
6. **The subscription record is a shared contract**, defined in the top-level `contracts` array
   with a single owner task, and named in the `contracts` list of every task that reads or writes
   it. A decomposition that leaves the schema implicit and coordinates by ordering alone has
   missed the point of the change being tested.
7. **Every task bound by that contract carries its verbatim text in its own `description`**, along
   with what it owns of it, what it consumes unchanged, and the `contract-conflict` instruction.
8. **Every `depends_on` entry carries a `reason`** — the edge had to be justified as not
   designable-away, not just asserted.
9. **The endpoint work and the nightly-job work are separate tasks.** They are genuinely
   independent of each other and folding them together is a decomposition failure.
10. **No task description references another task** by number, name, or ordinal ("after task 2",
    "using the schema from task 1"). The contract or the edge carries that relationship.

## Should pass

11. **Most tasks sit at wave 0** (empty `depends_on`). With the schema expressed as a contract,
    the endpoint and the nightly job can both be written immediately; an edge here needs the
    `reason` to say something stronger than "it reads the table".
12. `owns` entries look like plausible repo paths consistent with the spec's `db/` and `server/`
    modules, not invented directories.
13. Acceptance criteria are checkable statements a reviewer with no other context could grade,
    not restatements of the task title.
14. The prose section calls out any fold made, and any edge declared, with its reason.

## Automatic failure

- A cycle exists in `depends_on`.
- A `depends_on` names an id that isn't in the list.
- Two tasks own the same file.
- A task names a contract that isn't defined, or a contract has no owner or two owners.
- The entire spec collapses into a single task.
- The JSON block is absent, or present but malformed.
