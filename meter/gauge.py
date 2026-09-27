"""M17 Gauge: risk-proportional review depth for Phase 2.

**Class: heuristic, with a hard floor.** Phase 2 spawns a constant 3
`spec-reviewer` instances per specialist per round regardless of what is
being reviewed. A 300-word research note and a 4,000-word auth design get
identical scrutiny. In `full` mode that constant is the single largest line
item in the pipeline: 8 specialists x 3 rounds x 3 reviewers = 72 reviewer
spawns, each re-reading the same deliverable and the same request.

Gauge sizes that number to the deliverable instead, from signals available
before any model runs.

**How this differs from M8 Quorum, which is still disabled.** Quorum
narrows review by *measuring*, over 50+ full-quorum runs per domain, that
reviewers 2 and 3 contributed nothing — a bar this plugin structurally
cannot clear before it has that run history (see `meter/quorum.py`). Gauge
needs no history at all: it reads properties of the artifact in front of it.
The two are complementary and independent; Gauge shipping on does not
un-gate Quorum.

**The floor is the safety property.** Gauge can never reduce below
`full`-mode's 3 reviewers for a high-risk domain on its first round. A
security or data-schema deliverable that has not yet been reviewed once
gets the full panel, always, whatever its other signals say. Everything
Gauge does is a reduction from a ceiling it may not raise, and it is
inert when disabled — the caller falls back to the constant 3.

**Signals, and why each one is defensible:**

- *Domain risk.* Security and data-schema deliverables are where a missed
  finding is expensive and hard to walk back. Same rule the orchestrator's
  own routing uses to decide those specialists are worth spawning at all.
- *Round number.* Round 1 reviews a first draft; round 3 reviews something
  that already survived two adversarial passes and two revisions. Scrutiny
  that found nothing twice is the cheapest thing in the pipeline to narrow.
- *Prior round yield.* A round that produced zero findings is direct
  evidence about this specific deliverable, not a prior about its domain.
- *Hedge density.* Unresolved language ("TBD", "assume", "unclear",
  "probably") is what a completeness reviewer is for. Dense hedging earns
  back a reviewer; its absence is weak evidence of a settled deliverable.
- *Size.* A short deliverable has less surface for three readers to divide
  between them, and the fixed cost of re-reading the request dominates.

**What it never touches.** Phase 5's `task-reviewer` (already a single
reviewer), Phase 6's `integration-reviewer` (already one, and the only
whole-result gate), and the `--all` flag's guarantee. Gauge only ever
changes how many `spec-reviewer` instances Phase 2 spawns for one round.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Angles in yield order: the orchestrator drops from the back of this list
# when Gauge recommends fewer than three. Completeness-and-gaps is first
# because a missing section is the failure the later phases cannot recover
# from -- an inconsistency survives into Phase 3's reconciler, which is
# built to catch exactly that, while an omission reaches nobody.
ANGLE_PRIORITY = ("completeness", "feasibility", "consistency")

# Domains where a missed finding is expensive or hard to reverse. These get
# the full panel on round 1 in `full` mode no matter what else scores.
HIGH_RISK_DOMAINS = frozenset({"security-specialist", "data-schema-specialist"})

# Domains whose errors propagate into other specialists' work.
MEDIUM_RISK_DOMAINS = frozenset({
    "architecture-specialist", "research-specialist", "test-planning-specialist",
    "cost-estimation-specialist",
})

# Per-mode ceiling. Gauge reduces from this and may never exceed it.
MODE_CEILING = {"full": 3, "lite": 2, "micro": 0}

# A deliverable at or under this many words has too little surface to divide
# three ways; the fixed cost of three agents re-reading the request dominates.
SMALL_WORDS = 600
LARGE_WORDS = 2500

_HEDGE_RE = re.compile(
    r"\b(tbd|todo|unclear|unknown|unspecified|assum\w*|presum\w*|probably|"
    r"possibly|perhaps|might|maybe|roughly|approximat\w*|to be (?:decided|determined)|"
    r"open question|not sure|unverified|placeholder)\b",
    re.IGNORECASE,
)

# Hedges per 1,000 words above which a deliverable reads as unsettled.
HEDGE_DENSITY_HIGH = 8.0


@dataclass
class Assessment:
    """What Gauge recommends for one review round, and why."""
    reviewers: int
    angles: tuple[str, ...]
    single_pass: bool
    score: float
    reasons: list[str] = field(default_factory=list)
    floor_applied: bool = False

    @property
    def spawns(self) -> int:
        """Actual agent spawns implied -- `single_pass` folds several angles
        into one agent, which is the whole point of that flag."""
        return 1 if (self.single_pass and self.reviewers) else self.reviewers

    def summary(self) -> str:
        if not self.reviewers:
            return "review skipped: " + "; ".join(self.reasons)
        shape = f"{self.reviewers} angle(s) in 1 pass" if self.single_pass \
            else f"{self.reviewers} reviewer(s)"
        return f"{shape} ({', '.join(self.angles)}): " + "; ".join(self.reasons)


def word_count(text: str) -> int:
    return len(text.split())


def hedge_density(text: str) -> float:
    """Hedges per 1,000 words. Zero for an empty deliverable rather than a
    division error -- an empty deliverable is a different problem, and the
    caller's own escalation path handles it."""
    words = word_count(text)
    if words < 50:
        return 0.0
    return len(_HEDGE_RE.findall(text)) * 1000.0 / words


