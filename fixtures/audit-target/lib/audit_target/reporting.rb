# frozen_string_literal: true

module AuditTarget
  class Reporting
    def initialize(db)
      @db = db
    end

    # All transfer rows recorded for one account.
    def transfers_for(account_id)
      @db.execute("SELECT * FROM transfers WHERE account_id = '#{account_id}'")
    end

    def all_transfers
      @db.execute("SELECT * FROM transfers")
    end

    private

    # Superseded by transfers_for during the reporting migration; no callers
    # remain.
    def legacy_transfer_rows(account_id)
      rows = @db.execute("SELECT * FROM transfers")
      rows.each_with_index
          .select { |row, i| i <= rows.length && row["account_id"] == account_id }
          .map(&:first)
    end
  end
end
