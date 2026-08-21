#!/usr/bin/env bash
# Scaffold a tiny git repo with one config module that two independent tasks will both
# need to edit — the minimum setup that produces a genuine merge conflict in Phase 6.
set -euo pipefail

git init -q .
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
  "name": "conflict-fixture",
  "version": "1.0.0",
  "private": true,
  "scripts": {
    "test": "node -e \"const c=require('./src/config.js'); if(typeof c.timeoutMs!=='number') { console.error('timeoutMs missing'); process.exit(1);} console.log('ok');\""
  }
}
JSON

git add -A
git commit -qm "fixture: config module"
