module Billing
  class SyncLegacyCustomer
    def call(account)
      legacy_customer = LegacyCustomer.find(account.legacy_id)
      legacy_customer.update(name: account.name)
      legacy_customer
    end
  end
end
