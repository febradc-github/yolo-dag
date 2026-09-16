---
description: Turn the current project directory's statusline on or off, or check whether it's currently configured.
argument-hint: on | off | status
allowed-tools: ["Bash"]
---

# /dag-statusline — Toggle the statusline

Toggles the statusline in `.claude/settings.json` **in the current directory** — the repo the user
is actually working in when they run this, not yolo-dag's own repo (unless that happens to be the
same one). It operates on the directory Claude Code is running in, not on a git repository — the
two aren't the same thing, and no git repo is required.

Target: `$ARGUMENTS` — must be `on`, `off`, or `status`. If empty or anything else, ask which one
was meant rather than guessing.

This command's own logic lives in the plugin's `scripts/dag-statusline.py`, which is not
necessarily under the current directory (the user is very likely running this from some other
project, not a checkout of yolo-dag itself) — locate it and run it in one step:

```bash
script="${CLAUDE_PLUGIN_ROOT:-}/scripts/dag-statusline.py"
[ -f "$script" ] || script="$(find "$HOME/.claude/plugins" -maxdepth 5 -path '*/yolo-dag/scripts/dag-statusline.py' 2>/dev/null | head -n1)"
[ -f "$script" ] || script="scripts/dag-statusline.py"
python3 "$script" <on|off|status>
```

Report its output verbatim. Three things worth saying yourself if they come up, since the script's
output is terse:

- **`on` bootstraps a statusline if none exists yet in this directory** — it copies the plugin's
  own template script into `.claude/statusline.sh` here and points `.claude/settings.json` at it,
  rather than requiring `/statusline` to be run manually first. This only happens once per
  directory; it won't overwrite a statusline that's already configured (on or off).
- **`off` doesn't delete the configuration** — it moves it to a `_statusLineDisabled` key in
  `.claude/settings.json` so `on` can restore it exactly. If someone wants it gone for good, that's
  a manual edit, not what this command does.
- **A change here typically takes effect on the *next* session**, not the one you're running it
  from — same as most `settings.json` edits.
