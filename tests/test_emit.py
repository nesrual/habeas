import os
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import emit  # noqa: E402
import state  # noqa: E402

SNIPPET = "def fee_cents(amount_cents)\n  (dollars * 100).to_i\nend"


class EmitTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / "config.yaml").write_text("")
        settings = patch.dict(os.environ, {"HABEAS_CONFIG": str(self.repo / "config.yaml")})
        settings.start()
        self.addCleanup(settings.stop)
        (self.repo / "lib").mkdir()
        (self.repo / "lib" / "fees.rb").write_text("code\n")
        state.init_repo(str(self.repo))
        self.rid = state.run_start(str(self.repo), "lib")["id"]

    def tearDown(self):
        self._tmp.cleanup()

    def add_candidate(self, n=1, category="bug", hypothesis="fee loses cents to float truncation"):
        out = state.add_candidates(str(self.repo), [{
            "id": f"cand-{n}", "category": category, "file": "lib/fees.rb",
            "lines": [3, 5], "hypothesis": hypothesis, "confidence": 0.8,
            "evidence_snippet": SNIPPET + f" # {n}"}])
        return out["added"][0]

    def make_proof_artifacts(self, n=1, with_fix_run=False):
        log_dir = self.repo / ".audit" / "runs" / f"r-test-{n}"
        log_dir.mkdir(parents=True)
        (log_dir / "run1.log").write_text("Failures:\n  1) red\n1 example, 1 failure\n")
        (log_dir / "run2.log").write_text("Failures:\n  1) red\n1 example, 1 failure\n")
        if with_fix_run:
            (log_dir / "runfix.log").write_text("1 example, 0 failures\n")
        spec = log_dir / "proof_spec.rb"
        spec.write_text("RSpec.describe 'x' do\n  it 'fails' { expect(1).to eq(2) }\nend\n")
        return spec, log_dir

    def to_proved(self, fp, n=1, with_fix_run=False, review="valid", result="red",
                  sketch_type="rspec"):
        spec, log_dir = self.make_proof_artifacts(n, with_fix_run)
        state.transition(str(self.repo), fp, "expert_validated", "expert", record_patch={
            "failure_scenario": "fee_cents(100) returns 32; exact is 33",
            "severity": "high",
            "proof_sketch": {"type": sketch_type,
                             "expected_failure_pattern": r"expected: 33\s+got: 32"},
            "fix_sketch": "use integer math"})
        state.transition(str(self.repo), fp, "critic_survived", "critic", record_patch={
            "critic": {"verdict": "promote", "confidence": 0.7,
                       "strongest_counterargument": "none held"}})
        state.transition(str(self.repo), fp, "proved", "prover", record_patch={
            "proof": {"result": result, "runs": ["red", "red"],
                      "failure_message": "expected: 33\n     got: 32",
                      "fail_then_pass": True if with_fix_run else None,
                      "spec_path": str(spec), "log_path": str(log_dir)},
            "proof_review": {"verdict": review, "reason": "checks out"}})
        return fp


