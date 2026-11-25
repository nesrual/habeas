# audit_target

A small in-memory payments library: accounts, a ledger, transfers with fees,
token authentication, and reporting queries against a string-executing
database adapter.

## Usage

```ruby
require_relative "lib/audit_target"

ledger = AuditTarget::Ledger.new
alice  = ledger.add_account(AuditTarget::Account.new(id: "acc-1", owner: "Alice", balance_cents: 10_000))
bob    = ledger.add_account(AuditTarget::Account.new(id: "acc-2", owner: "Bob"))

transfer = AuditTarget::Transfer.new(id: "t-1", from: alice, to: bob, amount_cents: 2_500)
ledger.record(transfer.execute!)
```

## Tests

```
bundle install
bundle exec rspec
```
