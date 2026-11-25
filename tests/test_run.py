import json
import os
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent

FAKE_CLI = """#!/usr/bin/env bash
prompt=$(cat)
d="$HABEAS_FAKE_DIR"
case "$prompt" in
  *"Role: scout"*)
    if [ -f "$d/scout2.json" ] && [ -f "$d/.scout_called" ]; then
      cat "$d/scout2.json"
    else
      touch "$d/.scout_called"
      cat "$d/scout.json"
    fi ;;
  *"Role: prover"*)
    if [ -f "$d/prover2.json" ] && [ -f "$d/.prover_called" ]; then
      cat "$d/prover2.json"
    else
      touch "$d/.prover_called"
      cat "$d/prover.json"
    fi ;;
  *"Role: proof review"*) cat "$d/proof_review.json" ;;
  *"Role: critic"*)       cat "$d/critic.json" ;;
  *"Role: expert"*)       cat "$d/expert.json" ;;
  *) echo '{"error":"fake cli: no role marker in prompt"}' ;;
esac
"""

FEE_SPEC = """# frozen_string_literal: true

RSpec.describe "audit: fee_cents float truncation" do
  it "charges exactly 2.9% + 30c on a 100-cent amount" do
    expect(AuditTarget::Fees.fee_cents(100)).to eq(33)
  end
end
"""


