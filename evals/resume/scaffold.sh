#!/usr/bin/env bash
# Seed a run that died mid-Phase-5: t1 (retryLimit) is already MERGED on the integration
# branch; t2 (logLevel) depends on t1 and is still pending; t3 (a doc comment) was running
# when the session died. The resume must not redo phases 1-4, must not re-execute t1, and
# t2's worker must build on a tree that already contains t1's work.
set -euo pipefail

git init -q -b main .
git config user.email "eval@example.com"
git config user.name "Eval Harness"

mkdir -p src
cat > src/config.js <<'JS'
// Central runtime configuration.
module.exports = {
  timeoutMs: 5000,
};
JS

cat > package.json <<'JSON'
{
  "name": "resume-fixture",
  "version": "1.0.0",
  "private": true,
  "scripts": { "test": "node -e \"const c=require('./src/config.js'); if(typeof c.timeoutMs!=='number'){process.exit(1)} console.log('ok')\"" }
}
JSON

echo ".dag/" > .gitignore
git add -A
git commit -qm "fixture: config module"
BASE_COMMIT="$(git rev-parse HEAD)"

# t1's work, already merged onto the integration branch by the dead session.
git checkout -qb dag/2026-01-01-resm
cat > src/config.js <<'JS'
// Central runtime configuration.
module.exports = {
  timeoutMs: 5000,
  retryLimit: 3,
};
JS
git add -A
git commit -qm "t1: add retryLimit setting"
T1_COMMIT="$(git rev-parse HEAD)"
git checkout -q main

RUN=".dag/runs/2026-01-01-resm"
mkdir -p "$RUN"

cat > "$RUN/request.md" <<'MD'
Add a retryLimit setting (default 3) and, building on it, a logLevel setting (default "info")
whose validation reuses the same guard style retryLimit's does. Also add a file-header doc
comment. Mode: micro (the request is the spec).
MD

cat > "$RUN/routing.md" <<'MD'
micro — no specialists; the request is the spec.
MD

cp "$RUN/request.md" "$RUN/merged-spec.md"

cat > "$RUN/tasks.json" <<JSON
{
  "tasks": [
    {
      "id": "t1",
      "title": "Add retryLimit setting",
      "description": "Add retryLimit (default 3) to src/config.js.",
      "acceptance_criteria": ["retryLimit === 3 by default", "npm test passes"],
      "depends_on": [],
      "files": ["src/config.js"],
      "status": "merged",
      "attempts": 1,
      "commit": "$T1_COMMIT"
    },
    {
      "id": "t2",
      "title": "Add logLevel setting",
      "description": "Add logLevel (default \\"info\\") to src/config.js, validated in the same guard style the existing settings use, and extend the npm test check to assert it.",
      "acceptance_criteria": ["logLevel === \\"info\\" by default", "npm test asserts logLevel is a string and passes"],
      "depends_on": ["t1"],
      "files": ["src/config.js", "package.json"],
      "status": "pending",
      "attempts": 0
    },
    {
      "id": "t3",
      "title": "Add file-header doc comment",
      "description": "Add a brief doc comment at the top of src/config.js describing each setting.",
      "acceptance_criteria": ["A header comment names every exported setting", "npm test passes"],
      "depends_on": ["t2"],
      "files": ["src/config.js"],
      "status": "running",
      "attempts": 1
    }
  ]
}
JSON

cat > "$RUN/run.json" <<JSON
{
  "run_id": "2026-01-01-resm",
  "mode": "micro",
  "plan_only": false,
  "base_branch": "main",
  "base_commit": "$BASE_COMMIT",
  "clean_start": true,
  "integration_branch": "dag/2026-01-01-resm",
  "phases": { "1": "skipped", "2": "skipped", "3": "skipped", "4": "complete", "5": "in_progress", "6": "pending" },
  "specialists": [],
  "spawns": [
    { "agent": "task-specialist", "model": "inherit", "phase": 4 },
    { "agent": "task-worker", "model": "inherit", "phase": 5 },
    { "agent": "task-reviewer", "model": "sonnet", "phase": 5 },
    { "agent": "task-worker", "model": "inherit", "phase": 5 }
  ],
  "degradations": []
}
JSON
