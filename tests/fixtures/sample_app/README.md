# sample_app

A tiny plain-Ruby project shaped like a Rails app split into engines. It is the
fixture for the deep-audit engine tests. It needs only `rspec`, no Rails.

Run an engine's specs with `bin/engine-run <engine> rspec <paths>`.

Known defects, on purpose:

1. `Billing::InvoicesController#show` looks an invoice up by id with no
   account scope and no authorisation.
2. `Billing::PayInvoice` writes `status` directly, bypassing the state machine,
   so paying twice posts twice.
3. `Billing::SyncLegacyCustomer` writes to a V1 table.
4. `Ledger::PostEntry` reads `Billing::Invoice` directly instead of going
   through a facade (tech debt, not a defect candidate).
