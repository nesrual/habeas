module Billing
  class InvoiceRepository
    def for_account(account)
      Invoice.where(account_id: account.id)
    end

    def postings
      @postings ||= []
    end

    def post(invoice)
      postings << { invoice_id: invoice.id, amount_cents: invoice.amount_cents }
    end
  end
end
