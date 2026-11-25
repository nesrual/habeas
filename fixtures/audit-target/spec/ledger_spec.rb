# frozen_string_literal: true

RSpec.describe AuditTarget::Ledger do
  subject(:ledger) { described_class.new }

  let(:alice) { AuditTarget::Account.new(id: "acc-1", owner: "Alice", balance_cents: 10_000) }

  it "stores and retrieves accounts by id" do
    ledger.add_account(alice)
    expect(ledger.account("acc-1")).to be(alice)
  end

  it "raises for unknown accounts" do
    expect { ledger.account("acc-404") }.to raise_error(KeyError)
  end

  it "records transfers in order" do
    bob = AuditTarget::Account.new(id: "acc-2", owner: "Bob")
    t = AuditTarget::Transfer.new(id: "t-1", from: alice, to: bob, amount_cents: 100)
    ledger.record(t)
    expect(ledger.transfers).to eq([t])
  end

  it "returns the first page of transfers" do
    bob = AuditTarget::Account.new(id: "acc-2", owner: "Bob")
    ts = (1..5).map { |i| AuditTarget::Transfer.new(id: "t-#{i}", from: alice, to: bob, amount_cents: i) }
    ts.each { |t| ledger.record(t) }
    expect(ledger.transfers_page(page: 1, per_page: 3)).to eq(ts.first(3))
  end

  describe "force_adjust!" do
    # Contract: force_adjust! takes no actor on purpose — authentication is
    # the ops console's responsibility. This spec pins that contract.
    it "adjusts a balance directly and logs the adjustment" do
      ledger.add_account(alice)
      expect(ledger.force_adjust!("acc-1", -2_500, note: "chargeback CB-77")).to eq(7_500)
      expect(alice.balance_cents).to eq(7_500)
      expect(AuditTarget::Logging.io.string).to include("force_adjust acc-1 by -2500 cents (chargeback CB-77)")
    end
  end
end
