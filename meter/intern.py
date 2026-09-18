"""M9 Intern: path and symbol interning (meter-v2-handoff.md Section 3, M9).

**Class: lossless** by construction — every codebook round-trips (encode then
decode reproduces the original exactly; see `tests/test_intern.py`'s
round-trip test, which is the module's own stated exit criterion).

**Scope implemented: the Dossier's own codebook (meter/dossier.py).** Files,
symbols, contracts, and acceptance criteria mentioned in a dossier get a
codebook table at the top of the document; references elsewhere in that same
document use the short code instead of the full name (never across a message
boundary where the table might be absent, per the spec's own comprehension
mitigation).

**Scope NOT implemented:** task prompts, receipts, review findings, and
contract text authored by the orchestrator or by agents are NOT interned.
That needs either new daemon-side rewriting hooks on content this plugin
doesn't currently intercept, or teaching every agent template a shared
codebook protocol — both are materially bigger, riskier changes than
anything else in this module. More importantly, the spec's exit criteria
("no measurable change in rework count or review findings") need a live A/B
harness this repo doesn't have; building the wider mechanism without a way
to detect a quality regression would be exactly the "real risk is
comprehension, not correctness" failure the spec itself warns about, shipped
with no instrument to catch it.

**Default is `basename` mode, not `codebook`.** Basename compression — strip
the directory prefix shared by every file in the node, state it once — is
"guaranteed safe... no comprehension risk at all" per the spec, and captures
roughly 60% of the saving. Full codebook mode (symbol/contract/AC codes, not
just paths) is implemented and available (`meter.modules.intern.mode:
"codebook"`) but defaults off until an A/B harness exists to confirm the
exit criteria — turning it on without that evidence is an operator's
judgment call, not something this plugin should default to.
"""

from __future__ import annotations

import os
import re
from typing import Any


def common_prefix(paths: list[str]) -> str:
    """Directory-boundary-aware common prefix — "src/a/b.py" and "src/ac.py"
    share no real directory prefix even though they share the string "src/a",
    which a naive `os.path.commonprefix` would wrongly return."""
    if not paths:
        return ""
    split = [p.split("/") for p in paths]
    prefix_parts: list[str] = []
    for parts in zip(*split):
        if len(set(parts)) == 1:
            prefix_parts.append(parts[0])
        else:
            break
    if not prefix_parts:
        return ""
    # A prefix must end at a directory boundary — never chop a path in the
    # middle of a filename that happens to share a full component with others.
    if len(prefix_parts) >= min(len(p) for p in split):
        prefix_parts = prefix_parts[:-1]
    return "/".join(prefix_parts) + "/" if prefix_parts else ""


def basename_compress(paths: list[str]) -> tuple[str, dict[str, str]]:
    """Returns (prefix, {full_path: suffix}). Applying `prefix + suffix`
    recovers `full_path` exactly for every path — the round-trip guarantee."""
    prefix = common_prefix(paths)
    return prefix, {p: p[len(prefix):] for p in paths}


class Codebook:
    """Files/symbols/contracts/ACs -> short codes, and back. Built fresh per
    dossier (never reused across nodes — a stale codebook decoding a newer
    document is exactly the kind of silent-wrongness this must never risk)."""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}       # "f1" -> path
        self.symbols: dict[str, str] = {}      # "s1" -> "Name (f1:10-20)"
        self.contracts: dict[str, str] = {}    # "c1" -> contract id
        self.acs: dict[str, str] = {}          # "a1" -> AC id
        self._file_by_path: dict[str, str] = {}
        self._contract_by_id: dict[str, str] = {}
        self._ac_by_id: dict[str, str] = {}

    def file_code(self, path: str) -> str:
        if path in self._file_by_path:
            return self._file_by_path[path]
        code = f"f{len(self.files) + 1}"
        self.files[code] = path
        self._file_by_path[path] = code
        return code

    def symbol_code(self, name: str, file_code: str, start: int, end: int) -> str:
        code = f"s{len(self.symbols) + 1}"
        self.symbols[code] = f"{name} ({file_code}:{start}-{end})"
        return code

    def contract_code(self, contract_id: str) -> str:
        if contract_id in self._contract_by_id:
            return self._contract_by_id[contract_id]
        code = f"c{len(self.contracts) + 1}"
        self.contracts[code] = contract_id
        self._contract_by_id[contract_id] = code
        return code

    def ac_code(self, ac_id: str) -> str:
        if ac_id in self._ac_by_id:
            return self._ac_by_id[ac_id]
        code = f"a{len(self.acs) + 1}"
        self.acs[code] = ac_id
        self._ac_by_id[ac_id] = code
        return code

    def render_table(self) -> str:
        lines = []
        if self.files:
            lines.append("FILES   " + "\n        ".join(f"{c} {p}" for c, p in self.files.items()))
        if self.symbols:
            lines.append("SYMBOLS " + "\n        ".join(f"{c} {s}" for c, s in self.symbols.items()))
        if self.contracts or self.acs:
            parts = [f"{c} {v}" for c, v in self.contracts.items()]
            parts += [f"AC {c} {v}" for c, v in self.acs.items()]
            lines.append("CONTRACTS " + "   ".join(parts))
        return "\n".join(lines)

    def to_dict(self) -> dict[str, dict[str, str]]:
        return {"files": self.files, "symbols": self.symbols,
                "contracts": self.contracts, "acs": self.acs}

    @classmethod
    def from_dict(cls, data: dict[str, dict[str, str]]) -> "Codebook":
        cb = cls()
        cb.files = dict(data.get("files", {}))
        cb.symbols = dict(data.get("symbols", {}))
        cb.contracts = dict(data.get("contracts", {}))
        cb.acs = dict(data.get("acs", {}))
        cb._file_by_path = {v: k for k, v in cb.files.items()}
        cb._contract_by_id = {v: k for k, v in cb.contracts.items()}
        cb._ac_by_id = {v: k for k, v in cb.acs.items()}
        return cb


_CODE_RE = re.compile(r"\b([fsca]\d+)\b")


def decode_reference(codebook: Codebook, code: str) -> str | None:
    """De-interning at a human-facing boundary (spec: "mandatory at every
    human boundary... a wire format, never a storage format"). Returns None
    for a code the codebook doesn't recognise, rather than guessing. A
    symbol's stored form embeds a file code (e.g. "refresh (f1:120-168)"),
    so its lookup is itself decoded once more before being returned —
    otherwise a fully "de-interned" document would still contain a raw f1."""
    if code in codebook.files:
        return codebook.files[code]
    if code in codebook.symbols:
        return decode_text(codebook, codebook.symbols[code])
    if code in codebook.contracts:
        return codebook.contracts[code]
    if code in codebook.acs:
        return codebook.acs[code]
    return None


def decode_text(codebook: Codebook, text: str) -> str:
    """Expands every recognised code in `text` back to its full form. A code
    string that collides with ordinary prose (rare, given the f/s/c/a
    prefix + digit shape) is only ever expanded when the codebook actually
    defines it — anything else passes through untouched."""
    def _replace(m: re.Match) -> str:
        decoded = decode_reference(codebook, m.group(1))
        return decoded if decoded is not None else m.group(0)
    return _CODE_RE.sub(_replace, text)
