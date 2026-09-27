"""M16 Fold: deterministic consolidation of spec-review findings.

**Class: mechanical.** This module replaces an LLM spawn with computed
output, which is only defensible because the work it replaces is genuinely
mechanical. `agents/spec-consolidator.md` says so itself: "This is a
mechanical merge, which is why you run on a small, fast model: you are
deduplicating and ranking work someone else already did. You are explicitly
**not** re-reviewing the deliverable, adding findings of your own, or
judging whether a finding is correct."

Deduplicating and ranking pre-scored, pre-structured records is a function,
not a judgment. `agents/spec-reviewer.md` pins the input shape hard enough
to parse:

    ### <short title>
    - **Angle:** <angle>
    - **Confidence:** <80-100>
    - **Issue:** <what is concretely wrong>
    - **Evidence:** <quote or command>
    - **Suggested resolution:** <what would fix it>

So Fold parses the three finding files, merges near-duplicates, ranks the
survivors, and writes the same consolidated markdown the agent would have
written — for zero tokens and zero latency. In `full` mode that removes up
to 24 spawns per run (8 specialists x 3 rounds).

**The one thing it does not do, and why that is the whole design.** The
consolidator's contract has a second clause Fold cannot honour by
computation: "Preserve genuine disagreements. If two reviewers reach
conflicting conclusions about the same part of the deliverable, don't
silently pick a side." Detecting that two findings *contradict* rather than
*duplicate* is semantic, and a heuristic that got it wrong would silently
drop one side of a real disagreement — the exact failure the clause exists
to prevent.

Fold therefore never decides a conflict. It detects the *conditions under
which one is possible* and **escalates the whole round to the real
`spec-consolidator` agent**, unchanged. The cheap deterministic path runs
when it is provably safe; the model runs when it isn't. This is the same
shape as M12 Distill's fidelity gate: take the cheap path only behind a
check that fails closed.

Escalation triggers (any one is enough):

- a finding file could not be read, or parsed to zero findings while
  holding non-whitespace text (a format this parser does not understand is
  not evidence of an empty review);
- two findings are *about the same subject* — overlap coefficient at or
  above `RELATED_OVERLAP` — **and** their suggested resolutions point in
  opposite directions (one adds, the other removes). That pattern is what a
  genuine disagreement looks like from outside the semantics.

Everything outside those triggers is a merge-and-rank Fold can do exactly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

# Two findings whose normalized word sets overlap at least this much are the
# same issue seen from two angles: merge them and record the corroboration.
MERGE_THRESHOLD = 0.55

# ...but a ratio alone is not enough. Two four-word findings can score 0.6 on
# two incidentally shared stems, so a merge also needs this many in common
# outright.
MIN_SHARED_STEMS = 3

# Whether two findings are *about the same thing* is a different question
# from whether they are *the same finding*, and it needs a different measure.
# Jaccard divides by the union, so it collapses when one reviewer writes at
# length and the other doesn't: a genuine add-a-retry / remove-the-retry
# disagreement scored 0.24 on Jaccard purely because one finding had twelve
# content stems and the other nine. The overlap coefficient divides by the
# smaller set instead, which is what "do these talk about the same subject"
# actually asks -- the same pair scores 0.44. Jaccard decides merges; this
# decides whether a pair is close enough that an opposing resolution means
# the reviewers disagree rather than that they addressed different things.
RELATED_OVERLAP = 0.40

# Corroboration bonus per extra angle, in confidence points. The
# spec-consolidator contract makes confidence the ranking key and
# corroboration "a tiebreaker", so this is deliberately small enough that it
# only reorders findings already close on confidence -- a single 95 still
# outranks a doubly-corroborated 85.
CORROBORATION_BONUS = 4.0

ANGLES = ("completeness", "consistency", "feasibility")

_STOPWORDS = frozenset("""
a an and are as at be been but by can could does do for from had has have he her his
if in into is it its may might must no not of on or our should so than that the their
them then there these they this those to was were what when which who will with would
you your it's dont don't
""".split())

_WORD_RE = re.compile(r"[a-z0-9_]+")

# Suffixes stripped before comparing, longest first. Two reviewers describing
# the same defect from different angles paraphrase it -- "the requirements
# say email must be unique" and "no uniqueness constraint on email" are the
# same finding, and on raw word sets they score 0.36, far under the merge
# threshold. Folding unique/uniqueness, user/users and requires/requirement
# to a common stem takes that same pair to 0.64. This is a deliberately crude
# stemmer: over-stemming costs at most a false merge of two findings already
# near-identical in wording, while under-stemming silently leaves duplicates
# in the list the specialist has to answer.
_SUFFIXES = ("ations", "ation", "ments", "ment", "ness", "ingly", "edly",
             "ing", "ies", "ers", "ed", "ly", "er", "es", "s")


def _stem(word: str) -> str:
    if len(word) < 5:
        return word
    for suffix in _SUFFIXES:
        if not word.endswith(suffix) or len(word) - len(suffix) < 3:
            continue
        # "address"/"status"/"analysis" are not plurals; stripping their tail
        # would merge unrelated words.
        if suffix == "s" and word.endswith(("ss", "us", "is")):
            continue
        word = word[: -len(suffix)]
        if suffix == "ies":
            word += "y"
        break
    # A trailing "e" survives some strips and not others ("requirement" ->
    # "require" but "requires" -> "requir"); dropping it reconciles them.
    return word[:-1] if len(word) > 3 and word.endswith("e") else word

# Directive polarity cues, used only to decide whether a *related* pair might
# be a disagreement worth a model's attention. Never used to merge, rank, or
# drop anything -- a false positive here costs one spawn, which is the safe
# direction.
# Stemmed at definition time, because the text they are matched against is
# stemmed too -- an unstemmed "remove" here silently never matches the
# "remov" that `_normalize` produces, which would make every disagreement
# check a no-op without failing anything.
_ADD_CUES = frozenset(_stem(w) for w in """
add introduce include require enforce split separate expand extend specify constrain
validate restrict tighten mandate
""".split())
_REMOVE_CUES = frozenset(_stem(w) for w in """
remove drop avoid delete merge collapse simplify inline consolidate relax loosen
omit weaken unify
""".split())


def _normalize(text: str) -> set[str]:
    """Content stems of `text`: lowercased, stopword-stripped, suffix-folded."""
    return {_stem(w) for w in _WORD_RE.findall(text.lower())
            if w not in _STOPWORDS and len(w) > 2}


def _jaccard(a: set[str], b: set[str]) -> float:
    """Symmetric similarity, used to decide whether two findings are the
    same finding."""
    if not a or not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _overlap(a: set[str], b: set[str]) -> float:
    """Szymkiewicz-Simpson overlap, used to decide whether two findings are
    about the same subject. Insensitive to one being much longer than the
    other, which Jaccard is not."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


