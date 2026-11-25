import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))

import prove_gates  # noqa: E402

RED_LOG = """
Randomized with seed 12345

F

Failures:

  1) audit: fee charges exactly 2.9% + 30c
     Failure/Error: expect(AuditTarget::Fees.fee_cents(1000)).to eq(59)

       expected: 59
            got: 58

       (compared using ==)
     # ./spec/audit/audit_x_spec.rb:5

Finished in 0.01 seconds (files took 0.4 seconds to load)
1 example, 1 failure

Failed examples:
"""

GREEN_LOG = """
Randomized with seed 999
.

Finished in 0.01 seconds (files took 0.3 seconds to load)
1 example, 0 failures
"""

LOAD_ERROR_LOG = """
An error occurred while loading ./spec/audit/audit_x_spec.rb.
Failure/Error: __send__(method, file)

SyntaxError:
  unexpected token

Finished in 0.0001 seconds (files took 0.2 seconds to load)
0 examples, 0 failures, 1 error occurred outside of examples
"""


class TestParseAndGates(unittest.TestCase):
    def check(self, log_text, pattern=None, green=False):
        parsed = prove_gates.parse_log(log_text)
        return prove_gates.gate_check(parsed, pattern, green)

    def test_red_log_passes_both_gates(self):
        out = self.check(RED_LOG, r"expected: 59\s+got: 58")
        self.assertTrue(out["gate_a"] and out["gate_b"] and out["ok"], out)
        self.assertIn("expected: 59", out["first_failure"])

    def test_pattern_mismatch(self):
        out = self.check(RED_LOG, r"NoMethodError")
        self.assertTrue(out["gate_a"])
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "pattern_mismatch")

    def test_green_log_fails_gate_b(self):
        out = self.check(GREEN_LOG, r"anything")
        self.assertEqual(out["reason"], "zero_failures")

    def test_green_mode(self):
        self.assertTrue(self.check(GREEN_LOG, green=True)["green"])
        self.assertFalse(self.check(RED_LOG, green=True)["green"])

    def test_load_error_fails_gate_a(self):
        out = self.check(LOAD_ERROR_LOG, r"x")
        self.assertFalse(out["gate_a"])
        self.assertEqual(out["reason"], "errors_outside_examples")

    def test_no_summary_fails_gate_a(self):
        out = self.check("bash: bundle: command not found\n", r"x")
        self.assertEqual(out["reason"], "no_summary")

    def test_pattern_is_case_insensitive(self):
        out = self.check(RED_LOG, r"EXPECTED: 59")
        self.assertTrue(out["ok"])


PYTEST_RED_LOG = """============================= test session starts ==============================
collected 2 items

spec/audit/test_audit_x.py F.                                            [100%]

=================================== FAILURES ===================================
_________________________________ test_fee_cents _______________________________

    def test_fee_cents():
>       assert fee_cents(100) == 33
E       assert 32 == 33

spec/audit/test_audit_x.py:5: AssertionError
=========================== short test summary info ============================
FAILED spec/audit/test_audit_x.py::test_fee_cents - assert 32 == 33
========================= 1 failed, 1 passed in 0.03s ==========================
"""

PYTEST_GREEN_LOG = """============================= test session starts ==============================
collected 2 items

spec/audit/test_audit_x.py ..                                            [100%]

============================== 2 passed in 0.01s ===============================
"""

PYTEST_COLLECT_ERROR_LOG = """============================= test session starts ==============================
collected 0 items / 1 error

==================================== ERRORS ====================================
______________________ ERROR collecting test_audit_x.py _______________________
E   SyntaxError: invalid syntax
=========================== short test summary info ============================
ERROR spec/audit/test_audit_x.py
=============================== 1 error in 0.05s ===============================
"""


