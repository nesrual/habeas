import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import state  # noqa: E402
import tracker  # noqa: E402


class FakeJira(BaseHTTPRequestHandler):
    requests: list = []
    attachments: dict = {}
    fail_first_with_400 = False
    _failed_once = False
    _created = 0

    def _json(self, code, payload):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        if self.path.endswith("/comment"):
            key = self.path.split("/issue/")[1].split("/")[0]
            body = json.loads(raw)
            type(self).comments.setdefault(key, []).append(body["body"])
            self._json(201, {"id": "1"})
            return
        if self.path.endswith("/attachments"):
            import re
            key = self.path.split("/issue/")[1].split("/")[0]
            names = re.findall(rb'filename="([^"]+)"', raw)
            names = [n.decode() for n in names]
            type(self).requests.append({"method": "POST", "path": self.path,
                                        "filenames": names,
                                        "token_header": self.headers.get("X-Atlassian-Token")})
            type(self).attachments.setdefault(key, set()).update(names)
            self._json(200, [{"filename": n} for n in names])
            return
        body = json.loads(raw)
        type(self).requests.append({"path": self.path, "body": body,
                                    "auth": self.headers.get("Authorization", "")})
        if type(self).fail_first_with_400 and not type(self)._failed_once:
            type(self)._failed_once = True
            self._json(400, {"errors": {"labels": "field not on screen"}})
            return
        type(self)._created += 1
        key = f"AR-{type(self)._created}"
        for label in body.get("fields", {}).get("labels", []):
            type(self).labels_index[label] = key
        self._json(201, {"key": key})

    labels_index: dict = {}

    def do_GET(self):
        if "/search?" in self.path:
            import urllib.parse
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1])
            m = [w.strip('"') for w in qs["jql"][0].split() if w.startswith('"fp-')]
            key = type(self).labels_index.get(m[0].strip('"')) if m else None
            self._json(200, {"issues": [{"key": key}] if key else []})
            return
        if self.path.endswith("/comment"):
            key = self.path.split("/issue/")[1].split("/")[0]
            self._json(200, {"comments": [{"body": b}
                                          for b in type(self).comments.get(key, [])]})
            return
        key = self.path.split("/issue/")[1].split("?")[0]
        self._json(200, {"fields": {"attachment": [
            {"filename": n} for n in sorted(type(self).attachments.get(key, set()))]}})

    comments: dict = {}

    def do_PUT(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append({"method": "PUT", "path": self.path, "body": body,
                                    "auth": self.headers.get("Authorization", "")})
        self.send_response(204)
        self.end_headers()

    def log_message(self, *a):
        pass


class TrackerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeJira)
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeJira.requests = []
        FakeJira.attachments = {}
        FakeJira.comments = {}
        FakeJira.labels_index = {}
        FakeJira.fail_first_with_400 = False
        FakeJira._failed_once = False
        FakeJira._created = 0
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        subprocess.run(["git", "-C", str(self.repo), "init", "-q"], check=True)
        (self.repo / ".audit").mkdir()
        (self.repo / ".audit" / "config.yaml").write_text(
            "tracker:\n  kind: jira\n"
            f"  jira_base_url: \"{self.base_url}\"\n"
            "  jira_project: AR\n  jira_email: \"me@example.com\"\n")
        settings = patch.dict(os.environ, {"HABEAS_CONFIG": str(self.repo / ".audit" / "config.yaml"),
                                           "HABEAS_ALLOW_HOST_PROOFS": "1"})
        settings.start()
        self.addCleanup(settings.stop)
        (self.repo / "lib").mkdir()
        (self.repo / "lib" / "fees.rb").write_text("code\n")
        state.init_repo(str(self.repo))
        state.run_start(str(self.repo), "lib")
        os.environ["JIRA_API_TOKEN"] = "tok-123"

    def tearDown(self):
        os.environ.pop("JIRA_API_TOKEN", None)
        self._tmp.cleanup()

    def make_emitted(self, n=1, track="provable"):
        category = "bug" if track == "provable" else "improvement"
        fp = state.add_candidates(str(self.repo), [{
            "id": f"c{n}", "category": category, "file": "lib/fees.rb",
            "lines": [1, 1], "hypothesis": f"finding number {n}. More detail here.",
            "confidence": 0.8, "evidence_snippet": f"code # {n}"}])["added"][0]
        state.transition(str(self.repo), fp, "expert_validated", "expert", record_patch={
            "failure_scenario": "it breaks", "severity": "high",
            "fix_sketch": "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b",
            "proof_sketch": {"type": "rspec", "expected_failure_pattern": "expected: 32"}})
        if track == "provable":
            state.transition(str(self.repo), fp, "critic_survived", "critic")
            state.transition(str(self.repo), fp, "proved", "prover", record_patch={
                "proof": {"result": "red", "failure_message": "expected 33 got 32",
                          "fail_then_pass": True},
                "proof_review": {"verdict": "valid"}})
        else:
            state.transition(str(self.repo), fp, "critic_advisory", "critic")
        state.transition(str(self.repo), fp, "emitted", "emit", issue_id=f"{n:04d}")
        folder = self.repo / "issues" / f"{n:04d}-finding-number"
        folder.mkdir(parents=True)
        (folder / "ISSUE.md").write_text(f"# finding {n}\n")
        if track == "provable":
            (folder / "proof_spec.rb").write_text(
                f"RSpec.describe 'audit {n}' do\n  it 'is red' do\n"
                f"    expect(33).to eq(32)\n  end\nend\n")
            (folder / "proof_run.log").write_text("=== run 1 ===\n1 example, 1 failure\n")
        return fp

    def push(self, **kw):
        return tracker.push(str(self.repo), kw.get("fingerprint"), kw.get("dry_run", False))