@dataclass
class Finding:
    """One parsed finding. `angles` is a set because merging unions them."""
    title: str
    issue: str = ""
    evidence: str = ""
    resolution: str = ""
    confidence: float = 80.0
    angles: set[str] = field(default_factory=set)
    source_order: int = 0
    # Issue statements folded into this one by a merge, kept verbatim. A
    # merge is a similarity judgment, and this is what makes a wrong one
    # recoverable rather than lossy: the specialist still reads both.
    also_stated: list[str] = field(default_factory=list)
    _fingerprint: set[str] | None = field(default=None, repr=False, compare=False)

    @property
    def fingerprint(self) -> set[str]:
        """What duplicate detection compares. Title and issue describe *what
        is wrong*; evidence and resolution describe the reviewer's own
        reasoning, which legitimately differs between two angles that found
        the same defect -- including them would suppress real merges.

        Computed once: `dedupe` compares every surviving pair, and a merge
        rewrites the text it is derived from."""
        if self._fingerprint is None:
            self._fingerprint = _normalize(f"{self.title} {self.issue}")
        return self._fingerprint

    @property
    def score(self) -> float:
        return self.confidence + CORROBORATION_BONUS * (len(self.angles) - 1)

    def polarity(self) -> str | None:
        """`add`, `remove`, or None when the resolution is mixed or neutral."""
        words = _normalize(self.resolution)
        adds, removes = bool(words & _ADD_CUES), bool(words & _REMOVE_CUES)
        if adds and not removes:
            return "add"
        if removes and not adds:
            return "remove"
        return None


