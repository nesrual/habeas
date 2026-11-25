"""Phase 4: the proof gate. Red-green on the sample app, the proof check,
the native and scripted provers, and a clean clone after every attempt."""

import json
import os
import unittest
from pathlib import Path

import engine_support as es
from engine import verify
from engine_support import CONTROLLER, GOOD_TEST, schema_name

ALLOW = {"HABEAS_ALLOW_HOST_PROOFS": "1"}


class TestLogClassification(unittest.TestCase):
    RED = ("F\n\nFailures:\n\n  1) x a\n     Failure/Error: expect(1).to eq(2)\n\n       expected: 2\n"
           "            got: 1\n     # ./x_spec.rb:2\n\nFinished in 0.01 seconds\n1 example, 1 failure\n\n"
           "Failed examples:\n\nrspec ./x_spec.rb:2 # x a\n")

    def test_assertion_failure_is_red(self):
        self.assertEqual(verify.classify(1, self.RED), "red")
        self.assertEqual(verify.parse_rspec(self.RED)["failed_examples"], {"./x_spec.rb # x a"})

    def test_exception_load_error_and_no_summary_are_not_red(self):
        name_error = self.RED.replace("\n       expected: 2\n            got: 1\n",
                                      "     NameError:\n       uninitialized constant Nope\n")
        self.assertEqual(verify.classify(1, name_error), "error")
        load = ("An error occurred while loading ./y_spec.rb.\nFinished in 0.0 seconds\n"
                "0 examples, 0 failures, 1 error occurred outside of examples\n")
        self.assertEqual(verify.classify(1, load), "error")
        self.assertEqual(verify.classify(1, "ruby: command not found"), "error")
        self.assertEqual(verify.classify("timeout", ""), "timeout")

    def test_inline_failures_without_a_failures_header(self):
        # a custom progress formatter prints failure blocks with no "Failures:" line
        inline = ("LOADING SHARED ENVS\n\nProgress: |\n\n  1) Thing rejects bad input\n"
                  "     Failure/Error: expect(result).to be_failure\n       expected `result.failure?` to be truthy, got false\n"
                  "     # ./spec/x_spec.rb:9\n\nProgress: |====|\n\nFinished in 1.8 seconds (files took 3 seconds to load)\n"
                  "1 example, 1 failure\n\nFailed examples:\n\nrspec ./spec/x_spec.rb:7 # Thing rejects bad input\n")
        self.assertEqual(verify.classify(1, inline), "red")
        crashed = inline.replace("       expected `result.failure?` to be truthy, got false\n",
                                 "\n     NameError:\n       uninitialized constant VirtualBankRepository\n")
        self.assertEqual(verify.classify(1, crashed), "error")

    def test_green(self):
        self.assertEqual(verify.classify(0, "Finished in 0.1 seconds\n2 examples, 0 failures\n"), "green")
        self.assertEqual(verify.classify(0, "Finished in 0.1 seconds\n0 examples, 0 failures\n"), "error")


class ProofCase(unittest.TestCase):
    def setup(self, script=None, mutate=None):
        self.s = es.pipeline(self, script=script, mutate=mutate)
        (self.cluster,) = self.s.rows("SELECT * FROM clusters")
        self.assertEqual(self.cluster["state"], "upheld")
        self.cid = self.cluster["cluster_id"]
        self.work = self.s.root / "work"
        self.work.mkdir()
        return self.s

    def files(self, test=GOOD_TEST, patch=None):
        n = len(list(self.work.iterdir()))
        t, p = self.work / f"t{n}_spec.rb", self.work / f"f{n}.patch"
        t.write_text(test)
        p.write_text(patch if patch is not None else es.good_patch(self.s.repo))
        return str(t), str(p)

    def verify(self, test=GOOD_TEST, patch=None, prover="prover-native", env=ALLOW):
        t, p = self.files(test, patch)
        code, out, err = self.s.cli("verify-proof", self.cid, "--test", t, "--fix", p, "--prover", prover,
                                    "--json", env=env)
        self.assertEqual(es.git(self.s.repo, "status", "--porcelain", "--untracked-files=all"), "",
                         "the audit clone must be clean after every attempt")
        return code, (json.loads(out) if out.strip().startswith("{") else out), err

    def proofs(self):
        return self.s.rows("SELECT * FROM proofs ORDER BY created_at")

    def state(self):
        return self.s.rows("SELECT state FROM clusters")[0]["state"]


