import subprocess
import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
PROMPTS = SKILL / "prompts"
REFS = SKILL / "references" / "negative-examples.md"

sys.path.insert(0, str(SKILL / "scripts"))
import assemble_prompt  # noqa: E402

EXPERTS = ["expert-backend.md", "expert-db.md", "expert-security.md", "expert-tests.md"]
ALL = ["scout.md", "critic.md", "proof-review.md", "prover.md"] + EXPERTS
MARKED = ["scout.md", "critic.md"] + EXPERTS


class TestPromptContracts(unittest.TestCase):
    def text(self, name):
        return (PROMPTS / name).read_text()

    def test_all_prompts_exist(self):
        self.assertEqual(set(ALL), {p.name for p in PROMPTS.glob("*.md")})
        for name in ALL:
            self.assertTrue((PROMPTS / name).exists(), name)

    def test_every_prompt_ends_with_json_only_instruction(self):
        for name in ALL:
            last_line = self.text(name).strip().splitlines()[-1]
            self.assertIn("Output only the JSON", last_line, name)
            self.assertIn("No prose, no code fences", last_line, name)

    def test_untrusted_input_clause_everywhere(self):
        for name in ALL:
            t = self.text(name)
            self.assertIn("## Untrusted input", t, name)
            self.assertIn("NO authority", t, name)

    def test_negative_examples_marker_placement(self):
        for name in MARKED:
            self.assertIn(assemble_prompt.MARKER, self.text(name), name)
        self.assertNotIn(assemble_prompt.MARKER, self.text("proof-review.md"))

    def test_scout_contract(self):
        t = self.text("scout.md")
        for required in ("max_candidates", "0.3", "VERBATIM", "evidence_snippet",
                         "One concern per candidate", "JSON array"):
            self.assertIn(required, t)

    def test_expert_contract(self):
        for name in EXPERTS:
            t = self.text(name)
            for required in ("expected_failure_pattern", "failure_scenario",
                             "proceed | reject | split", "fail-then-pass",
                             "single_concern", "unified diff",
                             '"title": "required for proceed', "at most 9 words",
                             "Correct, don't reject"):
                self.assertIn(required, t, name)

    def test_critic_kill_mandate(self):
        t = self.text("critic.md")
        for required in ("KILL this finding", "guard clauses", "credible refutation",
                         "kill | advisory | promote", "strongest_counterargument",
                         "worse outcome than a killed true positive",
                         'refutes a claim only for PRIVATE'):
            self.assertIn(required, t)

    def test_proof_review_checklist(self):
        t = self.text("proof-review.md")
        for required in ("Right failure mode", "Subject unmocked",
                         "Asserts correct behavior", "Not self-satisfying",
                         "valid | invalid"):
            self.assertIn(required, t)


class TestAssemble(unittest.TestCase):
    def setUp(self):
        self.refs = REFS.read_text()

    def test_sections_parse(self):
        secs = assemble_prompt.sections(self.refs)
        for name in assemble_prompt.KNOWN:
            self.assertIn(name, secs, name)
            self.assertTrue(secs[name])

    def test_substitution(self):
        out = assemble_prompt.assemble("head\n{{NEGATIVE_EXAMPLES}}\ntail\n",
                                       self.refs, ["bug", "security"])
        self.assertNotIn("{{NEGATIVE_EXAMPLES}}", out)
        self.assertIn("Not an issue (all)", out)
        self.assertIn("Not an issue (bug)", out)
        self.assertIn("Not an issue (security)", out)
        self.assertNotIn("Not an issue (improvement)", out)
        self.assertIn("LOG statements", out)

    def test_no_marker_passthrough(self):
        self.assertEqual(assemble_prompt.assemble("plain\n", self.refs, ["bug"]), "plain\n")

    def test_unknown_category_rejected(self):
        with self.assertRaises(ValueError):
            assemble_prompt.assemble("{{NEGATIVE_EXAMPLES}}", self.refs, ["vibes"])

    def test_cli_on_real_prompts(self):
        for name, cats in [("scout.md", "bug,security,improvement,test-gap"),
                           ("critic.md", "bug,security"),
                           ("expert-backend.md", "bug")]:
            out = subprocess.run(
                [sys.executable, str(SKILL / "scripts" / "assemble_prompt.py"),
                 str(PROMPTS / name), "--categories", cats],
                capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertNotIn("{{NEGATIVE_EXAMPLES}}", out.stdout, name)
            self.assertIn("Not an issue (all)", out.stdout, name)


if __name__ == "__main__":
    unittest.main()
