import json
import os
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
DISPATCH = SKILL / "scripts" / "dispatch.sh"


def write_exec(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class DispatchTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        (self.repo / "config.yaml").write_text(
            "providers:\n  scout: generic\n  expert: generic\n  critic: generic\n  proof_review: generic\n")
        settings = patch.dict(os.environ, {"HABEAS_CONFIG": str(self.repo / "config.yaml")})
        settings.start()
        self.addCleanup(settings.stop)
        self.prompt = self.tmp / "prompt.md"
        self.prompt.write_text("You are a role.\nDo the thing.\n")
        self.input = self.tmp / "input.json"
        self.input.write_text('{"claim": "x"}\n')
        self.counter = self.tmp / "calls"

    def tearDown(self):
        self._tmp.cleanup()

    def dispatch(self, role, *extra, cmd=None, env_extra=None, cwd=None):
        env = dict(os.environ)
        env.pop("HABEAS_GENERIC_CMD", None)
        if cmd is not None:
            env["HABEAS_GENERIC_CMD"] = cmd
        env.update(env_extra or {})
        args = [str(DISPATCH), role, str(self.prompt), str(self.input),
                "--repo", str(self.repo)]
        if cwd is not None:
            args += ["--cwd", str(cwd)]
        args += list(extra)
        return subprocess.run(args, capture_output=True, text=True, env=env)

    def make_worktree(self):
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x",
                        "--allow-empty"], cwd=self.repo, check=True)
        wt = self.tmp / "wt"
        subprocess.run(["git", "worktree", "add", "-q", str(wt), "HEAD"], cwd=self.repo, check=True)
        return wt

    def fake_cli(self, name, body):
        return str(write_exec(self.tmp / name, body))


class TestHappyPath(DispatchTestCase):
    def test_extracts_json_and_builds_combined_prompt(self):
        fake = self.fake_cli("ok.sh", 'cat > /dev/null; echo \'{"verdict":"promote","confidence":0.8}\'\n')
        out = self.dispatch("scout", cmd=f"{fake} < {{prompt_file}}")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), {"verdict": "promote", "confidence": 0.8})
        combined = list((self.repo / ".audit" / "dispatch").glob("scout-*.prompt.md"))
        self.assertEqual(len(combined), 1)
        text = combined[0].read_text()
        self.assertIn("Do the thing.", text)
        self.assertIn("---INPUT---", text)
        self.assertIn('{"claim": "x"}', text)

    def test_failed_provider_output_is_never_a_verdict(self):
        fake = self.fake_cli("failed", """echo '{"verdict":"promote"}'; exit 2""")
        out = self.dispatch("expert", cmd=fake)
        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(out.stdout, "")

    def test_rejects_ambiguous_response(self):
        fake = self.fake_cli("ambiguous", """echo '{"verdict":"kill"} {"verdict":"promote"}'""")
        out = self.dispatch("expert", cmd=fake)
        self.assertEqual(json.loads(out.stdout).get("error"), "unparseable")

    def test_literal_model_and_quoted_filename(self):
        (self.repo / "config.yaml").write_text(
            'providers:\n  scout: generic\nmodels:\n  scout: "two words; literal"\n')
        fake = self.fake_cli("literal", """cat >/dev/null
python3 -I -c 'import json, sys; print(json.dumps(sys.argv[1:]))' "$@"
""")
        out = self.dispatch("scout", "--run-dir", str(self.tmp / "a 'quoted' & spaced dir"),
                            cmd=f"{fake} {{model_args}} < {{prompt_file}}")
        self.assertEqual(json.loads(out.stdout), ["--model", "two words; literal"], out.stderr)

    def test_unknown_role_is_rejected_before_creating_artifacts(self):
        out = self.dispatch("expert-extra")
        self.assertNotEqual(out.returncode, 0)
        self.assertFalse((self.repo / ".audit").exists())

    def test_builtin_adapters_deliver_prompt_and_restrict_tools(self):
        # Exercise each shipped template through the normal dispatch boundary.
        fake = """python3 -I -c 'import json,sys
from pathlib import Path
args=sys.argv[1:]
data = Path(args[args.index("--prompt-file")+1]).read_text() if "--prompt-file" in args else sys.stdin.read()
print(json.dumps({"args":args, "input":data}))' "$@"
"""
        for provider, flags in {
            "claude": ("--safe-mode", "--restricted", "--strict-mcp-config", "Read,Grep,Glob"),
            "grok": ("--disable-web-search", "--tools", "read,grep,glob"),
            "pi": ("--no-extensions", "--no-context-files", "--tools", "read"),
        }.items():
            with self.subTest(provider=provider):
                self.fake_cli(provider, fake)
                (self.repo / "config.yaml").write_text(f"providers:\n  scout: {provider}\n")
                out = self.dispatch("scout", env_extra={"PATH": f"{self.tmp}:{os.environ['PATH']}"})
                self.assertEqual(out.returncode, 0, out.stderr)
                result = json.loads(out.stdout)
                self.assertIn(self.input.read_text(), result["input"])
                for flag in flags:
                    self.assertIn(flag, result["args"])

    def test_codex_uses_selected_model_effort_and_explicit_daybreak(self):
        self.fake_cli("codex", """cat >/dev/null
python3 -I -c 'import json, sys; print(json.dumps(sys.argv[1:]))' "$@"
""")
        (self.repo / "config.yaml").write_text(
            "providers:\n  scout: codex\nmodels:\n  scout: gpt-6.2-sol\nreasoning:\n  scout: medium\n")
        for enabled in ("0", "1"):
            out = self.dispatch("scout", env_extra={"PATH": f"{self.tmp}:{os.environ['PATH']}",
                                                   "HABEAS_DAYBREAK": enabled})
            argv = json.loads(out.stdout)
            self.assertEqual(argv[argv.index("--model")+1], "gpt-6.2-sol")
            self.assertIn('model_reasoning_effort="medium"', argv)
            self.assertEqual(argv[argv.index("--sandbox")+1], "read-only")
            self.assertEqual("daybreak=true" in argv, enabled == "1")

    def test_prover_and_expert_roles_resolve_to_expert_provider(self):
        fake = self.fake_cli("ok.sh", 'cat > /dev/null; echo \'{"ok":1}\'\n')
        for role in ("prover", "expert-db"):
            out = self.dispatch(role, cmd=f"{fake} < {{prompt_file}}")
            self.assertEqual(json.loads(out.stdout), {"ok": 1}, out.stderr)


