#!/usr/bin/env python3
"""CLI for meter's M12 Distill cache (see meter/distill.py for the full design
and why the actual compile/fidelity-gate work happens in the orchestrator
skill, not here — this script only owns the hash-keyed cache).

Usage:
  python3 scripts/dag-distill.py check <spec-file>
      Prints the cached brief to stdout and exits 0 if this exact spec's
      content is already cached; exits 1 (nothing on stdout) on a miss.
  python3 scripts/dag-distill.py store <spec-file> <brief-file>
      Caches <brief-file>'s content, keyed by <spec-file>'s content hash.
  python3 scripts/dag-distill.py record-fetch <spec-file>
      Records that an agent read the full spec despite a brief existing for
      it — the fetch-rate signal meter-v2-handoff.md Section 3/M12 asks for.
  python3 scripts/dag-distill.py stats <spec-file>
      Prints delivery/fetch counts for this spec's cache entry.

Talks directly to the SQLite store, not the daemon over HTTP — none of this
is hot-path, and it needs to work even if the daemon isn't currently running.
Always exits 0 for `store`/`record-fetch`/`stats` (fire-and-forget bookkeeping
commands); `check` is the one command whose exit code is meaningful (cache
hit vs. miss) and is documented as such above.
"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

try:
    from meter import config as config_mod
    from meter import distill
    from meter import store
except Exception as exc:
    print(f"meter: could not load its own distill module ({exc!r}).", file=sys.stderr)
    sys.exit(0)


def _read_spec(path_arg: str) -> str | None:
    try:
        return Path(path_arg).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"meter: could not read {path_arg}: {exc}", file=sys.stderr)
        return None


def _distill_enabled() -> bool:
    repo_root = config_mod.find_repo_root(Path.cwd())
    cfg = config_mod.load_config(repo_root)
    if not config_mod.is_enabled(cfg):
        return False
    return bool((cfg.get("modules") or {}).get("distill", {}).get("enabled"))


def cmd_check(spec_path: str) -> int:
    if not _distill_enabled():
        return 1  # off switch: every check is a miss, same effect as "never cached"
    spec_text = _read_spec(spec_path)
    if spec_text is None:
        return 1
    conn = store.connect(config_mod.plugin_data_dir())
    entry = distill.get_cached_brief(conn, spec_text)
    if entry is None:
        return 1
    print(entry["brief"])
    return 0


def cmd_store(spec_path: str, brief_path: str) -> int:
    if not _distill_enabled():
        print("meter: distill is disabled; not caching.")
        return 0
    spec_text = _read_spec(spec_path)
    brief_text = _read_spec(brief_path)
    if spec_text is None or brief_text is None:
        return 0
    conn = store.connect(config_mod.plugin_data_dir())
    distill.store_brief(conn, spec_text=spec_text, brief=brief_text,
                         source_path=str(Path(spec_path).resolve()))
    print(f"meter: cached brief for {spec_path} ({len(brief_text)} chars, "
          f"was {len(spec_text)} chars).")
    return 0


def cmd_record_fetch(spec_path: str) -> int:
    spec_text = _read_spec(spec_path)
    if spec_text is None:
        return 0
    conn = store.connect(config_mod.plugin_data_dir())
    distill.record_full_spec_fetch(conn, spec_text)
    print(f"meter: recorded a full-spec fetch for {spec_path}.")
    return 0


def cmd_stats(spec_path: str) -> int:
    spec_text = _read_spec(spec_path)
    if spec_text is None:
        return 0
    conn = store.connect(config_mod.plugin_data_dir())
    rate = distill.fetch_rate(conn, spec_text)
    if rate is None:
        print(f"meter: no delivered brief on record for {spec_path} yet.")
    else:
        print(f"meter: full-spec fetch rate for {spec_path}: {rate * 100:.0f}%")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print("usage: python3 scripts/dag-distill.py <check|store|record-fetch|stats> <spec-file> [brief-file]")
        return 0

    command = args[0]
    if command == "check" and len(args) == 2:
        return cmd_check(args[1])
    if command == "store" and len(args) == 3:
        return cmd_store(args[1], args[2])
    if command == "record-fetch" and len(args) == 2:
        return cmd_record_fetch(args[1])
    if command == "stats" and len(args) == 2:
        return cmd_stats(args[1])

    print("usage: python3 scripts/dag-distill.py <check|store|record-fetch|stats> <spec-file> [brief-file]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
