module Billing
  class InvoicePolicy
    def initialize(account, invoice)
      @account = account
      @invoice = invoice
    end

    def show?
      @invoice.account_id == @account.id
    end

    def update?
      show?
    end
  end
end