class TestRetries(DispatchTestCase):
    def test_garbage_output_retries_once_then_reports_unparseable(self):
        fake = self.fake_cli("bad.sh", f'cat > /dev/null; echo x >> "{self.counter}"; echo "no json here"\n')
        out = self.dispatch("scout", cmd=f"{fake} < {{prompt_file}}")
        self.assertEqual(out.returncode, 0)
        result = json.loads(out.stdout)
        self.assertEqual(result["error"], "unparseable")
        self.assertTrue(Path(result["raw_path"]).exists())
        self.assertEqual(len(self.counter.read_text().splitlines()), 2)  # exactly one retry
        combined = next((self.repo / ".audit" / "dispatch").glob("scout-*.prompt.md"))
        self.assertIn("Output only the JSON object", combined.read_text())

    def test_retry_can_succeed(self):
        fake = self.fake_cli("flaky.sh",
                             f'cat > /dev/null\nif [ -f "{self.counter}" ]; then echo \'{{"ok":true}}\'; '
                             f'else touch "{self.counter}"; echo garbage; fi\n')
        out = self.dispatch("scout", cmd=f"{fake} < {{prompt_file}}")
        self.assertEqual(json.loads(out.stdout), {"ok": True})

    def test_timeout_is_enforced(self):
        fake = self.fake_cli("slow.sh", 'cat > /dev/null; sleep 30; echo \'{"late":true}\'\n')
        out = self.dispatch("scout", cmd=f"{fake} < {{prompt_file}}",
                            env_extra={"HABEAS_DISPATCH_TIMEOUT": "1"})
        self.assertEqual(json.loads(out.stdout).get("error"), "unparseable")
        self.assertIn("timeout", out.stderr)


class TestRoleModels(DispatchTestCase):
    """models.<role> / effort.<role> reach the adapter as {model_args} / {effort_args}."""

    def echo_args(self):
        fake = self.fake_cli("args.sh", 'cat > /dev/null; printf \'{"argv":"%s"}\\n\' "$*"\n')
        return f"{fake} {{model_args}} {{effort_args}} < {{prompt_file}}"

    def argv(self, role, **kw):
        out = self.dispatch(role, cmd=self.echo_args(), **kw)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)["argv"]

    def test_default_config_pins_each_role(self):
        self.assertEqual(self.argv("scout"), "--model claude-sonnet-5-5 --effort medium")
        self.assertEqual(self.argv("expert"), "--model claude-opus-5-5 --effort high")
        self.assertEqual(self.argv("prover"), "--model claude-opus-5-5 --effort high")
        wt = self.make_worktree()
        self.assertEqual(self.argv("critic", cwd=wt), "--model claude-fable-5-2 --effort high")
        self.assertEqual(self.argv("proof_review", cwd=wt),
                         "--model claude-sonnet-5-5 --effort medium")

    def test_expert_routes_share_models_expert(self):
        (self.repo / "config.yaml").write_text(
            "providers:\n  scout: generic\n  expert: generic\n"
            "models:\n  expert: m-expert\n  prover: m-prover\neffort:\n  expert: low\n")
        self.assertEqual(self.argv("expert-security"), "--model m-expert --effort low")
        self.assertEqual(self.argv("prover").split()[:2], ["--model", "m-prover"])

    def test_null_model_and_effort_pass_no_flags(self):
        (self.repo / "config.yaml").write_text(
            "providers:\n  scout: generic\nmodels:\n  scout: null\neffort:\n  scout: null\n")
        self.assertEqual(self.argv("scout"), "")

    def test_claude_template_carries_the_placeholders(self):
        conf = (SKILL / "adapters.conf").read_text()
        line = next(l for l in conf.splitlines() if l.startswith("claude="))
        self.assertIn("{model_args}", line)
        self.assertIn("{effort_args}", line)


