"""Phase 5: outcomes, issue and quarantine files, the report, reconcile and drift."""

import json
import unittest
from pathlib import Path

import engine_support as es
from engine_support import CONTROLLER, GOOD_TEST, schema_name
from fake_openai import candidate

ALLOW = {"HABEAS_ALLOW_HOST_PROOFS": "1"}


class Case(unittest.TestCase):
    def setup(self, **kw):
        self.s = es.pipeline(self, **kw)
        self.work = self.s.root / "work"
        self.work.mkdir()
        return self.s

    def cluster_ids(self):
        return [r["cluster_id"] for r in self.s.rows("SELECT cluster_id FROM clusters ORDER BY cluster_id")]

    def prove(self, cid, test=GOOD_TEST, patch=None):
        n = len(list(self.work.iterdir()))
        t, p = self.work / f"t{n}_spec.rb", self.work / f"f{n}.patch"
        t.write_text(test)
        p.write_text(patch or es.good_patch(self.s.repo))
        return self.s.cli("verify-proof", cid, "--test", str(t), "--fix", str(p), env=ALLOW)

    def refuse_all(self, cid):
        for prover in ("prover-native", "prover-c"):
            self.assertEqual(self.s.cli("prove", "refuse", cid, "--prover", prover)[0], 0)

    def outcomes(self):
        return {r["cluster_id"]: r for r in self.s.rows("SELECT * FROM outcomes")}

    def report(self):
        return json.loads((self.s.run_dir / "report.json").read_text())


