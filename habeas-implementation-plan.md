# habeas: Implementation Plan

Provider-agnostic Agent Skill that incrementally audits a git-checked-out codebase,
routes candidate findings through domain experts and a cold adversarial critic, and
emits only findings that survive a mechanical proof gate (failing test or scripted
repro). Advisory findings (improvements, refactors) are emitted separately, clearly
labeled, and severity-capped. Output is markdown issue folders, one per finding.

This document is the build spec. It is written for an implementing coding agent
(Claude Code, Codex, pi, or similar). Decisions marked **DECIDED** are settled and
must not be reopened. Items marked **IMPLEMENTER** are yours to resolve, with
constraints given.

---

## 1. Goals and non-goals

Goals:

- Scan small units of a codebase per run (module or diff cluster), never the whole
  repo in one pass. Small, single-concern findings only.
- Two tracks. **Provable** (categories: `bug`, `security`): a finding is emitted only
  with a mechanical proof attached (RSpec test red on HEAD for the claimed reason, or
  a scripted HTTP repro). **Advisory** (categories: `improvement`, `test-gap`):
  critic-gated only, severity permanently capped at `medium`, labeled as opinion.
- False-positive suppression is the primary quality target. Cold critic with a kill
  mandate, context asymmetry, proof gate, proof-review pass, quarantine for
  everything that fails proof.
- Provider-agnostic: the skill folder follows the Agent Skills standard (SKILL.md
  with `name` + `description` frontmatter) and runs under Claude Code, Codex CLI,
  pi, Grok CLI, and any host that can execute bash. **DECIDED**: these four hosts
  are the supported set; the skill must be callable from any of them. Model roles
  are invoked as fresh headless CLI sessions through a single dispatch script, so
  critic can run on a different model family than scout/expert.
- Everything auditable: state file, per-run metrics, full lifecycle history per
  finding.

Non-goals (all versions):

- No UI/UX analysis. **DECIDED**, dropped.
- No direct writes to external issue trackers before v3, and then only behind a flag.
- No MCP servers. pi rejects them by design; portability layer is skills + bash.
- No fixing of found issues. This skill finds and proves; remediation is out of scope.

Non-goals (v1 specifically): hotspot prioritization, cross-model critic, HTTP-class
proofs, container sandboxing, mutation testing, scheduled runs.

---

## 2. Portability requirements

- Skill format: directory with `SKILL.md` (YAML frontmatter: `name`, `description`;
  markdown body). Optional `scripts/`, `references/`, `prompts/` subdirectories.
  Keep SKILL.md under 500 lines; move detail to `references/` and point to it.
- Install locations the README must document: `~/.claude/skills/`,
  `~/.codex/skills/`, `~/.pi/agent/skills/`, `~/.grok/skills/`, and project-level
  `.claude/skills/`, `.pi/skills/`. Note that Claude Code discovers SKILL.md only
  one directory level deep, so the skill folder must sit directly under the skills
  directory. Grok CLI additionally scans `~/.claude/skills/` and
  `<cwd>/.claude/skills/` by default (`[compat.claude] skills = true` in
  `~/.grok/config.toml`), so a Claude-path install covers Grok too; document both.
- pi replaces a `{baseDir}` placeholder in SKILL.md with the skill's directory at
  runtime. Use `{baseDir}` when referencing bundled scripts from SKILL.md prose, and
  additionally have scripts self-locate (`$(dirname "$0")`) so paths work on hosts
  that do not substitute the placeholder.
- Runtime constraints for all bundled code: bash (POSIX-leaning, bash-isms allowed),
  Python >= 3.10 **stdlib only** (no pip installs at runtime; PyYAML is NOT stdlib,
  see config note in section 5), `git`, and optionally `docker` for container
  sandbox mode. No network access required by the scripts themselves.
- Model CLI flags drift. All provider invocations live in one adapters table
  (section 7.2), treated as data. **IMPLEMENTER**: verify current headless flags for
  each CLI you have installed (`claude --help`, `codex --help`, `pi --help`,
  `grok --help`) at build time and record working invocations in `adapters.conf`.
  Do not hardcode flags anywhere else.

