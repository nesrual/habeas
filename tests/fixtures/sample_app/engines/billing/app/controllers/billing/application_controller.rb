module Billing
  class ApplicationController
    attr_reader :current_account, :params

    def initialize(current_account:, params: {})
      @current_account = current_account
      @params = params
    end

    private

    def authorize(record, query)
      policy = InvoicePolicy.new(current_account, record)
      raise Sample::NotAuthorized unless policy.public_send(query)

      record
    end
  end
end
