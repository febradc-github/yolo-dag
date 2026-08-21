#!/usr/bin/env bash
# Minimal real project: a string-utils module with a working test runner, so the pipeline
# has genuine conventions to match and a real suite Phase 6 can execute.
set -euo pipefail

git init -q -b main .
git config user.email "eval@example.com"
git config user.name "Eval Harness"

mkdir -p src test

cat > src/strings.js <<'JS'
function titleCase(input) {
  return input
    .split(' ')
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
    .join(' ');
}

module.exports = { titleCase };
JS

cat > test/strings.test.js <<'JS'
const assert = require('node:assert');
const { test } = require('node:test');
const { titleCase } = require('../src/strings.js');

test('titleCase capitalises each word', () => {
  assert.strictEqual(titleCase('hello world'), 'Hello World');
});
JS

cat > package.json <<'JSON'
{
  "name": "smoke-fixture",
  "version": "1.0.0",
  "private": true,
  "scripts": {
    "test": "node --test"
  }
}
JSON

git add -A
git commit -qm "fixture: string utils"
