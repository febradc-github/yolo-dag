/brainstorm Replace the hand-rolled exponential backoff in our internal HTTP retry helper with a
shared utility. It is a pure library refactor: no user-facing surface, no new persistence, no
change to auth or to what data we handle, and it runs on existing infrastructure with no added
cost. Keep the public function signature identical.

Stop after the orchestrator has announced its routing decision and fanned out. Do not run the
full pipeline.
