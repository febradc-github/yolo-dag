#!/usr/bin/env bash
# yolo-dag statusline — project-scoped, see .claude/settings.json.
#
# Prints, left to right: model | meter:on/off | git branch[*dirty] |
# context-window usage bar.
#
# A 5th segment (count of currently-running background agents) was
# requested but is intentionally NOT implemented: the documented Claude
# Code statusline JSON payload has no field for it. The payload's `agent`
# object only describes the current session's own --agent identity
# (name/type) when Claude was started with --agent — it is not a live
# count of concurrent background agents, and nothing else in the payload
# carries one either. A shell script also has no independent way to
# introspect that. Revisit if Claude Code ever adds such a field.
#
# Hard rule: every segment is best-effort and fails SILENTLY. Missing data,
# a missing tool (jq/git/curl), or a command error just drops that segment —
# this script runs on every prompt render and must never error out or hang.
# The meter health check is the only network-ish call and is capped at 300ms.

set -u

input="$(cat 2>/dev/null || true)"

have_jq=0
command -v jq >/dev/null 2>&1 && have_jq=1

# jget <jq-filter> — prints the filtered value, or empty on any failure
# (no jq installed, invalid JSON, absent key). Never errors the script.
jget() {
    if [ "$have_jq" -eq 0 ]; then
        printf ''
        return
    fi
    printf '%s' "$input" | jq -r "$1" 2>/dev/null
}

# --- palette (256-color; echoes assets/workflow.svg's grey/teal/gold/violet) ---
c_reset=$'\033[0m'
c_model=$'\033[38;5;79m'     # teal   (~#5fb0a6) — model name
c_branch=$'\033[38;5;178m'   # gold   (~#e2a33f) — git branch
c_dirty=$'\033[38;5;203m'    # soft red — dirty-tree marker only
c_meter=$'\033[2;38;5;141m'  # dim + violet (~#9a8fd1) — meter, deliberately subtle
c_ctx_ok=$'\033[38;5;109m'   # calm teal-grey — context usage < 70%
c_ctx_warn=$'\033[38;5;178m' # gold — context usage 70-89%
c_ctx_crit=$'\033[38;5;203m' # soft red — context usage >= 90%
c_sepcol=$'\033[2m'          # dim separator

segments=()

# --- 1. model ---
model="$(jget '.model.display_name // .model.id // empty')"
if [ -n "$model" ] && [ "$model" != "null" ]; then
    segments+=("${c_model}${model}${c_reset}")
fi

# --- 2. meter live status (always shown, off if unreachable/unconfigured) ---
plugin_data="${CLAUDE_PLUGIN_DATA:-$HOME/.cache/yolo-dag-meter-fallback}"
port_file="$plugin_data/meter/port"
token_file="$plugin_data/meter/token"
meter_state="off"
if [ -f "$port_file" ] && [ -f "$token_file" ] && command -v curl >/dev/null 2>&1; then
    port="$(cat "$port_file" 2>/dev/null || true)"
    token="$(cat "$token_file" 2>/dev/null || true)"
    if [ -n "$port" ] && [ -n "$token" ]; then
        if curl -sf -o /dev/null -m 0.3 --connect-timeout 0.3 \
             -H "X-Dag-Meter-Token: ${token}" \
             "http://127.0.0.1:${port}/health" 2>/dev/null; then
            meter_state="on"
        fi
    fi
fi
segments+=("${c_meter}meter:${meter_state}${c_reset}")

# Resolve the repo root by walking up from the JSON's cwd for .git —
# used by the branch segment below. Empty if cwd is unknown or not
# inside any repo.
cwd="$(jget '.workspace.current_dir // .cwd // empty')"
[ "$cwd" = "null" ] && cwd=""
repo_root=""
if [ -n "$cwd" ] && [ -d "$cwd" ]; then
    d="$cwd"
    while [ -n "$d" ] && [ "$d" != "/" ]; do
        if [ -e "$d/.git" ]; then
            repo_root="$d"
            break
        fi
        d="$(dirname -- "$d" 2>/dev/null || true)"
    done
fi

# --- 3. git branch + dirty marker (omitted entirely if not in a git repo) ---
if [ -n "$repo_root" ] && command -v git >/dev/null 2>&1; then
    branch="$(git -C "$repo_root" --no-optional-locks rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
    if [ "$branch" = "HEAD" ]; then
        # detached HEAD — fall back to a short sha rather than the literal "HEAD"
        branch="$(git -C "$repo_root" --no-optional-locks rev-parse --short HEAD 2>/dev/null || true)"
    fi
    if [ -n "$branch" ]; then
        dirty=""
        status_out="$(git -C "$repo_root" --no-optional-locks status --porcelain 2>/dev/null || true)"
        [ -n "$status_out" ] && dirty="*"
        segments+=("${c_branch}${branch}${c_dirty}${dirty}${c_reset}")
    fi
fi

# --- 4. context window usage, as a progress bar (omitted if the field is
# absent — e.g. before the first API response, when used_percentage is null) ---
ctx_pct="$(jget '.context_window.used_percentage // empty')"
if [ -n "$ctx_pct" ] && [ "$ctx_pct" != "null" ]; then
    ctx_int="$(printf '%.0f' "$ctx_pct" 2>/dev/null || true)"
    case "$ctx_int" in
        ''|*[!0-9]*) : ;;  # non-numeric after rounding — drop the segment
        *)
            [ "$ctx_int" -gt 100 ] && ctx_int=100
            width=10
            filled=$(( ctx_int * width / 100 ))
            [ "$filled" -gt "$width" ] && filled=$width
            empty=$(( width - filled ))
            bar=""
            i=0
            while [ "$i" -lt "$filled" ]; do bar="${bar}█"; i=$((i + 1)); done
            i=0
            while [ "$i" -lt "$empty" ]; do bar="${bar}░"; i=$((i + 1)); done
            if [ "$ctx_int" -ge 90 ]; then
                c_ctx="$c_ctx_crit"
            elif [ "$ctx_int" -ge 70 ]; then
                c_ctx="$c_ctx_warn"
            else
                c_ctx="$c_ctx_ok"
            fi
            segments+=("${c_ctx}[${bar}] ${ctx_int}%${c_reset}")
            ;;
    esac
fi

# --- assemble ---
out=""
i=0
for seg in "${segments[@]:-}"; do
    [ -z "$seg" ] && continue
    if [ -z "$out" ]; then
        out="$seg"
    else
        out="${out}${c_sepcol} · ${c_reset}${seg}"
    fi
    i=$((i + 1))
done

printf '%s\n' "$out"
