# frozen_string_literal: true

RSpec.describe AuditTarget::Pagination do
  let(:items) { (1..10).to_a }

  it "returns the first page" do
    expect(described_class.page(items, page: 1, per_page: 4)).to eq([1, 2, 3, 4])
  end

  it "returns an empty page past the end" do
    expect(described_class.page(items, page: 50, per_page: 4)).to eq([])
  end

  it "rejects invalid arguments" do
    expect { described_class.page(items, page: 0, per_page: 4) }.to raise_error(ArgumentError)
    expect { described_class.page(items, page: 1, per_page: 0) }.to raise_error(ArgumentError)
  end
end