class TestOutcomes(Case):
    def test_full_dry_run_produces_issue_quarantine_and_a_report_that_reconciles(self):
        # static rules on: the controller unit gets a static candidate for `show` as well,
        # and finder-b reports an unrelated candidate on `update`
        s = self.setup(static=True, cands=[candidate(CONTROLLER, 3), candidate(CONTROLLER, 9, "update leaks")])
        states = {r["cluster_id"]: r for r in s.rows(
            "SELECT k.*, c.title FROM clusters k JOIN candidates c ON c.candidate_id = k.representative_candidate_id")}
        show = [cid for cid, r in states.items() if "update" not in r["title"]]
        (update,) = [cid for cid, r in states.items() if "update" in r["title"]]
        self.assertEqual(len(show), 1, "the static and the model candidate on `show` merge into one cluster")
        self.assertEqual(self.prove(show[0])[0], 0)
        self.refuse_all(update)
        code, out, err = s.cli("finalize")
        self.assertEqual(code, 0, out + err)
        self.assertLessEqual(len(out.splitlines()), 20)
        o = self.outcomes()
        self.assertEqual((o[show[0]]["outcome"], o[update]["outcome"]), ("proven", "quarantined"))
        self.assertEqual(o[update]["detail"], "all_provers_refused")
        self.assertEqual(s.rows("SELECT state FROM runs")[0]["state"], "finished")

        issue = (s.run_dir / o[show[0]]["issue_path"]).read_text()
        for needle in ('title: "Written title"', 'severity: "high"', 'lens: "authz_tenancy"', "raising_seats: [",
                       '"finder-a"', '"static-rules"', "audited_commit:", "## Invariant", "## How to reproduce",
                       "bin/engine-run billing rspec", "denies show to an account", "## Minimal fix",
                       "+      invoice = authorize(Invoice.find(params[:id]), :show?)", "not a merge candidate"):
            self.assertIn(needle, issue)
        entry = (s.run_dir / o[update]["issue_path"]).read_text()
        for needle in ('triage_state: "open"', "critic-a: **uphold**", "prover-native attempt 1: **refused**",
                       "all_provers_refused"):
            self.assertIn(needle, entry)
        self.assertEqual(len(list((s.run_dir / "issues").iterdir())), 1)
        self.assertEqual(len(list((s.run_dir / "quarantine").iterdir())), 1)

        r = self.report()
        self.assertEqual(r["partial"], [])
        self.assertEqual((r["summary"]["proven"], r["summary"]["quarantined"]), (1, 1))
        self.assertEqual(r["summary"]["proven_by_severity"]["high"], 1)
        self.assertEqual(r["summary"]["proven_by_engine"], {"billing": 1})
        self.assertEqual(r["counts"]["by_outcome"], {"proven": 1, "quarantined": 1})
        self.assertTrue(r["reconcile"]["ok"])
        seats = {x["seat_id"]: x for x in r["seats"]}
        self.assertEqual((seats["finder-a"]["proven"], seats["finder-a"]["unique_proven"]), (1, 0))
        self.assertEqual((seats["static-rules"]["proven"], seats["finder-b"]["proven"]), (1, 0))
        self.assertEqual(seats["finder-b"]["upheld"], 1)
        md = (s.run_dir / "report.md").read_text()
        self.assertLess(md.index("## Summary"), md.index("## Seats"))
        self.assertIn("Proven issues: **1**", md)

        code, out, _ = s.cli("reconcile")
        self.assertEqual(code, 0, out)
        self.assertNotIn("pending", out)
        code, out, _ = s.cli("show", show[0])
        self.assertIn("outcome: proven", out)
        self.assertIn("raised by: finder-a, static-rules", out)

        # removing a row by hand breaks conservation
        s.conn.execute("DELETE FROM outcomes WHERE cluster_id = ?", (update,))
        s.conn.commit()
        code, out, _ = s.cli("reconcile")
        self.assertEqual(code, 1)
        self.assertIn("clusters have no outcome", out)
        self.assertIn("report.json says outcomes = 2, the ledger says 1", out)

    def test_a_manual_review_sets_the_severity_and_is_written_into_the_issue(self):
        s = self.setup()
        (cid,) = self.cluster_ids()
        self.assertEqual(self.prove(cid)[0], 0)
        self.assertEqual(s.cli("review", cid, "--severity", "low", "--note", "Checked by hand: not reachable.",
                               "--reviewer", "the owner")[0], 0)
        self.assertEqual(s.cli("review", cid, "--severity", "low")[0], 1)          # a review needs a note
        self.assertEqual(s.cli("finalize")[0], 0)
        self.assertEqual(s.rows("SELECT severity FROM clusters")[0]["severity"], "low")
        issue = (s.run_dir / self.outcomes()[cid]["issue_path"]).read_text()
        self.assertIn("Severity: **low** (set by the owner after a manual check; the proof checker rated it high", issue)
        self.assertIn("## Manual review\n\nChecked by hand: not reachable.", issue)

    def test_unproven_cluster_goes_to_quarantine_or_is_dropped_by_lens(self):
        for lens, outcome in (("authz_tenancy", "quarantined"), ("money_ledger", "unproven_dropped")):
            s = self.setup()
            (cid,) = self.cluster_ids()
            s.conn.execute("UPDATE clusters SET lens = ?", (lens,))
            s.conn.commit()
            self.refuse_all(cid)
            self.assertEqual(s.cli("finalize")[0], 0)
            o = self.outcomes()[cid]
            self.assertEqual((o["outcome"], o["detail"]), (outcome, "all_provers_refused"))
            self.assertEqual(bool(o["issue_path"]), outcome == "quarantined")
            self.assertEqual(list((s.run_dir / "issues").iterdir()), [])
            self.assertEqual(s.cli("reconcile")[0], 0)

    def test_mechanically_valid_but_rejected_proof_goes_to_quarantine_whatever_the_lens(self):
        def reject(body, n):
            if schema_name(body) == "proof_check":
                return {"kind": "valid", "json": {"demonstrates_claim": False, "fix_minimal_at_root_cause": True,
                                                  "verdict": "reject", "severity": "low", "reason": "unrelated failure"}}
            return {"kind": "valid"}
        s = self.setup(script=reject)
        (cid,) = self.cluster_ids()
        s.conn.execute("UPDATE clusters SET lens = 'money_ledger'")      # not a quarantine lens
        s.conn.commit()
        self.assertEqual(self.prove(cid)[0], 3)
        self.assertEqual(self.prove(cid)[0], 3)
        self.assertEqual(s.cli("prove", "refuse", cid, "--prover", "prover-c")[0], 0)
        self.assertEqual(s.cli("finalize")[0], 0)
        o = self.outcomes()[cid]
        self.assertEqual(o["outcome"], "quarantined")
        entry = (s.run_dir / o["issue_path"]).read_text()
        self.assertIn("passed red-green but was not accepted", entry)
        self.assertIn("unrelated failure", entry)
        self.assertIn("fix.patch", entry)

    def test_known_cluster_gets_no_issue_file_and_is_listed(self):
        def known(m, r):
            r["baseline"]["known_issues"] = [{"ref": "JIRA-7", "path": CONTROLLER, "start_line": 3, "end_line": 6}]
        s = self.setup(mutate=known)
        self.assertEqual(s.cli("prove", "next", "--prover", "prover-native")[1].strip(),
                         "no cluster is waiting for prover-native")
        self.assertEqual(s.cli("finalize")[0], 0)
        self.assertEqual([o["outcome"] for o in self.outcomes().values()], ["known"])
        self.assertEqual(list((s.run_dir / "issues").iterdir()), [])
        self.assertEqual(self.report()["known"][0]["known_ref"], "JIRA-7")
        self.assertEqual(s.cli("reconcile")[0], 0)

    def test_severity_mismatch_is_flagged_and_writer_failure_still_writes_the_issue(self):
        def writer(body, n):
            if schema_name(body) == "issue_writer":
                return {"kind": "valid", "json": {"title": "T", "severity": "low", "how_it_breaks": "x"}}
            return {"kind": "valid"}
        s = self.setup(script=writer)
        (cid,) = self.cluster_ids()
        self.prove(cid)
        s.cli("finalize")
        issue = (s.run_dir / self.outcomes()[cid]["issue_path"]).read_text()
        self.assertIn("severity_mismatch: true", issue)
        self.assertIn('severity: "high"', issue)        # checker said high; the higher level is shown
        self.assertEqual(self.report()["severity_mismatches"], [cid])

        s = self.setup(script=lambda b, n: {"kind": "refusal"} if schema_name(b) == "issue_writer" else {"kind": "valid"})
        (cid,) = self.cluster_ids()
        self.prove(cid)
        self.assertEqual(s.cli("finalize")[0], 0)
        issue = (s.run_dir / self.outcomes()[cid]["issue_path"]).read_text()
        self.assertIn('written_by: "fallback"', issue)
        self.assertIn("# unscoped lookup", issue)

    def test_open_work_keeps_the_run_partial_until_closed(self):
        s = self.setup()
        (cid,) = self.cluster_ids()
        code, out, _ = s.cli("finalize")
        self.assertEqual(code, 0)
        self.assertIn("the run is PARTIAL", out)
        self.assertEqual(s.rows("SELECT state FROM runs")[0]["state"], "running")
        self.assertEqual(self.outcomes(), {})
        self.assertTrue(any("no outcome yet" in p for p in self.report()["partial"]))
        self.assertIn("**This report is partial.**", (s.run_dir / "report.md").read_text())
        code, out, _ = s.cli("reconcile")
        self.assertEqual(code, 0)                 # pending, not a failure, on an unfinished run
        self.assertIn("pending:", out)
        self.assertEqual(s.cli("finalize", "--close")[0], 0)
        self.assertEqual(self.outcomes()[cid]["detail"], "the run was closed before every prover finished")
        self.assertEqual(s.rows("SELECT state FROM runs")[0]["state"], "finished")
        # the finished run keeps its configuration, and later reports read that copy
        self.assertTrue((s.run_dir / "config" / "run.yaml").is_file())
        self.assertEqual(s.cli("report")[0], 0)

    def test_declined_budget_raise_leaves_a_partial_report(self):
        def budget(m, r):
            r["budget"]["limit_usd"] = 0.01          # the first finder call reaches it
        s = es.pipeline(self, mutate=budget, last="find")
        self.assertEqual(s.codes["find"][0], 3)
        code, out, err = s.cli("finalize")
        self.assertEqual(code, 0, out + err)
        s.cli("report")
        r = json.loads((s.run_dir / "report.json").read_text())
        self.assertEqual(r["run"]["state"], "paused_budget")
        self.assertTrue(any("planned finder calls did not run" in p for p in r["partial"]))
        self.assertEqual(r["coverage"]["finder_calls_pending"], 1)
        self.assertEqual(r["budget"]["events"][-1]["kind"], "pause")
        # the user changes their mind later: resume continues the interrupted find, not finalize
        self.assertEqual(s.cli("budget", "raise", "--usd", "5")[0], 0)
        code, out, err = s.cli("resume")
        self.assertEqual(code, 0, out + err)
        self.assertIn("find: 2 calls planned, 1 already done, 1 executed now", out)

    def test_per_unit_coverage_matches_the_calls_table(self):
        def refuse_b(body, n):
            return {"kind": "valid"}
        s = es.pipeline(self, last="find", cands=[candidate(CONTROLLER, 3)],
                        mutate=lambda m, r: r.update(runs_per_finder=2))
        s.conn.execute("UPDATE calls SET status = 'refused' WHERE seat_id = 'finder-b' AND lens = 'money_ledger'")
        s.conn.commit()
        s.cli("report")
        cov = json.loads((s.run_dir / "report.json").read_text())["coverage"]
        (unit,) = cov["per_unit"].values()
        self.assertEqual(unit, {"authz_tenancy": {"scanned_by": 2, "of": 2},
                                "money_ledger": {"scanned_by": 1, "of": 2}})
        for lens, c in unit.items():
            n = s.rows("SELECT COUNT(DISTINCT seat_id) n FROM calls WHERE role = 'finder' AND status = 'ok'"
                       " AND lens = ?", lens)[0]["n"]
            self.assertEqual(c["scanned_by"], n)
        self.assertEqual(cov["histogram"], {"2 of 2": 1, "1 of 2": 1})
        self.assertIn("| 1 | 1 of 2 finders |", (s.run_dir / "report.md").read_text())

    def test_drift_marks_an_issue_fixed_since_and_restores_the_clone(self):
        s = self.setup()
        (cid,) = self.cluster_ids()
        self.assertEqual(self.prove(cid)[0], 0)
        audited = es.git(s.repo, "rev-parse", "HEAD").strip()
        # the default branch moves: someone lands the fix
        es.git(s.repo, "checkout", "-q", "-b", "main-moved")
        path = s.repo / CONTROLLER
        path.write_text(path.read_text().replace("invoice = Invoice.find(params[:id])\n      { id",
                                                 "invoice = authorize(Invoice.find(params[:id]), :show?)\n      { id"))
        es.git(s.repo, "commit", "-q", "-am", "fix")
        fixed = es.git(s.repo, "rev-parse", "HEAD").strip()
        es.git(s.repo, "checkout", "-q", "--detach", audited)
        code, out, err = s.cli("finalize", "--drift-ref", "main-moved", env=ALLOW)
        self.assertEqual(code, 0, out + err)
        self.assertIn("1 proofs repeated, 1 fixed since", out)
        self.assertEqual(self.outcomes()[cid]["fixed_since"], fixed)
        self.assertEqual(self.outcomes()[cid]["outcome"], "proven")      # kept in the report
        self.assertEqual(self.report()["fixed_since"], [cid])
        self.assertEqual(es.git(s.repo, "rev-parse", "HEAD").strip(), audited)
        self.assertEqual(es.git(s.repo, "status", "--porcelain", "--untracked-files=all"), "")

    def test_drift_leaves_an_unfixed_issue_alone(self):
        s = self.setup()
        (cid,) = self.cluster_ids()
        self.prove(cid)
        audited = es.git(s.repo, "rev-parse", "HEAD").strip()
        es.git(s.repo, "checkout", "-q", "-b", "moved")
        (s.repo / "README.md").write_text("unrelated\n")
        es.git(s.repo, "commit", "-q", "-am", "docs")
        es.git(s.repo, "checkout", "-q", "--detach", audited)
        code, out, _ = s.cli("finalize", "--drift-ref", "moved", env=ALLOW)
        self.assertIn("1 proofs repeated, 0 fixed since", out)
        self.assertIsNone(self.outcomes()[cid]["fixed_since"])

    def test_quarantine_triage_state_is_read_from_the_file(self):
        s = self.setup()
        (cid,) = self.cluster_ids()
        self.refuse_all(cid)
        s.cli("finalize")
        entry = s.run_dir / self.outcomes()[cid]["issue_path"]
        entry.write_text(entry.read_text().replace('triage_state: "open"', "triage_state: dismissed"))
        s.cli("report")
        self.assertEqual(self.outcomes()[cid]["triage_state"], "dismissed")
        self.assertEqual(self.report()["quarantine"][0]["triage_state"], "dismissed")


if __name__ == "__main__":
    unittest.main()


class FixGroups(unittest.TestCase):
    def test_clusters_fixing_the_same_lines_share_a_group(self):
        import tempfile
        from pathlib import Path
        from engine import report
        d = Path(tempfile.mkdtemp())
        def patch(name, path, start):
            f = d / name
            f.write_text(f"--- a/{path}\n+++ b/{path}\n@@ -{start},7 +{start},8 @@\n context\n+added\n")
            return str(f)
        proven = [{"cluster_id": "a", "fix_patch_path": patch("a", "app/x.rb", 10)},
                  {"cluster_id": "b", "fix_patch_path": patch("b", "app/x.rb", 14)},      # overlaps a
                  {"cluster_id": "c", "fix_patch_path": patch("c", "app/x.rb", 80)},      # same file, elsewhere
                  {"cluster_id": "d", "fix_patch_path": patch("d", "app/y.rb", 10)},
                  {"cluster_id": "e", "fix_patch_path": None}]
        self.assertEqual(report.fix_groups(proven), [["a", "b"], ["c"], ["d"], ["e"]])