class TestPytestParser(unittest.TestCase):
    def check(self, log_text, pattern=None, green=False):
        parsed = prove_gates.parse_log(log_text, "pytest")
        return prove_gates.gate_check(parsed, pattern, green)

    def test_red_passes_gates(self):
        out = self.check(PYTEST_RED_LOG, r"assert 32 == 33")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["examples"], 2)
        self.assertEqual(out["failures"], 1)
        self.assertIn("test_fee_cents", out["first_failure"])

    def test_pattern_mismatch(self):
        out = self.check(PYTEST_RED_LOG, r"TypeError")
        self.assertEqual(out["reason"], "pattern_mismatch")

    def test_green_fails_gate_b_and_passes_green_mode(self):
        self.assertEqual(self.check(PYTEST_GREEN_LOG, "x")["reason"], "zero_failures")
        self.assertTrue(self.check(PYTEST_GREEN_LOG, green=True)["green"])

    def test_collect_error_fails_gate_a(self):
        out = self.check(PYTEST_COLLECT_ERROR_LOG, "x")
        self.assertFalse(out["gate_a"])
        self.assertEqual(out["reason"], "errors_outside_examples")

    def test_no_summary(self):
        self.assertEqual(self.check("boom\n", "x")["reason"], "no_summary")

    def test_unknown_framework_rejected(self):
        with self.assertRaises(ValueError):
            prove_gates.parse_log("x", "mocha")

    def test_pytest_durations_normalized(self):
        a = prove_gates.normalize("1 failed in 0.03s")
        b = prove_gates.normalize("1 failed in 12.99s")
        self.assertEqual(a, b)


JEST_RED_LOG = """FAIL app/javascript/__audit__/audit_x.test.tsx
  fees
    ✓ loads (1 ms)
    ✕ rounds cents (2 ms)

  ● fees › rounds cents

    expect(received).toBe(expected) // Object.is equality

    Expected: 33
    Received: 32

      at Object.<anonymous> (app/javascript/__audit__/audit_x.test.tsx:4:20)

Test Suites: 1 failed, 1 total
Tests:       1 failed, 1 passed, 2 total
Snapshots:   0 total
Time:        0.491 s
Ran all test suites matching /audit_x/i.
"""

JEST_GREEN_LOG = """PASS app/javascript/__audit__/audit_x.test.tsx
Test Suites: 1 passed, 1 total
Tests:       2 passed, 2 total
Snapshots:   0 total
Time:        1.02 s
"""

JEST_SUITE_ERROR_LOG = """FAIL app/javascript/__audit__/audit_x.test.tsx
  ● Test suite failed to run

    Cannot find module '../nope' from 'app/javascript/__audit__/audit_x.test.tsx'

Test Suites: 1 failed, 1 total
Tests:       0 total
Snapshots:   0 total
Time:        0.3 s
"""


