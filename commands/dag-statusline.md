---
description: Turn the current project directory's statusline on or off, or check whether it's currently configured.
argument-hint: on | off | status
allowed-tools: ["Bash"]
---

# /dag-statusline — Toggle the statusline

Toggles whatever statusline is configured in `.claude/settings.json` **in the current directory**
(set up via Claude Code's own `/statusline`) — this is its on/off switch, not its designer; for
that, run `/statusline` again. It operates on the directory Claude Code is running in, not on a
git repository — the two aren't the same thing, and no git repo is required.

Target: `$ARGUMENTS` — must be `on`, `off`, or `status`. If empty or anything else, ask which one
was meant rather than guessing.

Run `python3 scripts/dag-statusline.py <on|off|status>` and report its output verbatim. Two things
worth saying yourself if they come up, since the script's output is terse:

- **`off` doesn't delete the configuration** — it moves it to a `_statusLineDisabled` key in
  `.claude/settings.json` so `on` can restore it exactly. If someone wants it gone for good, that's
  a manual edit, not what this command does.
- **A change here typically takes effect on the *next* session**, not the one you're running it
  from — same as most `settings.json` edits.