class TestVerify(TrackerTestCase):
    def prep(self, spec_text):
        fp = self.make_emitted()
        cfg = (self.repo / ".audit" / "config.yaml")
        cfg.write_text(cfg.read_text() + 'proof:\n  test_cmd: "rspec {file}"\n')
        (self.repo / "issues" / "0001-finding-number" / "proof_spec.rb").write_text(spec_text)
        state.annotate(str(self.repo), fp,
                       {"kind": "jira", "key": "AR-1", "url": "u", "ts": "t"})
        (self.repo / ".gitignore").write_text(".audit/\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t",
                        "-c", "user.email=t@t", "commit", "-qm", "x"],
                       check=True, capture_output=True)
        return fp

    def test_passing_spec_comments_fixed_once(self):
        fp = self.prep("RSpec.describe 'audit' do\n  it 'passes' do\n"
                       "    expect(1).to eq(1)\n  end\nend\n")
        out = tracker.verify(str(self.repo), None)
        self.assertEqual(out["verified"],
                         [{"fingerprint": fp, "key": "AR-1", "status": "fixed",
                           "commented": True}])
        self.assertEqual(len(FakeJira.comments["AR-1"]), 1)
        self.assertIn("habeas verify:", FakeJira.comments["AR-1"][0])
        self.assertIn("appears fixed", FakeJira.comments["AR-1"][0])
        # a second verify never duplicates the comment
        out = tracker.verify(str(self.repo), None)
        self.assertEqual(out["verified"][0]["commented"], False)
        self.assertEqual(len(FakeJira.comments["AR-1"]), 1)

    def test_still_red_spec_reports_and_stays_silent(self):
        fp = self.prep("RSpec.describe 'audit' do\n  it 'is red' do\n"
                       "    expect(33).to eq(32)\n  end\nend\n")
        out = tracker.verify(str(self.repo), None)
        self.assertEqual(out["verified"],
                         [{"fingerprint": fp, "key": "AR-1", "status": "still_red"}])
        self.assertEqual(FakeJira.comments, {})
        # no worktrees left behind
        wt = subprocess.run(["git", "-C", str(self.repo), "worktree", "list"],
                            capture_output=True, text=True).stdout
        self.assertEqual(len(wt.strip().splitlines()), 1)


