#!/usr/bin/env bash
# Seed a run that completed Phases 1-4, whose persisted tasks.json contains a cycle.
# On-disk state can be corrupted or hand-edited; a resume must re-validate before executing.
set -euo pipefail

git init -q -b main .
git config user.email "eval@example.com"
git config user.name "Eval Harness"

mkdir -p src
cat > src/util.js <<'JS'
module.exports = { noop: () => {} };
JS

cat > package.json <<'JSON'
{
  "name": "cycle-fixture",
  "version": "1.0.0",
  "private": true,
  "scripts": { "test": "node -e \"require('./src/util.js'); console.log('ok')\"" }
}
JSON

echo ".dag/" > .gitignore
git add -A
git commit -qm "fixture: util module"

RUN=".dag/runs/2026-01-01-cycl"
mkdir -p "$RUN"
BASE_COMMIT="$(git rev-parse HEAD)"

cat > "$RUN/request.md" <<'MD'
Add three small utility helpers to src/util.js, each with a test.
Mode: micro (the request is the spec).
MD

cat > "$RUN/routing.md" <<'MD'
micro — no specialists; the request is the spec.
MD

cp "$RUN/request.md" "$RUN/merged-spec.md"

# The planted defect: t1 -> t2 -> t3 -> t1 is a cycle.
cat > "$RUN/tasks.json" <<'JSON'
{
  "tasks": [
    {
      "id": "t1",
      "title": "Add a clamp helper",
      "description": "Add clamp(n, lo, hi) to src/util.js with a test.",
      "acceptance_criteria": ["clamp(5,1,3) === 3", "npm test passes"],
      "depends_on": ["t3"],
      "files": ["src/util.js"],
      "status": "pending",
      "attempts": 0
    },
    {
      "id": "t2",
      "title": "Add a sum helper",
      "description": "Add sum(list) to src/util.js with a test.",
      "acceptance_criteria": ["sum([1,2,3]) === 6", "npm test passes"],
      "depends_on": ["t1"],
      "files": ["src/util.js"],
      "status": "pending",
      "attempts": 0
    },
    {
      "id": "t3",
      "title": "Add a mean helper",
      "description": "Add mean(list) to src/util.js with a test.",
      "acceptance_criteria": ["mean([2,4]) === 3", "npm test passes"],
      "depends_on": ["t2"],
      "files": ["src/util.js"],
      "status": "pending",
      "attempts": 0
    }
  ]
}
JSON

cat > "$RUN/run.json" <<JSON
{
  "run_id": "2026-01-01-cycl",
  "mode": "micro",
  "plan_only": false,
  "base_branch": "main",
  "base_commit": "$BASE_COMMIT",
  "clean_start": true,
  "integration_branch": null,
  "phases": { "1": "skipped", "2": "skipped", "3": "skipped", "4": "complete", "5": "pending", "6": "pending" },
  "specialists": [],
  "spawns": [{ "agent": "task-specialist", "model": "inherit", "phase": 4 }],
  "degradations": []
}
JSON
