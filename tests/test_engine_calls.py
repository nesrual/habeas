"""Plan 8.2 response handling, idempotent resume and soft budget limits."""

import json
import os
import signal
import sqlite3
import subprocess
import time
import unittest
from collections import Counter

import engine_support as es
from engine import calls, cli, find, ledger
from fake_openai import FakeOpenAI, candidate


def run_find(ws, **kw):
    specs = find.plan(ws.conn, ws.cfg, ws.repo)
    return calls.run_calls(ws.conn, ws.cfg, ws.run_dir, specs, find.record_candidates, **kw)


def rows(ws, q="SELECT * FROM calls ORDER BY created_at"):
    return [dict(r) for r in ws.conn.execute(q)]


class OneCall(unittest.TestCase):
    """One unit, one seat, one run: a single planned call per test."""

    def call(self, script, mutate=None):
        def one(m, r):
            m["seats"]["finder"] = m["seats"]["finder"][:1]
            r["runs_per_finder"] = 1
            if mutate:
                mutate(m, r)
        with FakeOpenAI(script) as server:
            ws = es.Workspace(server.base_url, n_units=1, mutate=one)
            self.addCleanup(ws.cleanup)
            summary = run_find(ws)
            (row,) = rows(ws)
            return ws, server, row, summary

    def candidates(self, ws):
        return ws.conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]

    def test_valid_answer_is_ok(self):
        ws, server, row, _ = self.call(lambda b, n: {"kind": "valid"})
        self.assertEqual(row["status"], "ok")
        self.assertEqual((row["requested_model"], row["served_model"], row["served_provider"]),
                         ("a/model-1", "a/model-1", "FakeHost"))
        self.assertEqual((row["input_tokens"], row["output_tokens"], row["cost_usd"]), (100, 20, 0.01))
        self.assertEqual(row["finish_reason"], "stop")
        self.assertEqual(self.candidates(ws), 1)
        body = server.requests[0]["body"]
        self.assertEqual(body["reasoning"], {"effort": "high"})
        self.assertEqual(body["provider"], {"allow_fallbacks": False, "require_parameters": True,
                                            "data_collection": "deny"})
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertIn("Every record lookup is scoped.", body["messages"][0]["content"])
        self.assertIn("=== lib/unit0.rb (lines 1-5) ===", body["messages"][1]["content"])

    def test_raw_log_holds_the_exchange_but_no_key(self):
        ws, server, row, _ = self.call(lambda b, n: {"kind": "valid"})
        self.assertEqual(server.requests[0]["headers"]["Authorization"], "Bearer sk-test-secret-value")
        raw = (ws.run_dir / row["raw_path"]).read_text()
        self.assertIn("[redacted]", raw)
        self.assertNotIn("sk-test-secret-value", raw)
        self.assertEqual(json.loads(raw)["responses"][0]["http_status"], 200)
        dump = "\n".join(ws.conn.iterdump())
        self.assertNotIn("sk-test-secret-value", dump)

    def test_empty_list_is_ok_with_zero_candidates(self):
        ws, _, row, _ = self.call(lambda b, n: {"kind": "empty"})
        self.assertEqual(row["status"], "ok")
        self.assertEqual(self.candidates(ws), 0)

    def test_endpoint_refusal_is_refused_not_zero_candidates(self):
        ws, server, row, summary = self.call(lambda b, n: {"kind": "refusal"})
        self.assertEqual(row["status"], "refused")
        self.assertEqual(len(server.requests), 1)
        # a refused call is distinguishable from an `ok` call that found nothing
        self.assertEqual(summary["by_status"], {"refused": 1})
        self.assertEqual(ws.conn.execute(
            "SELECT COUNT(*) FROM calls WHERE status IN ('ok', 'downgraded')").fetchone()[0], 0)

    def fallback_run(self, script, mutate):
        def one(m, r):
            m["seats"]["finder"] = m["seats"]["finder"][:1]
            r["runs_per_finder"] = 1
            mutate(m, r)
        with FakeOpenAI(script) as server:
            ws = es.Workspace(server.base_url, n_units=1, mutate=one)
            self.addCleanup(ws.cleanup)
            summary = run_find(ws)
            return ws, server, rows(ws), summary

    def test_refused_call_is_sent_once_to_the_alternate_model(self):
        def alternate(m, r):
            m["seats"]["finder"][0]["alternate_model"] = "a/model-2"
        ws, server, got, summary = self.fallback_run(
            lambda b, n: {"kind": "refusal"} if b["model"] == "a/model-1" else {"kind": "valid"}, alternate)
        self.assertEqual([b["body"]["model"] for b in server.requests], ["a/model-1", "a/model-2"])
        by_model = {r["requested_model"]: r for r in got}
        self.assertEqual(by_model["a/model-1"]["status"], "refused")       # the refusal stays on record
        self.assertEqual(by_model["a/model-2"]["status"], "ok")
        self.assertEqual(by_model["a/model-2"]["seat_id"], by_model["a/model-1"]["seat_id"])
        self.assertIn("fallback after refusal by a/model-1", by_model["a/model-2"]["reason"])
        self.assertGreater(self.candidates(ws), 0)
        # a second pass calls nothing again
        self.assertEqual(run_find(ws)["executed"], 0)
        self.assertEqual(len(server.requests), 2)

    def test_general_fallback_model_and_a_fallback_that_refuses_too(self):
        def general(m, r):
            m["refusal_fallback"] = {"enabled": True, "model": "z/other"}
        ws, server, got, _ = self.fallback_run(lambda b, n: {"kind": "refusal"}, general)
        self.assertEqual([b["body"]["model"] for b in server.requests], ["a/model-1", "z/other"])
        self.assertEqual([r["status"] for r in got], ["refused", "refused"])
        self.assertEqual(self.candidates(ws), 0)

    def test_fallback_can_be_switched_off_for_a_seat(self):
        def off(m, r):
            m["refusal_fallback"] = {"enabled": True, "model": "z/other"}
            m["seats"]["finder"][0]["refusal_fallback"] = False
        _, server, got, _ = self.fallback_run(lambda b, n: {"kind": "refusal"}, off)
        self.assertEqual(len(server.requests), 1)

    def test_refusal_text_without_json_is_refused(self):
        _, server, row, _ = self.call(lambda b, n: {"kind": "refusal_text"})
        self.assertEqual((row["status"], row["reason"]), ("refused", "refusal_text"))
        self.assertEqual(len(server.requests), 1)

    def test_blocked_input_403_is_refused(self):
        _, _, row, _ = self.call(lambda b, n: {
            "kind": "http", "status": 403, "metadata": {"error_type": "content_policy_violation"}})
        self.assertEqual(row["status"], "refused")

    def test_other_403_is_an_error(self):
        _, server, row, _ = self.call(lambda b, n: {"kind": "http", "status": 403})
        self.assertEqual(row["status"], "error")
        self.assertEqual(len(server.requests), 1)

    def test_different_model_is_downgraded_and_output_kept(self):
        ws, _, row, _ = self.call(lambda b, n: {"kind": "wrong_model"})
        self.assertEqual((row["status"], row["served_model"]), ("downgraded", "other/cheaper-model"))
        self.assertEqual(self.candidates(ws), 1)

    def test_dated_snapshot_of_the_pinned_model_is_not_a_downgrade(self):
        _, _, row, _ = self.call(lambda b, n: {"kind": "valid", "model": "a/model-1-20261109"})
        self.assertEqual(row["status"], "ok")

    def test_invalid_json_gets_one_repair_retry_then_error(self):
        ws, server, row, _ = self.call(lambda b, n: {"kind": "invalid_json"})
        self.assertEqual(row["status"], "error")
        self.assertIn("invalid_json", row["reason"])
        self.assertEqual(len(server.requests), 2)
        self.assertEqual(len(server.requests[1]["body"]["messages"]), 4)
        self.assertEqual((row["input_tokens"], row["cost_usd"]), (200, 0.02))  # both attempts are paid for
        self.assertEqual(self.candidates(ws), 0)

    def test_invalid_json_repaired_on_retry_is_ok(self):
        _, server, row, _ = self.call(lambda b, n: {"kind": "invalid_json" if n == 1 else "valid"})
        self.assertEqual(row["status"], "ok")
        self.assertEqual(len(server.requests), 2)

    def test_schema_mismatch_is_invalid(self):
        bad = dict(candidate(), confidence=7)
        _, _, row, _ = self.call(lambda b, n: {"kind": "valid", "candidates": [bad]})
        self.assertEqual(row["status"], "error")

    def test_429_and_500_are_retried(self):
        for status in (429, 500):
            _, server, row, _ = self.call(
                lambda b, n, status=status: {"kind": "http", "status": status} if n < 3 else {"kind": "valid"})
            self.assertEqual(row["status"], "ok")
            self.assertEqual(len(server.requests), 3)

    def test_retries_stop_after_five_attempts(self):
        _, server, row, _ = self.call(lambda b, n: {"kind": "http", "status": 500})
        self.assertEqual(row["status"], "error")
        self.assertEqual(len(server.requests), 5)

    def test_transport_failure_is_an_error(self):
        ws = es.Workspace("http://127.0.0.1:9/v1", n_units=1)
        self.addCleanup(ws.cleanup)
        run_find(ws)
        self.assertEqual({r["status"] for r in rows(ws)}, {"error"})

    def test_pinned_open_weight_seat_wrong_provider_is_error(self):
        def pin(m, r):
            m["seats"]["finder"][0].update(open_weights=True, provider={"only": ["good-host"]})
        ws, server, row, _ = self.call(lambda b, n: {"kind": "valid", "provider": "Elsewhere"}, pin)
        self.assertEqual(row["status"], "error")
        self.assertEqual(self.candidates(ws), 0)
        self.assertEqual(server.requests[0]["body"]["provider"]["only"], ["good-host"])
        _, _, row, _ = self.call(lambda b, n: {"kind": "valid", "provider": "Good Host"}, pin)
        self.assertEqual(row["status"], "ok")

    def test_non_sensitive_only_seat_is_skipped_on_a_sensitive_unit(self):
        def mark(m, r):
            m["seats"]["finder"][0]["non_sensitive_only"] = True
        _, server, row, _ = self.call(lambda b, n: {"kind": "valid"}, mark)
        self.assertEqual(row["status"], "skipped")
        self.assertEqual(server.requests, [])

    def test_candidate_outside_the_unit_is_rejected_and_counted(self):
        ws, _, row, _ = self.call(lambda b, n: {"kind": "valid", "candidates": [
            candidate("lib/unit0.rb", 2), candidate("app/controllers/base_controller.rb", 3)]})
        self.assertEqual((row["status"], row["rejected_candidates"]), ("ok", 1))
        self.assertEqual(self.candidates(ws), 1)


