#!/usr/bin/env python3
"""CLI for meter's M17 Gauge — recommends how many `spec-reviewer` instances
one Phase 2 round actually needs (see meter/gauge.py for the signals, the
safety floor, and how this differs from the still-disabled M8 Quorum).

Usage:
  python3 scripts/dag-gauge.py <deliverable-file> <domain> [options]

Options:
  --mode full|lite|micro     Run mode (default: full).
  --round N                  Review round number, 1-based (default: 1).
  --prior-findings N         Consolidated finding count from the previous
                             round. Omit on round 1.
  --all                      The orchestrator's --all flag: force the panel.

Prints one `GAUGE:` line the orchestrator parses, plus a human-readable
rationale:

  GAUGE: reviewers=2 angles=completeness,feasibility single_pass=no

Exit code is always 0. A recommendation is advice, and a failure to produce
one is not an error — on any internal failure it prints the unmodified
constant (`reviewers=3` in full mode) so the caller's behaviour is exactly
what it would have been without this module.
"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT))


def _fallback(mode: str) -> int:
    from_ceiling = {"full": 3, "lite": 2, "micro": 0}.get(mode, 3)
    angles = ("completeness", "feasibility", "consistency")[:from_ceiling]
    print(f"GAUGE: reviewers={from_ceiling} angles={','.join(angles)} single_pass=no")
    print("meter: gauge unavailable or disabled; using the unmodified constant.")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if len(args) < 2:
        print("usage: python3 scripts/dag-gauge.py <deliverable-file> <domain> "
              "[--mode M] [--round N] [--prior-findings N] [--all]", file=sys.stderr)
        return _fallback("full")

    deliverable_path, domain = args[0], args[1]
    mode, round_no, prior, force_full = "full", 1, None, False

    rest = args[2:]
    i = 0
    while i < len(rest):
        flag = rest[i]
        if flag == "--all":
            force_full = True
            i += 1
            continue
        if i + 1 >= len(rest):
            break
        value = rest[i + 1]
        try:
            if flag == "--mode":
                mode = value
            elif flag == "--round":
                round_no = max(1, int(value))
            elif flag == "--prior-findings":
                prior = max(0, int(value))
        except ValueError:
            pass
        i += 2

    try:
        from meter import config as config_mod
        from meter import gauge
    except Exception:
        return _fallback(mode)

    try:
        cfg = config_mod.load_config(config_mod.find_repo_root(Path.cwd()))
        module_cfg = (cfg.get("modules") or {}).get("gauge", {})
        if not config_mod.is_enabled(cfg) or not module_cfg.get("enabled"):
            return _fallback(mode)
        if module_cfg.get("min_reviewers") is not None:
            force_full = force_full or bool(module_cfg.get("force_full"))
    except Exception:
        return _fallback(mode)

    try:
        text = Path(deliverable_path).read_text(encoding="utf-8")
    except OSError:
        # A deliverable we cannot read is not one we can size review for.
        return _fallback(mode)

    try:
        result = gauge.assess(text, domain=domain, mode=mode, round_no=round_no,
                               prior_findings=prior, force_full=force_full)
        floor = int(module_cfg.get("min_reviewers") or 1)
        if result.reviewers and result.reviewers < floor:
            result.reviewers = min(floor, gauge.MODE_CEILING.get(mode, 3))
            result.angles = gauge.ANGLE_PRIORITY[:result.reviewers]
            result.single_pass = False
            result.reasons.append(f"raised to the configured floor of {floor}")
    except Exception:
        return _fallback(mode)

    print(f"GAUGE: reviewers={result.reviewers} "
          f"angles={','.join(result.angles) or 'none'} "
          f"single_pass={'yes' if result.single_pass else 'no'}")
    baseline = gauge.constant_baseline(mode)
    print(f"meter: {result.summary()}")
    if result.spawns < baseline:
        print(f"meter: {baseline - result.spawns} fewer reviewer spawn(s) than the "
              f"{baseline}-reviewer constant.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
