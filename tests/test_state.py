import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import state  # noqa: E402


def cand(id_="cand-1", file="lib/fees.rb", category="bug", snippet="a = 1\nb = 2"):
    return {
        "id": id_,
        "category": category,
        "file": file,
        "lines": [3, 4],
        "hypothesis": "float money",
        "confidence": 0.7,
        "evidence_snippet": snippet,
    }


class StateTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = self._tmp.name
        (Path(self.repo) / "lib").mkdir()
        (Path(self.repo) / "lib" / "fees.rb").write_text("a = 1\nb = 2\nc = 3\n")
        state.init_repo(self.repo)
        state.run_start(self.repo, "lib")

    def tearDown(self):
        self._tmp.cleanup()

    def add_one(self, **kw):
        result = state.add_candidates(self.repo, [cand(**kw)])
        self.assertEqual(len(result["added"]), 1, result)
        return result["added"][0]


class TestInitAndRuns(StateTestCase):
    def test_init_is_idempotent(self):
        self.assertFalse(state.init_repo(self.repo)["created"])

    def test_repository_cannot_supply_audit_state(self):
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        subprocess.run(["git", "-C", self.repo, "add", ".audit/state.json"], check=True)
        with self.assertRaisesRegex(ValueError, "repository-controlled"):
            state.init_repo(self.repo)

    def test_run_ids_sequence_per_day(self):
        date = datetime.now(timezone.utc).strftime("%Y%m%d")
        r2 = state.run_start(self.repo, "lib")
        self.assertEqual(r2["id"], f"r-{date}-2")
        self.assertIn(f"r-{date}-1", r2["abandoned"])

    def test_run_end_records_counts(self):
        out = state.run_end(self.repo, {"candidates": 3, "emitted": 1})
        self.assertEqual(out["counts"]["candidates"], 3)
        with self.assertRaises(state.StateError):
            state.run_end(self.repo, {})  # no run left in progress


class TestAddCandidates(StateTestCase):
    def test_adds_and_derives_track(self):
        fp = self.add_one(category="security")
        entry = state.show(self.repo, fp)
        self.assertEqual(entry["status"], "candidate")
        self.assertEqual(entry["record"]["track"], "provable")
        self.assertEqual(entry["history"][0]["actor"], "scout")
        fp2 = self.add_one(id_="cand-2", category="test-gap", snippet="x = 9")
        self.assertEqual(state.show(self.repo, fp2)["record"]["track"], "advisory")

    def test_scout_cannot_supply_later_stage_fields(self):
        record = cand()
        record.update(proof={"result": "red"}, proof_review={"verdict": "valid"},
                      fix_sketch="irrelevant", severity="critical")
        fp = state.add_candidates(self.repo, [record])["added"][0]
        stored = state.show(self.repo, fp)["record"]
        for key in ("proof", "proof_review", "fix_sketch", "severity"):
            self.assertNotIn(key, stored)

    def test_rejects_external_files_and_nonrecords_without_losing_valid_candidates(self):
        with tempfile.TemporaryDirectory() as outside:
            sentinel = Path(outside) / "other.rb"
            sentinel.write_text("unrelated")
            (Path(self.repo) / "link").symlink_to(sentinel)
            records = [None, cand(file=str(sentinel)), cand(file="link"),
                       cand(file=".audit/state.json"), dict(cand(), lines=None),
                       cand()]
            result = state.add_candidates(self.repo, records)
            self.assertEqual(len(result["added"]), 1)
            self.assertEqual(len(result["dropped"]), 5)

    def test_drops_duplicates_and_bad_input(self):
        self.add_one()
        result = state.add_candidates(self.repo, [
            cand(),                                # duplicate fingerprint
            cand(id_="c3", category="vibes"),      # unknown category
            {"id": "c4", "category": "bug"},       # missing fields
        ])
        self.assertEqual(result["added"], [])
        reasons = [d["reason"] for d in result["dropped"]]
        self.assertEqual(reasons[0], "duplicate:candidate")
        self.assertTrue(reasons[1].startswith("unknown_category"))
        self.assertTrue(reasons[2].startswith("missing_fields"))

    def test_drops_candidates_in_scanned_unchanged_files(self):
        state.record_hunks(self.repo, ["lib/fees.rb"])
        result = state.add_candidates(self.repo, [cand()])
        self.assertEqual([d["reason"] for d in result["dropped"]], ["already_scanned_unchanged"])
        # changed file content -> candidates flow again
        (Path(self.repo) / "lib" / "fees.rb").write_text("a = 1\nb = 2\nc = 3\nd = 4\n")
        self.assertEqual(len(state.add_candidates(self.repo, [cand()])["added"]), 1)

    def test_kill_suppression_and_revival(self):
        fp = self.add_one()
        state.transition(self.repo, fp, "killed", "critic", note="not a bug")
        # recent kill suppresses
        result = state.add_candidates(self.repo, [cand()])
        self.assertEqual([d["reason"] for d in result["dropped"]], ["killed_recently"])
        # backdate the kill past the suppression window
        p = state.state_path(self.repo)
        data = json.loads(p.read_text())
        old = datetime.now(timezone.utc) - timedelta(days=31)
        data["findings"][fp]["killed_ts"] = old.strftime("%Y-%m-%dT%H:%M:%SZ")
        p.write_text(json.dumps(data))
        result = state.add_candidates(self.repo, [cand()])
        self.assertEqual(result["added"], [fp])
        entry = state.show(self.repo, fp)
        self.assertEqual(entry["status"], "candidate")
        self.assertEqual(entry["history"][-1]["from"], "killed")  # history preserved