class ResumeAndBudget(unittest.TestCase):
    PLANNED = 3 * 2 * 2   # units x finder seats x runs

    def test_finished_calls_are_never_repeated(self):
        with FakeOpenAI() as server:
            ws = es.Workspace(server.base_url)
            self.addCleanup(ws.cleanup)
            first = run_find(ws)
            self.assertEqual((first["planned"], first["executed"], first["pending"]), (self.PLANNED, self.PLANNED, 0))
            again = run_find(ws)
            self.assertEqual((again["already_done"], again["executed"]), (self.PLANNED, 0))
            self.assertEqual(len(server.requests), self.PLANNED)
            keys = [r["idempotency_key"] for r in rows(ws)]
            self.assertEqual(len(set(keys)), self.PLANNED)
            # run i uses lens i and effort_schedule[i]
            self.assertEqual({(r["run_index"], r["lens"]) for r in rows(ws)},
                             {(0, "authz_tenancy"), (1, "money_ledger")})
            efforts = Counter(r["body"]["reasoning"]["effort"] for r in server.requests)
            self.assertEqual(efforts, {"high": 6, "low": 6})

    def test_errors_are_retried_only_on_request(self):
        fail = {"on": True}
        with FakeOpenAI(lambda b, n: {"kind": "http", "status": 400} if fail["on"] else {"kind": "valid"}) as server:
            ws = es.Workspace(server.base_url, n_units=1)
            self.addCleanup(ws.cleanup)
            run_find(ws)
            fail["on"] = False
            self.assertEqual(run_find(ws)["executed"], 0)
            self.assertEqual(run_find(ws, retry_errors=True)["by_status"], {"ok": 4})
            self.assertEqual(Counter(r["status"] for r in rows(ws)), {"ok": 4})

    def test_killed_find_resumes_without_repeating_a_finished_call(self):
        hang_at = 6
        with FakeOpenAI(lambda b, n: {"kind": "hang"} if n == hang_at else {"kind": "valid"}) as server:
            ws = es.Workspace(server.base_url)
            self.addCleanup(ws.cleanup)
            cmd = [str(es.ROOT / "bin" / "habeas"), *ws.cli_args("find")]
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    start_new_session=True)
            try:
                self.assertTrue(server.hung.wait(30), "find never reached the hanging call")
                deadline = time.time() + 30
                while time.time() < deadline:
                    if ws.conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == hang_at - 1:
                        break
                    time.sleep(0.05)
                os.killpg(proc.pid, signal.SIGKILL)
            finally:
                proc.wait(30)
            self.assertEqual(len(rows(ws)), hang_at - 1)

            done = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn(f"{self.PLANNED} calls planned, {hang_at - 1} already done", done.stdout)
            self.assertLessEqual(len(done.stdout.splitlines()), 20)
            self.assertEqual(len(rows(ws)), self.PLANNED)
            completed = Counter(server.completed_hashes())
            self.assertEqual(len(completed), self.PLANNED)
            self.assertEqual(set(completed.values()), {1}, "a finished call was sent twice")
            # only the call that was in flight when the process died went out again
            self.assertEqual(len(server.requests), self.PLANNED + 1)
            self.assertEqual(ws.conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], self.PLANNED)

    def cli(self, ws, *args):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(ws.cli_args(*args))
        return code, out.getvalue(), err.getvalue()

    def test_budget_pause_raise_and_resume(self):
        with FakeOpenAI() as server:   # every call costs $0.01
            ws = es.Workspace(server.base_url, limit_usd=0.05)
            self.addCleanup(ws.cleanup)
            code, out, _ = self.cli(ws, "find")
            self.assertEqual(code, cli.EXIT_HALTED)
            self.assertIn("PAUSED at a budget limit", out)
            self.assertEqual(len(server.requests), 5)
            self.assertEqual(ledger.run(ws.conn)["state"], "paused_budget")
            # nothing is dropped: the finished calls and their candidates are kept
            self.assertEqual(Counter(r["status"] for r in rows(ws)), {"ok": 5})
            self.assertEqual(ws.conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 5)
            kinds = [r["kind"] for r in rows(ws, "SELECT kind FROM budget_events ORDER BY event_id")]
            self.assertEqual(kinds, ["warning", "pause"])

            # paused: a plain `find` refuses, and `resume` without a raise pauses again
            code, _, err = self.cli(ws, "find")
            self.assertEqual(code, cli.EXIT_ERRORS)
            self.assertIn("paused_budget", err)
            code, _, _ = self.cli(ws, "resume")
            self.assertEqual(code, cli.EXIT_HALTED)
            self.assertEqual(len(server.requests), 5)
            self.assertEqual(ledger.run(ws.conn)["state"], "paused_budget")

            # declined: the run stays paused and status reports partial work with a projection
            code, out, _ = self.cli(ws, "status", "--json")
            status = json.loads(out)
            self.assertEqual((status["state"], status["partial"], status["finder_calls_pending"]),
                             ("paused_budget", True, 7))
            self.assertEqual(status["budget"]["spent_usd"], 0.05)
            self.assertEqual(status["budget"]["projected_more_usd"], 0.07)
            self.assertEqual(status["budget"]["spent_tokens"], 600)

            # a raise must be explicit and above what is used
            self.assertEqual(self.cli(ws, "budget", "raise", "--usd", "0.04")[0], cli.EXIT_ERRORS)
            code, out, _ = self.cli(ws, "budget", "raise", "--usd", "1", "--approved-by", "owner")
            self.assertEqual(code, 0)
            event = rows(ws, "SELECT * FROM budget_events WHERE kind = 'raise'")[0]
            self.assertEqual((event["old_limit"], event["new_limit"], event["approved_by"], event["unit"]),
                             (0.05, 1.0, "owner", "usd"))
            self.assertTrue(event["created_at"])
            self.assertEqual(ledger.run(ws.conn)["state"], "paused_budget")  # only resume continues

            code, out, _ = self.cli(ws, "resume")
            self.assertEqual(code, 0, out)
            self.assertEqual(ledger.run(ws.conn)["state"], "running")
            self.assertEqual(len(server.requests), self.PLANNED)
            self.assertEqual(set(Counter(server.completed_hashes()).values()), {1})
            self.assertEqual(len(rows(ws)), self.PLANNED)

    def test_token_limit_pauses_too(self):
        def tokens(m, r):
            r["budget"]["limit_tokens"] = 300   # 120 tokens per call
        with FakeOpenAI() as server:
            ws = es.Workspace(server.base_url, mutate=tokens)
            self.addCleanup(ws.cleanup)
            summary = run_find(ws)
            self.assertEqual(summary["state"], "paused_budget")
            self.assertEqual(len(server.requests), 3)
            self.assertEqual(summary["budget"]["at_limit"], ["tokens"])

    def test_concurrent_calls_do_not_run_far_past_the_limit(self):
        def wide(m, r):
            m["endpoints"]["fake"]["max_concurrency"] = 8
        with FakeOpenAI() as server:
            ws = es.Workspace(server.base_url, limit_usd=0.05, mutate=wide)
            self.addCleanup(ws.cleanup)
            summary = run_find(ws)
            self.assertEqual(summary["state"], "paused_budget")
            self.assertLessEqual(len(server.requests), 6)
            self.assertEqual(summary["executed"] + summary["pending"], self.PLANNED)

    def test_stop_lets_the_run_resume(self):
        with FakeOpenAI() as server:
            ws = es.Workspace(server.base_url)
            self.addCleanup(ws.cleanup)
            (ws.run_dir / "STOP").write_text("x")
            code, out, _ = self.cli(ws, "find")
            self.assertEqual(code, cli.EXIT_HALTED)
            self.assertEqual((ledger.run(ws.conn)["state"], server.requests), ("stopped", []))
            self.assertEqual(self.cli(ws, "stop")[0], 0)   # nothing running: stays stopped
            code, out, _ = self.cli(ws, "resume")
            self.assertEqual(code, 0, out)
            self.assertEqual(len(server.requests), self.PLANNED)

    def test_out_of_credits_stops_the_stage_instead_of_failing_every_call(self):
        with FakeOpenAI(lambda b, n: {"kind": "http", "status": 402}) as server:
            ws = es.Workspace(server.base_url)
            self.addCleanup(ws.cleanup)
            code, out, _ = self.cli(ws, "find")
            self.assertEqual(code, cli.EXIT_HALTED)
            self.assertIn("STOPPED: the endpoint rejected the account: http_402", out)
            self.assertEqual(len(server.requests), 1)
            self.assertEqual(ledger.run(ws.conn)["state"], "stopped")

    def test_missing_api_key_fails_before_any_call(self):
        def key(m, r):
            m["endpoints"]["fake"]["key_env"] = "HABEAS_TEST_UNSET_KEY"
        with FakeOpenAI() as server:
            ws = es.Workspace(server.base_url, mutate=key)
            self.addCleanup(ws.cleanup)
            code, _, err = self.cli(ws, "find")
            self.assertEqual(code, cli.EXIT_ERRORS)
            self.assertIn("HABEAS_TEST_UNSET_KEY is not set", err)
            self.assertEqual(rows(ws), [])


