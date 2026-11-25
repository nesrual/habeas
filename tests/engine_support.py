"""Shared fixtures for engine tests: a config, a tiny repo and a run."""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from engine import config, ledger, units  # noqa: E402

os.environ.setdefault("HABEAS_BACKOFF_BASE", "0.001")
os.environ["FAKE_KEY"] = "sk-test-secret-value"

LENSES = {
    "authz_tenancy": {"class": "security", "invariants": ["Every record lookup is scoped."],
                      "proof_shape": "x"},
    "money_ledger": {"class": "correctness", "invariants": ["Every ledger write is idempotent."],
                     "proof_shape": "x"},
    "concurrency": {"class": "correctness", "invariants": ["Two concurrent requests leave a valid state."],
                    "proof_shape": "x"},
}


def models(base_url="http://127.0.0.1:9/v1"):
    return {
        "version": 1,
        "endpoints": {"fake": {"adapter": "openai_compat", "base_url": base_url,
                               "key_env": "FAKE_KEY", "max_concurrency": 1, "openrouter": True}},
        "provider_defaults": {"allow_fallbacks": False, "require_parameters": True,
                              "data_collection": "deny"},
        "seats": {
            "finder": [
                {"id": "finder-a", "lab": "a", "endpoint": "fake", "model": "a/model-1",
                 "effort_schedule": ["high", "low"]},
                {"id": "finder-b", "lab": "b", "endpoint": "fake", "model": "b/model-1",
                 "effort_schedule": ["high", "low"]},
            ],
            "merger": [{"id": "merger", "lab": "a", "endpoint": "fake", "model": "a/merge-model"}],
            "merge_auditor": [{"id": "auditor", "lab": "b", "endpoint": "fake", "model": "b/audit-model"}],
            "critic": [{"id": "critic-a", "lab": "a", "endpoint": "fake", "model": "a/model-2"},
                       {"id": "critic-b", "lab": "b", "endpoint": "fake", "model": "b/model-2"}],
            "prover": [{"id": "prover-native", "lab": "harness", "endpoint": "native"},
                       {"id": "prover-c", "lab": "c", "endpoint": "fake", "model": "c/model-1",
                        "mode": "scripted"}],
            "proof_checker": [{"id": "checker", "lab": "b", "endpoint": "fake", "model": "b/check-model"},
                              {"id": "checker-alt", "lab": "a", "endpoint": "fake", "model": "a/check-model"}],
            "writer": [{"id": "writer", "lab": "a", "endpoint": "fake", "model": "a/write-model"}],
        },
    }


def run_cfg(repo="/nonexistent"):
    return {
        "target": {"repo_path": str(repo), "commit": "HEAD"},
        "lenses": ["authz_tenancy", "money_ledger"],
        "runs_per_finder": 2,
        "prompt_variant": "invariant",
        "critics": {"drop_rule": "unanimous"},
        "quarantine": {"lenses": ["authz_tenancy"]},
        "budget": {"limit_usd": 100, "limit_tokens": None, "warn_at_pct": 80},
    }


def make_config(m=None, r=None, lenses=None):
    return config.Config(copy.deepcopy(m or models()), copy.deepcopy(r or run_cfg()),
                         copy.deepcopy(lenses or LENSES))


class Workspace:
    """A temp dir holding a repo with n one-file units, a config dir and a run."""

    def __init__(self, base_url, n_units=3, limit_usd=100, mutate=None):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        (self.repo / "lib").mkdir(parents=True)
        self.config_dir = self.root / "config"
        self.config_dir.mkdir()
        m, r = models(base_url), run_cfg(self.repo)
        r["budget"]["limit_usd"] = limit_usd
        if mutate:
            mutate(m, r)
        for name, doc in (("models.yaml", m), ("run.yaml", r), ("lenses.yaml", LENSES)):
            (self.config_dir / name).write_text(json.dumps(doc))
        self.cfg = config.load(self.config_dir)
        assert not self.cfg.errors and not self.cfg.todos, (self.cfg.errors, self.cfg.todos)
        self.run_dir = self.root / "runs" / "r-test"
        self.conn = ledger.create_run(self.run_dir, self.cfg)
        with self.conn:
            for i in range(n_units):
                rel = f"lib/unit{i}.rb"
                (self.repo / rel).write_text(f"class Unit{i}\n  def find(id)\n    Rec.find(id)\n  end\nend\n")
                paths = json.dumps([rel])
                self.conn.execute(
                    "INSERT INTO units (unit_id, kind, paths_json, sha256, tokens, package, order_rank)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (f"u{i}", "file", paths, units.subject_hash(units.read(self.repo, paths)[1], []), 20, "lib", i))

    def cleanup(self):
        self.conn.close()
        self._tmp.cleanup()

    def cli_args(self, *args):
        return [*args, "--config-dir", str(self.config_dir), "--run-dir", str(self.run_dir)]


SAMPLE_APP = ROOT / "tests" / "fixtures" / "sample_app"
UNITS = {
    "max_tokens": 6000, "pack_small_files": True, "pack_target_tokens": 4000, "context_tokens": 2000,
    "include": ["engines/*/app/**/*.rb", "lib/**/*.rb"],
    "exclude": ["spec/**", "engines/*/spec/**"],
    "package_from_path": "engines/{package}/|gems/{package}/",
    "sensitive_globs": ["**/*"],
}


def git(repo, *args):
    import subprocess
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           *args], check=True, capture_output=True, text=True).stdout


