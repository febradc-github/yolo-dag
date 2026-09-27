#!/usr/bin/env python3
"""CLI for meter's M16 Fold — deterministic consolidation of spec-review
findings, replacing the `spec-consolidator` spawn when it is provably safe
to do so (see meter/fold.py for the full design and the escalation rules).

Usage:
  python3 scripts/dag-fold.py <out-file> <findings-file>...

Exit codes are the whole interface, because the orchestrator branches on
them:

  0  Consolidated. <out-file> is written and `CONSOLIDATED: <n>` is the last
     line on stdout, exactly as `spec-consolidator` would have reported it.
     Do not spawn the agent.
  2  Escalate. Nothing written. The reason is on stderr. Spawn
     `spec-consolidator` exactly as the unmodified Phase 2 loop does.

Any other failure also exits 2: the fallback path is always correct, just
more expensive, so every unknown resolves toward it.
"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))

ESCALATE = 2

try:
    from meter import config as config_mod
    from meter import fold
except Exception as exc:  # pragma: no cover - import guard
    print(f"meter: could not load its own fold module ({exc!r}); "
          f"spawn spec-consolidator.", file=sys.stderr)
    sys.exit(ESCALATE)


def _enabled() -> bool:
    try:
        cfg = config_mod.load_config(config_mod.find_repo_root(Path.cwd()))
        if not config_mod.is_enabled(cfg):
            return False
        return bool((cfg.get("modules") or {}).get("fold", {}).get("enabled"))
    except Exception:
        return False


def main() -> int:
    args = sys.argv[1:]
    if len(args) < 2:
        print("usage: python3 scripts/dag-fold.py <out-file> <findings-file>...",
              file=sys.stderr)
        return ESCALATE

    out_path, sources = Path(args[0]), args[1:]

    if not _enabled():
        print("meter: fold is disabled; spawn spec-consolidator.", file=sys.stderr)
        return ESCALATE

    payload: list[tuple[str, str | None]] = []
    for name in sources:
        path = Path(name)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"meter: could not read {name} ({exc}); spawn spec-consolidator.",
                  file=sys.stderr)
            return ESCALATE
        payload.append((text, fold.angle_from_filename(path.name)))

    # The specialist name and round are recoverable from the conventional
    # path shape `.../specialists/<name>/round-<r>-findings-<angle>.md`, and
    # are cosmetic when they are not -- never a reason to escalate.
    specialist, round_no = None, None
    try:
        parts = Path(sources[0]).resolve().parts
        if "specialists" in parts:
            specialist = parts[parts.index("specialists") + 1]
        stem = Path(sources[0]).name
        if stem.startswith("round-"):
            round_no = int(stem.split("-")[1])
    except (ValueError, IndexError):
        pass

    try:
        markdown, count = fold.consolidate(payload, specialist=specialist,
                                            round_no=round_no)
    except fold.EscalationRequired as exc:
        print(f"meter: fold escalating ({exc}); spawn spec-consolidator.",
              file=sys.stderr)
        return ESCALATE
    except Exception as exc:  # pragma: no cover - defensive
        print(f"meter: fold failed ({exc!r}); spawn spec-consolidator.", file=sys.stderr)
        return ESCALATE

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(markdown, encoding="utf-8")
    except OSError as exc:
        print(f"meter: could not write {out_path} ({exc}); spawn spec-consolidator.",
              file=sys.stderr)
        return ESCALATE

    print(f"meter: folded {len(sources)} finding file(s) into {out_path} "
          f"with no model call.")
    print(f"CONSOLIDATED: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
