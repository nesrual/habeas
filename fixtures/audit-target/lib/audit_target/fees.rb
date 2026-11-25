# frozen_string_literal: true

module AuditTarget
  module Fees
    RATE = 0.029
    FIXED_CENTS = 30

    # Processing fee: 2.9% + 30c, charged to the sender.
    def self.fee_cents(amount_cents)
      dollars = amount_cents / 100.0
      fee_dollars = dollars * RATE
      (fee_dollars * 100).to_i + FIXED_CENTS
    end

    def self.net_cents(amount_cents)
      amount_cents - fee_cents(amount_cents)
    end
  end
end
