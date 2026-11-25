"""Fake OpenAI-compatible server for engine tests (plan 17).

`script(body, n)` decides the answer to the n-th request (1-based) and returns
a dict with `kind`:
  valid | empty | refusal | refusal_text | wrong_model | invalid_json |
  http (with `status`) | hang
plus optional `candidates`, `provider`, `cost`, `model`, and `json` (the exact
answer object). The default answer depends on the schema name in the request:
finder candidates, a yes merge verdict, an uphold critic verdict, an accepting
proof check, or a written issue.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def candidate(path="lib/a.rb", line=1, title="unscoped lookup"):
    return {"title": title, "class": "security", "invariant": "lookups are scoped",
            "violation": "find without account scope",
            "locations": [{"path": path, "start_line": line, "end_line": line}],
            "trigger": "request with another account's id", "test_sketch": "expect denial",
            "confidence": 0.7}


class FakeOpenAI:
    def __init__(self, script=None, models=None):
        self.script = script or (lambda body, n: {"kind": "valid"})
        self.models = models or []
        self.requests: list[dict] = []     # {hash, body, headers, completed}
        self.lock = threading.Lock()
        self.hung = threading.Event()
        self.release = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, payload, headers=None):
                data = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._send(200, {"data": outer.models})

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                body = json.loads(raw)
                entry = {"hash": hashlib.sha256(raw).hexdigest(), "body": body,
                         "headers": dict(self.headers), "completed": False}
                with outer.lock:
                    outer.requests.append(entry)
                    n = len(outer.requests)
                spec = outer.script(body, n)
                kind = spec.get("kind", "valid")
                if kind == "hang":
                    outer.hung.set()
                    outer.release.wait(60)
                    self.close_connection = True
                    return
                if kind == "http":
                    self._send(spec["status"], {"error": {"code": spec["status"], "message": "fake",
                                                          "metadata": spec.get("metadata", {})}})
                    return
                user = body["messages"][1]["content"]
                m = re.search(r"^=== (\S+) \(lines (\d+)-", user, re.M)
                default = [candidate(m.group(1), int(m.group(2)))] if m else []
                name = ((body.get("response_format") or {}).get("json_schema") or {}).get("name")
                answer = {
                    "merge_verdict": {"same_root_cause": True, "reason": "same fix"},
                    "critic_verdict": {"verdict": "uphold", "reason": "line 3"},
                    "proof_check": {"demonstrates_claim": True, "fix_minimal_at_root_cause": True,
                                    "verdict": "accept", "severity": "high", "reason": "ok"},
                    "issue_writer": {"title": "Written title", "severity": "high",
                                     "how_it_breaks": "It breaks."},
                }.get(name, {"candidates": spec.get("candidates", default)})
                content = json.dumps(spec.get("json", answer))
                choice = {"index": 0, "finish_reason": "stop",
                          "message": {"role": "assistant", "content": content}}
                model = spec.get("model", body["model"])
                if kind == "empty":
                    choice["message"]["content"] = json.dumps({"candidates": []})
                elif kind == "refusal":
                    choice["finish_reason"] = "content_filter"
                    choice["message"] = {"role": "assistant", "content": None,
                                         "refusal": "I can't help with that."}
                elif kind == "refusal_text":
                    choice["message"]["content"] = "I'm sorry, but I can't help with this request."
                elif kind == "wrong_model":
                    model = "other/cheaper-model"
                elif kind == "invalid_json":
                    choice["message"]["content"] = '{"candidates": [oops'
                self._send(200, {
                    "id": f"gen-{n}", "model": model, "provider": spec.get("provider", "FakeHost"),
                    "choices": [choice],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                              "cost": spec.get("cost", 0.01)}})
                entry["completed"] = True

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()

    def completed_hashes(self) -> list[str]:
        return [r["hash"] for r in self.requests if r["completed"]]
