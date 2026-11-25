module Billing
  class PayInvoice
    def initialize(repository:)
      @repository = repository
    end

    def call(invoice)
      @repository.post(invoice)
      invoice.update(status: :paid)
      invoice
    end
  end
end
