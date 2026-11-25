module Billing
  # Lives in the V1 schema. V2 code may read it and must never write it.
  class LegacyCustomer < V1ApplicationRecord
  end
end
