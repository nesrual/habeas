# Negative examples: things NOT to flag

Grown list, one section per category; `## all` applies to every category.
`scripts/assemble_prompt.py` inlines the matching sections into prompts at
dispatch time (the `{{NEGATIVE_EXAMPLES}}` marker), so additions here reach
every role without editing prompt files.

Growth protocol: when the critic kills a finding for a reason that will
recur, or quarantine review shows a systematic false positive, add a bullet
to the matching section. Keep bullets concrete: name the pattern AND why it
is not an issue. Never add a bullet so broad it would suppress a real defect
class.

## all

- Anything whose only fix is adding a comment, documentation, or a TODO.
  If behavior is correct, there is no finding.
- Style and formatting preferences: naming, quote style, `frozen_string_literal`
  and other magic-comment nits, method ordering, "this could be a one-liner".
- Behavior that is documented as intentional at the call site AND pinned by a
  covering spec. Intent plus a test is a contract, not a bug.
- A defect inside dead code: private, zero callers anywhere in the repo, not
  exported, not reachable via metaprogramming. Unreachable wrongness has no
  failure scenario. (Deleting dead code may be an `improvement` finding; the
  "bug" inside it is not a finding.)
- Hypotheticals that require a caller that does not exist in the repo
  ("if someone ever passed X..."). Findings need reachable failure scenarios.

## bug

- Strict-null lookalikes (Ruby `nil`, TS `undefined`): a value that looks
  nullable but is guaranteed present by a validation, guard clause, database
  NOT NULL constraint, or because every call site in the repo constructs it.
  Check callers before claiming nil-dereference.
- "Possible race condition" in code that is only ever called single-threaded
  in this codebase (no threads, no concurrent job runners touching it).
- Float equality or precision complaints where the values never leave
  presentation code (display rounding is not a correctness bug; money math is).
- Boundary/off-by-one suspicions that a covering spec already pins to the
  claimed-wrong behavior being correct and intended.

## security

- String interpolation into LOG statements (`logger.info "user #{id} did X"`).
  A log message is not an injection sink here; flag interpolation only where
  the string is executed or parsed (SQL, shell, eval, HTML without escaping).
- Timing-unsafe comparison (`==`) of NON-secret values: public account ids,
  API key ids, slugs, UUIDs used as identifiers. Timing side channels matter
  only for secrets (tokens, password digests, HMACs).
- Theoretical DoS via unbounded input that is already capped upstream:
  middleware body limits, pagination caps, rack/proxy request limits. Verify
  the cap is absent before flagging.
- Documented intentional auth bypasses whose callers authenticate upstream,
  when the intent is stated at the definition AND a spec pins the contract.
- "Missing rate limiting" and other infrastructure-level controls that live
  outside the scanned code's responsibility.

## improvement

- `frozen_string_literal` and any other magic-comment or lint-style nit.
- Refactors whose only justification is taste: extract-method on short code,
  "this should be a service object", pattern preferences.
- Performance claims with no measured or structural evidence (an N+1 in a
  loop over a bounded constant-size collection is not a finding).
- Suggestions to add abstractions for flexibility no current caller needs.

## test-gap

- "Missing test" for trivial delegation, attr accessors, or methods whose
  body is a single call with no branching.
- Test gaps in dead or deprecated code slated for removal.
- "No test for the private method" — private methods are tested through
  their public callers.
- Demands for exhaustive input-matrix coverage where representative boundary
  cases already exist.
