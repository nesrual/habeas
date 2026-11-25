import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import contextpack  # noqa: E402

LEDGER = """module Toy
  class Ledger
    def pay!(account, amount)
      account.balance -= amount
      account.balance
    end

    def refund!(account, amount)
      pay!(account, -amount)
    end
  end
end
"""


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


class ContextpackTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / "lib").mkdir()
        (self.repo / "lib" / "ledger.rb").write_text(LEDGER)
        (self.repo / "lib" / "checkout.rb").write_text(
            "# checkout flow\nledger.pay!(acct, total)\n")
        (self.repo / "spec").mkdir()
        (self.repo / "spec" / "ledger_spec.rb").write_text(
            "it 'pays' do\n  ledger.pay!(acct, 5)\nend\n")
        (self.repo / "vendor").mkdir()
        (self.repo / "vendor" / "gemcode.rb").write_text("thing.pay!(a, b)\n")
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "add", "-A")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")

    def tearDown(self):
        self._tmp.cleanup()

    def cand(self, **kw):
        base = {"id": "cand-1", "category": "bug", "file": "lib/ledger.rb",
                "lines": [4, 5], "hypothesis": "h", "confidence": 0.5,
                "evidence_snippet": "s"}
        base.update(kw)
        return base

    def build(self, record):
        return contextpack.build_pack(str(self.repo), record)


class TestPackContents(ContextpackTestCase):
    def test_flagged_file_full_with_line_numbers(self):
        text, meta = self.build(self.cand())
        self.assertIn("## Flagged file: lib/ledger.rb (lines 4-5 flagged; full file)", text)
        self.assertIn(" 3 |     def pay!(account, amount)", text)
        self.assertIn('"id": "cand-1"', text)

    def test_symlinked_auth_file_cannot_disclose_host_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "outside.rb"
            dest.write_text("private fixture")
            policies = self.repo / "app" / "policies"
            policies.mkdir(parents=True)
            (policies / "sentinel.rb").symlink_to(dest)
            with self.assertRaisesRegex(ValueError, "outside"):
                self.build(self.cand(category="security"))

    def test_enclosing_symbol_found_for_body_lines(self):
        text, meta = self.build(self.cand(lines=[4, 5]))  # body of pay!
        self.assertEqual(meta["symbols"], ["pay!"])
        self.assertIn("## Callers of `pay!`", text)

    def test_callers_include_context_and_skip_definitions(self):
        text, meta = self.build(self.cand())
        self.assertIn("### lib/checkout.rb:2", text)
        self.assertIn("### spec/ledger_spec.rb:2", text)
        self.assertIn("### lib/ledger.rb:9", text)  # refund! calling pay!
        self.assertEqual(meta["caller_sites"], 3)
        self.assertNotIn("### lib/ledger.rb:3", text)  # the definition itself

    def test_excluded_dirs_not_searched(self):
        text, _ = self.build(self.cand())
        self.assertNotIn("vendor/gemcode.rb", text)

    def test_untracked_callers_found(self):
        (self.repo / "lib" / "new_flow.rb").write_text("x = svc.pay!(a, 1)\n")
        text, _ = self.build(self.cand())
        self.assertIn("### lib/new_flow.rb:1", text)

    def test_non_utf8_caller_does_not_crash(self):
        # a Latin-1 file matching the symbol used to kill the whole run with
        # UnicodeDecodeError from the git grep output
        (self.repo / "lib" / "legacy.rb").write_bytes(
            "# legacy\nsvc.pay!(a, 2)  # K\xf6benhavn\n".encode("latin-1"))
        text, _ = self.build(self.cand())
        self.assertIn("### lib/legacy.rb:2", text)

    def test_caller_cap_per_symbol(self):
        for i in range(8):
            (self.repo / "lib" / f"c{i}.rb").write_text(f"pay!(a, {i})\n")
        _, meta = self.build(self.cand())
        self.assertEqual(meta["caller_sites"], contextpack.MAX_SITES_PER_SYMBOL)

    def test_definition_in_flagged_range_wins_over_enclosing(self):
        _, meta = self.build(self.cand(lines=[8, 10]))  # def refund! + body
        self.assertEqual(meta["symbols"], ["refund!"])