class EscalationRequired(Exception):
    """Fold declines this round; the caller must spawn `spec-consolidator`."""


_HEADING_RE = re.compile(r"^#{2,4}\s+(.*\S)\s*$")
_FIELD_RE = re.compile(r"^\s*[-*]\s*\*\*(?P<key>[A-Za-z /]+?)\s*:?\*\*\s*:?\s*(?P<value>.*)$")

_FIELD_ALIASES = {
    "angle": "angle", "angles": "angle",
    "confidence": "confidence",
    "issue": "issue", "problem": "issue",
    "evidence": "evidence",
    "suggested resolution": "resolution", "resolution": "resolution",
    "suggestion": "resolution", "fix": "resolution",
}


def parse_findings(text: str, *, default_angle: str | None = None) -> list[Finding]:
    """Parse one reviewer's findings file.

    Tolerant by design on everything that does not change meaning (heading
    depth, field order, missing optional fields, `**Angle:**` vs
    `**Angle**:`), and strict on nothing -- an unrecognised line is skipped
    rather than fatal. The caller decides what an empty result means; see
    `consolidate`, which treats "no findings parsed from non-empty text" as
    an escalation rather than a clean review.
    """
    findings: list[Finding] = []
    current: Finding | None = None
    current_key: str | None = None

    for raw in text.splitlines():
        heading = _HEADING_RE.match(raw)
        if heading:
            title = heading.group(1).strip()
            # Section headers a reviewer may wrap its list in are not findings.
            if title.lower().rstrip(":") in {"findings", "output", "summary", "review"}:
                current, current_key = None, None
                continue
            current = Finding(title=title, source_order=len(findings))
            if default_angle:
                current.angles.add(default_angle)
            findings.append(current)
            current_key = None
            continue

        if current is None:
            continue

        field_match = _FIELD_RE.match(raw)
        if field_match:
            key = _FIELD_ALIASES.get(field_match.group("key").strip().lower())
            value = field_match.group("value").strip()
            current_key = key
            if key == "angle":
                for angle in ANGLES:
                    if angle in value.lower():
                        current.angles.add(angle)
            elif key == "confidence":
                number = re.search(r"\d+(?:\.\d+)?", value)
                if number:
                    current.confidence = max(0.0, min(100.0, float(number.group())))
            elif key in {"issue", "evidence", "resolution"}:
                setattr(current, key, value)
            continue

        # A continuation line belongs to whichever field opened last.
        stripped = raw.strip()
        if stripped and current_key in {"issue", "evidence", "resolution"}:
            setattr(current, current_key, f"{getattr(current, current_key)} {stripped}".strip())

    return [f for f in findings if f.title]


def _merge_into(target: Finding, other: Finding) -> None:
    """Fold `other` into `target`, keeping the clearer phrasing.

    "Clearer" is resolved by confidence, then by length -- the consolidator
    contract says to keep the clearer phrasing without defining it, and a
    higher-confidence reviewer stating the issue at greater length is the
    best mechanical proxy available. **The loser's text is never discarded**:
    the resolution that is not kept is appended, and the issue statement that
    is not kept is preserved under "Also stated as". A merge is a similarity
    judgment made without understanding either finding, and this is what
    keeps a wrong one recoverable instead of lossy -- the specialist reads
    both statements and decides for itself whether they were the same thing.
    """
    target.angles |= other.angles
    if (other.confidence, len(other.issue)) > (target.confidence, len(target.issue)):
        demoted = target.issue
        target.title, target.issue = other.title, other.issue
        target._fingerprint = None  # derived from the text just replaced
    else:
        demoted = other.issue
    if demoted and demoted != target.issue and demoted not in target.also_stated:
        target.also_stated.append(demoted)
    target.confidence = max(target.confidence, other.confidence)
    if other.evidence and other.evidence not in target.evidence:
        target.evidence = f"{target.evidence} {other.evidence}".strip()
    if other.resolution and other.resolution not in target.resolution:
        target.resolution = f"{target.resolution} {other.resolution}".strip()


