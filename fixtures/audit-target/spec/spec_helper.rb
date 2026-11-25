# frozen_string_literal: true

require_relative "../lib/audit_target"

RSpec.configure do |config|
  config.expect_with :rspec do |c|
    c.syntax = :expect
  end
  config.disable_monkey_patching!
  config.order = :random
  Kernel.srand config.seed

  config.before do
    AuditTarget::Logging.io = StringIO.new
  end
end