class TestPush(TrackerTestCase):
    def test_jest_ticket_uses_frontend_artifact_and_runner(self):
        fp = self.make_emitted()
        st = state.load_state(str(self.repo))
        st["findings"][fp]["record"]["proof_sketch"]["type"] = "jest"
        state.save_state(str(self.repo), st)
        cfg = self.repo / ".audit/config.yaml"
        cfg.write_text(cfg.read_text() + 'proof:\n  test_cmd_jest: "jest {file}"\n')
        folder = self.repo / "issues/0001-finding-number"
        (folder / "proof_spec.rb").rename(folder / "proof_spec.test.tsx")
        (folder / "proof_spec.test.tsx").write_text("test('fixture', () => {});")
        payload = self.push(dry_run=True)["pushed"][0]["payload"]
        self.assertIn("jest app/javascript/__audit__/", payload["fields"]["description"])
        self.assertIn("test('fixture'", payload["fields"]["description"])
        names = [n for n, _ in tracker.artifact_files(str(self.repo), st["findings"][fp])]
        self.assertIn("proof_spec.test.tsx", names)

    def test_creates_ticket_and_annotates(self):
        fp = self.make_emitted()
        out = self.push()
        self.assertEqual(len(out["pushed"]), 1, out)
        self.assertEqual(out["pushed"][0]["key"], "AR-1")
        self.assertEqual(out["failed"], [])
        req = FakeJira.requests[0]
        self.assertEqual(req["path"], "/rest/api/2/issue")
        expected_auth = "Basic " + base64.b64encode(b"me@example.com:tok-123").decode()
        self.assertEqual(req["auth"], expected_auth)
        fields = req["body"]["fields"]
        self.assertEqual(fields["project"], {"key": "AR"})
        self.assertEqual(fields["issuetype"], {"name": "Task"})
        self.assertEqual(fields["summary"], "[habeas 0001] finding number 1")
        for section in ("h2. Claim", "h2. Failure scenario", "h2. Evidence",
                        "h2. How to replicate", "h2. How to resolve", "h2. Metadata",
                        "expected 33 got 32", "fail-then-pass"):
            self.assertIn(section, fields["description"])
        # ticket is self-contained: the full proof spec is inlined, and the
        # repro command never points into the reporter's issues/ folder
        desc = fields["description"]
        self.assertIn("RSpec.describe 'audit 1'", desc)
        self.assertIn("expect(33).to eq(32)", desc)
        self.assertIn("spec/audit/habeas_", desc)
        replicate = desc.split("h2. How to replicate")[1].split("h2. How to resolve")[0]
        self.assertNotIn("issues/", replicate)
        self.assertIn("habeas", fields["labels"])
        self.assertIn(f"fp-{fp}", fields["labels"])
        entry = state.show(str(self.repo), fp)
        self.assertEqual(entry["tracker"]["key"], "AR-1")
        self.assertIn("/browse/AR-1", entry["tracker"]["url"])

    def test_second_push_is_a_noop(self):
        self.make_emitted()
        self.push()
        n_requests = len(FakeJira.requests)
        out = self.push()
        self.assertEqual(out.get("note"), "nothing to push")
        self.assertEqual(len(FakeJira.requests), n_requests)

    def test_push_attaches_artifacts(self):
        self.make_emitted()
        out = self.push()
        self.assertEqual(out["pushed"][0]["attachments"],
                         ["ISSUE.md", "proof_spec.rb", "proof_run.log"])
        self.assertNotIn("attach_error", out["pushed"][0])
        self.assertEqual(FakeJira.attachments["AR-1"],
                         {"ISSUE.md", "proof_spec.rb", "proof_run.log"})
        upload = [r for r in FakeJira.requests if r.get("path", "").endswith("/attachments")][0]
        self.assertEqual(upload["token_header"], "no-check")
        entry_tracker = state.show(str(self.repo), out["pushed"][0]["fingerprint"])["tracker"]
        self.assertEqual(len(entry_tracker["attachments"]), 3)

    def test_advisory_attaches_only_issue_md(self):
        self.make_emitted(track="advisory")
        out = self.push()
        self.assertEqual(out["pushed"][0]["attachments"], ["ISSUE.md"])

    def test_attach_backfill_is_idempotent(self):
        fp = self.make_emitted()
        self.push()
        out = tracker.attach(str(self.repo), None)
        self.assertEqual(out["attached"], [{"fingerprint": fp, "key": "AR-1",
                                            "uploaded": []}])  # nothing missing
        # a lost attachment gets re-uploaded, others untouched
        FakeJira.attachments["AR-1"].discard("proof_run.log")
        out = tracker.attach(str(self.repo), None)
        self.assertEqual(out["attached"][0]["uploaded"], ["proof_run.log"])
        self.assertEqual(FakeJira.attachments["AR-1"],
                         {"ISSUE.md", "proof_spec.rb", "proof_run.log"})

    def test_advisory_ticket_carries_banner_and_no_repro(self):
        self.make_emitted(track="advisory")
        self.push()
        desc = FakeJira.requests[0]["body"]["fields"]["description"]
        self.assertIn("Advisory finding: opinion, not evidence", desc)
        self.assertNotIn("h2. How to replicate", desc)
        self.assertIn("no mechanical proof", desc)

    def test_tracks_filter(self):
        (self.repo / ".audit" / "config.yaml").write_text(
            "tracker:\n  kind: jira\n"
            f"  jira_base_url: \"{self.base_url}\"\n"
            "  jira_project: AR\n  jira_email: \"me@example.com\"\n"
            "  tracks: [provable]\n")
        self.make_emitted(1, track="provable")
        self.make_emitted(2, track="advisory")
        out = self.push()
        self.assertEqual(len(out["pushed"]), 1)

    def test_400_retries_without_labels(self):
        FakeJira.fail_first_with_400 = True
        self.make_emitted()
        out = self.push()
        self.assertEqual(out["pushed"][0]["key"], "AR-1")
        self.assertIn("labels", FakeJira.requests[0]["body"]["fields"])
        self.assertNotIn("labels", FakeJira.requests[1]["body"]["fields"])

    def test_missing_token_is_an_error_before_any_request(self):
        os.environ.pop("JIRA_API_TOKEN", None)
        self.make_emitted()
        with self.assertRaisesRegex(tracker.TrackerError, "JIRA_API_TOKEN"):
            self.push()
        self.assertEqual(FakeJira.requests, [])

    def test_dry_run_posts_nothing(self):
        os.environ.pop("JIRA_API_TOKEN", None)  # dry-run needs no credentials
        self.make_emitted()
        out = self.push(dry_run=True)
        self.assertEqual(FakeJira.requests, [])
        self.assertTrue(out["pushed"][0]["dry_run"])
        self.assertIn("h2. Claim",
                      out["pushed"][0]["payload"]["fields"]["description"])
        self.assertIsNone(state.show(str(self.repo),
                                     out["pushed"][0]["fingerprint"]).get("tracker"))

    def test_no_tracker_configured(self):
        (self.repo / ".audit" / "config.yaml").write_text("orchestration: inline\n")
        with self.assertRaisesRegex(tracker.TrackerError, "no tracker configured"):
            self.push()

    def test_refresh_puts_updated_description(self):
        fp = self.make_emitted()
        self.push()
        (self.repo / "issues" / "0001-finding-number" / "proof_spec.rb").write_text(
            "RSpec.describe 'audit 1 v2' do\nend\n")
        out = tracker.refresh(str(self.repo), None)
        self.assertEqual(out["updated"], [{"fingerprint": fp, "key": "AR-1"}])
        put = FakeJira.requests[-1]
        self.assertEqual(put["method"], "PUT")
        self.assertEqual(put["path"], "/rest/api/2/issue/AR-1")
        self.assertEqual(set(put["body"]["fields"]), {"summary", "description"})
        self.assertIn("audit 1 v2", put["body"]["fields"]["description"])

    def test_wiped_state_recovers_ticket_via_fingerprint_label(self):
        fp = self.make_emitted()
        self.push()
        # simulate a lost .audit: drop the tracker annotation from state
        sp = state.state_path(str(self.repo))
        data = json.loads(sp.read_text())
        del data["findings"][fp]["tracker"]
        sp.write_text(json.dumps(data))
        n_created = FakeJira._created
        out = self.push()
        self.assertEqual(out["pushed"][0]["key"], "AR-1")
        self.assertTrue(out["pushed"][0]["recovered"])
        self.assertEqual(FakeJira._created, n_created)  # no duplicate ticket
        self.assertEqual(state.show(str(self.repo), fp)["tracker"]["key"], "AR-1")

    def test_title_annotation_drives_the_summary(self):
        fp = self.make_emitted()
        state.annotate(str(self.repo), fp, title="Short LLM-written defect title")
        self.push()
        self.assertEqual(FakeJira.requests[0]["body"]["fields"]["summary"],
                         "[habeas 0001] Short LLM-written defect title")
        # titles may be improved later; tracker info may not
        state.annotate(str(self.repo), fp, title="Even better title")
        self.assertEqual(state.show(str(self.repo), fp)["record"]["title"],
                         "Even better title")

    def test_annotate_refuses_overwrite(self):
        fp = self.make_emitted()
        self.push()
        with self.assertRaisesRegex(state.StateError, "already has a tracker ticket"):
            state.annotate(str(self.repo), fp, {"key": "AR-999"})


if __name__ == "__main__":
    unittest.main()
