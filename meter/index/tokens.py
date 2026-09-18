"""Offline token estimation for budgeting (meter-handoff.md Section 6.5):
"a calibrated heuristic... plus or minus 10 percent is fine for budgeting. Do
not call any network endpoint on the hot path for this, ever." Uncalibrated
for now — nobody has run the calibration pass against measured Ledger data
yet — so this is a plain chars-per-token constant, not the "calibrated"
version the spec eventually wants.

This is an ESTIMATE for ranking/budgeting only. Ground truth for reporting
always comes from the Ledger (meter/ledger.py's transcript-based accounting);
never print this where a measurement is expected.
"""

from __future__ import annotations

_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)
