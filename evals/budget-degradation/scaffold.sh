#!/usr/bin/env bash
# Seed a full-mode run resumed mid-Phase-2 whose spawn ledger already sits just under the
# 120-unit ceiling. Finishing Phase 2 at full scrutiny (3 rounds x 4 agents per specialist)
# cannot fit; the resume must degrade in the stated order and say so, not overspend or quit.
set -euo pipefail

git init -q -b main .
git config user.email "eval@example.com"
git config user.name "Eval Harness"

mkdir -p src
cat > src/report.js <<'JS'
module.exports = { render: (rows) => rows.map((r) => r.join('\t')).join('\n') };
JS

cat > package.json <<'JSON'
{
  "name": "budget-fixture",
  "version": "1.0.0",
  "private": true,
  "scripts": { "test": "node -e \"require('./src/report.js'); console.log('ok')\"" }
}
JSON

echo ".dag/" > .gitignore
git add -A
git commit -qm "fixture: report module"
BASE_COMMIT="$(git rev-parse HEAD)"

RUN=".dag/runs/2026-01-01-bdgt"
mkdir -p "$RUN/specialists/architecture-specialist" \
         "$RUN/specialists/research-specialist" \
         "$RUN/specialists/test-planning-specialist"

cat > "$RUN/request.md" <<'MD'
Add a CSV export mode to the report renderer, selectable per call, with escaping handled
correctly and covered by tests. Mode: full.
MD

cat > "$RUN/routing.md" <<'MD'
Routing to 3: architecture, research, test-planning — skipping design/ux-copy (no user-facing
surface), data-schema (no persistence), security (no untrusted input change), cost (no infra
delta). Mode: full.
MD

for s in architecture-specialist research-specialist test-planning-specialist; do
  cat > "$RUN/specialists/$s/round-1.md" <<MD
# $s — round 1 deliverable (seeded)
A plausible first-round deliverable for the CSV export request. Round 1 review completed;
rounds 2-3 had not started when the session died.
MD
done

# Ledger just under the 120-unit ceiling: 115 inherit spawns (1.0 each) + 6 sonnet (0.5)
# + 5 haiku (0.1) = 118.5 units. Written with python3 to avoid hand-typing 126 entries.
BASE_COMMIT="$BASE_COMMIT" python3 - "$RUN/run.json" <<'PY'
import json, os, sys

spawns = (
    [{"agent": "architecture-specialist", "model": "inherit", "phase": 1}]
    + [{"agent": "spec-reviewer", "model": "inherit", "phase": 2}] * 114
    + [{"agent": "spec-reviewer", "model": "sonnet", "phase": 2}] * 6
    + [{"agent": "spec-consolidator", "model": "haiku", "phase": 2}] * 5
)

run = {
    "run_id": "2026-01-01-bdgt",
    "mode": "full",
    "plan_only": False,
    "base_branch": "main",
    "base_commit": os.environ["BASE_COMMIT"],
    "clean_start": True,
    "integration_branch": None,
    "phases": {"1": "complete", "2": "in_progress", "3": "pending",
               "4": "pending", "5": "pending", "6": "pending"},
    "specialists": [
        {"name": "architecture-specialist", "spawn": "dead-session", "rounds": 1, "status": "revising"},
        {"name": "research-specialist", "spawn": "dead-session", "rounds": 1, "status": "revising"},
        {"name": "test-planning-specialist", "spawn": "dead-session", "rounds": 1, "status": "revising"},
    ],
    "spawns": spawns,
    "degradations": [],
}

with open(sys.argv[1], "w") as f:
    json.dump(run, f, indent=2)
PY
