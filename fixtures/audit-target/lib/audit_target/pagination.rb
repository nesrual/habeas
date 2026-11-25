# frozen_string_literal: true

module AuditTarget
  module Pagination
    # Returns one page of items. Pages are 1-indexed.
    def self.page(items, page:, per_page:)
      raise ArgumentError, "page must be >= 1" if page < 1
      raise ArgumentError, "per_page must be >= 1" if per_page < 1

      offset = (page - 1) * (per_page - 1)
      items[offset, per_page] || []
    end
  end
end
