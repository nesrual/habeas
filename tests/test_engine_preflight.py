"""Phase 2: preflight, context, static rules, baseline, and a dry run that reconciles."""

import json
import unittest

import engine_support as es
from engine import preflight, rubyindex
from fake_openai import FakeOpenAI, candidate


class Base(unittest.TestCase):
    def sample(self, **kw):
        s = es.SampleRun(**kw)
        self.addCleanup(s.cleanup)
        return s

    def unit_with(self, s, path):
        (row,) = [r for r in s.rows("SELECT * FROM units") if path in r["paths_json"]]
        return row


class TestHelpers(unittest.TestCase):
    def test_globs(self):
        self.assertTrue(preflight.matches("engines/a/app/models/x/y.rb", ["engines/*/app/**/*.rb"]))
        self.assertTrue(preflight.matches("app/x.rb", ["app/**/*.rb"]))
        self.assertFalse(preflight.matches("engines/a/spec/x_spec.rb", ["engines/*/app/**/*.rb"]))
        self.assertTrue(preflight.matches("engines/a/spec/x_spec.rb", ["engines/*/spec/**"]))
        self.assertEqual(preflight.package_of("gems/money/lib/m.rb", "engines/{package}/|gems/{package}/"), "money")
        self.assertIsNone(preflight.package_of("lib/x.rb", "engines/{package}/|gems/{package}/"))

    def test_split_cuts_at_method_starts(self):
        lines = ["class Big"]
        for i in range(40):
            lines += [f"  def m{i}", "    " + "x = 1; " * 20, "  end", ""]
        lines.append("end")
        segs = preflight.split_file("big.rb", lines, 300)
        self.assertGreater(len(segs), 3)
        self.assertEqual(segs[0]["start_line"], 1)
        self.assertEqual(segs[-1]["end_line"], len(lines))
        for a, b in zip(segs, segs[1:]):
            self.assertEqual(b["start_line"], a["end_line"] + 1)      # nothing lost, nothing twice
            self.assertTrue(lines[b["start_line"] - 1].strip().startswith("def "))
        for seg in segs:
            chunk = "\n".join(lines[seg["start_line"] - 1:seg["end_line"]])
            self.assertLessEqual(preflight.est_tokens(chunk), 300)

    def test_secret_scan_reports_rule_and_line_only(self):
        hits = preflight.scan_secrets('x = 1\nKEY = "AKIAABCDEFGHIJKLMNOP"\npassword = "short"\n'
                                      'api_secret: "abcdefghijklmnopqrstuvwxyz012345"\n')
        self.assertEqual(hits, [{"rule": "aws_access_key_id", "line": 2}, {"rule": "assigned_secret", "line": 4}])

    def test_ruby_index_follows_superclasses_and_includes(self):
        idx = rubyindex.Index()
        idx.add("a.rb", "module X\n  class Base\n    include Guard\n  end\nend\n")
        idx.add("b.rb", "module X\n  class Child < Base\n    def go\n      1\n    end\n\n    private\n\n    def hid\n    end\n  end\nend\n")
        idx.add("g.rb", "module Guard\nend\n")
        (child,) = [d for d in idx.by_path["b.rb"] if d["name"] == "X::Child"]
        self.assertEqual([p for p, _ in idx.ancestors(child)], ["a.rb", "g.rb"])
        self.assertTrue(idx.descends_from(child, "Base"))
        self.assertEqual([(m["name"], m["visibility"]) for m in rubyindex.methods(idx.text["b.rb"])],
                         [("go", "public"), ("hid", "private")])


