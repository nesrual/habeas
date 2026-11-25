import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import report  # noqa: E402


def metrics_line(run, emitted=1, verdicts=()):
    return {"run": run, "unit": "lib/x",
            "counts": {"candidates": 3, "expert_validated": 2, "critic_killed": 1,
                       "critic_advisory": 0, "promoted": 1, "proved": 1,
                       "quarantined": 0, "emitted": emitted},
            "stage_s": {"scout": 60, "expert": 120, "critic": 60, "prove": 120, "emit": 0},
            "dispatch_calls": {}, "proof_attempts": 1,
            "human_verdicts": [{"issue": i, "verdict": v} for i, v in verdicts]}


def finding(status, actor="critic", track="provable", reason=None):
    e = {"status": status, "issue_id": None,
         "record": {"category": "bug", "track": track, "file": "lib/x.rb",
                    "hypothesis": "h"},
         "history": [{"ts": "t", "from": None, "to": "candidate", "actor": "scout", "note": ""},
                     {"ts": "t", "from": "candidate", "to": status, "actor": actor, "note": ""}]}
    if reason:
        e["record"]["proof"] = {"result": "failed", "reason": reason}
    return e


class TestReport(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / ".audit").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, metrics, findings):
        (self.repo / ".audit" / "metrics.jsonl").write_text(
            "".join(json.dumps(m) + "\n" for m in metrics))
        (self.repo / ".audit" / "state.json").write_text(
            json.dumps({"version": 1, "scanned_hunks": {}, "findings": findings, "runs": []}))

    def test_useful_rate_and_funnel(self):
        self.write([metrics_line("r-1", verdicts=[("0001", "useful")]),
                    metrics_line("r-2", emitted=2,
                                 verdicts=[("0002", "noise"), ("0003", "useful")])],
                   {"a" * 16: finding("emitted"),
                    "b" * 16: finding("killed", actor="expert"),
                    "c" * 16: finding("killed", actor="critic")})
        r = report.build(str(self.repo))
        self.assertEqual(r["runs"], 2)
        self.assertEqual(r["funnel"]["candidates"], 6)
        self.assertEqual(r["verdicts"], {"useful": 2, "noise": 1, "unjudged": 0})
        self.assertAlmostEqual(r["useful_rate"], 2 / 3)
        self.assertEqual(r["kills"], {"expert": 1, "critic": 1, "other": 0})
        text = report.render(r)
        self.assertIn("USEFUL RATE: 67%", text)
        self.assertIn("below target", text)

    def test_verdict_cli_updates_emitting_run_and_report(self):
        import subprocess
        item = finding("emitted", actor="emit")
        item["issue_id"] = "0001"
        item["history"][-1]["note"] = "r-1"
        self.write([metrics_line("r-1"), metrics_line("r-2", emitted=0)], {"a"*16: item})
        verdict = subprocess.run([str(SCRIPTS / "verdict.sh"), "0001", "useful",
                                  "--repo", str(self.repo)], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(verdict.stdout)["run"], "r-1")
        out = subprocess.run([str(SCRIPTS / "run.sh"), "--report", "--json", "--repo", str(self.repo)],
                             capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(out.stdout)["useful_rate"], 1.0)

    def test_no_verdicts_yet(self):
        self.write([metrics_line("r-1")], {"a" * 16: finding("emitted")})
        r = report.build(str(self.repo))
        self.assertIsNone(r["useful_rate"])
        self.assertIn("n/a — 0 of 1 emissions judged", report.render(r))

    def test_kill_digest(self):
        killed = finding("killed", actor="critic")
        killed["history"][-1]["note"] = "critic kill: covered by spec X"
        killed["history"][-1]["ts"] = "2026-09-08T10:00:00.000000Z"
        killed2 = finding("killed", actor="expert")
        killed2["history"][-1]["note"] = "expert reject: no failure scenario"
        killed2["history"][-1]["ts"] = "2026-09-07T10:00:00.000000Z"
        self.write([metrics_line("r-1")],
                   {"a" * 16: killed, "b" * 16: killed2, "c" * 16: finding("emitted")})
        kills = report.build_kills(str(self.repo))
        self.assertEqual([k["actor"] for k in kills], ["critic", "expert"])  # newest first
        text = report.render_kills(str(self.repo), kills)
        self.assertIn("kills by actor: critic: 1, expert: 1", text)
        self.assertIn("covered by spec X", text)
        self.assertIn("negative-examples.md", text)

    def test_quarantine_composition(self):
        self.write([metrics_line("r-1", emitted=0)],
                   {"a" * 16: finding("quarantined", actor="emit",
                                      reason="security_requires_fail_then_pass")})
        r = report.build(str(self.repo))
        self.assertEqual(r["quarantine"][0]["reason"], "security_requires_fail_then_pass")
        text = report.render(r)
        self.assertIn("security_requires_fail_then_pass", text)
        self.assertIn("recall hole", text)


if __name__ == "__main__":
    unittest.main()