class TestMechanical(ProofCase):
    def test_correct_proof_passes_and_is_stored(self):
        self.setup()
        code, v, err = self.verify()
        self.assertEqual(code, 0, err)
        self.assertEqual((v["status"], v["mechanical"], v["checker"], v["checker_verdict"]),
                         ("valid", "pass", "checker", "accept"))
        (proof,) = self.proofs()
        self.assertEqual((proof["status"], proof["mechanical"], proof["attempt"], proof["checker_severity"]),
                         ("valid", "pass", 1, "high"))
        for key in ("test_path", "fix_patch_path", "red_log", "green_log", "regress_log"):
            self.assertTrue(Path(proof[key]).is_file(), key)
            self.assertIn(str(self.s.run_dir / "proofs" / self.cid), proof[key])
        self.assertIn("1 example, 1 failure", Path(proof["red_log"]).read_text())
        self.assertIn("1 example, 0 failures", Path(proof["green_log"]).read_text())
        self.assertIn("3 examples, 0 failures", Path(proof["regress_log"]).read_text())
        self.assertEqual(v["test_destination"], f"engines/billing/spec/habeas_proofs/{self.cid}_spec.rb")
        self.assertEqual(self.state(), "proven")
        # the checker saw the claim, the test, the fix and the logs
        (check,) = [r["body"] for r in self.s.server.requests if schema_name(r["body"]) == "proof_check"]
        user = check["messages"][1]["content"]
        for needle in ("unscoped lookup", "denies show to an account", "authorize(Invoice.find", "1 example, 1 failure"):
            self.assertIn(needle, user)
        # a proven cluster takes no further proofs
        code, _, err = self.verify()
        self.assertEqual(code, 1)
        self.assertIn("is proven", err)

    def verify_in(self, package):
        t, p = self.files()
        code, out, _ = self.s.cli("verify-proof", self.cid, "--test", t, "--fix", p, "--test-package", package,
                                  "--json", env=ALLOW)
        self.assertEqual(es.git(self.s.repo, "status", "--porcelain", "--untracked-files=all"), "")
        return json.loads(out)

    def test_the_test_may_live_in_the_package_that_owns_the_fix(self):
        self.assertEqual(verify.test_destination("c1", "engines/billing/app/x.rb", "engines/ledger"),
                         "engines/ledger/spec/habeas_proofs/c1_spec.rb")
        self.assertEqual(verify.test_destination("c1", "engines/billing/app/x.rb", " "),
                         "engines/billing/spec/habeas_proofs/c1_spec.rb")
        self.setup()
        v = self.verify_in("engines/nope")
        self.assertEqual((v["status"], v["reason"]), ("invalid", "test_package_unknown: engines/nope"))
        self.assertIn("test_package_unknown", verify.package_problem(self.s.repo, "../outside", []))
        self.assertIsNone(verify.package_problem(self.s.repo, "", []))
        # an existing package, but the fix changes nothing in it
        v = self.verify_in("engines/ledger")
        self.assertEqual(v["status"], "invalid")
        self.assertIn("test_package_without_fix", v["reason"])
        self.assertEqual(self.state(), "upheld")

    def test_naming_the_fixed_package_is_accepted_and_the_destination_is_stored(self):
        self.setup()
        v = self.verify_in("engines/billing")
        self.assertEqual(v["status"], "valid")
        (proof,) = self.proofs()
        self.assertEqual(proof["test_destination"], f"engines/billing/spec/habeas_proofs/{self.cid}_spec.rb")
        self.assertTrue(proof["prover_model"])

    def test_stored_proofs_can_be_checked_again_after_a_gate_fault(self):
        self.setup()
        self.verify()
        # as if the gate had wrongly rejected this proof
        import sqlite3
        db = sqlite3.connect(self.s.run_dir / "ledger.sqlite")
        with db:
            db.execute("UPDATE proofs SET status='invalid', mechanical='fail', checker_verdict=NULL,"
                       " reason='red_is_not_an_assertion_failure'")
            db.execute("UPDATE clusters SET state='upheld'")
        db.close()
        code, out, err = self.s.cli("prove", "recheck", "--reason", "no_such_reason%", env=ALLOW)
        self.assertIn("0 stored proofs match", out)
        code, out, err = self.s.cli("prove", "recheck", "--reason", "red_is_not%", env=ALLOW)
        self.assertEqual(code, 0, err)
        self.assertIn("1 now accepted", out)
        (proof,) = self.proofs()
        self.assertEqual((proof["status"], proof["mechanical"]), ("valid", "pass"))
        self.assertIn("re-checked", proof["reason"])
        self.assertEqual(self.state(), "proven")
        self.assertEqual(es.git(self.s.repo, "status", "--porcelain", "--untracked-files=all"), "")

    def test_host_proofs_need_the_explicit_opt_in(self):
        self.setup()
        code, _, err = self.verify(env={})
        self.assertEqual(code, 1)
        self.assertIn("HABEAS_ALLOW_HOST_PROOFS=1", err)
        self.assertEqual(self.proofs(), [])       # a refused precondition uses no attempt

    def test_container_runs_do_not_need_the_host_opt_in(self):
        def container(m, r):
            r["target"]["proof_environment"]["run_tests_in"] = "dev_container"
        self.setup(mutate=container)
        code, v, err = self.verify(env={})
        self.assertEqual((code, v["status"]), (0, "valid"), err)

    def test_outbound_http_must_be_confirmed(self):
        def unconfirmed(m, r):
            r["target"]["proof_environment"]["outbound_http_blocked"] = None
        self.setup(mutate=unconfirmed)
        code, _, err = self.verify()
        self.assertEqual(code, 1)
        self.assertIn("blocks unrecorded outbound HTTP", err)

    def test_dirty_clone_is_refused(self):
        self.setup()
        (self.s.repo / "stray.txt").write_text("x")
        t, p = self.files()
        code, _, err = self.s.cli("verify-proof", self.cid, "--test", t, "--fix", p, env=ALLOW)
        self.assertEqual(code, 1)
        self.assertIn("not clean", err)
        self.assertEqual(self.proofs(), [])

    def rejected(self, reason, **kw):
        code, v, err = self.verify(**kw)
        self.assertEqual(code, 3, err)
        self.assertEqual((v["status"], v["mechanical"]), ("invalid", "fail"))
        self.assertIn(reason, v["reason"])
        self.assertEqual(self.state(), "upheld")
        self.assertFalse(any(schema_name(r["body"]) == "proof_check" for r in self.s.server.requests))
        return v

    def test_test_that_fails_on_load_is_rejected(self):
        self.setup()
        self.rejected("red_is_not_an_assertion_failure", test="Missing::Constant.call\n" + GOOD_TEST)

    def test_test_that_raises_instead_of_asserting_is_rejected(self):
        self.setup()
        self.rejected("red_is_not_an_assertion_failure",
                      test=GOOD_TEST.replace("expect { controller.show }.to raise_error(Sample::NotAuthorized)",
                                             "NoSuchThing.call"))

    def test_test_that_passes_on_current_code_is_rejected(self):
        self.setup()
        self.rejected("test_passes_on_current_code",
                      test=GOOD_TEST.replace("expect { controller.show }.to raise_error(Sample::NotAuthorized)",
                                             "expect(controller.show[:id]).to eq(10)"))

    def test_fix_that_does_not_turn_the_test_green_is_rejected(self):
        self.setup()
        patch = es.make_patch(self.s.repo, CONTROLLER, "status: invoice.status }", "status: invoice.status.to_s }")
        self.rejected("test_still_fails_after_fix", patch=patch)

    def test_test_edited_between_red_and_green_is_rejected(self):
        self.setup()
        dest = f"engines/billing/spec/habeas_proofs/{self.cid}_spec.rb"
        edit = (f"--- /dev/null\n+++ b/{dest}\n@@ -0,0 +1,5 @@\n+RSpec.describe \"x\" do\n+  it \"passes\" do\n"
                "+    expect(1).to eq(1)\n+  end\n+end\n")
        self.rejected("fix_touches_test", patch=es.good_patch(self.s.repo) + edit)

    def test_oversized_fix_is_rejected(self):
        def small(m, r):
            r["provers"]["max_fix_lines"] = 1
        self.setup(mutate=small)
        self.rejected("oversized_fix")

    def test_fix_touching_too_many_files_or_escaping_the_repo_is_rejected(self):
        def one_file(m, r):
            r["provers"]["max_fix_files"] = 1
            r["provers"]["max_attempts_per_prover"] = 5
        self.setup(mutate=one_file)
        second = es.make_patch(self.s.repo, "README.md", "# sample_app", "# sample app")
        self.rejected("oversized_fix", patch=es.good_patch(self.s.repo) + second)
        escape = "--- a/../outside.rb\n+++ b/../outside.rb\n@@ -1 +1 @@\n-a\n+b\n"
        self.rejected("fix_path_not_allowed", patch=escape)
        self.rejected("fix_does_not_apply", patch=es.good_patch(self.s.repo).replace("invoice = Invoice.find", "invoice = Nope.find"))

    def test_fix_that_breaks_a_related_spec_is_rejected(self):
        self.setup()
        patch = es.make_patch(
            self.s.repo, CONTROLLER,
            "    def show\n      invoice = Invoice.find(params[:id])",
            "    def show\n      raise Sample::NotAuthorized\n      invoice = Invoice.find(params[:id])")
        v = self.rejected("regression", patch=patch)
        self.assertIn("1 new failures", v["reason"])

    def flaky(self, counter, fail_when):
        return GOOD_TEST.replace(
            "    expect { controller.show }.to raise_error(Sample::NotAuthorized)",
            f"    n = File.exist?({str(counter)!r}) ? File.read({str(counter)!r}).to_i : 0\n"
            f"    File.write({str(counter)!r}, (n + 1).to_s)\n"
            f"    expect {{ controller.show }}.to raise_error(Sample::NotAuthorized) if {fail_when}\n")

    def test_test_failing_on_one_of_two_red_runs_is_rejected(self):
        def twice(m, r):
            r["provers"]["repeat"] = 2
        self.setup(mutate=twice)
        self.rejected("red_flaky: failed 1 of 2 runs", test=self.flaky(self.s.root / "n1", "n == 0"))
        code, v, _ = self.verify()          # a steady proof still passes with repeat 2
        self.assertEqual((code, v["status"]), (0, "valid"))
        self.assertEqual(len(list(Path(v["dir"]).glob("red_*.log"))), 2)
        self.assertEqual(len(list(Path(v["dir"]).glob("green_*.log"))), 2)

    def test_concurrency_thresholds(self):
        # concurrency_repeat 4, concurrency_min_red 2 (see engine_support)
        self.setup()
        self.s.conn.execute("UPDATE clusters SET lens = 'concurrency'")
        self.s.conn.commit()
        self.rejected("red_too_rare: failed 1 of 4 runs", test=self.flaky(self.s.root / "n1", "n == 0"))
        # fails on 2 of the 4 runs before the fix, never after it: accepted
        code, v, err = self.verify(test=self.flaky(self.s.root / "n2", "n < 2 || n >= 4"))
        self.assertEqual((code, v["status"]), (0, "valid"), err)
        self.assertEqual(v["mechanical"], "pass")

    def test_concurrency_proof_that_fails_once_after_the_fix_is_rejected(self):
        def no_auth_on_fourth(m, r):
            r["provers"]["concurrency_min_red"] = 4
        self.setup(mutate=no_auth_on_fourth)
        self.s.conn.execute("UPDATE clusters SET lens = 'concurrency'")
        self.s.conn.commit()
        test = GOOD_TEST.replace(
            "    expect { controller.show }.to raise_error(Sample::NotAuthorized)",
            f"    c = {str(self.s.root / 'n3')!r}\n    n = File.exist?(c) ? File.read(c).to_i : 0\n"
            "    File.write(c, (n + 1).to_s)\n    expect(n).not_to eq(6)\n"
            "    expect { controller.show }.to raise_error(Sample::NotAuthorized)\n")
        self.rejected("test_still_fails_after_fix: passed 3 of 4 runs", test=test)

    def test_timeout_is_recorded_and_the_clone_is_left_clean(self):
        def short(m, r):
            r["provers"]["timeout_minutes"] = 0.03
        self.setup(mutate=short)
        code, v, err = self.verify(test=GOOD_TEST.replace("Billing::Account.create(id: 1", "sleep 30; Billing::Account.create(id: 1"))
        self.assertEqual((code, v["status"], v["clone_clean"]), (3, "timeout", True), err)
        self.assertEqual(self.proofs()[0]["status"], "timeout")
        self.assertFalse((self.s.repo / "engines/billing/spec/habeas_proofs").exists())


