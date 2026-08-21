#!/usr/bin/env bash
# Seed a run mid-Phase-5: t1 is BLOCKED at its 3-attempt cap, t2 depends on t1, t3 is an
# unrelated ready task. The resume must SKIP t2 without running it, leave t1 alone, and
# still execute and merge t3.
set -euo pipefail

git init -q -b main .
git config user.email "eval@example.com"
git config user.name "Eval Harness"

mkdir -p src
cat > src/flags.js <<'JS'
// Feature flags and limits.
module.exports = {
  maxRetries: 3,
};
JS

cat > package.json <<'JSON'
{
  "name": "blocked-fixture",
  "version": "1.0.0",
  "private": true,
  "scripts": { "test": "node -e \"const f=require('./src/flags.js'); if(typeof f.maxRetries!=='number'){process.exit(1)} console.log('ok')\"" }
}
JSON

echo ".dag/" > .gitignore
git add -A
git commit -qm "fixture: flags module"

BASE_COMMIT="$(git rev-parse HEAD)"
git branch dag/2026-01-01-blkd

RUN=".dag/runs/2026-01-01-blkd"
mkdir -p "$RUN"

cat > "$RUN/request.md" <<'MD'
Three independent-ish changes to the flags module. Mode: micro (the request is the spec).
MD

cat > "$RUN/routing.md" <<'MD'
micro — no specialists; the request is the spec.
MD

cp "$RUN/request.md" "$RUN/merged-spec.md"

cat > "$RUN/tasks.json" <<'JSON'
{
  "tasks": [
    {
      "id": "t1",
      "title": "Wire flags to the licensing daemon",
      "description": "Load flag defaults from the company licensing daemon's socket at /var/run/licensed.sock at module load.",
      "acceptance_criteria": ["Flags load from the licensing daemon", "npm test passes"],
      "depends_on": [],
      "files": ["src/flags.js"],
      "status": "blocked",
      "attempts": 3,
      "blocker": "environment",
      "findings": "No such daemon or socket exists in this environment; three independent workers confirmed."
    },
    {
      "id": "t2",
      "title": "Cache daemon-loaded flags",
      "description": "Cache the daemon-loaded flag values with a 60s TTL.",
      "acceptance_criteria": ["Second read within 60s does not hit the daemon", "npm test passes"],
      "depends_on": ["t1"],
      "files": ["src/flags.js"],
      "status": "pending",
      "attempts": 0
    },
    {
      "id": "t3",
      "title": "Add exportLimit flag",
      "description": "Add an exportLimit setting (default 100) to src/flags.js, and extend the npm test check to assert it is a number.",
      "acceptance_criteria": ["exportLimit === 100 by default", "npm test asserts exportLimit is a number and passes"],
      "depends_on": [],
      "files": ["src/flags.js", "package.json"],
      "status": "pending",
      "attempts": 0
    }
  ]
}
JSON

cat > "$RUN/run.json" <<JSON
{
  "run_id": "2026-01-01-blkd",
  "mode": "micro",
  "plan_only": false,
  "base_branch": "main",
  "base_commit": "$BASE_COMMIT",
  "clean_start": true,
  "integration_branch": "dag/2026-01-01-blkd",
  "phases": { "1": "skipped", "2": "skipped", "3": "skipped", "4": "complete", "5": "in_progress", "6": "pending" },
  "specialists": [],
  "spawns": [
    { "agent": "task-specialist", "model": "inherit", "phase": 4 },
    { "agent": "task-worker", "model": "inherit", "phase": 5 },
    { "agent": "task-reviewer", "model": "sonnet", "phase": 5 },
    { "agent": "task-worker", "model": "inherit", "phase": 5 },
    { "agent": "task-reviewer", "model": "sonnet", "phase": 5 },
    { "agent": "task-worker", "model": "inherit", "phase": 5 }
  ],
  "degradations": []
}
JSON