class TestProviderResolution(DispatchTestCase):
    def test_unknown_provider_falls_back_to_scout_provider(self):
        (self.repo / "config.yaml").write_text(
            "providers:\n  scout: generic\n  critic: ghostcli\nmodels:\n  critic: null\n")
        fake = self.fake_cli("ok.sh", 'cat > /dev/null; echo \'{"ok":2}\'\n')
        wt = self.make_worktree()
        out = self.dispatch("critic", cmd=f"{fake} < {{prompt_file}}", cwd=wt)
        self.assertEqual(json.loads(out.stdout), {"ok": 2}, out.stderr)
        self.assertIn("falling back to 'generic'", out.stderr)

    def test_pinned_model_refuses_provider_fallback(self):
        (self.repo / "config.yaml").write_text(
            "providers:\n  scout: generic\n  critic: ghostcli\nmodels:\n  critic: claude-fable-5-2\n")
        fake = self.fake_cli("ok.sh", 'cat > /dev/null; echo \'{"ok":2}\'\n')
        wt = self.make_worktree()
        out = self.dispatch("critic", cmd=f"{fake} < {{prompt_file}}", cwd=wt)
        self.assertEqual(out.returncode, 1)
        self.assertIn("refusing to fall back", out.stderr)
        self.assertEqual(out.stdout, "")

    def test_no_provider_at_all_errors(self):
        (self.repo / "config.yaml").write_text("providers:\n  scout: ghostcli\n")
        env = {k: v for k, v in os.environ.items()}
        env["PATH"] = str(self.tmp / "emptybin")
        (self.tmp / "emptybin").mkdir()
        for tool in ("bash", "python3", "dirname", "cat", "date", "mkdir", "command"):
            src = subprocess.run(["/usr/bin/which", tool], capture_output=True, text=True).stdout.strip()
            if src:
                os.symlink(src, self.tmp / "emptybin" / tool)
        out = subprocess.run([str(DISPATCH), "scout", str(self.prompt), str(self.input),
                              "--repo", str(self.repo)], capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 1)
        self.assertIn("no usable provider", out.stderr)

class TestIsolation(DispatchTestCase):
    def make_repo_with_emissions(self):
        (self.repo / "issues" / "0001-x").mkdir(parents=True)
        (self.repo / "issues" / "0001-x" / "ISSUE.md").write_text("# x\n")
        (self.repo / "quarantine.md").write_text("# q\n")
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-qm", "base"], cwd=self.repo, check=True)
        wt = self.tmp / "wt"
        subprocess.run(["git", "worktree", "add", "-q", str(wt), "HEAD"], cwd=self.repo, check=True)
        return wt

    def test_critic_refused_outside_worktree(self):
        self.make_repo_with_emissions()
        fake = self.fake_cli("ok.sh", 'cat > /dev/null; echo \'{"verdict":"kill"}\'\n')
        for role in ("critic", "proof_review"):
            out = self.dispatch(role, cmd=f"{fake} < {{prompt_file}}", cwd=self.repo)
            self.assertEqual(out.returncode, 4, out.stderr)
            self.assertIn("linked git worktree", out.stderr)
        self.assertTrue((self.repo / "quarantine.md").exists())  # main repo untouched

    def test_critic_worktree_gets_emissions_scrubbed(self):
        wt = self.make_repo_with_emissions()
        self.assertTrue((wt / "issues").exists() and (wt / "quarantine.md").exists())
        fake = self.fake_cli("ok.sh", 'cat > /dev/null; echo \'{"verdict":"kill"}\'\n')
        out = self.dispatch("critic", cmd=f"{fake} < {{prompt_file}}", cwd=wt)
        self.assertEqual(json.loads(out.stdout), {"verdict": "kill"}, out.stderr)
        self.assertFalse((wt / "issues").exists())
        self.assertFalse((wt / "quarantine.md").exists())
        self.assertTrue((self.repo / "issues").exists())
        self.assertTrue((self.repo / "quarantine.md").exists())


if __name__ == "__main__":
    unittest.main()
