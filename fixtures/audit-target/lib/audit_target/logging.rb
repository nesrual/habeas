# frozen_string_literal: true

require "stringio"

module AuditTarget
  module Logging
    class << self
      attr_writer :io

      def io
        @io ||= StringIO.new
      end

      def info(message)
        io.puts("[audit_target] #{message}")
      end
    end
  end
end