class ConfigCheckCli(unittest.TestCase):
    def test_todo_values_are_reported_and_other_commands_refuse(self):
        import contextlib
        import io
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            r = es.run_cfg()
            r["target"]["repo_path"] = "TODO"
            for name, doc in (("models.yaml", es.models()), ("run.yaml", r), ("lenses.yaml", es.LENSES)):
                (Path(d) / name).write_text(json.dumps(doc))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["config", "check", "--config-dir", d])
            self.assertEqual(code, cli.EXIT_TODO)
            self.assertIn("TODO: run.target.repo_path", out.getvalue())
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = cli.main(["find", "--config-dir", d, "--run-dir", "/nonexistent"])
            self.assertEqual(code, cli.EXIT_ERRORS)
            self.assertIn("config is not ready", err.getvalue())

    def test_online_check_flags_unlisted_models_and_unsupported_efforts(self):
        listed = [{"id": "a/model-1", "reasoning": {"supported_efforts": ["high"]}},
                  {"id": "a/model-2"}, {"id": "b/model-2"}, {"id": "c/model-1"},
                  {"id": "a/merge-model"}, {"id": "b/audit-model"}, {"id": "b/check-model"},
                  {"id": "a/check-model"}, {"id": "a/write-model"}]
        with FakeOpenAI(models=listed) as server:
            ws = es.Workspace(server.base_url, n_units=0)
            self.addCleanup(ws.cleanup)
            problems = cli.check_online(ws.cfg)
        self.assertEqual(len(problems), 2, problems)
        self.assertIn("a/model-1 does not accept effort low", problems[0])
        self.assertIn("b/model-1 is not listed", problems[1])



