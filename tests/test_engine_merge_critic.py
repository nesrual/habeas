"""Phase 3: merge needs two yes votes; refutation needs every critic."""

import json
import unittest

import engine_support as es
from fake_openai import FakeOpenAI, candidate

CONTROLLER = "engines/billing/app/controllers/billing/invoices_controller.rb"


def role_of(body):
    return body["response_format"]["json_schema"]["name"]


class Pregroup(unittest.TestCase):
    def test_lenses_are_kept_apart_unless_configured(self):
        import json
        from engine import merge
        loc = json.dumps([{"path": "a.rb", "start_line": 3, "end_line": 5}])
        cands = [{"candidate_id": "x", "lens": "authz_tenancy", "locations_json": loc},
                 {"candidate_id": "y", "lens": "input_to_sink", "locations_json": loc}]
        self.assertEqual(len(merge.pregroup(cands)), 2)
        self.assertEqual(len(merge.pregroup(cands, across_lenses=True)), 1)


class Pipeline(unittest.TestCase):
    """One unit (the invoices controller), no static rules, scripted answers."""

    def run_to(self, script, last="critique", mutate=None):
        def only_controller(m, r):
            r["units"]["include"] = [CONTROLLER]
            r["units"]["pack_small_files"] = False
            r["runs_per_finder"] = 1
            if mutate:
                mutate(m, r)
        self.server = FakeOpenAI(script)
        self.server.__enter__()
        self.addCleanup(self.server.__exit__)
        s = es.SampleRun(base_url=self.server.base_url, mutate=only_controller)
        self.addCleanup(s.cleanup)
        (s.config_dir / "static_rules.yaml").unlink()
        self.codes = {}
        for cmd in ("preflight", "scout", "find", "merge", "critique"):
            code, out, err = s.cli(cmd)
            self.codes[cmd] = (code, out + err)
            if cmd == last:
                break
        return s

    def finder(self, *cands):
        """Finder a reports cands[0], finder b reports cands[1] (or nothing)."""
        def answer(body):
            i = 0 if body["model"].startswith("a/") else 1
            return {"kind": "valid", "candidates": [cands[i]] if i < len(cands) and cands[i] else []}
        return answer

    def test_two_candidates_merge_only_when_merger_and_auditor_both_say_yes(self):
        for merger, auditor, clusters in ((True, True, 1), (True, False, 2), (False, True, 2), (False, False, 2)):
            find = self.finder(candidate(CONTROLLER, 3, "no scope"), candidate(CONTROLLER, 4, "unscoped find"))

            def script(body, n, merger=merger, auditor=auditor):
                if role_of(body) == "finder_candidates":
                    return find(body)
                yes = merger if body["model"] == "a/merge-model" else auditor
                return {"kind": "valid", "json": {"same_root_cause": yes, "reason": "r"}}

            def seats(m, r):
                m["seats"]["merger"] = [{"id": "merger", "lab": "a", "endpoint": "fake", "model": "a/merge-model"}]
                m["seats"]["merge_auditor"] = [{"id": "auditor", "lab": "b", "endpoint": "fake", "model": "b/audit-model"}]
            s = self.run_to(script, last="merge", mutate=seats)
            self.assertEqual(self.codes["merge"][0], 0, self.codes["merge"][1])
            self.assertEqual(len(s.rows("SELECT * FROM clusters")), clusters, (merger, auditor))
            (m,) = s.rows("SELECT * FROM merges")
            self.assertEqual((m["merger_verdict"], m["auditor_verdict"], m["merged"]),
                             ("yes" if merger else "no", "yes" if auditor else "no", int(merger and auditor)))
            self.assertEqual(len(s.rows("SELECT * FROM cluster_members")), 2)
            self.assertEqual(s.cli("reconcile")[0], 0)

    def test_partition_mode_joins_only_what_both_seats_group_together(self):
        # three nearby candidates; the merger groups all three, the auditor only the first two
        three = [candidate(CONTROLLER, 3, "no scope"), candidate(CONTROLLER, 4, "unscoped find"),
                 candidate(CONTROLLER, 5, "other cause")]

        def find(body):
            return {"kind": "valid", "candidates": three if body["model"].startswith("a/") else []}

        def script(body, n):
            if role_of(body) == "finder_candidates":
                return find(body)
            self.assertEqual(role_of(body), "merge_partition")
            self.assertIn("Report 3:", body["messages"][1]["content"])
            reports = [1, 2, 3] if body["model"] == "a/merge-model" else [1, 2, 99]      # 99: no such report
            return {"kind": "valid", "json": {"groups": [{"reports": reports, "root_cause": "r"}]}}

        def seats(m, r):
            m["seats"]["merger"] = [{"id": "merger", "lab": "a", "endpoint": "fake", "model": "a/merge-model"}]
            m["seats"]["merge_auditor"] = [{"id": "auditor", "lab": "b", "endpoint": "fake", "model": "b/audit-model"}]
            r["merge"] = {**(r.get("merge") or {}), "mode": "partition"}
        s = self.run_to(script, last="merge", mutate=seats)
        self.assertEqual(self.codes["merge"][0], 0, self.codes["merge"][1])
        self.assertEqual(s.rows("SELECT status, reason FROM calls WHERE role LIKE 'merge%' AND status != 'ok'"), [])
        sizes = sorted(r["n"] for r in s.rows("SELECT COUNT(*) n FROM cluster_members GROUP BY cluster_id"))
        self.assertEqual(sizes, [1, 2])
        # one call per seat for the whole group, not one per pair
        self.assertEqual(len([r for r in self.server.requests if role_of(r["body"]) == "merge_partition"]), 2)
        merged = s.rows("SELECT merger_verdict, auditor_verdict, merged FROM merges ORDER BY merged")
        self.assertEqual([m["merged"] for m in merged], [0, 0, 1])
        self.assertEqual(s.cli("reconcile")[0], 0)

    def test_candidates_in_distant_lines_or_other_lenses_are_never_compared(self):
        def lenses(m, r):
            r["runs_per_finder"] = 2

        def script(body, n):
            if role_of(body) != "finder_candidates":
                return {"kind": "valid"}
            first_lens = "Every record lookup is scoped." in body["messages"][0]["content"]
            if first_lens:      # same lens, lines 3 and 10: too far apart to compare
                return {"kind": "valid", "candidates": [candidate(CONTROLLER, 3 if body["model"].startswith("a/") else 10)]}
            if body["model"].startswith("a/"):   # other lens, line 3 again
                return {"kind": "valid", "candidates": [candidate(CONTROLLER, 3)]}
            return {"kind": "empty"}
        s = self.run_to(script, last="merge", mutate=lenses)
        self.assertEqual(self.codes["merge"][0], 0, self.codes["merge"][1])
        self.assertEqual(len(s.rows("SELECT * FROM candidates")), 3)
        self.assertEqual(s.rows("SELECT * FROM merges"), [])
        self.assertEqual(len(s.rows("SELECT * FROM clusters")), 3)
        self.assertEqual({role_of(r["body"]) for r in self.server.requests}, {"finder_candidates"})

    def test_failed_merger_call_keeps_candidates_apart(self):
        find = self.finder(candidate(CONTROLLER, 3), candidate(CONTROLLER, 3))

        def script(body, n):
            if role_of(body) == "finder_candidates":
                return find(body)
            return {"kind": "refusal"} if body["model"] == "a/merge-model" else {"kind": "valid"}

        def seats(m, r):
            m["seats"]["merger"] = [{"id": "merger", "lab": "a", "endpoint": "fake", "model": "a/merge-model"}]
            m["seats"]["merge_auditor"] = [{"id": "auditor", "lab": "b", "endpoint": "fake", "model": "b/audit-model"}]
        s = self.run_to(script, last="merge", mutate=seats)
        self.assertEqual(s.rows("SELECT merger_verdict, merged FROM merges"),
                         [{"merger_verdict": "unavailable", "merged": 0}])
        self.assertEqual(len(s.rows("SELECT * FROM clusters")), 2)

    def critic_run(self, verdict_a, verdict_b):
        find = self.finder(candidate(CONTROLLER, 3))

        def script(body, n):
            if role_of(body) == "finder_candidates":
                return find(body)
            v = verdict_a if body["model"].startswith("a/") else verdict_b
            if v in ("refusal", "invalid_json"):
                return {"kind": v}
            if v == "http500":
                return {"kind": "http", "status": 500}
            return {"kind": "valid", "json": {"verdict": v, "reason": "lines 3-4"}}
        s = self.run_to(script)
        self.assertEqual(self.codes["critique"][0], 0, self.codes["critique"][1])
        (cluster,) = s.rows("SELECT * FROM clusters")
        verdicts = {v["critic_seat"]: v["verdict"] for v in s.rows("SELECT * FROM verdicts")}
        return s, cluster, verdicts

    def test_refuted_only_when_every_critic_refutes(self):
        s, cluster, verdicts = self.critic_run("refute", "refute")
        self.assertEqual(cluster["state"], "refuted")
        self.assertEqual(s.rows("SELECT outcome FROM outcomes"), [{"outcome": "refuted"}])
        for a, b in (("refute", "uphold"), ("refute", "unsure"), ("uphold", "uphold")):
            s, cluster, verdicts = self.critic_run(a, b)
            self.assertEqual(cluster["state"], "upheld", (a, b))
            self.assertEqual(verdicts, {"critic-a": a, "critic-b": b})
            self.assertEqual(s.rows("SELECT * FROM outcomes"), [])

    def test_unavailable_critic_blocks_refutation(self):
        for failure in ("refusal", "invalid_json", "http500"):
            s, cluster, verdicts = self.critic_run("refute", failure)
            self.assertEqual(verdicts, {"critic-a": "refute", "critic-b": "unavailable"}, failure)
            self.assertEqual(cluster["state"], "upheld", failure)

    def test_critic_sees_the_claim_the_unit_and_its_context(self):
        s, _, _ = self.critic_run("uphold", "uphold")
        (body,) = [r["body"] for r in self.server.requests
                   if role_of(r["body"]) == "critic_verdict" and r["body"]["model"].startswith("a/")]
        user = body["messages"][1]["content"]
        self.assertIn('"title": "unscoped lookup"', user)
        self.assertIn(f"=== {CONTROLLER} (lines 1-", user)
        self.assertIn("=== CONTEXT engines/billing/app/controllers/billing/application_controller.rb", user)

    def test_cluster_matching_the_baseline_is_known_and_skips_critics(self):
        def known(m, r):
            r["baseline"]["known_issues"] = [{"ref": "JIRA-7", "path": CONTROLLER, "start_line": 3,
                                              "end_line": 6, "description": "tracked"}]
        find = self.finder(candidate(CONTROLLER, 3))

        s = self.run_to(lambda body, n: find(body), mutate=known)
        self.assertEqual({role_of(r["body"]) for r in self.server.requests}, {"finder_candidates"})
        (cluster,) = s.rows("SELECT * FROM clusters")
        self.assertEqual((cluster["state"], cluster["known_ref"]), ("known", "JIRA-7"))
        self.assertEqual(s.rows("SELECT outcome FROM outcomes"), [{"outcome": "known"}])

    def test_merge_and_critique_resume_after_a_budget_pause(self):
        find = self.finder(candidate(CONTROLLER, 3), candidate(CONTROLLER, 3))

        def budget(m, r):
            r["budget"]["limit_usd"] = 0.035      # 2 finder calls + 2 merge calls reach it
        s = self.run_to(lambda body, n: find(body) if role_of(body) == "finder_candidates" else {"kind": "valid"},
                        mutate=budget)
        self.assertEqual(self.codes["merge"][0], 0, self.codes["merge"][1])
        self.assertEqual(self.codes["critique"][0], 3, self.codes["critique"][1])
        self.assertIn("PAUSED at a budget limit", self.codes["critique"][1])
        self.assertEqual(s.rows("SELECT state FROM clusters"), [{"state": "merged"}])   # undecided, not dropped
        self.assertEqual(s.cli("budget", "raise", "--usd", "5", "--approved-by", "owner")[0], 0)
        code, out, err = s.cli("resume")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(s.rows("SELECT state FROM clusters"), [{"state": "upheld"}])
        sent = [role_of(r["body"]) for r in self.server.requests]
        self.assertEqual(sent.count("critic_verdict"), 2)


if __name__ == "__main__":
    unittest.main()
