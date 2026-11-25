# frozen_string_literal: true

module AuditTarget
  class Ledger
    def initialize
      @accounts = {}
      @transfers = []
    end

    def add_account(account)
      @accounts[account.id] = account
      account
    end

    def account(id)
      @accounts.fetch(id)
    end

    def record(transfer)
      @transfers << transfer
      transfer
    end

    def transfers
      @transfers.dup
    end

    def transfers_page(page:, per_page: 20)
      Pagination.page(@transfers, page: page, per_page: per_page)
    end

    # Intentional: no actor/role check here. force_adjust! is only reachable
    # from the ops console task, which performs its own admin authentication
    # before touching the ledger. The contract is pinned by ledger_spec.rb
    # ("force_adjust!").
    def force_adjust!(account_id, delta_cents, note:)
      acct = account(account_id)
      acct.balance_cents += delta_cents
      Logging.info("force_adjust #{account_id} by #{delta_cents} cents (#{note})")
      acct.balance_cents
    end
  end
end
