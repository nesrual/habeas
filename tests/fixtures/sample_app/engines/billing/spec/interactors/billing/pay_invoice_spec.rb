require_relative "../../spec_helper"

RSpec.describe Billing::PayInvoice do
  let(:repository) { Billing::InvoiceRepository.new }
  let(:invoice) { Billing::Invoice.create(id: 10, account_id: 1, amount_cents: 500, status: :open) }

  it "marks the invoice paid and posts once" do
    described_class.new(repository: repository).call(invoice)
    expect(invoice.status).to eq(:paid)
    expect(repository.postings.size).to eq(1)
  end
end
