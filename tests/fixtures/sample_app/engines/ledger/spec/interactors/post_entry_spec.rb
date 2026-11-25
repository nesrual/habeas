root = File.expand_path("../../../..", __dir__)
require File.join(root, "lib/sample/support")
require File.join(root, "engines/billing/app/models/billing/invoice")
require File.join(root, "engines/ledger/app/interactors/ledger/post_entry")

RSpec.describe Ledger::PostEntry do
  it "returns the amount of the invoice" do
    Billing::Invoice.reset!
    Billing::Invoice.create(id: 1, amount_cents: 250, status: :open)
    expect(described_class.new.call(1)[:amount]).to eq(2.5)
  end
end
