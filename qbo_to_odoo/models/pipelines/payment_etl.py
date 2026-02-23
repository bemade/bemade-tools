"""QuickBooks Online Payment ETL Pipeline

This module handles the migration of Payments and BillPayments from QBO to Odoo
using the ETL framework. Payments are always created as journal entries
(debit bank / credit receivable, or credit bank / debit payable) regardless
of whether a matching invoice/bill exists in Odoo. Reconciliation with the
original invoice/bill is attempted as a second pass.
"""

import logging
from typing import Dict, List, Optional

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext

from .utils import get_api_client

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.move",
    importer_name="qbo.payment.importer",
    sap_source="Payment",
    depends_on=[
        "qbo.invoice.importer",
        "qbo.bill.importer",
        "qbo.account.importer",
        "qbo.customer.importer",
        "qbo.vendor.importer",
    ],
)
class QboPaymentImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO Payments as journal entries."""

    _name = "qbo.payment.importer"
    _description = "QBO Payment Importer"

    @ETL.extract("Payment")
    def extract_payments(self, ctx: ETLContext) -> List[Dict]:
        """Extract payments from QBO API."""
        api_client = get_api_client(ctx)

        # Get existing QBO payment IDs from account.move
        ctx.env.cr.execute(
            "SELECT qbo_payment_id FROM account_move WHERE qbo_payment_id IS NOT NULL"
        )
        existing_payment_ids = {str(row[0]) for row in ctx.env.cr.fetchall()}

        ctx.env.cr.execute(
            "SELECT qbo_bill_payment_id FROM account_move WHERE qbo_bill_payment_id IS NOT NULL"
        )
        existing_bill_payment_ids = {str(row[0]) for row in ctx.env.cr.fetchall()}

        _logger.info(
            f"Found {len(existing_payment_ids)} existing customer payment JEs, "
            f"{len(existing_bill_payment_ids)} existing bill payment JEs in Odoo"
        )

        # Fetch customer payments from QBO
        payments = api_client.query_all(entity="Payment", order_by="Id")
        new_payments = [
            {"type": "customer", "data": p}
            for p in payments
            if str(p.get("Id")) not in existing_payment_ids
        ]

        # Fetch bill payments from QBO
        bill_payments = api_client.query_all(entity="BillPayment", order_by="Id")
        new_bill_payments = [
            {"type": "vendor", "data": bp}
            for bp in bill_payments
            if str(bp.get("Id")) not in existing_bill_payment_ids
        ]

        _logger.info(
            f"Extracted {len(payments)} customer payments, {len(new_payments)} new; "
            f"{len(bill_payments)} bill payments, {len(new_bill_payments)} new"
        )

        all_payments = new_payments + new_bill_payments
        return all_payments

    @ETL.transform()
    def transform_payments(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO payments into journal entry values.

        Every payment produces a JE regardless of whether a linked
        invoice/bill exists. Linked move IDs are stored for reconciliation.
        """
        all_payments = extracted.get("extract_payments", [])

        # Build lookups
        ctx.env.cr.execute(
            "SELECT qbo_id, id FROM account_account WHERE qbo_id IS NOT NULL"
        )
        account_map = {str(row[0]): row[1] for row in ctx.env.cr.fetchall()}

        ctx.env.cr.execute(
            "SELECT qbo_customer_id, id FROM res_partner "
            "WHERE qbo_customer_id IS NOT NULL"
        )
        customer_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        ctx.env.cr.execute(
            "SELECT qbo_vendor_id, id FROM res_partner "
            "WHERE qbo_vendor_id IS NOT NULL"
        )
        vendor_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        ctx.env.cr.execute(
            "SELECT qbo_invoice_id, id FROM account_move "
            "WHERE qbo_invoice_id IS NOT NULL AND state = 'posted'"
        )
        invoice_map = {str(row[0]): row[1] for row in ctx.env.cr.fetchall()}

        ctx.env.cr.execute(
            "SELECT qbo_bill_id, id FROM account_move "
            "WHERE qbo_bill_id IS NOT NULL AND state = 'posted'"
        )
        bill_map = {str(row[0]): row[1] for row in ctx.env.cr.fetchall()}

        company = ctx.env.company
        payment_data = []
        skipped = 0

        for pmt in all_payments:
            pmt_type = pmt["type"]
            data = pmt["data"]

            if pmt_type == "customer":
                result = self._transform_customer_payment(
                    data,
                    customer_map,
                    invoice_map,
                    account_map,
                    company,
                    ctx,
                )
            else:
                result = self._transform_bill_payment(
                    data,
                    vendor_map,
                    bill_map,
                    account_map,
                    company,
                    ctx,
                )

            if result:
                payment_data.append(result)
            else:
                skipped += 1

        _logger.info(f"Transformed {len(payment_data)} payments, skipped {skipped}")
        return payment_data

    def _transform_customer_payment(
        self,
        payment: Dict,
        customer_map: Dict,
        invoice_map: Dict,
        account_map: Dict,
        company,
        ctx: ETLContext,
    ) -> Optional[Dict]:
        """Transform a customer payment into a single journal entry."""
        customer_ref = payment.get("CustomerRef", {})
        qbo_customer_id = int(customer_ref.get("value", 0))
        partner_id = customer_map.get(qbo_customer_id)

        if not partner_id:
            _logger.warning(
                f"Customer not found for QBO ID {qbo_customer_id} "
                f"in payment {payment.get('Id')}"
            )
            return None

        txn_date = payment.get("TxnDate")
        total_amt = float(payment.get("TotalAmt", 0) or 0)
        if total_amt <= 0:
            _logger.debug(
                f"Customer payment {payment.get('Id')} has TotalAmt={total_amt}, skipping"
            )
            return None

        qbo_payment_id = int(payment.get("Id", 0))
        payment_ref = payment.get("PaymentRefNum", "") or f"QBO-{qbo_payment_id}"

        # Get bank account
        result = self._get_bank_account_from_payment(payment, account_map, ctx)
        if not result:
            _logger.warning(
                f"No valid bank account found for payment {qbo_payment_id}, skipping"
            )
            return None
        bank_account_id, journal_id = result

        # Get receivable account: prefer linked invoice, fall back to partner default
        recv_account_id = None
        linked_move_ids = []

        for line in payment.get("Line", []):
            for linked in line.get("LinkedTxn", []):
                txn_id = str(linked.get("TxnId", ""))
                txn_type = linked.get("TxnType", "")
                if txn_type == "Invoice" and txn_id in invoice_map:
                    linked_move_ids.append(invoice_map[txn_id])

        if linked_move_ids:
            # Use the receivable account from the first linked invoice
            move = ctx.env["account.move"].browse(linked_move_ids[0])
            recv_line = move.line_ids.filtered(
                lambda l: l.account_id.account_type == "asset_receivable"
            )
            if recv_line:
                recv_account_id = recv_line[0].account_id.id

        if not recv_account_id:
            # Fall back to partner's default receivable
            partner = ctx.env["res.partner"].browse(partner_id)
            recv_account_id = partner.property_account_receivable_id.id

        if not recv_account_id:
            _logger.warning(
                f"No receivable account for payment {qbo_payment_id}, skipping"
            )
            return None

        # Handle currency
        currency_code = payment.get("CurrencyRef", {}).get("value", "CAD")
        exchange_rate = float(payment.get("ExchangeRate", 1.0) or 1.0)
        currency = ctx.env["res.currency"].search(
            [("name", "=", currency_code)], limit=1
        )
        if not currency:
            currency = company.currency_id
        is_foreign = currency.id != company.currency_id.id

        if is_foreign and exchange_rate:
            amount_company = round(total_amt * exchange_rate, 2)
        else:
            amount_company = total_amt

        # Customer payment: debit bank, credit receivable
        recv_line = {
            "account_id": recv_account_id,
            "partner_id": partner_id,
            "debit": 0,
            "credit": amount_company,
            "name": payment_ref,
        }
        bank_line = {
            "account_id": bank_account_id,
            "partner_id": partner_id,
            "debit": amount_company,
            "credit": 0,
            "name": payment_ref,
        }

        if is_foreign:
            recv_line["currency_id"] = currency.id
            recv_line["amount_currency"] = -total_amt
            bank_line["currency_id"] = currency.id
            bank_line["amount_currency"] = total_amt

        return {
            "je_vals": {
                "move_type": "entry",
                "journal_id": journal_id,
                "date": txn_date,
                "ref": payment_ref,
                "partner_id": partner_id,
                "qbo_payment_id": qbo_payment_id,
                "qbo_bill_payment_id": None,
                "currency_id": currency.id,
                "line_ids": [(0, 0, recv_line), (0, 0, bank_line)],
            },
            "linked_move_ids": linked_move_ids,
            "is_customer": True,
        }

    def _get_bank_account_from_payment(
        self, payment: Dict, account_map: Dict, ctx: ETLContext
    ) -> Optional[tuple[int, int]]:
        """Extract account and journal IDs from QBO payment data.

        QBO customer Payment uses 'DepositToAccountRef' at the top level.
        QBO BillPayment uses 'CheckPayment.BankAccountRef' for cheque
        payments or 'CreditCardPayment.CCAccountRef' for credit card
        payments. When none of these are present, the payment goes to
        Undeposited Funds.
        """
        # Try DepositToAccountRef (customer payments)
        account_ref = payment.get("DepositToAccountRef", {})

        # Try CheckPayment.BankAccountRef (bill payments by cheque)
        if not account_ref or not account_ref.get("value"):
            check_payment = payment.get("CheckPayment", {})
            if check_payment:
                account_ref = check_payment.get("BankAccountRef", {})

        # Try CreditCardPayment.CCAccountRef (bill payments by credit card)
        if not account_ref or not account_ref.get("value"):
            cc_payment = payment.get("CreditCardPayment", {})
            if cc_payment:
                account_ref = cc_payment.get("CCAccountRef", {})

        if not account_ref or not account_ref.get("value"):
            # No account ref found — fall back to Undeposited Funds
            undeposited = ctx.env["account.account"].search(
                [
                    ("name", "ilike", "Undeposited Funds"),
                    ("company_ids", "in", [ctx.env.company.id]),
                ],
                limit=1,
            )
            if undeposited:
                _logger.debug(
                    f"Payment {payment.get('Id')} has no account ref, "
                    f"using Undeposited Funds ({undeposited.code})"
                )
                account_id = undeposited.id
            else:
                _logger.warning(
                    f"No account reference found in payment {payment.get('Id')} "
                    f"and no 'Undeposited Funds' account in Odoo"
                )
                return None
        else:
            qbo_account_id = str(account_ref.get("value", 0))
            account_id = account_map.get(qbo_account_id)
            if not account_id:
                _logger.warning(
                    f"Account with QBO ID {qbo_account_id} not found in Odoo "
                    f"for payment {payment.get('Id')}"
                )
                return None

        company = ctx.env.company

        # Prefer the journal whose default_account_id matches the resolved account
        # (covers bank and cash journals created from QBO bank accounts).
        journal = ctx.env["account.journal"].search(
            [
                ("default_account_id", "=", account_id),
                ("company_id", "=", company.id),
            ],
            limit=1,
        )

        if not journal:
            # Fall back to the general journal for accounts not linked to any
            # journal (e.g. Undeposited Funds).
            journal = ctx.env["account.journal"].search(
                [("type", "=", "general"), ("company_id", "=", company.id)],
                limit=1,
            )

        if not journal:
            _logger.warning(f"No journal found for payment {payment.get('Id')}")
            return None

        return account_id, journal.id

    def _transform_bill_payment(
        self,
        bp: Dict,
        vendor_map: Dict,
        bill_map: Dict,
        account_map: Dict,
        company,
        ctx: ETLContext,
    ) -> Optional[Dict]:
        """Transform a bill payment into a single journal entry."""
        vendor_ref = bp.get("VendorRef", {})
        qbo_vendor_id = int(vendor_ref.get("value", 0))
        partner_id = vendor_map.get(qbo_vendor_id)

        if not partner_id:
            _logger.warning(
                f"Vendor not found for QBO ID {qbo_vendor_id} "
                f"in bill payment {bp.get('Id')}"
            )
            return None

        txn_date = bp.get("TxnDate")
        total_amt = float(bp.get("TotalAmt", 0) or 0)
        if total_amt <= 0:
            _logger.debug(
                f"Bill payment {bp.get('Id')} has TotalAmt={total_amt}, skipping"
            )
            return None

        qbo_bill_payment_id = int(bp.get("Id", 0))
        payment_ref = bp.get("DocNumber", "") or f"QBO-BP-{qbo_bill_payment_id}"

        result = self._get_bank_account_from_payment(bp, account_map, ctx)
        if not result:
            _logger.warning(
                f"No valid bank account for bill payment {qbo_bill_payment_id}, skipping"
            )
            return None
        bank_account_id, journal_id = result

        # Get payable account: prefer linked bill, fall back to partner default
        payable_account_id = None
        linked_move_ids = []

        for line in bp.get("Line", []):
            for linked in line.get("LinkedTxn", []):
                txn_id = str(linked.get("TxnId", ""))
                txn_type = linked.get("TxnType", "")
                if txn_type == "Bill" and txn_id in bill_map:
                    linked_move_ids.append(bill_map[txn_id])

        if linked_move_ids:
            move = ctx.env["account.move"].browse(linked_move_ids[0])
            pay_line = move.line_ids.filtered(
                lambda l: l.account_id.account_type == "liability_payable"
            )
            if pay_line:
                payable_account_id = pay_line[0].account_id.id

        if not payable_account_id:
            partner = ctx.env["res.partner"].browse(partner_id)
            payable_account_id = partner.property_account_payable_id.id

        if not payable_account_id:
            _logger.warning(
                f"No payable account for bill payment {qbo_bill_payment_id}, skipping"
            )
            return None

        # Handle currency
        currency_code = bp.get("CurrencyRef", {}).get("value", "CAD")
        exchange_rate = float(bp.get("ExchangeRate", 1.0) or 1.0)
        currency = ctx.env["res.currency"].search(
            [("name", "=", currency_code)], limit=1
        )
        if not currency:
            currency = company.currency_id
        is_foreign = currency.id != company.currency_id.id

        if is_foreign and exchange_rate:
            amount_company = round(total_amt * exchange_rate, 2)
        else:
            amount_company = total_amt

        # Vendor payment: debit payable, credit bank
        payable_line = {
            "account_id": payable_account_id,
            "partner_id": partner_id,
            "debit": amount_company,
            "credit": 0,
            "name": payment_ref,
        }
        bank_line = {
            "account_id": bank_account_id,
            "partner_id": partner_id,
            "debit": 0,
            "credit": amount_company,
            "name": payment_ref,
        }

        if is_foreign:
            payable_line["currency_id"] = currency.id
            payable_line["amount_currency"] = total_amt
            bank_line["currency_id"] = currency.id
            bank_line["amount_currency"] = -total_amt

        return {
            "je_vals": {
                "move_type": "entry",
                "journal_id": journal_id,
                "date": txn_date,
                "ref": payment_ref,
                "partner_id": partner_id,
                "qbo_payment_id": None,
                "qbo_bill_payment_id": qbo_bill_payment_id,
                "currency_id": currency.id,
                "line_ids": [(0, 0, payable_line), (0, 0, bank_line)],
            },
            "linked_move_ids": linked_move_ids,
            "is_customer": False,
        }

    @ETL.load()
    def load_payments(self, ctx: ETLContext, transformed: Dict) -> None:
        """Create payment JEs and attempt reconciliation with invoices/bills."""
        payment_data = transformed.get("transform_payments", [])

        if not payment_data:
            _logger.info("No payments to process")
            return

        ctx.env.invalidate_all()

        # Phase 1: Create and post all payment JEs
        created = 0
        posted = 0
        reconciliation_queue = []  # (payment_move, linked_move_ids, is_customer)

        for pmt in payment_data:
            je_vals = pmt["je_vals"]
            qbo_id = (
                je_vals.get("qbo_payment_id")
                or je_vals.get("qbo_bill_payment_id")
                or "?"
            )
            with ctx.skippable(f"payment QBO#{qbo_id}"):
                move = ctx.env["account.move"].create(je_vals)
                created += 1
                move.action_post()
                posted += 1
                if pmt["linked_move_ids"]:
                    reconciliation_queue.append(
                        (move, pmt["linked_move_ids"], pmt["is_customer"])
                    )

        _logger.info(f"Created {created} payment JEs ({posted} posted)")

        # Phase 2: Attempt reconciliation
        reconciled = 0
        for payment_move, linked_move_ids, is_customer in reconciliation_queue:
            account_type = "asset_receivable" if is_customer else "liability_payable"
            payment_line = payment_move.line_ids.filtered(
                lambda l, at=account_type: l.account_id.account_type == at
            )
            if not payment_line:
                continue

            # Reconciliation requires lines on the same account_id.
            pay_account_id = payment_line[0].account_id.id

            for linked_id in linked_move_ids:
                with ctx.skippable(
                    f"reconcile {payment_move.name} <-> move#{linked_id}"
                ):
                    # Re-filter each iteration; prior reconcile may have
                    # consumed the payment line already.
                    pay_lines_open = payment_line.filtered(lambda l: not l.reconciled)
                    if not pay_lines_open:
                        break
                    original_move = ctx.env["account.move"].browse(linked_id)
                    line_to_reconcile = original_move.line_ids.filtered(
                        lambda l, aid=pay_account_id: (
                            l.account_id.id == aid and not l.reconciled
                        )
                    )
                    if line_to_reconcile:
                        (line_to_reconcile + pay_lines_open).reconcile()
                        reconciled += 1

        _logger.info(f"Reconciled {reconciled} payment/invoice pairs")
