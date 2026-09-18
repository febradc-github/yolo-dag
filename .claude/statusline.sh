#!/usr/bin/env bash
# yolo-dag statusline — project-scoped, see .claude/settings.json.
#
# Prints, left to right: model | meter:on/off | git branch[*dirty] |
# session:[bar] pct% (5-hour rate-limit window) | context:<pie glyph> pct%.
#
# A "count of running background agents" segment was requested earlier but is
# intentionally NOT implemented: the documented Claude Code statusline JSON
# payload has no field for it. Revisit if Claude Code ever adds one.
#
# Hard rule: every segment is best-effort and fails SILENTLY. Missing data,
# a missing tool (jq/git/curl), or a command error just drops that segment —
# this script runs on every prompt render and must never error out or hang.
# The meter health check is the only network-ish call and is capped at 300ms.

set -u

input="$(cat 2>/dev/null || true)"

have_jq=0
command -v jq >/dev/null 2>&1 && have_jq=1

# Distinct from have_jq: whether the input actually parses. jget() can't tell
# "the field is null" apart from "jq couldn't even read this" — both print
# nothing — and that distinction matters wherever a filter defaults a missing
# value to something other than empty (see the context-window segment below).
valid_json=0
if [ "$have_jq" -eq 1 ]; then
    printf '%s' "$input" | jq -e . >/dev/null 2>&1 && valid_json=1
fi

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
c_model=$'\033[38;5;79m'       # teal   (~#5fb0a6) — model name
c_branch=$'\033[38;5;178m'     # gold   (~#e2a33f) — git branch
c_dirty=$'\033[38;5;203m'      # soft red — dirty-tree marker only
c_meter=$'\033[2;38;5;141m'    # dim + violet (~#9a8fd1) — meter, deliberately subtle
c_usage_ok=$'\033[38;5;109m'   # calm teal-grey — usage < 70%
c_usage_warn=$'\033[38;5;178m' # gold — usage 70-89%
c_usage_crit=$'\033[38;5;203m' # soft red — usage >= 90%
c_sepcol=$'\033[2m'            # dim separator

segments=()

# usage_color <pct> — prints the threshold color for a 0-100 percentage,
# shared by the session bar and the context pie so both read consistently.
usage_color() {
    pct="$1"
    if [ "$pct" -ge 90 ]; then printf '%s' "$c_usage_crit"
    elif [ "$pct" -ge 70 ]; then printf '%s' "$c_usage_warn"
    else printf '%s' "$c_usage_ok"
    fi
}

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

# --- 4. current session usage, as a bar (5-hour rate-limit window) ---
# `rate_limits` is present only for claude.ai Pro/Max subscribers (or behind a
# spend-limit gateway), and — unlike context_window — it doesn't exist in the
# payload AT ALL until the first API response of the session. That's the gap
# reported: "when there is no conversation, session isn't showing." It can't
# be defaulted to 0% the way context was: the 5-hour window is account-wide,
# not reset by starting a new conversation, so at the start of a fresh
# session it's very likely genuinely non-zero, just not yet known here.
#
# The honest fix is to remember the last real reading rather than invent one:
# every time a live value comes in, cache it alongside its own `resets_at`;
# when the payload has no live value, fall back to that cache but ONLY while
# `resets_at` hasn't passed yet — a cached value from an already-reset window
# would be wrong, not just stale. A cached fallback is marked with a leading
# `~` so it's never mistaken for a live reading.
session_cache_dir="$HOME/.cache/yolo-dag-statusline"
session_cache_file="$session_cache_dir/session_rate_limit"

session_pct="$(jget '.rate_limits.five_hour.used_percentage // empty')"
session_is_live=0
if [ -n "$session_pct" ] && [ "$session_pct" != "null" ]; then
    session_is_live=1
    session_resets_at="$(jget '.rate_limits.five_hour.resets_at // empty')"
    if [ -n "$session_resets_at" ] && [ "$session_resets_at" != "null" ]; then
        mkdir -p "$session_cache_dir" 2>/dev/null || true
        printf '%s\n%s\n' "$session_pct" "$session_resets_at" > "$session_cache_file" 2>/dev/null || true
    fi
