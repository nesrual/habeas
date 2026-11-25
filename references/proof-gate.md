# The proof gate (normative; plan section 9)

A provable-track finding (`bug`, `security`) is emitted only if ALL of the
following hold. Everything here is checked mechanically by `scripts/prove.sh`
+ `scripts/prove_gates.py`, except item 4, which is the proof-review role's
checklist.

1. **The proof ran.** The test framework executed: the run log contains a
   summary line (parsed per `proof.framework`: rspec or pytest; per finding for
   `proof_sketch.type: jest`), with >= 1
   example and no errors outside examples/collection.
   A timeout, crash, or load error is failure OF proof, not proof.
   (`reason: no_summary | zero_examples | errors_outside_examples`)
2. **Red for the claimed reason.** The process exits with status 1, the log records at
   least one failure, and its first failure block matches the expert’s
   `expected_failure_pattern` (case-insensitive regex).
   A red test failing for a different reason proves nothing.
   (`reason: zero_failures | pattern_mismatch`)
3. **Deterministic.** Two runs in fresh processes produce the same failure
   count and the same first failure message after normalizing object ids,
   hex addresses, timestamps, durations, and seeds. Mismatch fails the
   attempt (`reason: nondeterministic`) and records an advisory
   `flaky-behavior` note.
4. **Not self-satisfying.** The proof-review role confirms: the failure is the
   claimed mode (not setup noise), the subject is unmocked, assertions
   describe correct behavior (the spec would pass on fixed code), and the
   spec does not manufacture its own failure.
5. **Security findings additionally: fail-then-pass.** `fix_sketch` must be a
   content-only unified diff to the tracked flagged file. After applying it,
   the same proof must pass with exit status zero. The runner then restores
   the flagged file. No valid diff, failed apply, or no
   green => the attempt fails (`reason: security_requires_fail_then_pass`).
   For non-security findings fail-then-pass is attempted when a diff exists
   and recorded either way, but does not gate.
6. **Executed by prove.sh under the configured sandbox.** Never by hand,
   never by the host agent calling rspec directly. Sandbox modes (plan 10):
   `env` (requires operator-set `HABEAS_ALLOW_HOST_PROOFS=1`, scrubbed allowlist environment, throwaway DATABASE_URL,
   `unshare -n` where available — recorded as `network_isolated`),
   `container` (docker, `--network none`, read-only worktree mount, disposable writable copy —
   `network_isolated: true` by construction; refused without an image),
   `none` (refused unless configured AND `HABEAS_I_UNDERSTAND=1`; logged
   loudly).

HTTP-class proofs (`proof_sketch.type: http`) require `sandbox.mode:
container` plus `proof.boot_cmd`; their execution is not yet implemented —
they always return `{result: quarantine, reason: harness_missing}`.

An attempt that fails gates 1-5 consumes one of `proof.attempts_max`
(default 2). After the budget: quarantine, with the attempt logs attached.
Nothing is silently dropped: kill, quarantine, and emit are the only exits.
