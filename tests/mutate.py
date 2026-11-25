"""Run small, named skill mutations against their regression tests.

Each variant gets one change in a temporary copy. Only an assertion failure
in its named test counts as a kill. A broken import, skipped test, timeout,
or stale mutation anchor makes this run fail.
"""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]

# File, unique original text, replacement, and observing test.
MUTANTS = [
    ("prompts/prover.md", "## Untrusted input", "## Input",
     "test_prompts.TestPromptContracts.test_untrusted_input_clause_everywhere"),
    ("prompts/critic.md", "KILL this finding", "assess this finding",
     "test_prompts.TestPromptContracts.test_critic_kill_mandate"),
    ("scripts/state.py", "if k in allowed}", "}",
     "test_state.TestAddCandidates.test_scout_cannot_supply_later_stage_fields"),
    ("scripts/dispatch.sh", 'json_extract.py" --strict', 'json_extract.py"',
     "test_dispatch.TestHappyPath.test_rejects_ambiguous_response"),
    ("scripts/prove_gates.py", "pattern and not re.search", "False and not re.search",
     "test_prove.TestParseAndGates.test_pattern_mismatch"),
    ("scripts/emit.py", 'review.get("verdict") != "valid"', "False",
     "test_emit.TestProvableEmission.test_refuses_without_valid_proof_review"),
    ("scripts/emit.py", 'for n in ("run1.log", "run2.log")', 'for n in ("run1.log",)',
     "test_emit.TestProvableEmission.test_both_red_run_logs_are_required"),
    ("scripts/emit.py", 'if record.get("category") == "security" and (', 'if False and (',
     "test_emit.TestProvableEmission.test_security_emission_rechecks_fix_run"),
    ("scripts/state.py", 'entry["record"]["track"] = "advisory"', "pass",
     "test_state.TestTransitions.test_critic_advisory_demotes_and_clamps"),
]


def check(root, test):
    # Use the runner's result, so a crash cannot masquerade as a killed mutant.
    program = """
import sys, unittest
suite = unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])
r = unittest.TextTestRunner(verbosity=0).run(suite)
if r.errors or r.skipped or r.testsRun != len(sys.argv) - 1:
    sys.exit(2)
sys.exit(1 if r.failures else 0)
"""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HABEAS_", "PYTHON"))}
    return subprocess.run([sys.executable, "-B", "-c", program, *test],
                          cwd=root / "tests", capture_output=True, text=True,
                          env=env, timeout=90)


def main():
    with tempfile.TemporaryDirectory(prefix="habeas-mutation-") as tmp:
        root = Path(tmp)
        for directory in ("tests", "scripts", "prompts", "fixtures", "references"):
            shutil.copytree(ROOT / directory, root / directory,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for name in ("adapters.conf", "config.example.yaml"):
            shutil.copy2(ROOT / name, root / name)
        baseline = check(root, [test for _, _, _, test in MUTANTS])
        if baseline.returncode:
            sys.exit("Baseline failed\n" + baseline.stderr)
        killed = 0
        for name, original, replacement, test in MUTANTS:
            path = root / name
            contents = path.read_text()
            if contents.count(original) != 1:
                sys.exit(f"Stale mutation anchor in {name}: {original}")
            path.write_text(contents.replace(original, replacement))
            result = check(root, [test])
            path.write_text(contents)
            if result.returncode == 1:
                killed += 1
                print(f"KILLED    {test}")
            elif result.returncode == 0:
                print(f"SURVIVED  {test}")
            else:
                sys.exit(f"Invalid mutation run for {test}\n{result.stderr}")
        print(f"{killed}/{len(MUTANTS)} mutants killed")
        return int(killed != len(MUTANTS))


if __name__ == "__main__":
    sys.exit(main())