class TestWindowing(ContextpackTestCase):
    def test_big_file_gets_window_containing_flagged_lines(self):
        big = "\n".join(f"x{i} = {i}" for i in range(1, 1001))
        (self.repo / "lib" / "big.rb").write_text(big + "\n")
        text, _ = self.build(self.cand(file="lib/big.rb", lines=[700, 702]))
        self.assertIn("window", text)
        self.assertIn("700 | x700 = 700", text)
        self.assertNotIn("| x100 = 100", text)
        window_note = [l for l in text.splitlines() if "## Flagged file" in l][0]
        self.assertRegex(window_note, r"window \d+-\d+ of 1000 lines")

    def test_budget_truncates_callers_never_the_window(self):
        # The 1500-line cap is unreachable with the v1 recipe (few, small
        # caller sites); exercise the mechanism with a lowered cap.
        original = contextpack.PACK_LINE_CAP
        contextpack.PACK_LINE_CAP = 60
        try:
            text, meta = self.build(self.cand())
        finally:
            contextpack.PACK_LINE_CAP = original
        self.assertGreater(meta["truncated_sites"], 0)
        self.assertLess(meta["caller_sites"], 3)
        self.assertIn("truncated:", text)
        self.assertIn(" 3 |     def pay!(account, amount)", text)  # window intact


class TestRouting(unittest.TestCase):
    def r(self, **kw):
        base = {"category": "bug", "file": "app/services/x.rb", "evidence_snippet": "a = 1"}
        base.update(kw)
        return contextpack.route(base)

    def test_routing_table(self):
        self.assertEqual(self.r(category="security"), "expert-security")
        self.assertEqual(self.r(category="test-gap"), "expert-tests")
        self.assertEqual(self.r(category="improvement"), "expert-backend")
        self.assertEqual(self.r(file="app/models/user.rb"), "expert-db")
        self.assertEqual(self.r(file="db/data/backfill.rb"), "expert-db")
        self.assertEqual(self.r(file="app/repositories/user_repository.rb"), "expert-db")
        self.assertEqual(self.r(evidence_snippet="db.execute(\"SELECT * FROM t\")"), "expert-db")
        self.assertEqual(self.r(), "expert-backend")

    def test_sql_regex_not_fooled_by_plain_words(self):
        self.assertEqual(self.r(evidence_snippet="please select the from list"), "expert-db")
        self.assertEqual(self.r(evidence_snippet="items.delete(x); update_cache"), "expert-backend")


SCHEMA_RB = """ActiveRecord::Schema[8.0].define(version: 2026_01_01_000000) do
  create_table "ledgers", force: :cascade do |t|
    t.integer "balance_cents", null: false
  end

  create_table "other_things", force: :cascade do |t|
    t.string "name"
  end
end
"""