---

## 3. Repository layout of the skill

```
habeas/
  SKILL.md
  README.md                     # install per host, quickstart, config reference
  config.example.yaml
  adapters.conf                 # provider -> headless invocation template
  scripts/
    run.sh                      # scripted-mode orchestrator (whole pipeline)
    dispatch.sh                 # run one role headlessly, fresh session
    json_extract.py             # pull first valid JSON value out of model output
    state.py                    # state.json CRUD + finding lifecycle transitions
    fingerprint.py              # dedupe fingerprints
    contextpack.py              # assemble expert context packs
    prove.sh                    # proof execution + mechanical gate checks
    emit.py                     # write issue folder / quarantine entry from record
    hotspots.py                 # v2: churn x complexity scan queue
    selftest.sh                 # run pipeline against fixture repo, assert results
  prompts/
    scout.md
    expert-backend.md
    expert-db.md
    expert-security.md
    expert-tests.md
    critic.md
    proof-review.md
  references/
    proof-gate.md               # the gate rules, normative text (section 9)
    context-packs.md            # pack recipes per category (section 8.3)
    negative-examples.md        # grown list of things NOT to flag, per category
  fixtures/
    audit-target/               # tiny plain-Ruby + RSpec repo with planted defects
```

Run artifacts live in the **target repo**, not the skill folder, under `.audit/`
(gitignored; emit.py appends to the target's `.gitignore` if missing):

```
<target-repo>/.audit/
  state.json
  metrics.jsonl
  runs/<run-id>/               # candidates.json, expert outputs, critic outputs, logs
<target-repo>/issues/          # emitted findings (committed, reviewable)
<target-repo>/quarantine.md    # critic survivors that failed proof
```

---

## 4. Orchestration model **DECIDED**

Two modes, configured as `orchestration: inline | scripted`.

- **inline** (default, interactive use): the host agent reads SKILL.md and performs
  scout and expert passes itself, using its own repo tools, following the prompt
  files verbatim as its instructions and writing stage outputs through `state.py`.
  Critic, proof-review, and prover always go through `dispatch.sh` / `prove.sh`
  regardless of mode; critic isolation is non-negotiable.
- **scripted** (unattended/cron, v3): `run.sh` drives every role through
  `dispatch.sh`. No interactive host needed.

Rationale: scout/expert need no isolation, and the interactive host has better repo
navigation than a cold headless call, so inline is cheaper and better there. Critic
must be a fresh session (anchoring) and benefits from a different model family, so
it always dispatches. Build `run.sh` so inline mode can call it with
`--from critic` to run only the back half of the pipeline; that gives one code path
for the isolated stages.

---

## 5. Configuration

`config.yaml` at the target repo root (fall back to `.audit/config.yaml`, then
skill's `config.example.yaml` defaults).

```yaml
orchestration: inline            # inline | scripted
providers:
  scout:  claude                 # key into adapters.conf
  expert: claude
  critic: codex                  # cross-family on purpose; fallback: scout provider
  proof_review: codex            # default: same as critic
proof:
  test_cmd: "bundle exec rspec {file}"
  spec_dir: "spec/audit"         # where proof specs are placed inside the worktree
  boot_cmd: null                 # e.g. "bin/rails server -e test -p {port}"; null => HTTP claims quarantine
  base_url: null                 # e.g. "http://localhost:{port}"
  seed_cmd: null                 # synthetic fixtures only; never production copies
  timeout_s: 120
  attempts_max: 2
sandbox:
  mode: env                      # none | env | container
  env_allowlist: [PATH, HOME, LANG, GEM_HOME, BUNDLE_PATH]
  database_url: "postgres://localhost/habeas_throwaway"
  container_image: null          # required when mode: container
gates:
  advisory_severity_cap: medium
  max_candidates_per_unit: 8
  max_emissions_per_run: 5
scope:
  exclude: ["vendor/", "node_modules/", "db/schema.rb", "tmp/", "log/", ".audit/", "issues/"]
scan:
  unit_max_lines: 500            # soft cap on lines of focus per run
```

Parsing note: PyYAML is not stdlib. **DECIDED**: config must remain parseable by a
~60-line restricted-YAML parser in `state.py` (two-space indent, scalars, lists,
one nesting level of maps, `null`, no anchors/multiline). Document the restriction
in README. Do not add a pip dependency for this.

---

## 6. Data model

### 6.1 Finding lifecycle **DECIDED**

```
candidate -> expert_validated -> critic_survived -> proved      -> emitted
                              -> critic_advisory  -> emitted (advisory track)
any stage -> killed
critic_survived -> proof_failed -> quarantined
```

Every transition appends `{ts, from, to, actor, note}` to the finding's `history`
array. `actor` is one of `scout|expert|critic|prover|proof_review|emit`.

### 6.2 `.audit/state.json`

```json
{
  "version": 1,
  "scanned_hunks": { "<sha256-16>": { "path": "app/models/x.rb", "run": "r-...", "ts": "..." } },
  "findings": {
    "<fingerprint>": {
      "status": "quarantined",
      "issue_id": null,
      "record": { "...full finding record..." },
      "history": [ { "ts": "...", "from": "candidate", "to": "expert_validated", "actor": "expert", "note": "" } ]
    }
  },
  "runs": [ { "id": "r-20260905-1", "unit": "app/services/refunds", "counts": { "candidates": 6, "expert_validated": 4, "critic_killed": 2, "critic_advisory": 1, "promoted": 1, "proved": 1, "quarantined": 0, "emitted": 2 } } ]
}
```

`state.py` is the only writer. Subcommands: `init`, `add-candidates <json>`,
`transition <fp> <to> --actor --note`, `record-hunks <paths...>`, `is-scanned
<path>`, `open-fingerprints`, `run-start`, `run-end --counts <json>`. All output
JSON on stdout. Writes are atomic (write temp file, `os.replace`).

### 6.3 Finding record (produced incrementally through the stages)

```json
{
  "id": "cand-3",
  "fingerprint": "a1b2c3d4e5f60718",
  "category": "security",          // bug | security | improvement | test-gap
  "track": "provable",             // derived: bug|security -> provable, else advisory
  "file": "app/controllers/refunds_controller.rb",
  "lines": [41, 58],
  "hypothesis": "refund amount not validated against original charge",
  "confidence": 0.7,
  "evidence_snippet": "...",
  "failure_scenario": "...",        // expert
  "proof_sketch": {                 // expert; required for provable track
    "type": "rspec",                // rspec | http
    "outline": "...",
    "expected_failure_pattern": "expected .* to raise .*InsufficientFunds"
  },
  "fix_sketch": "unified diff or prose",   // optional; required for security fail-then-pass
  "severity": "high",
  "single_concern": true,
  "critic": { "verdict": "promote", "strongest_counterargument": "...", "confidence": 0.8 },
  "proof": { "spec_path": "...", "runs": ["red","red"], "failure_message": "...", "fail_then_pass": true, "log_path": "..." },
  "proof_review": { "verdict": "valid", "reason": "..." }
}
```

### 6.4 Fingerprint (`fingerprint.py`) **DECIDED: snippet-based, not line-based**

`sha256(relpath + "\n" + category + "\n" + normalize(snippet))[:16]` where
`normalize` strips leading/trailing whitespace per line, collapses internal
whitespace runs to one space, and drops lines that are only comments (`#` prefix
after strip). Snippet is the flagged lines plus one line of context each side.
Survives line drift and reformatting; does not survive a rewrite of the flagged
code, which is acceptable (a rewritten site deserves a re-look). Dedupe rule: a
candidate whose fingerprint exists in `findings` with any status other than
`killed` is dropped before the expert stage. `killed` fingerprints suppress
re-emission for 30 days (store `killed_ts`), then may recur.

### 6.5 Issue folder (emitted, provable track)

```
issues/0007-refund-amount-unvalidated/
  ISSUE.md
  proof_spec.rb
  proof_run.log        # both red runs on HEAD; green-with-fix run if achieved
```

`ISSUE.md` frontmatter: `id, slug, fingerprint, track, category, severity, status:
open, discovered, run, files, proof_command, fail_then_pass`. Body sections, in
order: `## Claim`, `## Failure scenario`, `## Evidence` (code refs, not pasted
walls), `## Proof` (exact repro command, expected output), `## Suggested fix
direction`, `## Scope` (one concern; if the fix would span more, the finding must
have been split upstream). Advisory issues: single ISSUE.md, frontmatter
`track: advisory`, body carries a fixed first line: `> Advisory finding: opinion,
not evidence. Severity capped at medium.`

Issue ids are zero-padded sequential, allocated by `emit.py` from the max existing
id in `issues/`. `quarantine.md` entries: heading with fingerprint + slug, the
claim card, why proof failed (attempt logs summarized, link to full log under
`.audit/runs/`), and the critic's confidence. Quarantine is append-only.

---

## 7. Component specs

### 7.1 SKILL.md

Frontmatter description must be pushy about triggering. Draft:

> name: habeas
> description: Incremental, proof-gated codebase audit. Scans one module at a time
> for bugs, security issues, and test gaps; validates candidates with domain
> context packs; kills false positives with a cold adversarial critic; emits only
> findings backed by a failing test or mechanical repro, as small single-concern
> markdown issues. Use whenever the user asks to audit, review, or scan a codebase
> or module for bugs/security issues/tech debt, asks "what's wrong with this
> code", or wants issues/tickets generated from code analysis.

Name rationale (put a short version in README): habeas, from habeas corpus,
the writ that forces whoever holds a prisoner to produce the evidence or release
them. The pipeline applies the same contract to findings: produce a red test that
fails for the claimed reason, or the finding is released (killed or quarantined).
README one-liner: "habeas: produce the evidence or release the finding."
Keep the directory slug identical to the frontmatter name (`habeas/SKILL.md`,
`name: habeas`) so install paths stay consistent across hosts.

Body (keep < 300 lines): quickstart (init config, pick a unit, run), the inline-mode
operating procedure step by step (section 8) with explicit "now run scripts/state.py
..." instructions, pointers into `references/` for the proof gate and pack recipes,
and hard rules restated: never skip the critic, never emit a provable-track finding
without a proof folder, never run proofs outside `prove.sh`.

### 7.2 `adapters.conf` + `dispatch.sh`

`adapters.conf`, shell-sourceable, one template per provider; `{prompt_file}` and
`{input_file}` placeholders:

```
claude="claude -p --dangerously-skip-permissions < {prompt_file}"   # VERIFY FLAGS
codex="codex exec --sandbox read-only < {prompt_file}"              # VERIFY FLAGS
pi="pi --print < {prompt_file}"                                     # VERIFY FLAGS
grok="grok --prompt-file {prompt_file}"                             # VERIFY FLAGS
generic="$HABEAS_GENERIC_CMD"                                   # user-supplied, e.g. ollama wrapper
```

**IMPLEMENTER**: the flags above are placeholders from memory; verify each against
the installed CLI and correct. Requirements that must hold whatever the flags are:
non-interactive, fresh session (no continue/resume), exits when done, prompt on
stdin or via file, repo-read allowed, **write and network permissions off** for the
critic and proof-review roles if the CLI supports permission scoping.

`dispatch.sh <role> <prompt_file> <input_file> [--cwd <dir>]`:

1. Resolve provider for role from config; look up template in adapters.conf;
   error out with a clear message if the CLI binary is absent (fallback chain:
   configured provider, then scout's provider, then any detected CLI).
2. Concatenate prompt file + `\n\n---INPUT---\n` + input file into a temp prompt.
3. Run in `--cwd` (default: current repo root) with a hard timeout (600s).
4. Pipe stdout through `json_extract.py`. On parse failure, retry once appending
   "Output only the JSON object, no prose, no code fences." Second failure: emit
   `{"error":"unparseable","raw_path":"..."}` and let the caller decide (pipeline
   treats it as a kill for critic stage, as a reject for expert stage).

`json_extract.py`: scan text for the first balanced `{...}` or `[...]` that
`json.loads` accepts, ignoring fenced-block markers. ~40 lines, stdlib.

### 7.3 Context asymmetry via git worktree **DECIDED**

Critic, proof-review, and prover each run in a throwaway worktree:
`git worktree add <tmpdir> HEAD` in the target repo. A worktree contains only
tracked files, so `.audit/`, unemitted pipeline JSON, and prior expert reasoning are
physically absent; `issues/` and `quarantine.md` are tracked once committed, so
`dispatch.sh --cwd <worktree>` for critic roles first deletes `issues/` and
`quarantine.md` inside the worktree. Remove worktrees (`git worktree remove
--force`) in a trap on exit. The critic gets repo read access on purpose: finding
the guard clause that refutes a claim requires looking around. What it must never
get is the expert's argument, and the worktree plus input-file discipline
guarantees that structurally rather than by prompt politeness.

### 7.4 `contextpack.py`

`contextpack.py <finding-json> --out <pack.md>`. Recipes (full detail in
`references/context-packs.md`):

- Always: the flagged file in full (or a 400-line window around the flagged lines
  for big files), plus candidate metadata.
- Callers: `git grep -n` for the defining symbol(s) of the flagged lines
  (method/class names extracted by regex, Ruby-aware: `def x`, `class X`,
  `module X`); include up to 5 call sites with 10 lines of context each.
- `expert-db`: append the `db/schema.rb` sections for any table whose model or
  table name appears in the flagged file, plus the 3 most recent migrations
  touching those tables.
- `expert-security`: append the auth/authz files (configurable glob, default
  `app/controllers/application_controller.rb`, `app/policies/**`, `config/initializers/*auth*`).
- `expert-tests`: append existing specs whose path mirrors the flagged file.
- Hard cap ~1500 lines per pack; truncate callers first, then schema, never the
  flagged file window.

Routing: `security -> expert-security`, `bug` in `app/models|db|*repository*` or
SQL present in snippet `-> expert-db`, `test-gap -> expert-tests`, everything else
`-> expert-backend`.

### 7.5 Prompts: required contracts

Every prompt ends with an explicit output schema and "output only JSON". Every
prompt includes an inlined copy of the relevant category sections from
`references/negative-examples.md` (assembled at dispatch time by `run.sh`/host, so
the list grows without editing prompts). Seed negative examples, minimum set:
Ruby/TS strict-null lookalikes, safe interpolation into log statements, `frozen_
string_literal` style nits, "missing test" for trivial delegation methods,
theoretical DoS via unbounded input already capped by middleware, and anything
whose fix is "add a comment".

- `scout.md`: input is the unit's file list + contents (inline mode: the host reads
  them itself). Output: JSON array of candidates (schema 6.3, fields through
  `evidence_snippet`), max `gates.max_candidates_per_unit`, `confidence` >= 0.3
  or omit. Instructions: prefer fewer, stronger candidates; a candidate must name
  the concrete failure, not a vibe ("could be slow" is not a candidate).
- `expert-*.md`: input candidate + context pack. Output: `verdict:
  proceed|reject`, and on proceed the fields `failure_scenario, proof_sketch,
  fix_sketch?, severity, single_concern`. Hard rules in-prompt: no failure
  scenario => reject; provable track without a `proof_sketch` containing an
  `expected_failure_pattern` regex => reject; `single_concern: false` => output a
  `split` array of narrower candidates instead of proceeding.
- `critic.md`: input is the **claim card only**: file, lines, category, severity,
  failure_scenario, evidence_snippet. Not the expert's reasoning, not confidence,
  not the fix sketch. Mandate text: "Your job is to kill this finding. Search the
  repository for evidence the code is correct: guard clauses, validations,
  middleware, callers that make the scenario unreachable, tests that already cover
  it, comments documenting intent. Argue the strongest case that this is not a
  real issue. Only if you cannot construct a credible refutation may you promote."
  Output: `verdict: kill|advisory|promote`, `strongest_counterargument`,
  `confidence`. `advisory` is for real-but-unprovable-or-judgment findings; it
  demotes track and caps severity.
- `proof-review.md`: input = claim card + proof spec source + full run log. Output
  `verdict: valid|invalid`, `reason`. In-prompt checklist: (1) failure message is
  the claimed failure mode, not a setup/typo error; (2) the subject of the claim is
  not mocked or stubbed; (3) assertions would pass on corrected code, i.e. the test
  asserts desired behavior, not the bug's existence; (4) the test does not trivially
  assert `raise_error` on code the test itself wrote.

### 7.6 `prove.sh`

`prove.sh <finding-json> <proof-file> --worktree <dir>`. Steps, all mechanical:

1. Copy proof file to `{worktree}/{proof.spec_dir}/audit_<fingerprint>_spec.rb`.
2. Build sandboxed environment per `sandbox.mode`:
   - `env`: exec with scrubbed environment (allowlist only), `DATABASE_URL` and
     `RAILS_ENV=test` overridden from config. On Linux, if `unshare` is available,
     wrap the command in `unshare -n` for no-network; otherwise proceed and record
     `"network_isolated": false` in the log.
   - `container`: `docker run --rm --network none -v {worktree}:/work -w /work
     {image} {cmd}`. No other mounts. Refuse to run if image unset.
   - `none`: allowed only when config explicitly says so AND an env var
     `HABEAS_I_UNDERSTAND=1` is set; log loudly.
3. Run `proof.test_cmd` with `timeout_s`, kill the process group on expiry.
4. Mechanical gate A (ran at all): parse RSpec summary line
   `N examples, M failures[, E errors]`. `N == 0` or errors outside examples or a
   nonzero exit without a summary line => proof attempt failed (this is failure of
   proof, not proof).
5. Mechanical gate B (red for the claimed reason): `M >= 1` AND first failure
   message matches `expected_failure_pattern` (regex, case-insensitive). No match
   => attempt failed.
6. Determinism: run again from step 3 in a fresh process; require same failure
   count and same first failure message after normalizing object ids, hexadecimal
   addresses, timestamps, and durations. Mismatch => attempt failed with reason
   `nondeterministic`; this also auto-files an advisory `flaky-behavior` note.
7. Fail-then-pass: if `fix_sketch` is a valid unified diff, `git apply` it in the
   worktree, rerun once, require `0 failures`, then `git checkout -- .`.
   **Mandatory for `category: security`** (no valid diff or no green => attempt
   failed). Best effort otherwise: attempted when a diff exists, recorded either
   way, not gating.
8. HTTP-type proofs (`proof_sketch.type: http`): only when `boot_cmd` set AND
   `sandbox.mode: container`. Otherwise return `quarantine: harness_missing`
   without executing anything. When runnable: boot inside the container, wait for
   `base_url` readiness (max 60s), run the proof script (a Ruby script using
   Net::HTTP against `base_url`; same red-reason and determinism gates keyed on the
   script's exit code + a `PROOF-VIOLATION:` line it must print). v3 feature; stub
   the branch in v1 with the quarantine path implemented.
9. Output JSON: `{result: red|failed|quarantine, runs, failure_message,
   fail_then_pass, log_path}` and write the full log under `.audit/runs/<run>/`.

Attempts: the pipeline allows `attempts_max` (default 2) proof materializations per
finding. Attempt 1 materializes the expert's sketch (inline mode: host writes the
spec; scripted mode: dispatch a `prover` role with expert provider). Attempt 2 gets
the attempt-1 log appended to the input. After that: quarantine.

### 7.7 `emit.py`

`emit.py <fingerprint>`: reads the finding record from state, allocates id, writes
the issue folder or the advisory ISSUE.md or appends to `quarantine.md`, updates
state status, refuses to emit a provable-track finding whose record lacks
`proof.result == "red"` plus `proof_review.verdict == "valid"` (defense in depth
against a host agent shortcutting the pipeline). Also enforces
`gates.max_emissions_per_run` and the advisory severity cap.

### 7.8 `hotspots.py` (v2)

`hotspots.py --top 10`: score per directory (depth 2 by default) =
`commits_touching_last_180d * (total_lines / file_count)` using `git log
--name-only` and `wc -l`, excluding `scope.exclude`, minus units whose files are
all in `scanned_hunks` and unchanged (compare content hashes). Output JSON queue.
Line count stands in for complexity; cyclomatic parsing is not worth a dependency.

---

## 8. Pipeline algorithm (one run)

Inline mode; scripted mode is identical with dispatch calls replacing "host does".

1. `state.py run-start --unit <path>`. Unit = directory argument in v1, hotspot
   queue head in v2. Enforce `scan.unit_max_lines` by trimming the file list
   (largest-churn files first in v2, alphabetical in v1) and record what was
   deferred.
2. **Scout**: host (or dispatch) produces candidates per `scout.md`.
   `state.py add-candidates` fingerprints them, drops dedupe hits and
   already-scanned unchanged hunks, records the rest as `candidate`.
3. **Expert**: for each candidate: `contextpack.py`, then run the routed expert
   prompt. `reject` => `killed`. `split` => replace with narrower candidates (they
   re-enter at step 3, once; a second split kills). `proceed` => `expert_validated`.
4. **Critic**: create worktree; for each validated finding, `dispatch.sh critic`
   with the claim card. `kill` => `killed` (with counterargument recorded; feed
   recurring kill patterns into `negative-examples.md` manually). `advisory` =>
   demote track, cap severity, `critic_advisory`. `promote` => `critic_survived`.
   Advisory-category findings (improvement, test-gap) also pass through the critic
   and need `advisory` or `promote` to survive; `promote` on an advisory-track
   finding just means "emit as advisory".
5. **Prove** (provable track only): in the worktree, materialize proof attempt,
   `prove.sh`, then `dispatch.sh proof_review`. `red` + `valid` => `proved`.
   Attempt budget exhausted or `harness_missing` => `quarantined`. `proof_review:
   invalid` counts as a failed attempt.
6. **Emit**: `emit.py` per surviving finding, respecting the per-run emission cap
   (excess findings stay `proved`/`critic_advisory` in state and emit next run).
7. `state.py record-hunks` for all scanned files; `state.py run-end` with counts;
   append a metrics line (section 11); remove worktrees.

Ordering rationale (do not change): proof is the expensive stage, so only critic
survivors reach it; the critic is cheaper than proof but pricier than expert, so
experts filter first.

---

## 9. Proof gate, normative summary (also goes in `references/proof-gate.md`)

A provable-track finding is emitted only if ALL hold:

1. The proof ran: test framework executed, >= 1 example, no errors outside examples.
2. Red for the claimed reason: >= 1 failure whose message matches the expert's
   `expected_failure_pattern`.
3. Deterministic: two runs, identical normalized failure.
4. Not self-satisfying: proof-review confirms the subject is unmocked, the failure
   is the claimed mode, and assertions describe correct behavior.
5. Security findings additionally: fail-then-pass with the fix diff.
6. Executed by `prove.sh` under the configured sandbox. Never by hand, never by the
   host agent calling rspec directly.

Anything that fails 1-5 after `attempts_max` goes to quarantine with the attempt
attached. Nothing is silently dropped: kill, quarantine, and emit are the only
exits, and all three are recorded.

---

## 10. Sandbox policy **DECIDED**

v1 default `env` mode: scrubbed environment, throwaway DATABASE_URL, `unshare -n`
where available. `container` mode becomes mandatory (README states this, `prove.sh`
enforces the HTTP part) at any of: HTTP-class proofs, unattended/scheduled runs, or
a target repo whose dev/test config carries real credentials. Seeds must be
synthetic (`seed_cmd` contract); never point the sandbox DB at a production copy.

---

## 11. Metrics

Append one JSON line per run to `.audit/metrics.jsonl`: run id, unit, stage counts
(as in state), wallclock per stage, dispatch call count per provider, proof
attempts, and `human_verdicts: []` placeholder. Provide
`scripts/verdict.sh <issue-id> useful|noise` for the operator to record judgments
into the latest matching metrics line. The two numbers that drive tuning, printed
by `run.sh --report`: useful rate of emissions (target >= 70% by end of v2; below
50% => tighten critic and negative examples before any feature work) and
quarantine composition (real bugs landing there => the recall hole is too big,
pull the HTTP harness forward).

---

## 12. Fixture repo and self-test (build this FIRST, before the pipeline)

`fixtures/audit-target/`: plain Ruby + RSpec + bundler, no Rails, an in-memory
"payments" toy (Ledger, Account, Transfer, TokenAuth classes, ~300 lines). Planted
defects, each in its own commit with message `PLANT: <n>`:

1. SQL built by string interpolation in a `Reporting.query` method (SQLite via
   stdlib or a fake adapter that executes strings, enough for an injection proof).
2. Off-by-one in pagination (`limit`/`offset` math) returning a duplicate row.
3. Missing authorization check: `Transfer.reverse!` never checks the actor's role.
4. Timing-unsafe token comparison (`==` on secrets) in TokenAuth.
5. Float money arithmetic causing a rounding loss in fee calculation.

Lookalikes (must NOT be emitted): (a) interpolation into a log message, (b) a
documented intentional admin bypass with a comment and a covering spec, (c) dead
code containing a "bug" that is unreachable and unexported, (d) `==` comparison on
non-secret public ids.

`scripts/selftest.sh`: runs the full pipeline in scripted mode over the fixture
(providers: whatever CLI is on PATH; skip with a clear message if none), then
asserts: >= 3 of 5 plants emitted with valid proofs, 0 lookalikes emitted,
quarantine+killed accounts for every candidate (no silent drops), state.json
lifecycle histories complete, second run emits 0 duplicates. This is the
definition of done for v1 and the regression harness for every later change.
Also write plain pytest-style unit tests (stdlib `unittest`) for `fingerprint.py`
(drift cases), `json_extract.py` (fenced, prosed, broken inputs), `state.py`
(transitions, atomicity), and the restricted YAML parser.

---

## 13. Phasing

**v1, walking skeleton.** Build order: (1) fixture repo + unit tests, (2)
`state.py`, `fingerprint.py`, `json_extract.py`, (3) `adapters.conf` +
`dispatch.sh` verified against >= 1 installed CLI, (4) prompts, (5) `contextpack.py`
with the always+callers recipe only, (6) `prove.sh` env-mode rspec-only, (7)
`emit.py`, (8) SKILL.md + README, (9) `selftest.sh` green. Single generic expert
(`expert-backend.md`), same-model critic (still fresh-session via dispatch),
directory-argument units, no hotspots. Exit: selftest green + 10 manual runs on a
real repo the operator knows, every emission judged.

**v2, precision.** Expert routing + per-domain packs, cross-model critic
(second CLI), `hotspots.py`, negative-examples grown from v1 kills/quarantine,
fail-then-pass enforcement wired for security, `run.sh --report`. Exit: useful
rate >= 70% over 20 runs.

**v3, reach.** HTTP proofs behind container+boot contract, `test-gap` findings via
the `mutant` gem when present in the target's Gemfile (surviving mutants become
advisory findings with the mutant diff as mechanical evidence; model opinion on
test quality is retired), `gh issue create` emitter behind `--tracker github`,
scripted-mode cron recipe in README. Start only when v2's exit number holds.

---

## 14. Notes for the implementing agent

- Verify every CLI flag in `adapters.conf` against the installed binaries before
  wiring dispatch; record verified invocations and the CLI versions in README.
- Keep all Python stdlib-only. If you feel a dependency is needed, the design is
  wrong; simplify instead.
- `emit.py`'s refusal to emit unproved provable findings is deliberate redundancy
  with the pipeline; implement both.
- Do not "improve" the critic by giving it the expert's reasoning, confidence, or
  fix sketch. Context asymmetry is the mechanism, not an oversight.
- Do not let any stage delete or skip findings without a state transition. The
  invariant selftest checks: candidates == killed + quarantined + emitted +
  still-in-flight.
- When in doubt on ambiguous small choices (log formats, temp dir layout), decide
  and note it in README; do not block.
