# frozen_string_literal: true

RSpec.describe AuditTarget::TokenAuth do
  subject(:auth) { described_class.new("tok_live_s3cret") }

  it "requires a secret at construction" do
    expect { described_class.new(nil) }.to raise_error(ArgumentError)
    expect { described_class.new("") }.to raise_error(ArgumentError)
  end

  it "accepts the correct token" do
    expect(auth.authenticate?("tok_live_s3cret")).to be(true)
  end

  it "rejects wrong, empty, and missing tokens" do
    expect(auth.authenticate?("tok_live_wrong")).to be(false)
    expect(auth.authenticate?("")).to be(false)
    expect(auth.authenticate?(nil)).to be(false)
  end

  it "matches public key ids" do
    expect(auth.key_id_matches?("key_123", "key_123")).to be(true)
    expect(auth.key_id_matches?("key_456", "key_123")).to be(false)
  end
end
