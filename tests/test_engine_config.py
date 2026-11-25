import unittest
from pathlib import Path

import engine_support as es
from engine import config, yamlish


class TestYamlish(unittest.TestCase):
    def test_nested_maps_lists_of_maps_and_flow(self):
        doc = yamlish.loads("""
version: 1
endpoints:
  local:
    base_url: http://localhost:1234/v1   # a comment
    key_env: null
seats:
  finder:
    - id: one
      enabled: false                 # later
      provider: { only: [TODO_HOST] }
      effort_schedule: [high, low]
    - { id: two, lab: x, non_sensitive_only: true }
  empty: []
globs:
  - "engines/*/app/**/*.rb"
  - 'it''s'
cmd: "bin/engine-run {engine} rspec {paths}"
pct: 0.05
""")
        self.assertEqual(doc["endpoints"]["local"], {"base_url": "http://localhost:1234/v1", "key_env": None})
        self.assertEqual(doc["seats"]["finder"][0], {
            "id": "one", "enabled": False, "provider": {"only": ["TODO_HOST"]},
            "effort_schedule": ["high", "low"]})
        self.assertEqual(doc["seats"]["finder"][1], {"id": "two", "lab": "x", "non_sensitive_only": True})
        self.assertEqual(doc["seats"]["empty"], [])
        self.assertEqual(doc["globs"], ["engines/*/app/**/*.rb", "it's"])
        self.assertEqual(doc["cmd"], "bin/engine-run {engine} rspec {paths}")
        self.assertEqual(doc["pct"], 0.05)

    def test_plain_sentences_in_lists_stay_strings(self):
        doc = yamlish.loads("inv:\n  - Every write is idempotent, under retry.\n  - Nothing writes to a V1 table.\n")
        self.assertEqual(doc["inv"], ["Every write is idempotent, under retry.", "Nothing writes to a V1 table."])

    def test_unsupported_features_are_rejected(self):
        for text in ("a: &x 1", "a: |\n  text", "a:\n\tb: 1", "a: 1\na: 2", "a: {b: 1", "a: [1, 2"):
            with self.assertRaises(yamlish.YamlError, msg=text):
                yamlish.loads(text)

    def test_shipped_config_parses_with_only_todo_values(self):
        # the owner's own run.yaml is git-ignored; what ships is run.example.yaml
        import shutil, tempfile
        d = Path(tempfile.mkdtemp())
        for f in (es.ROOT / "config").glob("*.yaml"):
            shutil.copy(f, d / f.name)
        shutil.copy(es.ROOT / "config" / "run.example.yaml", d / "run.yaml")
        cfg = config.load(d)
        self.assertEqual(cfg.errors, [])
        # TODO values of disabled seats (the open-weight ones awaiting a host) are not reported
        self.assertTrue(set(cfg.todos) <= {"run.target.repo_path", "run.target.engine_test_command",
                                           "run.target.root_test_command",
                                           "run.target.proof_environment.outbound_http_blocked"}, cfg.todos)
        self.assertTrue(any("TODO" in str(s.get("provider")) for s in cfg.seats("finder", True)))
        self.assertGreaterEqual(len({s["lab"] for s in cfg.seats("finder")}), 3)


class TestValidation(unittest.TestCase):
    """Plan 6.1: each case config validation must reject."""

    def errors(self, mutate):
        m, r = es.models(), es.run_cfg()
        mutate(m, r)
        return es.make_config(m, r).errors

    def assertRejected(self, mutate, needle):
        errs = self.errors(mutate)
        self.assertTrue(any(needle in e for e in errs), errs)

    def test_baseline_is_valid(self):
        cfg = es.make_config()
        self.assertEqual(cfg.errors, [])
        self.assertEqual(cfg.todos, [])
        cfg.require_ready()

    def test_alias_model_ids(self):
        def latest(m, r):
            m["seats"]["finder"][0]["model"] = "a/model-latest"

        def tilde(m, r):
            m["seats"]["critic"][0]["model"] = "~a/model"

        def alternate(m, r):
            m["seats"]["finder"][0]["alternate_model"] = "a/other:latest"
        for mutate in (latest, tilde, alternate):
            self.assertRejected(mutate, "is an alias")

    def test_critics_need_two_labs(self):
        def one_lab(m, r):
            m["seats"]["critic"][1]["lab"] = "a"
        self.assertRejected(one_lab, "critic: needs seats from at least two labs")

    def test_provers_need_two_enabled_labs(self):
        def disabled(m, r):
            m["seats"]["prover"][1]["enabled"] = False
        self.assertRejected(disabled, "prover: needs enabled seats from at least two labs")

    def test_proof_checker_needs_a_lab_outside_the_provers(self):
        def same_lab(m, r):
            m["seats"]["proof_checker"][0]["lab"] = "c"
            m["seats"]["proof_checker"][1]["lab"] = "harness"
        self.assertRejected(same_lab, "proof_checker: needs a seat from a lab other")

    def test_open_weights_on_openrouter_needs_provider_only(self):
        def open_weights(m, r):
            m["seats"]["finder"][0]["open_weights"] = True
        self.assertRejected(open_weights, "needs a provider.only list")

        def pinned(m, r):
            m["seats"]["finder"][0].update(open_weights=True, provider={"only": ["hostx"]})
        self.assertEqual(self.errors(pinned), [])

    def test_todo_blocks_every_command_but_config_check(self):
        m, r = es.models(), es.run_cfg()
        r["target"]["repo_path"] = "TODO"
        m["seats"]["finder"][0]["provider"] = {"only": ["TODO_APPROVED_HOST"]}
        m["seats"]["finder"].append({"id": "off", "lab": "z", "endpoint": "fake", "model": "z/m",
                                     "enabled": False, "provider": {"only": ["TODO_APPROVED_HOST"]}})
        cfg = es.make_config(m, r)
        self.assertEqual(cfg.errors, [])
        self.assertEqual(cfg.todos, ["run.target.repo_path", "models.seats.finder.finder-a.provider.only[0]"])
        with self.assertRaisesRegex(config.ConfigError, "TODO value at run.target.repo_path"):
            cfg.require_ready()

    def test_other_rules(self):
        def drop_rule(m, r):
            r["critics"]["drop_rule"] = "majority"
        self.assertRejected(drop_rule, "only allowed value is unanimous")

        def short_schedule(m, r):
            m["seats"]["finder"][0]["effort_schedule"] = ["high"]
        self.assertRejected(short_schedule, "effort_schedule shorter")

        def bad_effort(m, r):
            m["seats"]["finder"][0]["effort_schedule"] = ["high", "extreme"]
        self.assertRejected(bad_effort, "unknown effort")

        def no_limit(m, r):
            r["budget"]["limit_usd"] = None
        self.assertRejected(no_limit, "a starting limit is required")

        def no_merger(m, r):
            m["seats"]["merge_auditor"][0]["enabled"] = False
        self.assertRejected(no_merger, "merge_auditor: needs an enabled seat")

        def unknown_lens(m, r):
            r["lenses"] = ["authz_tenancy", "nope"]
        self.assertRejected(unknown_lens, "lens nope")


if __name__ == "__main__":
    unittest.main()
