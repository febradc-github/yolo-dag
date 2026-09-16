#!/usr/bin/env bash
# yolo-dag statusline — project-scoped, see .claude/settings.json.
#
# Prints, left to right: model | cwd basename | git branch[*dirty] |
# dag:<run_id>(<mode>) for an in-flight yolo-dag run | meter:on/off.
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
c_dir=$'\033[38;5;250m'      # light grey — cwd basename
c_branch=$'\033[38;5;178m'   # gold   (~#e2a33f) — git branch
c_dirty=$'\033[38;5;203m'    # soft red — dirty-tree marker only
c_dag=$'\033[38;5;103m'      # muted grey (~#726d7d) — active dag run
c_meter=$'\033[2;38;5;141m'  # dim + violet (~#9a8fd1) — meter, deliberately subtle
c_sepcol=$'\033[2m'          # dim separator

segments=()

# --- 1. model ---
model="$(jget '.model.display_name // .model.id // empty')"
if [ -n "$model" ] && [ "$model" != "null" ]; then
    segments+=("${c_model}${model}${c_reset}")
fi

# --- 2. current directory (basename only) ---
cwd="$(jget '.workspace.current_dir // .cwd // empty')"
[ "$cwd" = "null" ] && cwd=""
if [ -n "$cwd" ]; then
    dirbase="$(basename -- "$cwd" 2>/dev/null || true)"
    [ -n "$dirbase" ] && segments+=("${c_dir}${dirbase}${c_reset}")
fi

# Resolve the repo root by walking up for .git — shared by the branch and
# dag segments below. Empty if cwd is unknown or not inside any repo.
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

# --- 4. active yolo-dag run (omitted if none, or the latest is fully done) ---
if [ -n "$repo_root" ] && [ -d "$repo_root/.dag/runs" ] && [ "$have_jq" -eq 1 ]; then
    latest_run_json="$(ls -t "$repo_root"/.dag/runs/*/run.json 2>/dev/null | head -n1 || true)"
    if [ -n "$latest_run_json" ] && [ -f "$latest_run_json" ]; then
        in_flight="$(jq -r '
            if ((.phases // {}) | to_entries
                | map(select(.value != "complete" and .value != "skipped"))
                | length) > 0
            then "1" else "0" end
        ' "$latest_run_json" 2>/dev/null || true)"
        if [ "$in_flight" = "1" ]; then
            run_id="$(jq -r '.run_id // empty' "$latest_run_json" 2>/dev/null || true)"
            [ "$run_id" = "null" ] && run_id=""
            if [ -z "$run_id" ]; then
                run_id="$(basename -- "$(dirname -- "$latest_run_json")" 2>/dev/null || true)"
            fi
            mode="$(jq -r '.mode // empty' "$latest_run_json" 2>/dev/null || true)"
            [ "$mode" = "null" ] && mode=""
            if [ -n "$run_id" ] && [ -n "$mode" ]; then
                segments+=("${c_dag}dag:${run_id}(${mode})${c_reset}")
            elif [ -n "$run_id" ]; then
                segments+=("${c_dag}dag:${run_id}${c_reset}")
            fi
        fi
    fi
fi

# --- 5. meter live status (omitted unless port+token files exist) ---
plugin_data="${CLAUDE_PLUGIN_DATA:-$HOME/.cache/yolo-dag-meter-fallback}"
port_file="$plugin_data/meter/port"
token_file="$plugin_data/meter/token"
if [ -f "$port_file" ] && [ -f "$token_file" ] && command -v curl >/dev/null 2>&1; then
    port="$(cat "$port_file" 2>/dev/null || true)"
    token="$(cat "$token_file" 2>/dev/null || true)"
    if [ -n "$port" ] && [ -n "$token" ]; then
        if curl -sf -o /dev/null -m 0.3 --connect-timeout 0.3 \
             -H "X-Dag-Meter-Token: ${token}" \
             "http://127.0.0.1:${port}/health" 2>/dev/null; then
            segments+=("${c_meter}meter:on${c_reset}")
        else
            segments+=("${c_meter}meter:off${c_reset}")
        fi
    fi
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
