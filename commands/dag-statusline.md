---
description: Turn this repo's custom statusline on or off, or check whether it's currently configured.
argument-hint: on | off | status
allowed-tools: ["Bash"]
---

# /dag-statusline — Toggle the statusline

This repo ships a custom statusline in `.claude/settings.json` (set up via Claude Code's
`/statusline`). This command is its on/off switch — it never re-designs or regenerates the
statusline itself; for that, run `/statusline` again.

Target: `$ARGUMENTS` — must be `on`, `off`, or `status`. If empty or anything else, ask which one
was meant rather than guessing.

Run `python3 scripts/dag-statusline.py <on|off|status>` and report its output verbatim. Two things
worth saying yourself if they come up, since the script's output is terse:

- **`off` doesn't delete the configuration** — it moves it to a `_statusLineDisabled` key in
  `.claude/settings.json` so `on` can restore it exactly. If someone wants it gone for good, that's
  a manual edit, not what this command does.
- **A change here typically takes effect on the *next* session**, not the one you're running it
  from — same as most `settings.json` edits.
