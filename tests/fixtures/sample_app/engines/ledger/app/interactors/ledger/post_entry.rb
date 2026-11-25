module Ledger
  class PostEntry
    def call(invoice_id)
      invoice = Billing::Invoice.find(invoice_id)
      { invoice_id: invoice.id, amount: invoice.amount_cents / 100.0 }
    end
  end
end
