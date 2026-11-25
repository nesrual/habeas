import os
from unittest.mock import patch
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import state  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parent.parent


class TestRestrictedYaml(unittest.TestCase):
    def test_parses_the_shipped_example_config(self):
        cfg = state.parse_restricted_yaml((SKILL_DIR / "config.example.yaml").read_text())
        self.assertEqual(cfg["orchestration"], "inline")
        self.assertEqual(cfg["providers"]["scout"], "claude")
        self.assertEqual(cfg["providers"]["critic"], "claude")
        self.assertEqual(cfg["models"], {
            "scout": "claude-sonnet-5-5", "expert": "claude-opus-5-5",
            "prover": "claude-opus-5-5", "critic": "claude-fable-5-2",
            "proof_review": "claude-sonnet-5-5"})
        self.assertNotEqual(cfg["models"]["critic"], cfg["models"]["expert"])
        self.assertEqual(cfg["effort"]["expert"], "high")
        self.assertEqual(cfg["proof"]["test_cmd"], "bundle exec rspec {file}")
        self.assertIsNone(cfg["proof"]["boot_cmd"])
        self.assertEqual(cfg["proof"]["timeout_s"], 120)
        self.assertEqual(cfg["sandbox"]["env_allowlist"],
                         ["PATH", "HOME", "LANG", "GEM_HOME", "GEM_PATH", "BUNDLE_PATH"])
        self.assertIn("vendor/", cfg["scope"]["exclude"])
        self.assertEqual(cfg["gates"]["max_candidates_per_unit"], 8)
        self.assertEqual(cfg["scan"]["unit_max_lines"], 500)

    def test_scalars(self):
        cfg = state.parse_restricted_yaml(
            'a: 1\nb: 2.5\nc: true\nd: false\ne: null\nf: ~\ng: "quoted # kept"\nh: plain\n')
        self.assertEqual(cfg, {"a": 1, "b": 2.5, "c": True, "d": False,
                               "e": None, "f": None, "g": "quoted # kept", "h": "plain"})

    def test_comments_stripped_outside_quotes_only(self):
        cfg = state.parse_restricted_yaml('cmd: "rspec {file}"  # the test command\n')
        self.assertEqual(cfg["cmd"], "rspec {file}")

    def test_block_list(self):
        cfg = state.parse_restricted_yaml("xs:\n  - one\n  - 2\n  - null\n")
        self.assertEqual(cfg["xs"], ["one", 2, None])

    def test_inline_list_with_quotes(self):
        cfg = state.parse_restricted_yaml('xs: ["a, b", c, 3]\n')
        self.assertEqual(cfg["xs"], ["a, b", "c", 3])

    def test_empty_inline_list(self):
        self.assertEqual(state.parse_restricted_yaml("xs: []\n")["xs"], [])

    def test_rejects_deeper_nesting(self):
        with self.assertRaises(state.StateError):
            state.parse_restricted_yaml("a:\n  b:\n    c: 1\n")

    def test_rejects_tab_indent(self):
        with self.assertRaises(state.StateError):
            state.parse_restricted_yaml("a:\n\tb: 1\n")

    def test_rejects_orphan_indent(self):
        with self.assertRaises(state.StateError):
            state.parse_restricted_yaml("  a: 1\n")


class TestConfigLoading(unittest.TestCase):
    def test_deep_merge(self):
        base = {"a": {"x": 1, "y": 2}, "b": 1}
        override = {"a": {"y": 3}, "c": 4}
        self.assertEqual(state.deep_merge(base, override), {"a": {"x": 1, "y": 3}, "b": 1, "c": 4})

    def test_target_config_overrides_defaults(self):
        import tempfile
        with tempfile.TemporaryDirectory() as repo:
            (Path(repo) / "config.yaml").write_text("providers:\n  critic: pi\n")
            with patch.dict(os.environ, {"HABEAS_CONFIG": str(Path(repo) / "config.yaml")}):
                cfg = state.load_config(repo, skill_dir=str(SKILL_DIR))
            self.assertEqual(cfg["providers"]["critic"], "pi")     # overridden
            self.assertEqual(cfg["providers"]["scout"], "claude")  # from defaults
            self.assertEqual(cfg["proof"]["timeout_s"], 120)       # from defaults

    def test_repository_cannot_authorize_execution_or_a_tracker(self):
        import tempfile
        with tempfile.TemporaryDirectory() as repo, patch.dict(os.environ, {"HABEAS_CONFIG": ""}):
            for section in ("tracker", "sandbox", "proof", "providers", "models"):
                (Path(repo) / "config.yaml").write_text(f"{section}: {{}}\n")
                with self.assertRaisesRegex(state.StateError, "HABEAS_CONFIG"):
                    state.load_config(repo)

    def test_config_get_dotted(self):
        import tempfile
        with tempfile.TemporaryDirectory() as repo:
            self.assertEqual(state.config_get(repo, "proof.attempts_max", skill_dir=str(SKILL_DIR)), 2)
            with self.assertRaises(state.StateError):
                state.config_get(repo, "proof.nope", skill_dir=str(SKILL_DIR))


if __name__ == "__main__":
    unittest.main()