class TestProvableEmission(EmitTestCase):
    def test_full_issue_folder(self):
        fp = self.to_proved(self.add_candidate(), with_fix_run=True)
        out = emit.emit(str(self.repo), fp)
        self.assertEqual(out["action"], "emitted")
        self.assertEqual(out["issue_id"], "0001")
        issue_dir = Path(out["path"])
        self.assertEqual(issue_dir.name, "0001-fee-loses-cents-to-float-truncation")
        text = (issue_dir / "ISSUE.md").read_text()
        for token in ('id: "0001"', 'track: "provable"', 'category: "bug"',
                      'severity: "high"', 'status: "open"', f'run: "{self.rid}"',
                      'files: ["lib/fees.rb"]', "## Claim", "## Failure scenario",
                      "## Evidence", "## Proof", "## Suggested fix direction",
                      "## Scope", "issues/0001-fee-loses-cents-to-float-truncation/proof_spec.rb",
                      "`lib/fees.rb:3-5`"):
            self.assertIn(token, text, token)
        self.assertNotIn("Advisory finding", text)
        self.assertIn("RSpec.describe", (issue_dir / "proof_spec.rb").read_text())
        log = (issue_dir / "proof_run.log").read_text()
        self.assertIn("=== run 1 (red on HEAD) ===", log)
        self.assertIn("=== run 2 (determinism check) ===", log)
        self.assertIn("=== run with fix applied ===", log)
        entry = state.show(str(self.repo), fp)
        self.assertEqual(entry["status"], "emitted")
        self.assertEqual(entry["issue_id"], "0001")
        self.assertIn(".audit/", (self.repo / ".gitignore").read_text())

    def test_pytest_proof_emitted_as_python(self):
        (self.repo / "config.yaml").write_text('proof:\n  framework: pytest\n  test_cmd: "pytest {file}"\n')
        fp = self.to_proved(self.add_candidate(), sketch_type="pytest")
        folder = Path(emit.emit(str(self.repo), fp)["path"])
        self.assertTrue((folder / "proof_spec.py").is_file())
        self.assertIn(f"pytest issues/{folder.name}/proof_spec.py", (folder / "ISSUE.md").read_text())

    def test_jest_proof_emitted_as_test_tsx(self):
        (self.repo / "config.yaml").write_text(
            'proof:\n  test_cmd_jest: "npx jest --ci {file}"\n')
        fp = self.to_proved(self.add_candidate(), sketch_type="jest")
        out = emit.emit(str(self.repo), fp)
        self.assertEqual(out["action"], "emitted")
        issue_dir = Path(out["path"])
        self.assertTrue((issue_dir / "proof_spec.test.tsx").exists())
        self.assertFalse((issue_dir / "proof_spec.rb").exists())
        text = (issue_dir / "ISSUE.md").read_text()
        self.assertIn(f"npx jest --ci issues/{issue_dir.name}/proof_spec.test.tsx", text)

    def test_id_allocation_continues_from_existing(self):
        (self.repo / "issues" / "0007-existing").mkdir(parents=True)
        fp = self.to_proved(self.add_candidate())
        out = emit.emit(str(self.repo), fp)
        self.assertEqual(out["issue_id"], "0008")

    def test_output_symlink_refused_before_emitting(self):
        fp = self.to_proved(self.add_candidate())
        dest = self.repo / "unrelated"
        dest.write_text("fixture")
        (self.repo / ".gitignore").symlink_to(dest)
        with self.assertRaisesRegex(ValueError, "unsafe output"):
            emit.emit(str(self.repo), fp)
        self.assertFalse((self.repo / "issues").exists())
        self.assertEqual(dest.read_text(), "fixture")

    def test_both_red_run_logs_are_required(self):
        fp = self.to_proved(self.add_candidate())
        (self.repo / ".audit/runs/r-test-1/run2.log").unlink()
        with self.assertRaisesRegex(emit.EmitError, "both red runs"):
            emit.emit(str(self.repo), fp)
        self.assertFalse((self.repo / "issues").exists())

    def test_security_emission_rechecks_fix_run(self):
        fp = self.to_proved(self.add_candidate(category="security"))
        with self.assertRaisesRegex(emit.EmitError, "verified green"):
            emit.emit(str(self.repo), fp)

    def test_refuses_without_red_proof(self):
        fp = self.to_proved(self.add_candidate(), result="failed")
        with self.assertRaisesRegex(emit.EmitError, "not \"red\""):
            emit.emit(str(self.repo), fp)
        self.assertEqual(state.show(str(self.repo), fp)["status"], "proved")
        self.assertFalse((self.repo / "issues").exists())

    def test_refuses_without_valid_proof_review(self):
        fp = self.to_proved(self.add_candidate(), review="invalid")
        with self.assertRaisesRegex(emit.EmitError, "proof_review"):
            emit.emit(str(self.repo), fp)

    def test_refuses_missing_spec_artifact(self):
        fp = self.to_proved(self.add_candidate())
        entry = state.show(str(self.repo), fp)
        Path(entry["record"]["proof"]["spec_path"]).unlink()
        with self.assertRaisesRegex(emit.EmitError, "spec_path"):
            emit.emit(str(self.repo), fp)

    def test_refuses_non_emittable_status(self):
        fp = self.add_candidate()
        with self.assertRaisesRegex(emit.EmitError, "not emittable"):
            emit.emit(str(self.repo), fp)