else
    now="$(date +%s 2>/dev/null || true)"
    case "$now" in
        ''|*[!0-9]*) : ;;  # can't tell the time — don't trust a cached value at all
        *)
            if [ -f "$session_cache_file" ]; then
                cached_pct="$(sed -n '1p' "$session_cache_file" 2>/dev/null || true)"
                cached_resets_at="$(sed -n '2p' "$session_cache_file" 2>/dev/null || true)"
                case "$cached_resets_at" in
                    ''|*[!0-9]*) : ;;
                    *) [ "$now" -lt "$cached_resets_at" ] && session_pct="$cached_pct" ;;
                esac
            fi
            ;;
    esac
fi

if [ -n "$session_pct" ] && [ "$session_pct" != "null" ]; then
    session_int="$(printf '%.0f' "$session_pct" 2>/dev/null || true)"
    case "$session_int" in
        ''|*[!0-9]*) : ;;  # non-numeric after rounding — drop the segment
        *)
            [ "$session_int" -gt 100 ] && session_int=100
            width=10
            filled=$(( session_int * width / 100 ))
            [ "$filled" -gt "$width" ] && filled=$width
            empty=$(( width - filled ))
            bar=""
            i=0
            while [ "$i" -lt "$filled" ]; do bar="${bar}█"; i=$((i + 1)); done
            i=0
            while [ "$i" -lt "$empty" ]; do bar="${bar}░"; i=$((i + 1)); done
            c_session="$(usage_color "$session_int")"
            approx=""
            [ "$session_is_live" -eq 0 ] && approx="~"
            segments+=("${c_session}session:[${bar}] ${approx}${session_int}%${c_reset}")
            ;;
    esac
fi

# --- 5. context window usage, as a pie-style glyph. Defaults to 0 rather
# than omitting when the field is null/absent — that's what a fresh session
# and a just-/clear'd one both look like (no API call yet this "session"),
# and 0% is the true value in both cases, not a stale one. (This is also what
# Claude Code's own example status-line scripts do: `used_percentage // 0`,
# not `// empty`.) `session` below stays omit-on-absent deliberately — that
# field means "not applicable to this account," not "zero."
# A real circle can't render in a terminal; the closest honest equivalent is
# the standard quarter-circle glyph set (○ ◔ ◑ ◕ ●), rounded to the nearest
# quarter, with the exact percentage printed alongside so nothing is lost to
# the rounding — the glyph is symbolic, the number next to it is exact. ---
if [ "$valid_json" -eq 1 ]; then
    ctx_pct="$(jget '.context_window.used_percentage // 0')"
    ctx_int="$(printf '%.0f' "$ctx_pct" 2>/dev/null || true)"
    case "$ctx_int" in
        ''|*[!0-9]*) : ;;  # jq produced garbage despite being present — drop the segment
        *)
            [ "$ctx_int" -gt 100 ] && ctx_int=100
            if [ "$ctx_int" -ge 88 ]; then pie="●"
            elif [ "$ctx_int" -ge 63 ]; then pie="◕"
            elif [ "$ctx_int" -ge 38 ]; then pie="◑"
            elif [ "$ctx_int" -ge 13 ]; then pie="◔"
            else pie="○"
            fi
            c_ctx="$(usage_color "$ctx_int")"
            segments+=("${c_ctx}context:${pie} ${ctx_int}%${c_reset}")
            ;;
    esac
fi
# valid_json == 0 (no jq, or the input itself didn't parse): we genuinely
# can't tell "fresh session" from "couldn't read this," so omit rather than
# default to 0 — that default only applies once jq has actually confirmed
# the field is null/absent in otherwise-valid JSON.

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
