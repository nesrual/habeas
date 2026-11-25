module Billing
  class Invoice < ApplicationRecord
    include AASM

    aasm column: :status do
      event :pay, from: :open, to: :paid
      event :void, from: :open, to: :void
    end
  end
end
