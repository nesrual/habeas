# frozen_string_literal: true

RSpec.describe AuditTarget::Account do
  it "defaults to the member role with a zero balance" do
    acct = described_class.new(id: "acc-1", owner: "Alice")
    expect(acct.role).to eq(:member)
    expect(acct.balance_cents).to eq(0)
    expect(acct.admin?).to be(false)
  end

  it "recognizes admins" do
    acct = described_class.new(id: "acc-9", owner: "Root", role: :admin)
    expect(acct.admin?).to be(true)
  end

  it "matches its own public id" do
    acct = described_class.new(id: "acc-1", owner: "Alice")
    expect(acct.same_account?("acc-1")).to be(true)
    expect(acct.same_account?("acc-2")).to be(false)
  end
end
