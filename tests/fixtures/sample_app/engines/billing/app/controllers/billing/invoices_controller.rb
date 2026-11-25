module Billing
  class InvoicesController < ApplicationController
    def show
      invoice = Invoice.find(params[:id])
      { id: invoice.id, amount_cents: invoice.amount_cents, status: invoice.status }
    end

    def update
      invoice = authorize(Invoice.find(params[:id]), :update?)
      invoice.update(memo: params[:memo])
      { id: invoice.id, memo: invoice.memo }
    end
  end
end
