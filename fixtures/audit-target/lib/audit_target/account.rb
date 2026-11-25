# frozen_string_literal: true

module AuditTarget
  class Account
    attr_reader :id, :owner, :role
    attr_accessor :balance_cents

    def initialize(id:, owner:, role: :member, balance_cents: 0)
      @id = id
      @owner = owner
      @role = role
      @balance_cents = balance_cents
    end

    def admin?
      role == :admin
    end

    # Account ids are public identifiers (they appear in URLs and receipts).
    def same_account?(other_id)
      id == other_id
    end
  end
end