class SampleRun:
    """The sample app copied into a temp git repo, with a config that points at it."""

    def __init__(self, base_url="http://127.0.0.1:9/v1", mutate=None, prepare=None):
        import shutil
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        shutil.copytree(SAMPLE_APP, self.repo)
        if prepare:
            prepare(self.repo)
        git(self.repo, "init", "-q")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "sample")
        self.config_dir = self.root / "config"
        self.config_dir.mkdir()
        m, r = models(base_url), run_cfg(self.repo)
        r.update(units=copy.deepcopy(UNITS), scanners=[],
                 baseline={"brakeman_ignore": "config/brakeman.ignore", "scanner_reports": [], "known_issues": []})
        r["target"].update(language="ruby", engine_test_command="bin/engine-run {engine} rspec {paths}",
                           root_test_command="rspec {paths}",
                           proof_environment={"kind": "local_checkout", "run_tests_in": "host",
                                              "outbound_http_blocked": "no network use in the sample app"})
        r["provers"] = {"rule": "any_valid", "max_attempts_per_prover": 2, "timeout_minutes": 2,
                        "max_fix_lines": 40, "max_fix_files": 3, "repeat": 1,
                        "concurrency_repeat": 4, "concurrency_min_red": 2}
        if mutate:
            mutate(m, r)
        for name, doc in (("models.yaml", m), ("run.yaml", r), ("lenses.yaml", LENSES)):
            (self.config_dir / name).write_text(json.dumps(doc))
        for name in ("static_rules.yaml", "severity.yaml"):
            if (ROOT / "config" / name).exists():
                shutil.copy(ROOT / "config" / name, self.config_dir / name)
        self.runs = self.root / "runs"
        self.run_dir = None
        self.conn = None

    def cli(self, *args, env=None):
        import contextlib
        import io
        from engine import cli
        out, err = io.StringIO(), io.StringIO()
        old = dict(os.environ)
        os.environ["HABEAS_RUNS_DIR"] = str(self.runs)
        os.environ.update(env or {})
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main([*args, "--config-dir", str(self.config_dir)])
        finally:
            os.environ.clear()
            os.environ.update(old)
        if self.conn is None and (self.runs / "current").exists():
            self.run_dir = self.runs / (self.runs / "current").read_text().strip()
            self.conn = ledger.connect(self.run_dir)
        return code, out.getvalue(), err.getvalue()

    def rows(self, q, *args):
        return [dict(r) for r in self.conn.execute(q, args)]

    def cfg(self):
        return config.load(self.config_dir)

    def cleanup(self):
        if self.conn:
            self.conn.close()
        self._tmp.cleanup()


CONTROLLER = "engines/billing/app/controllers/billing/invoices_controller.rb"


def schema_name(body):
    return body["response_format"]["json_schema"]["name"]


def pipeline(test, script=None, last="critique", mutate=None, cands=None, static=False, prepare=None):
    """Sample app limited to the invoices controller, run through `last`.
    By default finder-a reports one candidate on the controller's `show`."""
    from fake_openai import FakeOpenAI, candidate
    cands = cands if cands is not None else [candidate(CONTROLLER, 3)]

    def default(body, n):
        if schema_name(body) == "finder_candidates":
            i = 0 if body["model"].startswith("a/") else 1
            return {"kind": "valid", "candidates": [cands[i]] if i < len(cands) and cands[i] else []}
        return script(body, n) if script else {"kind": "valid"}

    def only_controller(m, r):
        r["units"]["include"] = [CONTROLLER]
        r["units"]["pack_small_files"] = False
        r["runs_per_finder"] = 1
        if mutate:
            mutate(m, r)
    server = FakeOpenAI(default)
    server.__enter__()
    test.addCleanup(server.__exit__)
    s = SampleRun(base_url=server.base_url, mutate=only_controller, prepare=prepare)
    test.addCleanup(s.cleanup)
    if not static:
        (s.config_dir / "static_rules.yaml").unlink()
    s.server, s.codes = server, {}
    for cmd in ("preflight", "scout", "find", "merge", "critique"):
        code, out, err = s.cli(cmd)
        s.codes[cmd] = (code, out + err)
        assert code == 0 or cmd == last, (cmd, out, err)
        if cmd == last:
            break
    return s


GOOD_TEST = """require_relative "../spec_helper"

RSpec.describe Billing::InvoicesController do
  it "denies show to an account that does not own the invoice" do
    Billing::Account.create(id: 1, name: "A")
    other = Billing::Account.create(id: 2, name: "B")
    Billing::Invoice.create(id: 10, account_id: 1, amount_cents: 500, status: :open)
    controller = described_class.new(current_account: other, params: { id: 10 })
    expect { controller.show }.to raise_error(Sample::NotAuthorized)
  end
end
"""


def make_patch(repo, path, old, new):
    """A unified diff that replaces `old` with `new` in one file of the repo."""
    target = Path(repo) / path
    original = target.read_text()
    assert old in original
    target.write_text(original.replace(old, new))
    patch = git(repo, "diff")
    target.write_text(original)
    return patch


def good_patch(repo):
    return make_patch(repo, CONTROLLER, "invoice = Invoice.find(params[:id])\n      { id: invoice.id, amount",
                      "invoice = authorize(Invoice.find(params[:id]), :show?)\n      { id: invoice.id, amount")
