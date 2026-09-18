"""Per-tool output compressors for M3b (meter-handoff.md Section 4/M3).

Each compressor is a pure function `compress(output: str, **kwargs) -> str | None`:
`None` means "leave the output alone" (under threshold, or the compressor isn't
confident enough to touch it). Every compressor obeys one absolute rule, stated
in the spec verbatim: **never remove an error message**. When a compressor
can't recognise the shape of what it's looking at well enough to be sure it
isn't a needed error, it returns `None` rather than guess — uncertainty is
cheap, a swallowed stack trace is not.

None of these write to disk themselves; `meter/clamp.py` does that (the full
original always lands in `.dag/runs/<run-id>/meter/tool-output/<id>.txt`
before a compressed replacement is ever returned to the caller).
"""
