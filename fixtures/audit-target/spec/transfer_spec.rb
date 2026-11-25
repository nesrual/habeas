# frozen_string_literal: true

RSpec.describe AuditTarget::Transfer do
  let(:alice) { AuditTarget::Account.new(id: "acc-1", owner: "Alice", balance_cents: 1_000) }
  let(:bob)   { AuditTarget::Account.new(id: "acc-2", owner: "Bob", balance_cents: 0) }
  let(:admin) { AuditTarget::Account.new(id: "acc-0", owner: "Ops", role: :admin) }

  def transfer(amount = 400)
    described_class.new(id: "t-1", from: alice, to: bob, amount_cents: amount)
  end

  it "rejects non-positive amounts" do
    expect { transfer(0) }.to raise_error(ArgumentError)
    expect { transfer(-5) }.to raise_error(ArgumentError)
  end

  describe "#execute!" do
    it "moves money and completes" do
      t = transfer.execute!
      expect(t.status).to eq(:completed)
      expect(alice.balance_cents).to eq(600)
      expect(bob.balance_cents).to eq(400)
    end

    it "refuses when funds are insufficient" do
      expect { transfer(5_000).execute! }.to raise_error(AuditTarget::InsufficientFunds)
      expect(alice.balance_cents).to eq(1_000)
    end

    it "cannot run twice" do
      t = transfer.execute!
      expect { t.execute! }.to raise_error(ArgumentError, /pending/)
    end
  end

  describe "#void!" do
    it "requires an admin actor" do
      expect { transfer.void!(actor: alice) }.to raise_error(AuditTarget::NotAuthorized)
    end

    it "voids a pending transfer for an admin" do
      expect(transfer.void!(actor: admin).status).to eq(:voided)
    end

    it "refuses on completed transfers" do
      t = transfer.execute!
      expect { t.void!(actor: admin) }.to raise_error(ArgumentError, /pending/)
    end
  end

  describe "#reverse!" do
    it "reverses a completed transfer" do
      t = transfer.execute!
      t.reverse!(actor: admin)
      expect(t.status).to eq(:reversed)
      expect(alice.balance_cents).to eq(1_000)
      expect(bob.balance_cents).to eq(0)
    end

    it "refuses on pending transfers" do
      expect { transfer.reverse!(actor: admin) }.to raise_error(ArgumentError, /completed/)
    end
  end
end