def _check_for_disagreement(findings: list[Finding]) -> None:
    """Escalate if any pair of findings about the same subject proposes
    opposite fixes. See the module docstring: this is a trigger, not a
    verdict -- Fold never decides who is right, it only declines to be the
    one that decides.

    Pairs that would merge are checked too, and deliberately: two reviewers
    describing the same defect and proposing opposite remedies is the
    clearest possible disagreement, not a duplicate to be folded away."""
    prints = [(f, f.fingerprint) for f in findings]
    for i, (a, fa) in enumerate(prints):
        for b, fb in prints[i + 1:]:
            overlap = _overlap(fa, fb)
            if overlap < RELATED_OVERLAP:
                continue
            pa, pb = a.polarity(), b.polarity()
            if pa and pb and pa != pb:
                raise EscalationRequired(
                    f"possible disagreement between {a.title!r} and {b.title!r} "
                    f"(subject overlap {overlap:.2f}, opposing resolutions: "
                    f"one {pa}s, one {pb}s)"
                )


def dedupe(findings: list[Finding]) -> list[Finding]:
    """Merge duplicates, then rank. Order within the input is preserved for
    findings that tie, so the output is stable across runs."""
    merged: list[Finding] = []
    for finding in findings:
        fingerprint = finding.fingerprint
        for existing in merged:
            shared = existing.fingerprint & fingerprint
            if (len(shared) >= MIN_SHARED_STEMS
                    and _jaccard(existing.fingerprint, fingerprint) >= MERGE_THRESHOLD):
                _merge_into(existing, finding)
                break
        else:
            merged.append(finding)
    return sorted(merged, key=lambda f: (-f.score, f.source_order))


def render(findings: list[Finding], *, specialist: str | None = None,
           round_no: int | None = None) -> str:
    """The consolidated markdown the specialist is resumed against. Shape
    matches `agents/spec-consolidator.md`'s "Output format" section: a single
    ranked list, one entry per deduplicated finding, each carrying the issue,
    the originating angle(s), and the confidence."""
    header = "# Consolidated findings"
    if specialist:
        header += f" — {specialist}"
    if round_no is not None:
        header += f" (round {round_no})"

    lines = [header, ""]
    if not findings:
        lines += ["No findings: all reviewers came back clean, or every finding "
                  "dissolved into a duplicate of another.", ""]
    else:
        lines += [
            f"{len(findings)} finding(s), most significant first. Consolidated "
            f"deterministically by meter's Fold module (no model call) — "
            f"dedupe + rank only; no finding was added, dropped, or judged.",
            "",
        ]
        for n, finding in enumerate(findings, 1):
            angles = ", ".join(sorted(finding.angles)) or "unattributed"
            corroborated = " (corroborated across angles)" if len(finding.angles) > 1 else ""
            lines.append(f"## {n}. {finding.title}")
            lines.append(f"- **Angle(s):** {angles}{corroborated}")
            lines.append(f"- **Confidence:** {finding.confidence:.0f}")
            if finding.issue:
                lines.append(f"- **Issue:** {finding.issue}")
            for other in finding.also_stated:
                lines.append(f"- **Also stated as:** {other}")
            if finding.evidence:
                lines.append(f"- **Evidence:** {finding.evidence}")
            if finding.resolution:
                lines.append(f"- **Suggested resolution:** {finding.resolution}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def consolidate(sources: Iterable[tuple[str, str | None]], *,
                specialist: str | None = None,
                round_no: int | None = None) -> tuple[str, int]:
    """Consolidate `(file_text, default_angle)` pairs into `(markdown, count)`.

    Raises `EscalationRequired` when the deterministic path is not provably
    safe; the caller must then spawn `spec-consolidator` exactly as before.
    """
    all_findings: list[Finding] = []
    for text, angle in sources:
        parsed = parse_findings(text, default_angle=angle)
        if not parsed and text.strip():
            raise EscalationRequired(
                "a finding file held text this parser produced no findings from"
            )
        for finding in parsed:
            finding.source_order = len(all_findings)
            all_findings.append(finding)

    _check_for_disagreement(all_findings)
    merged = dedupe(all_findings)
    return render(merged, specialist=specialist, round_no=round_no), len(merged)


def angle_from_filename(name: str) -> str | None:
    """`round-2-findings-consistency.md` -> `consistency`. The per-finding
    `**Angle:**` field wins when present; this is the fallback."""
    lowered = name.lower()
    for angle in ANGLES:
        if angle in lowered:
            return angle
    return None
