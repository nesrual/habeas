# Models per role

habeas runs every model role on Claude by default. `config.example.yaml`
sets the model under `models.<role>` and the effort under `effort.<role>`.
`dispatch.sh` passes them to the `claude` adapter as `--model` / `--effort`
and logs `role=… provider=… model=… effort=…` to stderr on every dispatch, so
you can confirm what actually ran.

## Defaults

| Role | Model | Effort | What it needs | Why this model |
|---|---|---|---|---|
| scout | `claude-sonnet-5-5` | medium | Reads many files, broad recall; highest-volume role | Strong code reading at half the Opus price. Haiku 5.5 is 1/20 the cost, but a finding the scout misses is gone for good, since nothing downstream recovers it. |
| expert (all `expert-*` routes) | `claude-opus-5-5` | high | Judgment: a precise failure scenario and a proof sketch | This is where quality pays off. A bad sketch burns prover and critic calls. |
| critic | `claude-fable-5-2` | high | Argues independently of the expert | Must be a **different model** from the expert, so it doesn't share the expert's blind spots. Fable 5.2 is the strongest, and its kills decide what reaches the proof gate. Sonnet 5.5 is the cheaper alternative, but it should still differ from `models.expert`. |
| prover | `claude-opus-5-5` | high | Writes RSpec/Jest proof specs and fix diffs | Sonnet 5.5 also works, because `prove.sh` rejects bad specs mechanically. The cost is more retry attempts. |
| proof_review | `claude-sonnet-5-5` | medium | Checks that a red spec proves the claim | A bounded judgment over a short input. |

Report and summary jobs outside the pipeline (dedup, plain-language
explanations) fit Haiku 5.5 or Sonnet 5.5.

## Prices (Anthropic API list, USD per 1M tokens, input / output)

| Model | Price |
|---|---|
| Claude Fable 5.2 | $8 / $40 |
| Claude Opus 5.5 | $4 / $20 |
| Claude Sonnet 5.5 | $2 / $10 |
| Claude Haiku 5.5 | $0.10 / $0.50 (prompts ≤100K tokens; $0.50 / $2.50 above) |

Older generations cost the same or more and are not stronger. Through
`claude -p`, usage bills against the Claude plan or credits of the logged-in
account, not these API rates. Check current prices at
https://platform.claude.com/docs/en/about-claude/pricing.

## Rules

- **Inline mode:** the interactive host acts as scout and expert, and also
  materializes proofs. `models.scout`, `models.expert`, and `models.prover`
  apply only to dispatched roles, so use `orchestration: scripted`
  (`run.sh --from scout`) to pin them. The critic and proof review are always
  dispatched, in every mode.
- **No silent substitution:** if a role's provider CLI is missing and its
  model is pinned, `dispatch.sh` fails instead of falling back to another
  provider, which would run a different model. Set `models.<role>: null` to
  allow the fallback chain.
- **Model ids are provider-specific.** When you move a role to another
  adapter, set its `models.<role>` to that provider's id (and add
  `{model_args}` / `{effort_args}` to its template), or set it to `null`.
- **`null`** for a model or effort means the CLI's own default.
