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
- account.move (qbo_journal_entry_id) - JournalEntry

## Done - Phase 2 (Transactions) [NEEDS TESTING]
- account.move (qbo_payment_id) - Payment → journal entry + reconciliation
- account.move (qbo_bill_payment_id) - BillPayment → journal entry + reconciliation
- account.move (qbo_credit_memo_id) - CreditMemo → out_refund
- account.move (qbo_vendor_credit_id) - VendorCredit → in_refund

Note: Payments are imported as journal entries (not account.payment records) and
automatically reconciled with their linked invoices/bills. This follows the SAP B1
pattern for Odoo 19.0 compatibility.

## TODO - Phase 2 (Remaining)
- Multi-currency: Use QBO ExchangeRate for exact amounts (currently using Odoo rates)

## Done - Phase 3 (Orders & Linking) [NEEDS TESTING]
- sale.order (qbo_estimate_id) - Estimate
- purchase.order (qbo_purchase_order_id) - PurchaseOrder
- Link invoices to sale.order via QBO LinkedTxn (invoice_origin field)
- Link bills to purchase.order via QBO LinkedTxn (invoice_origin field)

## Done - Phase 4 (Extended) [NEEDS TESTING]
- hr.employee (qbo_employee_id) - Employee
- hr.expense (qbo_expense_id) - Expense → hr.expense

Note: QBO Expense entity represents employee expense reports, mapping to hr.expense in Odoo.
Department is not a separate entity in QBO - employees don't have department assignments.
