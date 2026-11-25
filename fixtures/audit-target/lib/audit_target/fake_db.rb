# frozen_string_literal: true

module AuditTarget
  # Minimal in-memory adapter that executes SQL strings, standing in for a
  # real database driver. Supported dialect:
  #
  #   SELECT * FROM <table> [WHERE <predicate>]
  #
  # where <predicate> is comparisons joined by OR / AND, each comparison
  # being <column> = '<value>' or '<literal>' = '<literal>'.
  class FakeDB
    def initialize
      @tables = Hash.new { |h, k| h[k] = [] }
    end

    def insert(table, row)
      @tables[table.to_s] << row.transform_keys(&:to_s)
      nil
    end

    def execute(sql)
      m = /\Aselect \* from (\w+)(?: where (.+))?\z/i.match(sql.strip)
      raise ArgumentError, "unsupported SQL: #{sql}" unless m

      rows = @tables[m[1]]
      return rows.map(&:dup) unless m[2]

      rows.select { |row| predicate_matches?(m[2], row) }.map(&:dup)
    end

    private

    def predicate_matches?(clause, row)
      clause.split(/\s+or\s+/i).any? do |disjunct|
        disjunct.split(/\s+and\s+/i).all? do |comparison|
          comparison_matches?(comparison.strip, row)
        end
      end
    end

    def comparison_matches?(comparison, row)
      m = /\A(?:(\w+)|'([^']*)')\s*=\s*'([^']*)'\z/.match(comparison)
      raise ArgumentError, "unsupported comparison: #{comparison}" unless m

      left = m[1] ? row[m[1]].to_s : m[2]
      left == m[3]
    end
  end
end
