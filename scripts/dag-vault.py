#!/usr/bin/env python3
"""CLI for meter's M5 Vault (shadow mode only — see meter/vault.py).

Usage: python3 scripts/dag-vault.py <stats|purge>

Talks directly to the SQLite store rather than the daemon over HTTP — a purge
should work even if the daemon isn't currently running, and stats are a
point-in-time read that doesn't need the daemon's involvement either.

Always exits 0 — this is a reporting/maintenance command, not something that
should be able to break a session by failing.
"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

try:
    from meter import config as config_mod
    from meter import store
    from meter import vault
except Exception as exc:
    print(f"meter: could not load its own vault module ({exc!r}); nothing to report.",
          file=sys.stderr)
    sys.exit(0)


def cmd_stats() -> None:
    plugin_data_dir = config_mod.plugin_data_dir()
    conn = store.connect(plugin_data_dir)
    result = vault.stats(conn, plugin_data_dir)

    total_checks = result["total_checks"]
    agreement_pct = (result["agreed"] / total_checks * 100) if total_checks else None
    disk_mb = result["disk_bytes"] / (1024 ** 2)

    print(f"Vault entries: {result['entries']}  (hits: {result['hits']}, fails: {result['fails']})")
    print(f"Disk usage: {disk_mb:.1f} MB")
    if total_checks:
        print(f"Shadow-mode agreement: {result['agreed']}/{total_checks} "
              f"({agreement_pct:.0f}%), {result['disagreed']} disagreement(s)")
        if result["disagreed"]:
            print("  A disagreement means: had this been a real replay instead of shadow "
                  "measurement, the applied patch would NOT have matched what the worker "
                  "actually produced. This is exactly the evidence the spec's promotion path "
                  "(shadow -> rework_only -> full) is gated on — see meter-handoff.md Section 4/M5.")
    else:
        print("Shadow-mode agreement: no checks yet (no node has hit an existing vault entry's "
              "key twice)")
    print()
    print("Real replay (rework_only/full modes) is not implemented — vault.mode stays effectively "
          "\"shadow\" regardless of .dag/meter.json. See meter/vault.py's module docstring.")


def cmd_purge() -> None:
    plugin_data_dir = config_mod.plugin_data_dir()
    conn = store.connect(plugin_data_dir)
    cfg = config_mod.load_config(repo_root=config_mod.find_repo_root(Path.cwd()))
    max_gb = float((cfg.get("modules") or {}).get("vault", {}).get("max_gb", 2))

    before = vault.stats(conn, plugin_data_dir)
    result = vault.purge(conn, plugin_data_dir, max_gb=max_gb)
    after_mb = result["remaining_bytes"] / (1024 ** 2)

    if result["removed"] == 0:
        print(f"meter: vault already under its {max_gb:g}GB cap "
              f"({before['disk_bytes'] / (1024 ** 2):.1f} MB used) — nothing purged.")
    else:
        print(f"meter: purged {result['removed']} entry(ies), oldest-hit-first. "
              f"Now using {after_mb:.1f} MB (cap: {max_gb:g}GB).")


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("stats", "purge"):
        print("usage: python3 scripts/dag-vault.py <stats|purge>")
        return 0
    {"stats": cmd_stats, "purge": cmd_purge}[sys.argv[1]]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
