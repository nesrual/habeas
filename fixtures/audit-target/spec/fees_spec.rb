# frozen_string_literal: true

RSpec.describe AuditTarget::Fees do
  it "charges 2.9% plus the fixed fee" do
    expect(described_class.fee_cents(600)).to eq(47)   # 17 + 30
    expect(described_class.fee_cents(800)).to eq(53)   # 23 + 30
  end

  it "nets the fee out of the amount" do
    expect(described_class.net_cents(600)).to eq(553)
  end
end
