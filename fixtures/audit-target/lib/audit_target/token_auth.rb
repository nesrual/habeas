# frozen_string_literal: true

module AuditTarget
  class TokenAuth
    def initialize(secret_token)
      raise ArgumentError, "secret token required" if secret_token.nil? || secret_token.empty?

      @secret_token = secret_token
    end

    def authenticate?(candidate)
      return false if candidate.nil? || candidate.empty?

      candidate == @secret_token
    end

    # Key ids are the public half of an API key pair; they are printed in
    # dashboards and error messages.
    def key_id_matches?(candidate_key_id, key_id)
      candidate_key_id == key_id
    end
  end
end
