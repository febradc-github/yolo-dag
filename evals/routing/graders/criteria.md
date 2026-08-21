# Grading criteria — routing

The request is deliberately a pure internal library refactor. Every optional specialist domain
is explicitly negated in the prompt.

## Must pass

1. **The always-on three are selected**: `architecture-specialist`, `research-specialist`, and
   `test-planning-specialist` all appear in the routing decision.
2. **`design-specialist` is skipped.** The request has no user-facing surface.
3. **`ux-copy-specialist` is skipped.** There is no user-facing text.
4. **`data-schema-specialist` is skipped.** Nothing is persisted.
5. **The skips are justified**, not silent — the response states which specialists were skipped
   and gives a reason, however brief.
6. **Routing is not put to the user as a question.** The pipeline's stated principle is that it
   decides and the user reviews the outcome. Asking "which specialists should I use?" fails.

## Should pass

7. `security-specialist` and `cost-estimation-specialist` are also skipped. These are judgment
   calls — a defensible case exists for running security on any code change — so treat this as a
   quality signal rather than a hard requirement.
8. The routing line names a count and reads as one compact statement rather than a section.

## Automatic failure

- All eight specialists are spawned. This is the exact `0.1.0` behaviour the routing step exists
  to replace.
- Fewer than three specialists are spawned; the always-on set is not optional.
- The response claims to have routed but never names which specialists it selected.
