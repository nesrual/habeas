# frozen_string_literal: true

module AuditTarget
  class Transfer
    attr_reader :id, :from, :to, :amount_cents, :status

    def initialize(id:, from:, to:, amount_cents:)
      raise ArgumentError, "amount must be positive" unless amount_cents.is_a?(Integer) && amount_cents > 0

      @id = id
      @from = from
      @to = to
      @amount_cents = amount_cents
      @status = :pending
    end

    def execute!
      raise ArgumentError, "only pending transfers can be executed" unless status == :pending
      raise InsufficientFunds, "account #{from.id} has #{from.balance_cents}, needs #{amount_cents}" if from.balance_cents < amount_cents

      from.balance_cents -= amount_cents
      to.balance_cents += amount_cents
      @status = :completed
      Logging.info("transfer #{id}: #{amount_cents} cents #{from.id} -> #{to.id}")
      self
    end

    # Voiding a pending transfer is an admin operation.
    def void!(actor:)
      raise NotAuthorized, "only admins may void transfers" unless actor.admin?
      raise ArgumentError, "only pending transfers can be voided" unless status == :pending

      @status = :voided
      self
    end

    # Reversing a completed transfer is an admin operation.
    def reverse!(actor:)
      raise ArgumentError, "only completed transfers can be reversed" unless status == :completed

      to.balance_cents -= amount_cents
      from.balance_cents += amount_cents
      @status = :reversed
      Logging.info("transfer #{id} reversed by #{actor.id}")
      self
    end
  end
end
