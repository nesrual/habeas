# frozen_string_literal: true

RSpec.describe AuditTarget::Reporting do
  let(:db) do
    AuditTarget::FakeDB.new.tap do |db|
      db.insert(:transfers, { "id" => "t-1", "account_id" => "acc-1", "amount_cents" => 100 })
      db.insert(:transfers, { "id" => "t-2", "account_id" => "acc-2", "amount_cents" => 200 })
      db.insert(:transfers, { "id" => "t-3", "account_id" => "acc-1", "amount_cents" => 300 })
    end
  end

  subject(:reporting) { described_class.new(db) }

  it "returns the transfers recorded for one account" do
    rows = reporting.transfers_for("acc-1")
    expect(rows.map { |r| r["id"] }).to contain_exactly("t-1", "t-3")
  end

  it "returns nothing for an unknown account" do
    expect(reporting.transfers_for("acc-404")).to eq([])
  end

  it "returns every transfer" do
    expect(reporting.all_transfers.size).to eq(3)
  end
end
