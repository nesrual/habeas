root = File.expand_path("../../..", __dir__)
require File.join(root, "lib/sample/support")
%w[models policies repositories interactors].each do |layer|
  Dir[File.join(root, "engines/billing/app", layer, "**/*.rb")].sort.each { |f| require f }
end
require File.join(root, "engines/billing/app/controllers/billing/application_controller")
require File.join(root, "engines/billing/app/controllers/billing/invoices_controller")

RSpec.configure do |config|
  config.before do
    [Billing::Invoice, Billing::Account, Billing::LegacyCustomer].each(&:reset!)
  end
end
