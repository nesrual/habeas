# Minimal stand-ins for the framework pieces the sample engines use.
module Sample
  class NotAuthorized < StandardError; end
  class NotFound < StandardError; end
  class InvalidTransition < StandardError; end
end

class ApplicationRecord
  class << self
    def store
      @store ||= {}
    end

    def create(attrs = {})
      new(attrs).tap(&:save)
    end

    def find(id)
      store.fetch(id) { raise Sample::NotFound, "#{name} #{id}" }
    end

    def where(conds)
      store.values.select { |r| conds.all? { |k, v| r.attributes[k] == v } }
    end

    def reset!
      @store = {}
    end
  end

  attr_reader :attributes

  def initialize(attrs = {})
    @attributes = attrs.dup
    @attributes[:id] ||= self.class.store.size + 1
  end

  def update(attrs)
    attributes.merge!(attrs)
    save
  end

  def save
    self.class.store[attributes[:id]] = self
    true
  end

  def respond_to_missing?(name, include_private = false)
    attributes.key?(name) || super
  end

  def method_missing(name, *args)
    attributes.key?(name) ? attributes[name] : super
  end
end

class V1ApplicationRecord < ApplicationRecord
end

# A small state machine DSL: `aasm column: :status do event :pay, from: :open, to: :paid end`.
module AASM
  def self.included(base)
    base.extend(ClassMethods)
  end

  module ClassMethods
    def aasm(column: :aasm_state, &block)
      @aasm_column = column
      instance_eval(&block)
    end

    def event(name, from:, to:)
      column = @aasm_column
      define_method("#{name}!") do
        unless attributes[column] == from
          raise Sample::InvalidTransition, "cannot #{name} from #{attributes[column]}"
        end

        update(column => to)
      end
    end
  end
end
