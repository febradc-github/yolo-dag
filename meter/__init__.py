"""meter — token-accounting wrapper for yolo-dag.

M0 (Ledger) only: measures per-node token spend and changes no pipeline behaviour.
See meter-handoff.md for the full design. Nothing past M0 ships until M0 is
producing real numbers (meter-handoff.md, Section 0, Rule 1).
"""

SCHEMA_VERSION = 1
PLUGIN_VERSION = "0.1.0"  # meter subsystem version, independent of plugin.json