class TestAdvisoryEmission(EmitTestCase):
    def to_advisory(self, fp):
        state.transition(str(self.repo), fp, "expert_validated", "expert", record_patch={
            "failure_scenario": "concrete cost", "severity": "critical"})
        state.transition(str(self.repo), fp, "critic_advisory", "critic", record_patch={
            "critic": {"verdict": "advisory", "confidence": 0.6}})
        return fp

    def test_advisory_issue(self):
        fp = self.to_advisory(self.add_candidate(category="improvement",
                                                 hypothesis="dead code should be deleted"))
        out = emit.emit(str(self.repo), fp)
        issue_dir = Path(out["path"])
        text = (issue_dir / "ISSUE.md").read_text()
        first_body_line = text.split("---\n\n", 1)[1].splitlines()[0]
        self.assertEqual(first_body_line,
                         "> Advisory finding: opinion, not evidence. Severity capped at medium.")
        self.assertIn('track: "advisory"', text)
        self.assertIn('severity: "medium"', text)  # clamped from critical
        self.assertIn("proof_command: null", text)
        self.assertNotIn("## Proof", text)
        self.assertFalse((issue_dir / "proof_spec.rb").exists())
        self.assertFalse((issue_dir / "proof_run.log").exists())


class TestQuarantine(EmitTestCase):
    def to_proof_failed(self, fp):
        state.transition(str(self.repo), fp, "expert_validated", "expert", record_patch={
            "failure_scenario": "scenario", "severity": "high",
            "proof_sketch": {"type": "rspec", "expected_failure_pattern": "x"}})
        state.transition(str(self.repo), fp, "critic_survived", "critic", record_patch={
            "critic": {"verdict": "promote", "confidence": 0.9}})
        state.transition(str(self.repo), fp, "proof_failed", "prover", record_patch={
            "proof": {"result": "failed", "reason": "pattern_mismatch",
                      "runs": ["failed"], "log_path": ".audit/runs/r-x"}})
        return fp

    def test_quarantine_appends_and_transitions(self):
        fp1 = self.to_proof_failed(self.add_candidate(1))
        out = emit.emit(str(self.repo), fp1)
        self.assertEqual(out["action"], "quarantined")
        q = (self.repo / "quarantine.md").read_text()
        self.assertIn(f"## {fp1}", q)
        self.assertIn("pattern_mismatch", q)
        self.assertIn("Critic confidence: 0.9", q)
        self.assertEqual(state.show(str(self.repo), fp1)["status"], "quarantined")
        # append-only: a second entry keeps the first
        fp2 = self.to_proof_failed(self.add_candidate(2))
        emit.emit(str(self.repo), fp2)
        q = (self.repo / "quarantine.md").read_text()
        self.assertIn(f"## {fp1}", q)
        self.assertIn(f"## {fp2}", q)


class TestEmissionCap(EmitTestCase):
    def test_cap_enforced_but_not_for_quarantine(self):
        (self.repo / "config.yaml").write_text("gates:\n  max_emissions_per_run: 2\n")
        fps = [self.to_proved(self.add_candidate(n), n=n) for n in (1, 2, 3)]
        emit.emit(str(self.repo), fps[0])
        emit.emit(str(self.repo), fps[1])
        with self.assertRaisesRegex(emit.EmitError, "emission cap"):
            emit.emit(str(self.repo), fps[2])
        self.assertEqual(state.show(str(self.repo), fps[2])["status"], "proved")
        # a new run resets the count
        state.run_end(str(self.repo), {})
        state.run_start(str(self.repo), "lib")
        self.assertEqual(emit.emit(str(self.repo), fps[2])["action"], "emitted")


class TestCli(EmitTestCase):
    def test_cli_roundtrip_and_refusal_exit_code(self):
        fp = self.to_proved(self.add_candidate())
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "emit.py"), fp, "--repo", str(self.repo)],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["action"], "emitted")
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "emit.py"), "deadbeef00000000",
             "--repo", str(self.repo)],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("unknown fingerprint", proc.stderr)


if __name__ == "__main__":
    unittest.main()