class TestPreflight(Base):
    def test_units_are_packed_per_directory_and_never_across(self):
        s = self.sample()
        code, out, err = s.cli("preflight")
        self.assertEqual(code, 0, err)
        self.assertLessEqual(len(out.splitlines()), 20)
        units = s.rows("SELECT * FROM units")
        for u in units:
            dirs = {p.rsplit("/", 1)[0] for p in json.loads(u["paths_json"])}
            self.assertEqual(len(dirs), 1, u["paths_json"])
        models = self.unit_with(s, "models/billing/invoice.rb")
        self.assertEqual(len(json.loads(models["paths_json"])), 3)
        self.assertEqual(models["package"], "billing")
        self.assertEqual(self.unit_with(s, "lib/sample/support.rb")["package"], None)
        self.assertFalse(any("spec" in u["paths_json"] for u in units))
        self.assertTrue(all(u["sensitive"] == 1 for u in units))
        run = s.rows("SELECT * FROM runs")[0]
        self.assertEqual(run["repo_commit"], es.git(s.repo, "rev-parse", "HEAD").strip())

    def test_engine_filter_and_dirty_clone(self):
        def only_ledger(m, r):
            r["target"]["engines"] = ["ledger"]
        s = self.sample(mutate=only_ledger)
        (s.repo / "README.md").write_text("changed")
        code, _, err = s.cli("preflight")
        self.assertEqual(code, 1)
        self.assertIn("uncommitted changes", err)
        es.git(s.repo, "checkout", "--", "README.md")
        self.assertEqual(s.cli("preflight")[0], 0)
        self.assertEqual({u["package"] for u in s.rows("SELECT * FROM units")}, {"ledger"})

    def test_unit_with_a_secret_is_blocked_and_never_planned(self):
        def plant(repo):
            (repo / "engines/ledger/app/interactors/ledger/keys.rb").write_text(
                'module Ledger\n  AWS = "AKIAABCDEFGHIJKLMNOP"\nend\n')
        with FakeOpenAI() as server:
            s = self.sample(base_url=server.base_url, prepare=plant)
            self.assertEqual(s.cli("preflight")[0], 0)
            blocked = self.unit_with(s, "ledger/keys.rb")
            self.assertEqual(blocked["blocked_reason"], "secret_found")
            self.assertEqual(json.loads(blocked["paths_json"]), ["engines/ledger/app/interactors/ledger/keys.rb"])
            report = (s.run_dir / "blocked_units.json").read_text()
            self.assertIn("aws_access_key_id", report)
            self.assertNotIn("AKIAABCDEFGHIJKLMNOP", report)
            self.assertEqual(s.cli("find")[0], 0)
            self.assertFalse(any("AKIAABCDEFGHIJKLMNOP" in json.dumps(r["body"]) for r in server.requests))
            self.assertEqual(s.rows("SELECT COUNT(*) n FROM calls WHERE unit_id = ?", blocked["unit_id"])[0]["n"], 0)

    def test_scanner_hits_order_the_queue_and_baseline_loads(self):
        target = "engines/ledger/app/interactors/ledger/post_entry.rb"

        def plant(repo):
            (repo / "config").mkdir()
            (repo / "config/brakeman.ignore").write_text(json.dumps({"ignored_warnings": [
                {"warning_type": "SQL Injection", "fingerprint": "abc123", "file": target, "line": 4,
                 "message": "known", "note": "accepted"}]}))

        def known(m, r):
            r["baseline"]["known_issues"] = [{"ref": "JIRA-1", "path": "lib/sample/support.rb",
                                              "start_line": 10, "end_line": 12, "description": "known"}]
        s = self.sample(prepare=plant, mutate=known)
        self.assertEqual(s.cli("preflight")[0], 0)
        self.assertEqual(s.cli("scout")[0], 0)
        first = s.rows("SELECT * FROM units ORDER BY order_rank LIMIT 1")[0]
        self.assertIn(target, first["paths_json"])
        self.assertEqual(len(s.rows("SELECT * FROM units WHERE order_rank IS NULL")), 0)   # nothing removed
        self.assertEqual({b["ref"] for b in s.rows("SELECT * FROM baseline")}, {"brakeman_ignore:abc123", "JIRA-1"})
        loc = {"path": target, "start_line": 3, "end_line": 5}
        self.assertEqual(preflight.baseline_match(s.conn, [loc]), "brakeman_ignore:abc123")
        self.assertIsNone(preflight.baseline_match(s.conn, [dict(loc, start_line=5)]))

    def test_controller_unit_carries_its_base_controller_and_policy(self):
        def no_pack(m, r):
            r["units"]["pack_small_files"] = False
        controller = "engines/billing/app/controllers/billing/invoices_controller.rb"
        base = "engines/billing/app/controllers/billing/application_controller.rb"

        def script(body, n):
            if controller in body["messages"][1]["content"].split("Context, read only")[0]:
                return {"kind": "valid", "candidates": [candidate(controller, 3), candidate(base, 12)]}
            return {"kind": "empty"}
        with FakeOpenAI(script) as server:
            s = self.sample(base_url=server.base_url, mutate=no_pack)
            self.assertEqual(s.cli("preflight")[0], 0)
            unit = self.unit_with(s, controller)
            context = json.loads(unit["context_json"])
            self.assertTrue(context[0].startswith(f"=== CONTEXT {base} ==="))
            self.assertTrue(any("invoice_policy.rb" in c for c in context))
            self.assertEqual(s.cli("find")[0], 0)
            sent = [r["body"]["messages"][1]["content"] for r in server.requests
                    if controller in r["body"]["messages"][1]["content"].split("Context, read only")[0]]
            self.assertIn("raise Sample::NotAuthorized unless policy", sent[0].split("Context, read only")[1])
            # the candidate raised against context only is rejected and counted
            found = s.rows("SELECT c.* FROM candidates c JOIN calls k ON k.call_id = c.call_id"
                           " WHERE k.role = 'finder' AND c.unit_id = ?", unit["unit_id"])
            self.assertTrue(found)
            self.assertTrue(all(controller in c["locations_json"] for c in found))
            calls = s.rows("SELECT * FROM calls WHERE role = 'finder' AND unit_id = ?", unit["unit_id"])
            self.assertEqual({c["rejected_candidates"] for c in calls}, {1})

    def test_each_static_rule_fires_on_the_fixture(self):
        s = self.sample()
        self.assertEqual(s.cli("preflight")[0], 0)
        found = s.rows("SELECT c.*, k.seat_id, k.role FROM candidates c JOIN calls k ON k.call_id = c.call_id")
        self.assertEqual({c["seat_id"] for c in found}, {"static-rules"})
        by_title = {c["title"]: c for c in found}
        self.assertEqual(sorted(by_title), [
            "Direct write to the state column of Billing::Invoice",
            "InvoicesController#show has no authorisation call",
            "Write to Billing::LegacyCustomer, which inherits from V1ApplicationRecord"])
        auth = by_title["InvoicesController#show has no authorisation call"]
        self.assertEqual((auth["lens"], auth["class"]), ("authz_tenancy", "security"))
        self.assertEqual(json.loads(auth["locations_json"]), [{
            "path": "engines/billing/app/controllers/billing/invoices_controller.rb",
            "start_line": 3, "end_line": 6}])
        self.assertIn("pay_invoice.rb", by_title["Direct write to the state column of Billing::Invoice"]["locations_json"])
        self.assertIn("sync_legacy_customer.rb",
                      by_title["Write to Billing::LegacyCustomer, which inherits from V1ApplicationRecord"]["locations_json"])
        debt = (s.run_dir / "tech_debt.md").read_text()
        self.assertIn("engines/ledger/app/interactors/ledger/post_entry.rb:4", debt)
        self.assertIn("Billing::Invoice", debt)
        self.assertNotIn("engines/billing/", debt)

    def test_dry_run_fills_calls_and_candidates_and_reconciles(self):
        with FakeOpenAI() as server:
            s = self.sample(base_url=server.base_url)
            for cmd in ("preflight", "scout", "find"):
                code, out, err = s.cli(cmd)
                self.assertEqual(code, 0, f"{cmd}: {out}{err}")
            units = len(s.rows("SELECT * FROM units"))
            finder = s.rows("SELECT * FROM calls WHERE role = 'finder'")
            self.assertEqual(len(finder), units * 2 * 2)          # units x finder seats x runs
            self.assertEqual(len(server.requests), len(finder))
            self.assertEqual({c["status"] for c in finder}, {"ok"})
            from_models = s.rows("SELECT COUNT(*) n FROM candidates c JOIN calls k ON k.call_id = c.call_id"
                                 " WHERE k.role = 'finder'")[0]["n"]
            self.assertEqual(from_models, len(finder))
            code, out, _ = s.cli("reconcile")
            self.assertEqual(code, 0, out)
            self.assertIn("reconcile: OK", out)
            self.assertIn("pending:", out)       # merge has not run yet: pending, not a failure
            # a candidate whose call row is removed by hand breaks conservation
            s.conn.execute("DELETE FROM calls WHERE call_id = ?", (finder[0]["call_id"],))
            s.conn.commit()
            code, out, _ = s.cli("reconcile")
            self.assertEqual(code, 1)
            self.assertIn("point at a call that is not in the ledger", out)


if __name__ == "__main__":
    unittest.main()
