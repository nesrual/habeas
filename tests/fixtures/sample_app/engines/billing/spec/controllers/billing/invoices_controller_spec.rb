require_relative "../../spec_helper"

RSpec.describe Billing::InvoicesController do
  let(:owner) { Billing::Account.create(id: 1, name: "A") }
  let(:other) { Billing::Account.create(id: 2, name: "B") }
  let(:invoice) { Billing::Invoice.create(id: 10, account_id: owner.id, amount_cents: 500, status: :open) }

  it "shows an invoice to its owner" do
    result = described_class.new(current_account: owner, params: { id: invoice.id }).show
    expect(result[:amount_cents]).to eq(500)
  end

  it "lets the owner update the memo" do
    result = described_class.new(current_account: owner, params: { id: invoice.id, memo: "x" }).update
    expect(result[:memo]).to eq("x")
  end

  it "denies an update from another account" do
    controller = described_class.new(current_account: other, params: { id: invoice.id, memo: "x" })
    expect { controller.update }.to raise_error(Sample::NotAuthorized)
  end
end
