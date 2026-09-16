---
description: Turn meter on or off, or check whether it's currently running — meter is on by default; this is the opt-out (and opt-back-in) switch.
argument-hint: on | off | status
allowed-tools: ["Bash"]
---

# /dag-meter — Toggle meter

meter (see [Meter](../README.md#meter-on-by-default-measurement-only) in the README, and
`meter-handoff.md` for the full design) is on by default. This command is its switch.

Target: `$ARGUMENTS` — must be `on`, `off`, or `status`. If empty or anything else, ask which one
was meant rather than guessing.

This command's own logic lives in the plugin's `scripts/dag-meter.py`, which is not necessarily
under the current directory (the user may be running this from any repo, not a checkout of
yolo-dag itself) — locate it and run it in one step:

```bash
script="${CLAUDE_PLUGIN_ROOT:-}/scripts/dag-meter.py"
[ -f "$script" ] || script="$(find "$HOME/.claude/plugins" -maxdepth 5 -path '*/yolo-dag/scripts/dag-meter.py' 2>/dev/null | head -n1)"
[ -f "$script" ] || script="scripts/dag-meter.py"
python3 "$script" <on|off|status>
```

Report its output verbatim — it already explains what happened in plain language, including the
one case worth calling out yourself if it comes up: **turning off doesn't always take effect for
the rest of the *current* session.**
Claude Code's hooks fire unconditionally once registered; `off` also asks the running daemon to
shut down so this session stops recording too, but if that daemon can't be reached the config
change is still saved (off for every future session) while this one keeps going until it ends.
The script's own output says which of those happened — don't paraphrase it into a flat "done".