class TestJestParser(unittest.TestCase):
    def check(self, log_text, pattern=None, green=False):
        parsed = prove_gates.parse_log(log_text, "jest")
        return prove_gates.gate_check(parsed, pattern, green)

    def test_red_passes_gates(self):
        out = self.check(JEST_RED_LOG, r"Expected: 33\s+Received: 32")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["examples"], 2)
        self.assertEqual(out["failures"], 1)
        self.assertTrue(out["first_failure"].startswith("fees › rounds cents"))
        self.assertNotIn("Test Suites:", out["first_failure"])

    def test_pattern_mismatch(self):
        self.assertEqual(self.check(JEST_RED_LOG, r"TypeError")["reason"], "pattern_mismatch")

    def test_green_fails_gate_b_and_passes_green_mode(self):
        self.assertEqual(self.check(JEST_GREEN_LOG, "x")["reason"], "zero_failures")
        self.assertTrue(self.check(JEST_GREEN_LOG, green=True)["green"])

    def test_suite_error_fails_gate_a(self):
        out = self.check(JEST_SUITE_ERROR_LOG, "x")
        self.assertFalse(out["gate_a"])
        self.assertEqual(out["reason"], "errors_outside_examples")

    def test_no_summary(self):
        self.assertEqual(self.check("boom\n", "x")["reason"], "no_summary")

    def test_runs_compare_deterministic(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = Path(d) / "a.log", Path(d) / "b.log"
            a.write_text(JEST_RED_LOG)
            b.write_text(JEST_RED_LOG.replace("0.491 s", "2.7 s"))
            out = subprocess.run([sys.executable, str(SCRIPTS / "prove_gates.py"), "compare",
                                  str(a), str(b), "--framework", "jest"],
                                 capture_output=True, text=True, check=True)
            self.assertTrue(json.loads(out.stdout)["deterministic"])


class TestNormalizeCompare(unittest.TestCase):
    def test_normalization_masks_noise(self):
        a = "at 0xdeadbeef on 2026-09-05 10:11:12 took 1.5 seconds\nRandomized with seed 111"
        b = "at 0xcafebabe on 2026-09-05 22:33:44 took 0.2 seconds\nRandomized with seed 222"
        self.assertEqual(prove_gates.normalize(a), prove_gates.normalize(b))

    def test_values_are_not_masked(self):
        self.assertNotEqual(prove_gates.normalize("expected: 59"),
                            prove_gates.normalize("expected: 58"))


class TestFindingInfo(unittest.TestCase):
    def info(self, record, fix_out=None):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "finding.json"
            f.write_text(json.dumps(record))
            args = [sys.executable, str(SCRIPTS / "prove_gates.py"), "finding-info", str(f)]
            if fix_out:
                args += ["--fix-out", fix_out]
            return subprocess.run(args, capture_output=True, text=True)

    def test_prose_fix_is_not_a_diff(self):
        out = self.info({"fingerprint": "ab", "category": "bug",
                         "proof_sketch": {"expected_failure_pattern": "x"},
                         "fix_sketch": "just use integers"})
        self.assertFalse(json.loads(out.stdout)["has_fix"])

    def test_diff_fix_detected_and_written(self):
        with tempfile.TemporaryDirectory() as d:
            fix_path = str(Path(d) / "fix.patch")
            out = self.info({"fingerprint": "ab", "category": "security",
                             "proof_sketch": {"expected_failure_pattern": "x"},
                             "fix_sketch": "--- a/x.rb\n+++ b/x.rb\n@@ -1 +1 @@\n-a\n+b\n"},
                            fix_out=fix_path)
            self.assertTrue(json.loads(out.stdout)["has_fix"])
            self.assertTrue(Path(fix_path).exists())

    def test_missing_pattern_is_an_error(self):
        out = self.info({"fingerprint": "ab", "category": "bug"})
        self.assertEqual(out.returncode, 1)
        self.assertIn("expected_failure_pattern", out.stderr)


FEE_SPEC = """# frozen_string_literal: true

RSpec.describe "audit: fee_cents float truncation" do
  it "charges exactly 2.9% + 30c on a 100-cent amount" do
    expect(AuditTarget::Fees.fee_cents(100)).to eq(33)
  end
end
"""

FEE_FIX_OLD = """      dollars = amount_cents / 100.0
      fee_dollars = dollars * RATE
      (fee_dollars * 100).to_i + FIXED_CENTS"""
FEE_FIX_NEW = "      (amount_cents * 29 + 500) / 1000 + FIXED_CENTS"


class ProveE2E(unittest.TestCase):
    """End-to-end runs against the built fixture repo (real rspec)."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.repo = Path(cls._tmp.name) / "target"
        subprocess.run([str(SKILL / "fixtures" / "build_fixture.sh"), str(cls.repo)],
                       check=True, capture_output=True)
        cls.n = 0

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        (self.repo / "config.yaml").write_text("")
        settings = patch.dict(os.environ, {"HABEAS_CONFIG": str(self.repo / "config.yaml"),
                                           "HABEAS_ALLOW_HOST_PROOFS": "1"})
        settings.start()
        self.addCleanup(settings.stop)
        type(self).n += 1
        self.wt = Path(self._tmp.name) / f"wt{self.n}"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "-q",
                        str(self.wt), "HEAD"], check=True, capture_output=True)
        self.log_dir = Path(self._tmp.name) / f"logs{self.n}"

    def tearDown(self):
        subprocess.run(["git", "-C", str(self.repo), "worktree", "remove", "--force",
                        str(self.wt)], capture_output=True)
        cfg = self.repo / "config.yaml"
        if cfg.exists():
            cfg.unlink()

    def finding(self, **kw):
        base = {"file": "lib/audit_target/fees.rb", "fingerprint": f"e2e{self.n:04d}cafe", "category": "bug",
                "proof_sketch": {"type": "rspec",
                                 "expected_failure_pattern": r"expected: 33\s+got: 32"}}
        base.update(kw)
        return base

    def prove(self, record, spec_text, env_extra=None, expect_rc=0):
        d = Path(self._tmp.name)
        fj = d / f"finding{self.n}.json"
        fj.write_text(json.dumps(record))
        spec = d / f"proof{self.n}.rb"
        spec.write_text(spec_text)
        env = dict(os.environ)
        env.update(env_extra or {})
        proc = subprocess.run(
            [str(SCRIPTS / "prove.sh"), str(fj), str(spec), "--worktree", str(self.wt),
             "--repo", str(self.repo), "--log-dir", str(self.log_dir)],
            capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, expect_rc, proc.stderr)
        return json.loads(proc.stdout) if expect_rc == 0 else proc

    def test_red_proof(self):
        out = self.prove(self.finding(), FEE_SPEC)
        self.assertEqual(out["result"], "red", out)
        self.assertEqual(out["runs"], ["red", "red"])
        self.assertIn("expected: 33", out["failure_message"])
        self.assertIsNone(out["fail_then_pass"])
        self.assertFalse(out["network_isolated"])  # macOS: no unshare
        self.assertTrue((self.log_dir / "run1.log").exists())
        self.assertTrue((self.log_dir / "run2.log").exists())
        self.assertTrue((self.log_dir / "prove.json").exists())

    def test_host_proofs_require_operator_opt_in(self):
        out = self.prove(self.finding(), FEE_SPEC, {"HABEAS_ALLOW_HOST_PROOFS": ""})
        self.assertEqual(out["reason"], "host_execution_disabled")
        self.assertFalse((self.log_dir / "run1.log").exists())

    def test_main_checkout_rejected(self):
        wt = self.wt
        try:
            self.wt = self.repo
            proc = self.prove(self.finding(), FEE_SPEC, expect_rc=1)
        finally:
            self.wt = wt
        self.assertIn("distinct linked git worktree", proc.stderr)

    def test_process_status_must_agree_with_logged_failure(self):
        red = Path(self._tmp.name) / "fixture-red.log"
        red.write_text(RED_LOG)
        (self.repo / "config.yaml").write_text(f'proof:\n  test_cmd: "cat {red}"\n')
        record = self.finding()
        record["proof_sketch"]["expected_failure_pattern"] = "expected: 59"
        self.assertEqual(self.prove(record, FEE_SPEC)["reason"], "unexpected_exit")

    def test_wrong_pattern_fails(self):
        record = self.finding()
        record["proof_sketch"]["expected_failure_pattern"] = "NoMethodError"
        out = self.prove(record, FEE_SPEC)
        self.assertEqual(out["result"], "failed")
        self.assertEqual(out["reason"], "pattern_mismatch")
        self.assertEqual(out["runs"], ["failed"])

    def test_passing_spec_fails_the_gate(self):
        spec = FEE_SPEC.replace("fee_cents(100)).to eq(33)", "fee_cents(600)).to eq(47)")
        out = self.prove(self.finding(), spec)
        self.assertEqual(out["result"], "failed")
        self.assertEqual(out["reason"], "zero_failures")

    def test_broken_spec_fails_gate_a(self):
        out = self.prove(self.finding(), "def (broken ruby\n")
        self.assertEqual(out["result"], "failed")
        self.assertEqual(out["reason"], "errors_outside_examples")

    def test_env_is_scrubbed_and_db_url_overridden(self):
        record = self.finding()
        record["proof_sketch"]["expected_failure_pattern"] = \
            r"LEAKME_PRESENT=false DB=postgres://localhost/habeas_throwaway"
        spec = ('RSpec.describe "audit: env probe" do\n'
                '  it "reports env" do\n'
                '    raise "LEAKME_PRESENT=#{ENV.key?(\'LEAKME\')} DB=#{ENV[\'DATABASE_URL\']}"\n'
                '  end\nend\n')
        out = self.prove(record, spec, env_extra={"LEAKME": "supersecret"})
        self.assertEqual(out["result"], "red", out)

    def make_fix_diff(self):
        fees = self.wt / "lib" / "audit_target" / "fees.rb"
        original = fees.read_text()
        self.assertIn(FEE_FIX_OLD, original)
        fees.write_text(original.replace(FEE_FIX_OLD, FEE_FIX_NEW))
        diff = subprocess.run(["git", "-C", str(self.wt), "diff"],
                              capture_output=True, text=True, check=True).stdout
        subprocess.run(["git", "-C", str(self.wt), "checkout", "--", "."], check=True)
        return diff

    def test_security_fail_then_pass(self):
        record = self.finding(category="security", fix_sketch=self.make_fix_diff())
        out = self.prove(record, FEE_SPEC)
        self.assertEqual(out["result"], "red", out)
        self.assertIs(out["fail_then_pass"], True)
        # worktree restored to the buggy state
        self.assertIn(FEE_FIX_OLD, (self.wt / "lib" / "audit_target" / "fees.rb").read_text())

    def test_security_without_fix_is_failed(self):
        out = self.prove(self.finding(category="security"), FEE_SPEC)
        self.assertEqual(out["result"], "failed")
        self.assertEqual(out["reason"], "security_requires_fail_then_pass")
        self.assertEqual(out["runs"], ["red", "red"])

    def test_http_type_quarantines_without_executing(self):
        record = self.finding()
        record["proof_sketch"]["type"] = "http"
        out = self.prove(record, FEE_SPEC)
        self.assertEqual(out["result"], "quarantine")
        self.assertEqual(out["reason"], "harness_missing")
        self.assertFalse((self.wt / "spec" / "audit").exists())

    def test_timeout_fails_gate_a(self):
        (self.repo / "config.yaml").write_text("proof:\n  timeout_s: 3\n")
        spec = 'RSpec.describe "audit: slow" do\n  it "hangs" do\n    sleep 60\n  end\nend\n'
        out = self.prove(self.finding(), spec)
        self.assertEqual(out["result"], "failed")
        self.assertEqual(out["reason"], "no_summary")
        self.assertIn("hard timeout", (self.log_dir / "run1.log").read_text())

    def test_container_mode_without_image_refused(self):
        (self.repo / "config.yaml").write_text("sandbox:\n  mode: container\n")
        proc = self.prove(self.finding(), FEE_SPEC, expect_rc=1)
        self.assertIn("container_image", proc.stderr)

    def test_none_mode_requires_env_ack(self):
        (self.repo / "config.yaml").write_text("sandbox:\n  mode: none\n")
        env = {k: v for k, v in os.environ.items() if k != "HABEAS_I_UNDERSTAND"}
        d = Path(self._tmp.name)
        fj = d / f"finding{self.n}.json"
        fj.write_text(json.dumps(self.finding()))
        spec = d / f"proof{self.n}.rb"
        spec.write_text(FEE_SPEC)
        proc = subprocess.run(
            [str(SCRIPTS / "prove.sh"), str(fj), str(spec), "--worktree", str(self.wt),
             "--repo", str(self.repo), "--log-dir", str(self.log_dir)],
            capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("HABEAS_I_UNDERSTAND", proc.stderr)
        # and with the ack it runs (loudly)
        env["HABEAS_I_UNDERSTAND"] = "1"
        proc = subprocess.run(
            [str(SCRIPTS / "prove.sh"), str(fj), str(spec), "--worktree", str(self.wt),
             "--repo", str(self.repo), "--log-dir", str(self.log_dir)],
            capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("WARNING", proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["result"], "red")


    def write_fake_jest(self):
        """A stand-in jest command: red unless the test file contains PASSME."""
        d = Path(self._tmp.name)
        red, green = d / "jest_red.log", d / "jest_green.log"
        red.write_text(JEST_RED_LOG)
        green.write_text(JEST_GREEN_LOG)
        fake = d / "fake_jest.sh"
        fake.write_text("#!/usr/bin/env bash\n"
                        f'if grep -Fq "(amount_cents * 29 + 500)" lib/audit_target/fees.rb; then cat {green}; exit 0; fi\n'
                        f"cat {red}; exit 1\n")
        fake.chmod(0o755)
        return fake

    def test_jest_type_without_jest_cmd_quarantines(self):
        record = self.finding()
        record["proof_sketch"] = {"type": "jest", "expected_failure_pattern": "x"}
        out = self.prove(record, "it('x', () => {})\n")
        self.assertEqual(out["result"], "quarantine")
        self.assertEqual(out["reason"], "harness_missing")

    def test_fix_cannot_change_the_generated_test(self):
        record = self.finding(category="security")
        rel = f"spec/audit/audit_{record['fingerprint']}_spec.rb"
        record["fix_sketch"] = (f"--- a/{rel}\n+++ b/{rel}\n@@ -1 +1 @@\n"
                                "-# frozen_string_literal: true\n+# edited proof\n")
        out = self.prove(record, FEE_SPEC)
        self.assertEqual(out["reason"], "security_requires_fail_then_pass")
        self.assertFalse((self.log_dir / "runfix.log").exists())
        self.assertEqual((self.wt / rel).read_text(), FEE_SPEC)

    def test_jest_type_routes_to_jest_cmd_and_parser(self):
        fake = self.write_fake_jest()
        (self.repo / "config.yaml").write_text(
            f'proof:\n  test_cmd_jest: "{fake} {{file}}"\n  jest_dir: "web/__audit__"\n')
        record = self.finding()
        record["proof_sketch"] = {"type": "jest",
                                  "expected_failure_pattern": r"Expected: 33\s+Received: 32"}
        out = self.prove(record, "it('rounds', () => { expect(32).toBe(33); });\n")
        self.assertEqual(out["result"], "red", out)
        self.assertTrue((self.wt / "web" / "__audit__"
                         / f"audit_{record['fingerprint']}.test.tsx").exists())
        self.assertFalse((self.wt / "spec" / "audit").exists())

    def test_jest_fail_then_pass(self):
        fake = self.write_fake_jest()
        (self.repo / "config.yaml").write_text(
            f'proof:\n  test_cmd_jest: "{fake} {{file}}"\n')
        record = self.finding(category="security")
        record["proof_sketch"] = {"type": "jest", "expected_failure_pattern": "Expected: 33"}
        record["fix_sketch"] = self.make_fix_diff()
        out = self.prove(record, "it('rounds', () => {});\n")
        self.assertEqual(out["result"], "red", out)
        self.assertIs(out["fail_then_pass"], True)

def _docker_proof_image() -> bool:
    import shutil
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "image", "inspect", "habeas-proof-ruby:latest"],
                          capture_output=True).returncode == 0


@unittest.skipUnless(_docker_proof_image(),
                     "needs docker + habeas-proof-ruby:latest "
                     "(FROM ruby, RUN gem install rspec)")
class ContainerSandboxE2E(ProveE2E):
    """Same fixture, sandbox.mode: container — docker, --network none,
    worktree-only mount. Skipped where the proof image is absent."""

    def prove(self, record, spec_text, env_extra=None, expect_rc=0):
        (self.repo / "config.yaml").write_text(
            'proof:\n  test_cmd: "rspec {file}"\n'
            "sandbox:\n  mode: container\n"
            "  container_image: habeas-proof-ruby:latest\n")
        return super().prove(record, spec_text, env_extra, expect_rc)

    # restrict to the two container-meaningful cases
    def test_red_proof(self):
        spec = 'require_relative "../../lib/audit_target"\n' + FEE_SPEC
        result = self.prove(self.finding(), spec)
        self.assertEqual(result["result"], "red", result)
        self.assertTrue(result["network_isolated"])  # --network none

    def test_timed_out_container_is_removed(self):
        # Docker's default hostname identifies this exact test container.
        (self.repo / "config.yaml").write_text(
            'proof:\n  timeout_s: 3\n  test_cmd: "hostname && sleep 30"\n'
            'sandbox:\n  mode: container\n  container_image: habeas-proof-ruby:latest\n')
        out = ProveE2E.prove(self, self.finding(), FEE_SPEC)
        self.assertEqual(out["result"], "failed")
        log = (self.log_dir / "run1.log").read_text()
        self.assertIn("hard timeout", log)
        container_id = log.splitlines()[0]
        self.assertRegex(container_id, r"^[0-9a-f]{12,64}$")
        remaining = subprocess.check_output(
            ["docker", "ps", "-aq", "--filter", f"id={container_id}"], text=True)
        self.assertEqual(remaining.strip(), "")

    def test_container_writes_do_not_change_the_host_worktree(self):
        record = self.finding()
        record["proof_sketch"]["expected_failure_pattern"] = "isolated fixture"
        spec = """RSpec.describe "container scratch space" do
  it "writes inside the disposable copy" do
    File.write("container-marker", "fixture")
    raise "isolated fixture"
  end
end
"""
        result = self.prove(record, spec)
        self.assertEqual(result["result"], "red", result)
        self.assertFalse((self.wt / "container-marker").exists())

    def test_network_is_cut(self):
        record = self.finding()
        record["proof_sketch"]["expected_failure_pattern"] = \
            r"NETWORK_PROBE=(Socket::ResolutionError|SocketError|Errno::|Net::OpenTimeout)"
        spec = ('require "net/http"\n'
                'RSpec.describe "audit: net probe" do\n'
                '  it "probes" do\n'
                '    probe = begin\n'
                '      Net::HTTP.start("example.com", 80, open_timeout: 3) { |h| h.get("/") }\n'
                '      "reachable"\n'
                '    rescue SocketError, SystemCallError, Net::OpenTimeout => e\n'
                '      e.class.to_s\n'
                '    end\n'
                '    raise "NETWORK_PROBE=#{probe}"\n'
                '  end\nend\n')
        result = self.prove(record, spec)
        self.assertEqual(result["result"], "red", result)
        self.assertNotIn("NETWORK_PROBE=reachable", result["failure_message"])

    # inherited env-mode cases don't apply under this config
    test_host_proofs_require_operator_opt_in = None
    test_main_checkout_rejected = None
    test_process_status_must_agree_with_logged_failure = None
    test_wrong_pattern_fails = None
    test_passing_spec_fails_the_gate = None
    test_broken_spec_fails_gate_a = None
    test_env_is_scrubbed_and_db_url_overridden = None
    test_security_fail_then_pass = None
    test_security_without_fix_is_failed = None
    test_http_type_quarantines_without_executing = None
    test_timeout_fails_gate_a = None
    test_container_mode_without_image_refused = None
    test_none_mode_requires_env_ack = None
    test_jest_type_without_jest_cmd_quarantines = None
    test_fix_cannot_change_the_generated_test = None
    test_jest_type_routes_to_jest_cmd_and_parser = None
    test_jest_fail_then_pass = None
    write_fake_jest = None
    make_fix_diff = None


if __name__ == "__main__":
    unittest.main()