class TestTransitions(StateTestCase):
    def test_full_provable_lifecycle(self):
        fp = self.add_one(category="security")
        state.transition(self.repo, fp, "expert_validated", "expert",
                         record_patch={"failure_scenario": "x", "severity": "high"})
        state.transition(self.repo, fp, "critic_survived", "critic")
        state.transition(self.repo, fp, "proved", "prover")
        state.transition(self.repo, fp, "emitted", "emit", issue_id="0001")
        entry = state.show(self.repo, fp)
        self.assertEqual(entry["status"], "emitted")
        self.assertEqual(entry["issue_id"], "0001")
        self.assertEqual([h["to"] for h in entry["history"]],
                         ["candidate", "expert_validated", "critic_survived", "proved", "emitted"])

    def test_quarantine_path(self):
        fp = self.add_one(category="bug")
        state.transition(self.repo, fp, "expert_validated", "expert")
        state.transition(self.repo, fp, "critic_survived", "critic")
        state.transition(self.repo, fp, "proof_failed", "prover", note="attempt 1")
        state.transition(self.repo, fp, "proof_failed", "prover", note="attempt 2")
        state.transition(self.repo, fp, "quarantined", "prover")
        self.assertEqual(state.show(self.repo, fp)["status"], "quarantined")

    def test_illegal_transitions_rejected(self):
        fp = self.add_one()
        with self.assertRaises(state.StateError):
            state.transition(self.repo, fp, "proved", "prover")
        with self.assertRaises(state.StateError):
            state.transition(self.repo, fp, "expert_validated", "wizard")
        with self.assertRaises(state.StateError):
            state.transition(self.repo, "deadbeefdeadbeef", "killed", "critic")

    def test_critic_advisory_demotes_and_clamps(self):
        fp = self.add_one(category="bug")
        state.transition(self.repo, fp, "expert_validated", "expert",
                         record_patch={"severity": "critical"})
        state.transition(self.repo, fp, "critic_advisory", "critic")
        record = state.show(self.repo, fp)["record"]
        self.assertEqual(record["track"], "advisory")
        self.assertEqual(record["severity"], "medium")

    def test_advisory_track_cannot_enter_provable_path(self):
        fp = self.add_one(category="improvement")
        state.transition(self.repo, fp, "expert_validated", "expert")
        with self.assertRaises(state.StateError):
            state.transition(self.repo, fp, "critic_survived", "critic")

    def test_terminal_statuses_are_terminal(self):
        fp = self.add_one()
        state.transition(self.repo, fp, "expert_validated", "expert")
        state.transition(self.repo, fp, "critic_advisory", "critic")
        state.transition(self.repo, fp, "emitted", "emit", issue_id="0002")
        with self.assertRaises(state.StateError):
            state.transition(self.repo, fp, "killed", "critic")


class TestHunksAndQueries(StateTestCase):
    def test_record_and_is_scanned(self):
        self.assertFalse(state.is_scanned(self.repo, "lib/fees.rb")["scanned"])
        state.record_hunks(self.repo, ["lib/fees.rb"])
        self.assertTrue(state.is_scanned(self.repo, "lib/fees.rb")["scanned"])
        (Path(self.repo) / "lib" / "fees.rb").write_text("changed\n")
        self.assertFalse(state.is_scanned(self.repo, "lib/fees.rb")["scanned"])

    def test_is_scanned_missing_file(self):
        out = state.is_scanned(self.repo, "lib/nope.rb")
        self.assertEqual(out, {"path": "lib/nope.rb", "exists": False, "scanned": False})

    def test_open_fingerprints_excludes_terminal(self):
        fp1 = self.add_one()
        fp2 = self.add_one(id_="c2", snippet="z = 0")
        state.transition(self.repo, fp2, "killed", "critic")
        self.assertEqual(state.open_fingerprints(self.repo)["open"], {fp1: "candidate"})


class TestCli(unittest.TestCase):
    def run_cli(self, repo, *args, stdin=None):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "state.py"), "--repo", repo, *args],
            capture_output=True, text=True, input=stdin)

    def test_cli_roundtrip(self):
        with tempfile.TemporaryDirectory() as repo:
            (Path(repo) / "app.rb").write_text("x = 1\n")
            self.assertEqual(self.run_cli(repo, "init").returncode, 0)
            out = self.run_cli(repo, "run-start", "--unit", ".")
            self.assertEqual(json.loads(out.stdout)["unit"], ".")
            out = self.run_cli(repo, "add-candidates",
                               stdin=json.dumps([cand(file="app.rb", snippet="x = 1")]))
            fp = json.loads(out.stdout)["added"][0]
            out = self.run_cli(repo, "transition", fp, "expert_validated",
                               "--actor", "expert", "--record", '{"severity": "low"}')
            self.assertEqual(json.loads(out.stdout)["to"], "expert_validated")
            bad = self.run_cli(repo, "transition", fp, "emitted", "--actor", "emit")
            self.assertEqual(bad.returncode, 1)
            self.assertIn("illegal transition", bad.stderr)
            out = self.run_cli(repo, "config-get", "gates.max_emissions_per_run")
            self.assertEqual(json.loads(out.stdout), 5)


if __name__ == "__main__":
    unittest.main()
