---
description: Clean up after yolo-dag runs — prune stale worktrees, remove finished runs' state directories, and (only with confirmation) delete their dag/* integration branches.
argument-hint: A run id to clean one run, or --all to clean every finished run. Never touches an in-flight run.
allowed-tools: ["Read", "Glob", "Bash", "AskUserQuestion"]
---

# /dag-clean — Clean up run debris

Runs leave debris by design — worktrees from crashed sessions, `.dag/runs/` directories, and
`dag/<run-id>` branches. This command removes what is safe to remove, and asks before touching
anything that is someone's only copy of work.

Target: `$ARGUMENTS` — a run id for one run, `--all` for every *finished* run (every phase in
its `run.json` complete or skipped). Never clean a run that is unfinished — it may be resumed —
unless the user names it explicitly, and then say what resuming will no longer be possible.

## Process

1. **List before removing.** Enumerate what would be cleaned and show it first: stale worktrees
   (`git worktree list` — anything under the Agent tool's worktree area or left by a dead run),
   run directories under `.dag/runs/`, and `dag/*` branches. If there is nothing to clean, say
   so in one line.
2. **Worktrees.** `git worktree prune` first (clears dangling metadata), then
   `git worktree remove` each worktree whose commit is already merged into its run's
   integration branch. **Keep any worktree belonging to an UNMERGED task** — per `tasks.json`,
   that worktree is the only copy of that work — and say where it is instead of removing it.
3. **Run directories.** Remove the targeted runs' directories under `.dag/runs/`. This deletes
   the audit trail (specs, review rounds, task graph); it does not touch code.
4. **Branches — ask first.** Deleting a `dag/<run-id>` branch deletes the run's actual product.
   Ask per branch (or once for the batch with the branches named) before `git branch -D`, and
   never touch the repo's base branch. A branch the user has merged elsewhere is safe to offer;
   an unmerged one deserves a warning to that effect in the question.
5. **Report** what was removed and what was deliberately kept, with paths.