class TestProofCheckAndProvers(ProofCase):
    def checker(self, by_model):
        def script(body, n):
            if schema_name(body) == "proof_check":
                answer = by_model.get(body["model"], "accept")
                if answer in ("refusal", "invalid_json"):
                    return {"kind": answer}
                return {"kind": "valid", "json": {
                    "demonstrates_claim": answer == "accept", "fix_minimal_at_root_cause": True,
                    "verdict": answer, "severity": "medium", "reason": f"says {answer}"}}
            return {"kind": "valid"}
        return script

    def test_rejected_proof_is_invalid_and_the_other_prover_still_runs(self):
        self.setup(self.checker({"b/check-model": "reject"}))
        code, v, _ = self.verify()
        self.assertEqual((code, v["status"], v["mechanical"], v["checker_verdict"]), (3, "invalid", "pass", "reject"))
        self.assertEqual(self.state(), "upheld")
        from engine import prove
        self.assertEqual(prove.queue(self.s.conn, self.s.cfg(), "prover-c"), [self.cid])
        self.assertEqual(prove.queue(self.s.conn, self.s.cfg(), "prover-native"), [self.cid])

    def test_refused_checker_falls_back_to_the_alternate_lab(self):
        self.setup(self.checker({"b/check-model": "refusal"}))
        code, v, _ = self.verify()
        self.assertEqual((code, v["status"], v["checker"]), (0, "valid", "checker-alt"))

    def test_unchecked_proof_is_never_accepted(self):
        self.setup(self.checker({"b/check-model": "refusal", "a/check-model": "invalid_json"}))
        code, v, _ = self.verify()
        self.assertEqual((code, v["status"], v["mechanical"]), (3, "error", "pass"))
        self.assertIn("not accepted", v["reason"])
        self.assertEqual(self.state(), "upheld")

    def test_checker_is_never_from_the_provers_lab(self):
        def same_lab(m, r):
            m["seats"]["prover"][1]["lab"] = "b"       # prover-c now shares lab b with `checker`
            m["seats"]["proof_checker"][1]["lab"] = "z"
        self.setup(mutate=same_lab)
        code, v, err = self.verify(prover="prover-c")
        self.assertEqual((code, v["checker"]), (0, "checker-alt"), err)

    def test_native_prover_brief_refusal_and_attempt_limit(self):
        self.setup()
        code, out, err = self.s.cli("prove", "next", "--prover", "prover-native", "--json")
        self.assertEqual(code, 0, err)
        b = json.loads(out)
        self.assertEqual((b["cluster_id"], b["attempt"]), (self.cid, 1))
        brief = Path(b["brief"]).read_text()
        for needle in ("Every record lookup is scoped.", "unscoped lookup", "at most 40 lines in at most 3 files",
                       f"bin/engine-run billing rspec engines/billing/spec/habeas_proofs/{self.cid}_spec.rb",
                       f"habeas verify-proof {self.cid}", "=== CONTEXT", "def show"):
            self.assertIn(needle, brief)
        bad = GOOD_TEST.replace("raise_error(Sample::NotAuthorized)", "raise_error(KeyError)")
        for attempt in (1, 2):
            code, v, _ = self.verify(test=bad, patch=es.good_patch(self.s.repo))
            self.assertEqual((v["status"], v["attempt"]), ("invalid", attempt))
        code, _, err = self.verify()
        self.assertEqual(code, 1)
        self.assertIn("has used its attempts", err)
        self.assertEqual(self.state(), "upheld")           # the other prover has not run yet
        self.assertEqual(json.loads(self.s.cli("prove", "next", "--prover", "prover-native", "--json")[1])["cluster_id"], None)
        code, out, _ = self.s.cli("prove", "refuse", self.cid, "--prover", "prover-c", "--reason", "no")
        self.assertEqual(code, 0)
        self.assertEqual(self.state(), "unproven")         # every enabled prover is done
        self.assertEqual([p["status"] for p in self.proofs()], ["invalid", "invalid", "refused"])

    def prover_script(self, answers):
        def script(body, n):
            if schema_name(body) == "prover_output":
                answer = answers.pop(0)
                return answer if "kind" in answer else {"kind": "valid", "json": answer}
            return {"kind": "valid"}
        return script

    @staticmethod
    def step(action, **kw):
        return {"action": action, "paths": [], "query": "", "reason": "", "test_file": "", "test_package": "", "edits": [], **kw}

    EDITS = [{"path": CONTROLLER,
              "old": "invoice = Invoice.find(params[:id])\n      { id: invoice.id, amount",
              "new": "invoice = authorize(Invoice.find(params[:id]), :show?)\n      { id: invoice.id, amount"}]

    def prover_requests(self):
        return [r["body"]["messages"][1]["content"] for r in self.s.server.requests
                if schema_name(r["body"]) == "prover_output"]

    def run_prover(self):
        code, out, err = self.s.cli("prove", "run", "--prover", "prover-c", env=ALLOW)
        self.assertEqual(es.git(self.s.repo, "status", "--porcelain", "--untracked-files=all"), "")
        return code, out + err

    def test_scripted_prover_browses_the_clone_then_proves(self):
        spec = "engines/billing/spec/controllers/billing/invoices_controller_spec.rb"
        answers = [self.step("read", paths=[spec, "engines/billing/spec/spec_helper.rb"]),
                   self.step("search", query="NotAuthorized", paths=["engines/billing"]),
                   self.step("list", paths=["engines/billing/app/policies"]),
                   self.step("submit", test_file=GOOD_TEST, edits=self.EDITS)]
        self.setup(self.prover_script(answers))
        code, out = self.run_prover()
        self.assertEqual(code, 0, out)
        self.assertIn("1 proven", out)
        self.assertEqual([(p["status"], p["attempt"]) for p in self.proofs()], [("valid", 1)])
        sent = self.prover_requests()
        self.assertEqual(len(sent), 4)
        # the first request already names the specs next to the claimed code
        self.assertIn(f"Existing specs for the claimed files: {spec}", sent[0])
        self.assertIn("Step 1 of 8", sent[0])
        self.assertIn('it "denies an update from another account" do', sent[1])        # the spec it read
        self.assertIn("require File.join(root, \"lib/sample/support\")", sent[1])      # the spec helper
        self.assertIn("engines/billing/app/controllers/billing/application_controller.rb:14:", sent[2])
        self.assertIn("engines/billing/app/policies/billing/invoice_policy.rb", sent[3])
        # the engine built the patch from the edits, and it is a real unified diff
        patch = Path(self.proofs()[0]["fix_patch_path"]).read_text()
        self.assertIn(f"--- a/{CONTROLLER}", patch)
        self.assertIn("+      invoice = authorize(Invoice.find(params[:id]), :show?)", patch)

    def test_browsing_is_limited_to_safe_tracked_files(self):
        def plant(repo):
            (repo / "engines/billing/spec/fixtures").mkdir(parents=True)
            (repo / "engines/billing/spec/fixtures/customer.rb").write_text("REAL = 1\n")
            (repo / "engines/billing/app/keys.rb").write_text('AWS = "AKIAABCDEFGHIJKLMNOP"\nOTHER = "findme_marker"\n')
        answers = [self.step("read", paths=["engines/billing/spec/fixtures/customer.rb", "engines/billing/app/keys.rb",
                                            "../outside.rb", "/etc/passwd", ".git/config"]),
                   self.step("search", query="findme_marker"),
                   self.step("decline", reason="nothing to see")]
        self.s = es.pipeline(self, script=self.prover_script(answers), prepare=plant)
        (self.s.root / "outside.rb").write_text("SECRET_OUTSIDE = 1\n")
        self.run_prover()
        sent = self.prover_requests()
        for needle in ("REAL = 1", "AKIAABCDEFGHIJKLMNOP", "SECRET_OUTSIDE", "root:", "[core]"):
            self.assertNotIn(needle, sent[1] + sent[2])
        self.assertIn("this kind of file is not available to provers", sent[1])
        self.assertIn("withheld: the secret scan flagged this file", sent[1])
        self.assertIn("not a tracked file in the repository", sent[1])
        self.assertIn("(no matches)", sent[2])

    def test_unusable_edit_is_explained_and_can_be_corrected_in_the_same_attempt(self):
        bad = [{"path": CONTROLLER, "old": "Invoice.find(params[:id])", "new": "x"}]     # occurs twice
        answers = [self.step("submit", test_file=GOOD_TEST, edits=bad),
                   self.step("submit", test_file=GOOD_TEST, edits=[{"path": "nope.rb", "old": "a", "new": "b"}]),
                   self.step("submit", test_file=GOOD_TEST, edits=self.EDITS)]
        self.setup(self.prover_script(answers))
        code, out = self.run_prover()
        self.assertIn("1 proven", out)
        sent = self.prover_requests()
        self.assertIn("`old` must occur exactly once in the file, it occurs 2 times", sent[1])
        self.assertIn("cannot edit nope.rb: not a tracked file", sent[2])
        self.assertEqual(len(self.proofs()), 1)          # only the real submission used an attempt

    def test_step_limit_ends_the_attempt(self):
        def two_steps(m, r):
            r["provers"]["browse"] = {"max_steps": 2}
        answers = [self.step("list", paths=["engines"])] * 6     # the last step gets one repair retry
        self.setup(self.prover_script(answers), mutate=two_steps)
        self.run_prover()
        self.assertIn("This is your last step", self.prover_requests()[1])
        self.assertEqual([(p["status"], p["reason"]) for p in self.proofs()],
                         [("error", "no submission within 2 steps")] * 2)

    def test_scripted_prover_gets_the_log_back_and_proves_on_the_second_attempt(self):
        wrong = GOOD_TEST.replace("raise_error(Sample::NotAuthorized)", "raise_error(KeyError)")
        answers = [self.step("submit", test_file=wrong, edits=self.EDITS),
                   self.step("submit", test_file=GOOD_TEST, edits=self.EDITS)]
        self.setup(self.prover_script(answers))
        code, out = self.run_prover()
        self.assertEqual(code, 0, out)
        self.assertEqual([(p["status"], p["attempt"], p["prover_seat"]) for p in self.proofs()],
                         [("invalid", 1, "prover-c"), ("valid", 2, "prover-c")])
        self.assertEqual(self.state(), "proven")
        sent = self.prover_requests()
        self.assertNotIn("previous attempt", sent[0])
        self.assertIn("Your previous attempt was not accepted: test_still_fails_after_fix", sent[1])
        self.assertIn("expected KeyError, got #<Sample::NotAuthorized", sent[1])

    def test_scripted_prover_refusal_is_recorded_and_native_can_still_prove(self):
        self.setup(self.prover_script([{"kind": "refusal"}]))
        code, out = self.run_prover()
        self.assertEqual(code, 0, out)
        self.assertEqual([(p["status"], p["prover_seat"]) for p in self.proofs()], [("refused", "prover-c")])
        self.assertEqual(self.state(), "upheld")
        code, v, _ = self.verify()
        self.assertEqual((code, v["status"]), (0, "valid"))

    def test_scripted_prover_declining_counts_as_refused(self):
        self.setup(self.prover_script([self.step("decline", reason="cannot show it")]))
        self.run_prover()
        self.assertEqual([(p["status"], p["reason"]) for p in self.proofs()], [("refused", "cannot show it")])

    def with_fallback(self, m, r):
        for seat in m["seats"]["prover"]:
            if seat["id"] == "prover-c":
                seat["alternate_model"] = "c/second"

    def prover_models(self):
        return [r["body"]["model"] for r in self.s.server.requests if schema_name(r["body"]) == "prover_output"]

    def test_a_decline_goes_to_the_fallback_model_which_proves(self):
        answers = [self.step("decline", reason="will not write this"),
                   self.step("submit", test_file=GOOD_TEST, edits=self.EDITS)]
        self.setup(self.prover_script(answers), mutate=self.with_fallback)
        code, out = self.run_prover()
        self.assertIn("1 proven", out)
        models = self.prover_models()
        self.assertEqual(models[1], "c/second")
        self.assertNotEqual(models[0], "c/second")
        self.assertEqual([p["status"] for p in self.proofs()], ["valid"])
        self.assertEqual(self.proofs()[0]["prover_model"], "c/second")     # who wrote the accepted proof

    def test_attempts_lost_to_a_rate_limit_block_finalize_and_are_repeated(self):
        limited = {"on": True}

        def script(body, n):
            if schema_name(body) == "prover_output":
                if limited["on"]:
                    return {"kind": "http", "status": 429}
                return {"kind": "valid", "json": self.step("submit", test_file=GOOD_TEST, edits=self.EDITS)}
            return {"kind": "valid"}
        self.setup(script)
        self.run_prover()
        self.assertTrue(all(p["status"] == "error" and "prover call error: http_429" in p["reason"]
                            for p in self.proofs()), self.proofs())
        # the guardrail: the run cannot be closed over a call that never reached a model
        code, out, _ = self.s.cli("finalize", "--close")
        self.assertIn("lost to failed model calls", out)
        self.assertIn("the run is PARTIAL", out)
        self.assertEqual(self.s.rows("SELECT COUNT(*) n FROM outcomes")[0]["n"], 0)
        self.assertEqual(self.s.rows("SELECT state FROM runs")[0]["state"], "running")
        # the next prover pass repeats the lost attempts without being asked to
        limited["on"] = False
        code, out = self.run_prover()
        self.assertIn("1 proven", out)
        self.assertEqual(self.state(), "proven")
        code, out, _ = self.s.cli("finalize", "--close")
        self.assertNotIn("lost to failed model calls", out)
        self.assertEqual(self.s.rows("SELECT state FROM runs")[0]["state"], "finished")

    def test_lost_attempts_can_be_accepted_explicitly(self):
        self.setup(lambda body, n: {"kind": "http", "status": 429}
                   if schema_name(body) == "prover_output" else {"kind": "valid"})
        self.run_prover()
        self.s.cli("finalize", "--close")
        self.assertEqual(self.s.rows("SELECT COUNT(*) n FROM outcomes")[0]["n"], 0)
        code, out, _ = self.s.cli("finalize", "--close", "--accept-lost-attempts")
        self.assertEqual(self.s.rows("SELECT COUNT(*) n FROM outcomes")[0]["n"], 1)
        self.assertEqual(self.s.rows("SELECT state FROM runs")[0]["state"], "finished")

    def test_both_models_declining_is_refused_with_both_reasons(self):
        answers = [self.step("decline", reason="first no"), self.step("decline", reason="second no")]
        self.setup(self.prover_script(answers), mutate=self.with_fallback)
        self.run_prover()
        (p,) = self.proofs()
        self.assertEqual(p["status"], "refused")
        self.assertIn("declined: first no | fallback c/second: second no", p["reason"])
        self.assertEqual(len(self.prover_models()), 2)

    def test_model_text_is_never_run_as_a_command(self):
        answers = []
        self.setup(self.prover_script(answers))
        marker = self.s.root / "pwned"
        cmd = f"$(touch {marker}); touch {marker}"
        answers += [self.step("read", paths=[cmd, f"; touch {marker}"]),
                    self.step("search", query=cmd, paths=[cmd]),
                    self.step("submit", test_file=GOOD_TEST, reason=cmd,
                              edits=[{"path": cmd, "old": cmd, "new": cmd}]),
                    self.step("decline", reason=cmd)]
        self.run_prover()
        self.assertFalse(marker.exists())


class DiffLinesAreNotExceptions(unittest.TestCase):
    LOG = """.F
Failures:
  1) Thing does it
     Failure/Error: expect(repo).to have_received(:create).with(hash_including(a: 1))
       #<Repo (class)> received :create with unexpected arguments
         expected: (hash_including(:a=>1))
              got: ({:b=>2})
       Diff:
       @@ -1 +1 @@
     # ./spec/x_spec.rb:24:in `block (2 levels) in <main>'
Finished in 1.36 seconds (files took 3.06 seconds to load)
2 examples, 1 failure
"""

    def test_a_diff_line_keeps_the_failure_an_assertion(self):
        from engine import verify
        self.assertEqual(verify.classify(1, self.LOG), "red")
        raised = self.LOG.replace("       Diff:\n", "").replace(
            "     Failure/Error: expect", "     Failure/Error: call\n\n     NameError:\n       undefined expect")
        self.assertEqual(verify.classify(1, raised), "error")


if __name__ == "__main__":
    unittest.main()
