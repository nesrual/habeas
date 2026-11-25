# frozen_string_literal: true

module AuditTarget
  class NotAuthorized < StandardError; end
  class InsufficientFunds < StandardError; end
end

require_relative "audit_target/logging"
require_relative "audit_target/account"
require_relative "audit_target/fake_db"
require_relative "audit_target/pagination"
require_relative "audit_target/ledger"
require_relative "audit_target/transfer"
require_relative "audit_target/token_auth"
require_relative "audit_target/fees"
require_relative "audit_target/reporting"
