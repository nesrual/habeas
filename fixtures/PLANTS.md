# Ground truth for fixtures/audit-target

Reference for `selftest.sh` assertions and human review. This file must never
be copied into a built fixture repo (`build_fixture.sh` copies only
`audit-target/`), so no pipeline role can read it.

## Planted defects (must be found and proved; selftest requires >= 3 of 5 emitted)

| # | Category | File | Defect | Mechanical demonstration |
|---|----------|------|--------|--------------------------|
| 1 | security | `lib/audit_target/reporting.rb` | `Reporting#transfers_for` interpolates `account_id` into SQL | `transfers_for("x' OR '1'='1")` returns every row instead of none |
| 2 | bug | `lib/audit_target/pagination.rb` | Offset computed as `(page - 1) * (per_page - 1)`; pages overlap | page 1 and page 2 (per_page 3 over 10 items) share item 3 |
| 3 | security | `lib/audit_target/transfer.rb` | `Transfer#reverse!` documented admin-only but never checks `actor.admin?` (sibling `void!` does) | a `:member` actor reverses a completed transfer without `NotAuthorized` |
| 4 | security | `lib/audit_target/token_auth.rb` | `TokenAuth#authenticate?` compares secrets with `==` (timing-unsafe; no `OpenSSL.secure_compare` / `Rack::Utils.secure_compare`) | code inspection + short-circuit comparison; proof asserts constant-time comparison is absent/behavioral timing difference |
| 5 | bug | `lib/audit_target/fees.rb` | Fee computed via float dollars then truncated with `.to_i`; loses cents | `fee_cents(100)` returns 32; exact 2.9% + 30c is 33. Wrong for ~half of all cent amounts (100, 200, 300, ...) |

Fee values safe for green specs (float result matches exact): 600 -> 47, 800 -> 53.

## Lookalikes (must NOT be emitted; selftest requires 0)

| # | File | Pattern | Why it is not an issue |
|---|------|---------|------------------------|
| a | `lib/audit_target/ledger.rb`, `lib/audit_target/transfer.rb` | String interpolation into `Logging.info` messages | Interpolation into a log string, not into SQL or a shell; no injection sink |
| b | `lib/audit_target/ledger.rb` | `Ledger#force_adjust!` mutates balances with no actor check | Documented intentional contract (ops console authenticates upstream) with a covering spec in `spec/ledger_spec.rb` |
| c | `lib/audit_target/reporting.rb` | `Reporting#legacy_transfer_rows` contains an off-by-one (`i <= rows.length`) | Private, zero callers, dead code explicitly noted as superseded |
| d | `lib/audit_target/account.rb`, `lib/audit_target/token_auth.rb` | `==` comparison in `same_account?` / `key_id_matches?` | Operands are public identifiers, not secrets; timing side channels are irrelevant |

## Commit layout (produced by build_fixture.sh)

`base: toy payments library` (Gemfile, README, logging, account, fake_db,
ledger, base specs), then `PLANT: 1` through `PLANT: 5`, each adding the file
carrying that defect plus its (gap-containing but green) spec. The fixture's
own suite is green over all plants: 29 examples, 0 failures.