def assess(deliverable: str, *, domain: str, mode: str = "full", round_no: int = 1,
           prior_findings: int | None = None, force_full: bool = False) -> Assessment:
    """Recommend a reviewer count for one Phase 2 round.

    `prior_findings` is the consolidated count from the previous round, or
    None on round 1. `force_full` is the `--all` flag and any other caller
    that has already decided it wants the full panel.
    """
    ceiling = MODE_CEILING.get(mode, 3)
    if ceiling == 0:
        return Assessment(0, (), False, 0.0, ["micro mode runs no spec review"])

    if force_full:
        return Assessment(ceiling, ANGLE_PRIORITY[:ceiling], False, 100.0,
                          ["full panel forced by --all"], floor_applied=True)

    words = word_count(deliverable)
    density = hedge_density(deliverable)
    reasons: list[str] = []

    # Base score by domain risk.
    if domain in HIGH_RISK_DOMAINS:
        score, why = 70.0, f"{domain} is a high-risk domain"
    elif domain in MEDIUM_RISK_DOMAINS:
        score, why = 50.0, f"{domain} is a medium-risk domain"
    else:
        score, why = 32.0, f"{domain} is a low-risk domain"
    reasons.append(why)

    # Rounds 2+ review something that already survived adversarial review.
    if round_no >= 3:
        score -= 22.0
        reasons.append("round 3 (survived two prior review rounds)")
    elif round_no == 2:
        score -= 12.0
        reasons.append("round 2 (survived one prior review round)")

    # Direct evidence about this deliverable beats any prior about its domain.
    if prior_findings is not None:
        if prior_findings == 0:
            score -= 18.0
            reasons.append("previous round found nothing")
        elif prior_findings >= 4:
            score += 12.0
            reasons.append(f"previous round found {prior_findings}")

    # Unsettled language is what a completeness reviewer exists to catch.
    if density >= HEDGE_DENSITY_HIGH:
        score += 14.0
        reasons.append(f"dense hedging ({density:.0f}/1k words)")

    # Size: less surface to divide, and the fixed re-read cost dominates.
    if words <= SMALL_WORDS:
        score -= 14.0
        reasons.append(f"short deliverable ({words} words)")
    elif words >= LARGE_WORDS:
        score += 10.0
        reasons.append(f"large deliverable ({words} words)")

    if score >= 60:
        reviewers = 3
    elif score >= 34:
        reviewers = 2
    else:
        reviewers = 1
    reviewers = min(reviewers, ceiling)

    # The floor: a high-risk domain's first draft always gets the full panel.
    floor_applied = False
    if domain in HIGH_RISK_DOMAINS and round_no == 1 and reviewers < ceiling:
        reviewers = ceiling
        floor_applied = True
        reasons.append("floor: high-risk domain, first round")

    # One agent covering several angles reads the deliverable and the request
    # once instead of N times. Only safe where the angles' independence is
    # not itself load-bearing: not on a high-risk domain, not on a large
    # deliverable, and never when the full panel is warranted.
    single_pass = (
        reviewers == 2
        and words <= SMALL_WORDS
        and domain not in HIGH_RISK_DOMAINS
        and not floor_applied
    )
    if single_pass:
        reasons.append("angles folded into one pass (small, low-risk)")

    return Assessment(reviewers, ANGLE_PRIORITY[:reviewers], single_pass, score,
                      reasons, floor_applied)


def constant_baseline(mode: str = "full") -> int:
    """What Phase 2 would have spawned without Gauge -- the comparison the
    orchestrator reports as a saving."""
    return MODE_CEILING.get(mode, 3)