class RunPipelineTest(unittest.TestCase):
    """run.sh end-to-end with a deterministic fake provider; prove.sh still
    executes real rspec inside the worktree."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = self.tmp / "target"
        subprocess.run([str(SKILL / "fixtures" / "build_fixture.sh"), str(self.repo)],
                       check=True, capture_output=True)
        (self.repo / "config.yaml").write_text(
            "providers:\n  scout: generic\n  expert: generic\n"
            "  critic: generic\n  proof_review: generic\n")
        settings = patch.dict(os.environ, {"HABEAS_CONFIG": str(self.repo / "config.yaml"),
                                           "HABEAS_ALLOW_HOST_PROOFS": "1"})
        settings.start()
        self.addCleanup(settings.stop)
        self.fake_dir = self.tmp / "fake"
        self.fake_dir.mkdir()
        fake = self.tmp / "fake_cli.sh"
        fake.write_text(FAKE_CLI)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.fake_cli = str(fake)
        self.write_canned_responses()

    def tearDown(self):
        subprocess.run(["git", "-C", str(self.repo), "worktree", "prune"],
                       capture_output=True)
        self._tmp.cleanup()

    def write_canned_responses(self):
        fees = (self.repo / "lib" / "audit_target" / "fees.rb").read_text().splitlines()
        snippet = "\n".join(fees[7:13])  # def fee_cents +/- context
        (self.fake_dir / "scout.json").write_text(json.dumps([{
            "id": "cand-1", "category": "bug", "file": "lib/audit_target/fees.rb",
            "lines": [9, 12],
            "hypothesis": "fee_cents computes the fee in float dollars and truncates, losing cents",
            "confidence": 0.9, "evidence_snippet": snippet}]))
        (self.fake_dir / "expert.json").write_text(json.dumps({
            "verdict": "proceed",
            "failure_scenario": "fee_cents(100) returns 32; exact 2.9% + 30c is 33",
            "proof_sketch": {"type": "rspec",
                             "outline": "assert fee_cents(100) == 33",
                             "expected_failure_pattern": r"expected: 33\s+got: 32"},
            "severity": "high", "single_concern": True}))
        (self.fake_dir / "critic.json").write_text(json.dumps({
            "verdict": "promote",
            "strongest_counterargument": "specs pin 600/800 but those are float-exact",
            "confidence": 0.8}))
        (self.fake_dir / "prover.json").write_text(json.dumps({"spec": FEE_SPEC}))
        (self.fake_dir / "proof_review.json").write_text(json.dumps({
            "verdict": "valid", "reason": "asserts correct fee, subject unmocked"}))

    def run_pipeline(self, expect_rc=0, extra_args=()):
        env = dict(os.environ)
        env["HABEAS_GENERIC_CMD"] = f"{self.fake_cli} < {{prompt_file}}"
        env["HABEAS_FAKE_DIR"] = str(self.fake_dir)
        proc = subprocess.run(
            [str(SKILL / "scripts" / "run.sh"), "--unit", "lib/audit_target",
             "--repo", str(self.repo), *extra_args],
            capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, expect_rc,
                         f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
        return json.loads(proc.stdout.strip().splitlines()[-1]) if expect_rc == 0 else proc

    def state(self):
        return json.loads((self.repo / ".audit" / "state.json").read_text())

    def test_happy_path_emits_a_proved_issue(self):
        counts = self.run_pipeline()
        self.assertEqual(counts["candidates"], 1)
        self.assertEqual(counts["expert_validated"], 1)
        self.assertEqual(counts["promoted"], 1)
        self.assertEqual(counts["proved"], 1)
        self.assertEqual(counts["emitted"], 1)
        issues = list((self.repo / "issues").iterdir())
        self.assertEqual(len(issues), 1)
        issue = issues[0]
        self.assertTrue((issue / "proof_spec.rb").exists())
        self.assertTrue((issue / "proof_run.log").exists())
        text = (issue / "ISSUE.md").read_text()
        self.assertIn('track: "provable"', text)
        self.assertIn("expected: 33", text)
        entry = next(iter(self.state()["findings"].values()))
        self.assertEqual(entry["status"], "emitted")
        metrics = [json.loads(l) for l in
                   (self.repo / ".audit" / "metrics.jsonl").read_text().splitlines()]
        self.assertEqual(metrics[0]["counts"]["emitted"], 1)
        self.assertEqual(metrics[0]["dispatch_calls"]["critic"], 1)
        # no worktrees left behind
        wt = subprocess.run(["git", "-C", str(self.repo), "worktree", "list"],
                            capture_output=True, text=True).stdout
        self.assertEqual(len(wt.strip().splitlines()), 1)

    def test_ambiguous_scout_never_marks_the_unit_scanned(self):
        (self.fake_dir / "scout.json").write_text('unrelated text containing []')
        self.run_pipeline(expect_rc=1)
        self.assertEqual(self.state()["scanned_hunks"], {})

    def test_advisory_path_ignores_scope_and_proof_claims_from_scout(self):
        first = json.loads((self.fake_dir / "scout.json").read_text())[0]
        first.update(category="test-gap", proof={"result": "red"})
        second = dict(first, file="README.md")
        (self.fake_dir / "scout.json").write_text(json.dumps([first, second]))
        counts = self.run_pipeline()
        self.assertEqual(counts["candidates"], 1)
        self.assertEqual(counts["critic_advisory"], 1)
        entry = next(iter(self.state()["findings"].values()))
        self.assertEqual(entry["status"], "emitted")
        self.assertNotIn("proof", entry["record"])
        self.assertEqual(entry["record"]["severity"], "medium")

    def test_quoted_unit_survives_metrics_and_hunk_recording(self):
        name = """test 'quoted' & "data".rb"""
        (self.repo / name).write_text("fixture")
        (self.fake_dir / "scout.json").write_text("[]")
        self.run_pipeline(extra_args=("--unit", name))
        hunk_paths = [h["path"] for h in self.state()["scanned_hunks"].values()]
        self.assertIn(name, hunk_paths)
        line = json.loads((self.repo / ".audit/metrics.jsonl").read_text())
        self.assertEqual(line["unit"], "lib/audit_target," + name)

    def test_second_run_emits_no_duplicates(self):
        self.run_pipeline()
        counts = self.run_pipeline()
        self.assertEqual(counts["candidates"], 0)  # already-scanned drop
        self.assertEqual(counts["emitted"], 0)
        self.assertEqual(len(list((self.repo / "issues").iterdir())), 1)

    def test_critic_kill_stops_the_finding(self):
        (self.fake_dir / "critic.json").write_text(json.dumps({
            "verdict": "kill",
            "strongest_counterargument": "the values used are float-exact; no cent is lost",
            "confidence": 0.9}))
        counts = self.run_pipeline()
        self.assertEqual(counts["critic_killed"], 1)
        self.assertEqual(counts["emitted"], 0)
        self.assertFalse((self.repo / "issues").exists())
        entry = next(iter(self.state()["findings"].values()))
        self.assertEqual(entry["status"], "killed")
        self.assertIn("critic kill", entry["history"][-1]["note"])

    def test_scout_sweeps_union_candidates(self):
        # sweep 2 returns a second, different candidate; identical ones dedupe
        pagination = (self.repo / "lib" / "audit_target" / "pagination.rb").read_text().splitlines()
        first = json.loads((self.fake_dir / "scout.json").read_text())
        second = first + [{
            "id": "cand-2", "category": "bug", "file": "lib/audit_target/pagination.rb",
            "lines": [10, 11], "hypothesis": "pagination offset math overlaps pages",
            "confidence": 0.8, "evidence_snippet": "\n".join(pagination[8:12])}]
        (self.fake_dir / "scout2.json").write_text(json.dumps(second))
        counts = self.run_pipeline(extra_args=("--sweeps", "2"))
        self.assertEqual(counts["candidates"], 2)  # fee (sweep1) + pagination (sweep2)
        state = self.state()
        self.assertEqual(len(state["findings"]), 2)
        metrics = json.loads((self.repo / ".audit" / "metrics.jsonl").read_text().splitlines()[0])
        self.assertEqual(metrics["dispatch_calls"]["scout"], 2)

    def test_inline_handoff_keeps_cold_claim_card_and_records_an_invalid_review(self):
        state = [os.sys.executable, str(SKILL / "scripts/state.py"), "--repo", str(self.repo)]
        subprocess.run([*state, "init"], check=True, capture_output=True)
        subprocess.run([*state, "run-start", "--unit", "lib"], check=True, capture_output=True)
        added = subprocess.check_output([*state, "add-candidates", str(self.fake_dir / "scout.json")])
        fp = json.loads(added)["added"][0]
        expert = json.loads((self.fake_dir / "expert.json").read_text())
        subprocess.run([*state, "transition", fp, "expert_validated", "--actor", "expert",
                        "--record", json.dumps(expert)], check=True, capture_output=True)
        (self.fake_dir / "proof_review.json").write_text('{"verdict":"invalid","reason":"fixture"}')
        counts = self.run_pipeline(extra_args=("--from", "critic"))
        self.assertEqual(counts["quarantined"], 1)
        self.assertFalse((self.fake_dir / ".scout_called").exists())
        self.assertEqual(self.state()["scanned_hunks"], {})
        card = next((self.repo / ".audit/runs").glob("*/*.claim.json"))
        self.assertEqual(set(json.loads(card.read_text())),
                         {"file", "lines", "category", "severity", "failure_scenario", "evidence_snippet"})

    def test_split_stops_after_one_level(self):
        narrower = json.loads((self.fake_dir / "scout.json").read_text())[0]
        narrower["evidence_snippet"] += " narrower"
        (self.fake_dir / "expert.json").write_text(json.dumps({"verdict": "split", "split": [narrower]}))
        counts = self.run_pipeline()
        self.assertEqual(counts["candidates"], 2)
        self.assertEqual({e["status"] for e in self.state()["findings"].values()}, {"killed"})

    def test_selftest_checks_recall_and_preserves_supplied_work_directory(self):
        work = self.tmp / "selftest-work"
        work.mkdir()
        marker = work / "existing.txt"
        marker.write_text("keep")
        # A deterministic empty scout result must fail the fixture recall threshold.
        (self.fake_dir / "scout.json").write_text("[]")
        fake_claude = self.tmp / "claude"
        fake_claude.symlink_to(self.fake_cli)
        env = dict(os.environ, HABEAS_GENERIC_CMD=f"{self.fake_cli} < {{prompt_file}}",
                   HABEAS_FAKE_DIR=str(self.fake_dir), PATH=f"{self.tmp}:{os.environ['PATH']}")
        out = subprocess.run([str(SKILL / "scripts/selftest.sh"), "--work", str(work)],
                             capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 1, out.stderr)
        self.assertIn("0 of 5 plants", out.stdout)
        self.assertEqual(list(work.iterdir()), [marker])

    def test_multi_unit_scans_as_one_run(self):
        env = dict(os.environ)
        env["HABEAS_GENERIC_CMD"] = f"{self.fake_cli} < {{prompt_file}}"
        env["HABEAS_FAKE_DIR"] = str(self.fake_dir)
        proc = subprocess.run(
            [str(SKILL / "scripts" / "run.sh"),
             "--unit", "lib/audit_target/fees.rb",
             "--unit", "lib/audit_target/pagination.rb",
             "--repo", str(self.repo)],
            capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = self.state()
        run = state["runs"][-1]
        self.assertEqual(run["unit"],
                         "lib/audit_target/fees.rb,lib/audit_target/pagination.rb")
        scout_in = (self.repo / ".audit" / "runs" / run["id"] / "scout-input.txt").read_text()
        self.assertIn("=== FILE: lib/audit_target/fees.rb ===", scout_in)
        self.assertIn("=== FILE: lib/audit_target/pagination.rb ===", scout_in)
        hunk_paths = {h["path"] for h in state["scanned_hunks"].values()}
        self.assertEqual(hunk_paths,
                         {"lib/audit_target/fees.rb", "lib/audit_target/pagination.rb"})

    def test_security_finding_routed_and_fix_supplied_by_prover(self):
        # security category: routed to expert-security, and emission requires
        # fail-then-pass — the expert supplies no usable diff, the prover does.
        fees = (self.repo / "lib" / "audit_target" / "fees.rb")
        fixed = fees.read_text().replace(
            "      dollars = amount_cents / 100.0\n"
            "      fee_dollars = dollars * RATE\n"
            "      (fee_dollars * 100).to_i + FIXED_CENTS",
            "      (amount_cents * 29 + 500) / 1000 + FIXED_CENTS")
        fees.write_text(fixed)
        diff = subprocess.run(["git", "-C", str(self.repo), "diff"],
                              capture_output=True, text=True, check=True).stdout
        subprocess.run(["git", "-C", str(self.repo), "checkout", "--", "."], check=True)

        scout = json.loads((self.fake_dir / "scout.json").read_text())
        scout[0]["category"] = "security"
        (self.fake_dir / "scout.json").write_text(json.dumps(scout))
        expert = json.loads((self.fake_dir / "expert.json").read_text())
        expert["fix_sketch"] = "just use integer math"  # prose: not appliable
        (self.fake_dir / "expert.json").write_text(json.dumps(expert))
        (self.fake_dir / "prover.json").write_text(
            json.dumps({"spec": FEE_SPEC, "fix": diff}))

        counts = self.run_pipeline()
        self.assertEqual(counts["proved"], 1, counts)
        self.assertEqual(counts["emitted"], 1)
        prompt = next((self.repo / ".audit" / "runs").glob("*/*.expert-prompt.md")).read_text()
        self.assertIn("expert (security)", prompt)
        issue = next((self.repo / "issues").iterdir())
        text = (issue / "ISSUE.md").read_text()
        self.assertIn("fail_then_pass: true", text)
        self.assertIn("=== run with fix applied ===", (issue / "proof_run.log").read_text())

    def test_repair_fixes_always_repairs_bug_diffs(self):
        # bug category, no usable fix on attempt 1: with repair_fixes: always
        # the prover gets one repair pass and the emitted issue verifies the fix
        (self.repo / "config.yaml").write_text(
            (self.repo / "config.yaml").read_text() + "proof:\n  repair_fixes: always\n")
        fees = self.repo / "lib" / "audit_target" / "fees.rb"
        fixed = fees.read_text().replace(
            "      dollars = amount_cents / 100.0\n"
            "      fee_dollars = dollars * RATE\n"
            "      (fee_dollars * 100).to_i + FIXED_CENTS",
            "      (amount_cents * 29 + 500) / 1000 + FIXED_CENTS")
        fees.write_text(fixed)
        diff = subprocess.run(["git", "-C", str(self.repo), "diff"],
                              capture_output=True, text=True, check=True).stdout
        subprocess.run(["git", "-C", str(self.repo), "checkout", "--", "."], check=True)
        (self.fake_dir / "prover2.json").write_text(
            json.dumps({"spec": FEE_SPEC, "fix": diff}))
        counts = self.run_pipeline()
        self.assertEqual(counts["proved"], 1, counts)
        issue = next((self.repo / "issues").iterdir())
        self.assertIn("fail_then_pass: true", (issue / "ISSUE.md").read_text())
        metrics = json.loads((self.repo / ".audit" / "metrics.jsonl").read_text().splitlines()[0])
        self.assertEqual(metrics["dispatch_calls"]["prover"], 2)  # materialize + repair

    def test_failed_proof_quarantines(self):
        bad_spec = FEE_SPEC.replace("fee_cents(100)).to eq(33)",
                                    "fee_cents(600)).to eq(47)")  # passes on HEAD
        (self.fake_dir / "prover.json").write_text(json.dumps({"spec": bad_spec}))
        counts = self.run_pipeline()
        self.assertEqual(counts["proved"], 0)
        self.assertEqual(counts["quarantined"], 1)
        q = (self.repo / "quarantine.md").read_text()
        self.assertIn("zero_failures", q)
        entry = next(iter(self.state()["findings"].values()))
        self.assertEqual(entry["status"], "quarantined")
        # both attempts consumed
        notes = [h["note"] for h in entry["history"] if h["to"] == "proof_failed"]
        self.assertEqual(len(notes), 2)


if __name__ == "__main__":
    unittest.main()
