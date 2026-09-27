"""Guards on this plugin's three token-cost layers (v0.17.0).

Every number here is a ceiling someone has to consciously raise, which is the whole
point: each layer was cut once already (0.16.0) and each one drifts back one paragraph
at a time if nothing measures it.

- **Always-on** — agent/skill/command descriptions load into every session in any repo
  where the plugin is installed, used or not. It is the only cost with no opt-out.
- **Resident** — the orchestrator spine stays in context for a whole run and is re-paid
  on every compaction.
- **Per-spawn** — an agent's body is re-paid in full on every spawn of it, so its real
  cost is bytes x spawns-per-run. `spec-reviewer` at ~40 spawns in a full run is the
  single largest line item in the plugin.
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_validator():
    spec = importlib.util.spec_from_file_location("dag_validate", ROOT / "scripts" / "validate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


V = _load_validator()


class ValidatorRunsCleanTests(unittest.TestCase):
    def test_repo_passes_its_own_validator(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "validate.py")],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class AgentBodyBudgetTests(unittest.TestCase):
    """The per-spawn layer."""

    def setUp(self):
        V.errors.clear()
        V.warnings.clear()

    def _bodies(self):
        out = {}
        for path in sorted((ROOT / "agents").glob("*.md")):
            text = path.read_text(encoding="utf-8")
            out[path.stem] = text[text.find("\n---\n", 4) + 5:]
        return out

    def test_every_agent_body_is_within_budget(self):
        for stem, body in self._bodies().items():
            size = len(body.encode("utf-8"))
            budget = V.AGENT_BODY_BUDGETS.get(stem, V.AGENT_BODY_DEFAULT)
            self.assertLessEqual(size, budget, f"agents/{stem}.md is {size} bytes (budget {budget})")

    def test_an_oversized_body_is_a_failure_not_a_warning(self):
        V.check_agent_body_budgets({"spec-reviewer": {"body": "x" * 99_999}})
        self.assertTrue(V.errors, "a bloated high-multiplicity agent must fail the build")
        self.assertIn("re-paid in full on every spawn", V.errors[0])

    def test_an_unlisted_agent_falls_back_to_the_default_budget(self):
        V.check_agent_body_budgets({"some-new-agent": {"body": "x" * (V.AGENT_BODY_DEFAULT + 1)}})
        self.assertTrue(V.errors)
        self.assertIn("unlisted", V.errors[0])

    def test_the_highest_multiplicity_agent_has_the_tightest_relative_budget(self):
        """spec-reviewer is spawned ~40x per full run and task-specialist once. If
        spec-reviewer's budget ever exceeds task-specialist's, the table has stopped
        tracking spawn multiplicity and is just describing the files."""
        self.assertLess(V.AGENT_BODY_BUDGETS["spec-reviewer"],
                        V.AGENT_BODY_BUDGETS["task-specialist"])


class AlwaysOnBudgetTests(unittest.TestCase):
    """The always-on layer: paid in every session, in every repo, used or not."""

    # Measured total at 0.17.0 is ~6.4k chars. The ceiling is deliberately close to it.
    TOTAL_CHAR_CEILING = 6600

    def _descriptions(self):
        import re
        out = []
        for pattern in ("agents/*.md", "skills/*/SKILL.md", "commands/*.md"):
            for path in sorted(ROOT.glob(pattern)):
                text = path.read_text(encoding="utf-8")
                for key in ("description", "argument-hint"):
                    match = re.search(rf"^{key}: (.*)$", text, re.M)
                    if match:
                        out.append((path.relative_to(ROOT), key, match.group(1)))
        return out

    def test_total_always_on_text_is_bounded(self):
        total = sum(len(value) for _, _, value in self._descriptions())
        self.assertLessEqual(
            total, self.TOTAL_CHAR_CEILING,
            f"descriptions total {total} chars (~{total // 4} tokens) charged to every "
            f"session in every repo with this plugin installed")

    def test_no_single_description_dominates(self):
        for rel, key, value in self._descriptions():
            if key != "description":
                continue
            limit = (V.MAX_SKILL_DESCRIPTION if rel.name == "SKILL.md"
                     else V.MAX_COMMAND_DESCRIPTION if rel.parts[0] == "commands"
                     else V.MAX_AGENT_DESCRIPTION)
            self.assertLessEqual(len(value), limit, f"{rel}: {len(value)} chars > {limit}")


class ResidentBudgetTests(unittest.TestCase):
    """The resident layer: re-paid on every compaction of a long run."""

    def test_spine_is_within_budget(self):
        spine = (ROOT / "skills" / "orchestrator" / "SKILL.md").read_text(encoding="utf-8")
        self.assertLessEqual(len(spine.encode("utf-8")), V.SPINE_BYTE_BUDGET)

    def test_phase_bodies_live_outside_the_spine(self):
        """The split is the saving. A phase whose body drifted back into the spine would
        be paid by every run, including the micro runs that skip phases 1-3 entirely."""
        spine = (ROOT / "skills" / "orchestrator" / "SKILL.md").read_text(encoding="utf-8")
        phase_dir = ROOT / "skills" / "orchestrator" / "phases"
        phase_bytes = sum(len(p.read_bytes()) for p in phase_dir.glob("phase-*.md"))
        self.assertGreater(phase_bytes, len(spine.encode("utf-8")),
                           "the phase bodies should still outweigh the spine that indexes them")


if __name__ == "__main__":
    unittest.main()
