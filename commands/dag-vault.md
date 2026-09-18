---
description: Report meter's Vault (M5) stats — entries, disk usage, and shadow-mode agreement rate — or purge it under its size cap. Vault is shadow-mode only; it never applies a cached patch.
argument-hint: stats | purge
allowed-tools: ["Bash"]
---

# /dag-vault — Vault stats and purge

meter's Vault (see [Meter](../README.md#meter-on-by-default-measurement-only) in the README, and
`meter-handoff.md` Section 4/M5 for the full design) is implemented in **shadow mode only**: it
measures whether a cached patch would have matched what a task's worker actually produced, but it
never applies one and never skips a spawn. Real replay (rework_only/`full` modes) isn't built
yet — see `meter/vault.py`'s module docstring for why.

Target: `$ARGUMENTS` — must be `stats` or `purge`. If empty or anything else, ask which one was
meant rather than guessing.

This command's logic lives in the plugin's `scripts/dag-vault.py`, which is not necessarily under
the current directory (the user may be running this from any repo, not a checkout of yolo-dag
itself) — locate it and run it in one step:

```bash
script="${CLAUDE_PLUGIN_ROOT:-}/scripts/dag-vault.py"
[ -f "$script" ] || script="$(find "$HOME/.claude/plugins" -maxdepth 5 -path '*/yolo-dag/scripts/dag-vault.py' 2>/dev/null | head -n1)"
[ -f "$script" ] || script="scripts/dag-vault.py"
python3 "$script" <stats|purge>
```

Report its output verbatim. For `stats`, if it reports any disagreements, call that out plainly —
it means shadow mode caught a case where a cached patch would NOT have matched real work, which is
exactly the signal meter-handoff.md's promotion path (shadow, then rework_only, then `full`,
each a deliberate config change) is designed to surface before real replay is ever turned on.

The Vault stores diffs of the user's own source code (`${CLAUDE_PLUGIN_DATA}/meter/artifacts/`).
`purge` is the one-command way to clear space under `meter.modules.vault.max_gb`; it evicts the
least-recently-hit entries first and never fails the session if it can't run.