class TestDomainExtras(ContextpackTestCase):
    def test_db_route_gets_schema_and_migrations(self):
        (self.repo / "db" / "migrate").mkdir(parents=True)
        (self.repo / "db" / "schema.rb").write_text(SCHEMA_RB)
        for stamp, body in [("20250101000001", "add_column :ledgers, :x, :integer"),
                            ("20250101000002", "create_table :other_things"),
                            ("20250101000003", "add_index :ledgers, :x"),
                            ("20250101000004", "change_column :ledgers, :x, :bigint"),
                            ("20250101000005", "add_column :ledgers, :y, :integer")]:
            (self.repo / "db" / "migrate" / f"{stamp}_m.rb").write_text(body + "\n")
        (self.repo / "app" / "models").mkdir(parents=True)
        (self.repo / "app" / "models" / "ledger.rb").write_text(
            "class Ledger < ApplicationRecord\n  def pay!(a)\n    a\n  end\nend\n")
        text, meta = self.build(self.cand(file="app/models/ledger.rb", lines=[2, 4]))
        self.assertEqual(meta["route"], "expert-db")
        self.assertIn('## Schema: ledgers (db/schema.rb)', text)
        self.assertIn('t.integer "balance_cents"', text)
        self.assertNotIn("other_things", text.split("## Schema")[1].split("##")[0])
        # 3 most recent ledger-touching migrations, newest first
        self.assertIn("20250101000005_m.rb", text)
        self.assertIn("20250101000004_m.rb", text)
        self.assertIn("20250101000003_m.rb", text)
        self.assertNotIn("20250101000001_m.rb", text)

    def test_structure_sql_fallback(self):
        (self.repo / "db").mkdir()
        (self.repo / "db" / "structure.sql").write_text(
            "CREATE TABLE public.ledgers (\n    id bigint NOT NULL,\n"
            "    balance_cents integer\n);\n\nCREATE TABLE public.others (\n    id bigint\n);\n")
        (self.repo / "app" / "models").mkdir(parents=True)
        (self.repo / "app" / "models" / "ledger.rb").write_text(
            "class Ledger < ApplicationRecord\nend\n")
        text, meta = self.build(self.cand(file="app/models/ledger.rb", lines=[1, 1]))
        self.assertIn("## Schema: ledgers (db/structure.sql)", text)
        self.assertIn("balance_cents integer", text)

    def test_security_route_gets_auth_files(self):
        (self.repo / "app" / "controllers").mkdir(parents=True)
        (self.repo / "app" / "controllers" / "application_controller.rb").write_text(
            "class ApplicationController\n  before_action :authenticate!\nend\n")
        (self.repo / "app" / "policies" / "nested").mkdir(parents=True)
        (self.repo / "app" / "policies" / "nested" / "pay_policy.rb").write_text("POLICY\n")
        text, meta = self.build(self.cand(category="security"))
        self.assertEqual(meta["route"], "expert-security")
        self.assertIn("## Auth: app/controllers/application_controller.rb", text)
        self.assertIn("## Auth: app/policies/nested/pay_policy.rb", text)

    def test_tests_route_gets_mirror_spec(self):
        (self.repo / "app" / "services").mkdir(parents=True)
        (self.repo / "app" / "services" / "payer.rb").write_text("class Payer\nend\n")
        (self.repo / "spec" / "services").mkdir(parents=True)
        (self.repo / "spec" / "services" / "payer_spec.rb").write_text("MIRROR\n")
        text, meta = self.build(self.cand(category="test-gap", file="app/services/payer.rb",
                                          lines=[1, 1]))
        self.assertEqual(meta["route"], "expert-tests")
        self.assertIn("## Mirror spec: spec/services/payer_spec.rb", text)
        self.assertIn("MIRROR", text)

    def test_budget_drops_extras_after_callers_never_schema_before_migrations(self):
        (self.repo / "db" / "migrate").mkdir(parents=True)
        (self.repo / "db" / "schema.rb").write_text(SCHEMA_RB)
        (self.repo / "db" / "migrate" / "20250101000001_m.rb").write_text(
            "add_column :ledgers, :x\n" * 5)
        (self.repo / "app" / "models").mkdir(parents=True)
        (self.repo / "app" / "models" / "ledger.rb").write_text(
            "class Ledger < ApplicationRecord\n  def pay!(a)\n    a\n  end\nend\n")
        original = contextpack.PACK_LINE_CAP
        contextpack.PACK_LINE_CAP = 40
        try:
            text, meta = self.build(self.cand(file="app/models/ledger.rb", lines=[2, 4]))
        finally:
            contextpack.PACK_LINE_CAP = original
        self.assertEqual(meta["extras_dropped"], 1)          # migration dropped (rank 1)
        self.assertIn("Schema: ledgers", " ".join(meta["extras"]))  # schema survives
        self.assertIn("truncated:", text)

    def test_tableize(self):
        for model, table in [("Ledger", "ledgers"), ("Company", "companies"),
                             ("Address", "addresses"), ("LedgerPosting", "ledger_postings"),
                             ("Box", "boxes")]:
            self.assertEqual(contextpack.tableize(model), table)


class TestCli(ContextpackTestCase):
    def test_cli_roundtrip(self):
        cand_file = self.repo / "cand.json"
        cand_file.write_text(json.dumps(self.cand()))
        out_file = self.repo / "pack.md"
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "contextpack.py"), str(cand_file),
             "--out", str(out_file), "--repo", str(self.repo)],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = json.loads(proc.stdout)
        self.assertEqual(summary["route"], "expert-backend")
        self.assertEqual(summary["symbols"], ["pay!"])
        self.assertTrue(out_file.exists())

    def test_cli_accepts_full_state_entry(self):
        # SKILL.md pipes `state.py show <fp>` output straight in
        cand_file = self.repo / "entry.json"
        cand_file.write_text(json.dumps({"status": "candidate", "issue_id": None,
                                         "record": self.cand(), "history": []}))
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "contextpack.py"), str(cand_file),
             "--out", str(self.repo / "p.md"), "--repo", str(self.repo)],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["symbols"], ["pay!"])

    def test_cli_missing_field_errors(self):
        cand_file = self.repo / "cand.json"
        cand_file.write_text(json.dumps({"file": "lib/ledger.rb"}))
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "contextpack.py"), str(cand_file),
             "--out", str(self.repo / "p.md"), "--repo", str(self.repo)],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("missing field", proc.stderr)


if __name__ == "__main__":
    unittest.main()