class WhitespaceRunaway(unittest.TestCase):
    def test_object_left_open_before_a_run_of_whitespace_is_closed(self):
        from engine import adapter
        value, problem = adapter._parse('{"action": "search", "query": "x"' + " \t" * 40, None)
        self.assertEqual((value, problem), ({"action": "search", "query": "x"}, None))
        self.assertEqual(adapter._parse('{"action": "search", "query": "x', None), (None, "not JSON"))
        self.assertEqual(adapter._parse('{"action": "sea' + " " * 40, None), (None, "not JSON"))
        sch = {"type": "object", "required": ["action", "edits", "test_file"],
               "properties": {"action": {"type": "string"}, "edits": {"type": "array"},
                              "test_file": {"type": "string"}}}
        self.assertEqual(adapter._parse('{"action": "list"' + "\r\n" * 30, sch),
                         ({"action": "list", "edits": [], "test_file": ""}, None))


if __name__ == "__main__":
    unittest.main()


class RateLimitPatience(unittest.TestCase):
    def test_a_rate_limit_gets_more_tries_and_waits_at_most_about_a_minute(self):
        from engine import adapter
        self.assertGreater(adapter.MAX_ATTEMPTS_RATE_LIMITED, adapter.MAX_ATTEMPTS)
        old = os.environ.get("HABEAS_BACKOFF_BASE")
        os.environ["HABEAS_BACKOFF_BASE"] = "1.0"
        try:
            waits = [adapter._backoff(n, {}, 429) for n in range(1, adapter.MAX_ATTEMPTS_RATE_LIMITED)]
        finally:
            if old is None:
                os.environ.pop("HABEAS_BACKOFF_BASE")
            else:
                os.environ["HABEAS_BACKOFF_BASE"] = old
        self.assertLessEqual(max(waits), 75.0)
        self.assertGreater(sum(waits), 120.0)       # long enough for a per-minute window to move twice


class LongTitles(unittest.TestCase):
    def test_a_long_candidate_title_is_cut_not_rejected(self):
        from engine import schema
        long = "word " * 60
        self.assertLessEqual(len(find._short(long)), 120)
        self.assertTrue(find._short(long).endswith("..."))
        self.assertEqual(find._short("short  title"), "short title")
        cand = candidate("engines/billing/app/x.rb", 1)
        cand["title"] = long
        self.assertEqual(schema.errors({"candidates": [cand]}, schema.FINDER), [])


class FlaggedContentIsARefusal(unittest.TestCase):
    def test_a_403_content_flag_without_an_error_type_counts_as_refused(self):
        from engine import adapter
        def script(body, n):
            return {"kind": "http", "status": 403}
        with FakeOpenAI(script) as server:
            server_error = adapter._send
            def fake_send(url, headers, body, timeout, attempts):
                return None, (403, {"message": "This content was flagged for possible cybersecurity risk."})
            adapter._send = fake_send
            try:
                out = adapter.complete({"id": "s", "model": "m/x"}, {"base_url": server.base_url, "key_env": None},
                                       None, "sys", "user")
            finally:
                adapter._send = server_error
        self.assertEqual((out["status"], out["reason"]), ("refused", "http_403_flagged"))
