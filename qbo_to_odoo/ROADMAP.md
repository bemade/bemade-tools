# QBO to Odoo - Roadmap

**Goal: "Import All" button should do everything hands-off**

## Done - Phase 1 (Master Data)
- account.account (qbo_id)
- res.partner (qbo_customer_id, qbo_vendor_id)
- product.product (qbo_item_id)
- product.category (qbo_category_id)
- Auto-detect AR/AP from Invoices/Bills → ir.default

## Done - Phase 2 (Transactions)
- account.payment.term (qbo_term_id) - Term
- account.tax (qbo_tax_id, qbo_tax_rate_id) - TaxCode, TaxRate
- account.move (qbo_invoice_id) - Invoice → out_invoice
- account.move (qbo_bill_id) - Bill → in_invoice
- account.payment (qbo_payment_id) - Payment
- account.payment (qbo_bill_payment_id) - BillPayment
- account.move (qbo_journal_entry_id) - JournalEntry

## TODO - Phase 2 (Remaining)
- CreditMemo → account.move (out_refund) - separate entity in QBO
- VendorCredit → account.move (in_refund) - separate entity in QBO
- Multi-currency: Use QBO ExchangeRate for exact amounts (currently using Odoo rates)
- Payment reconciliation: Match payments to invoices/bills using QBO Line references

## TODO - Phase 3 (Extended)
- hr.employee (qbo_employee_id) - Employee
- hr.department - Department
- account.analytic.account - Class
- sale.order - Estimate
- purchase.order - PurchaseOrder
- account.analytic.line - TimeActivity

## TODO - Phase 4 (Advanced)
- Incremental sync (SyncToken)
- Webhook support
- Multi-company
- Export (Odoo → QBO)
