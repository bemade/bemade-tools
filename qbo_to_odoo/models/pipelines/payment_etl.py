"""QuickBooks Online Payment ETL Pipeline

This module handles the migration of Payments and BillPayments from QBO to Odoo
using the ETL framework. Payments are created as journal entries (not account.payment
records) and reconciled with their corresponding invoices/bills.

This approach follows the SAP B1 pattern for Odoo 19.0 compatibility.
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext

from .utils import get_api_client

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.move",
    importer_name="qbo.payment.importer",
    sap_source="Payment",
    depends_on=["qbo.invoice.importer", "qbo.bill.importer"],
)
class QboPaymentImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO Payments as journal entries."""

    _name = "qbo.payment.importer"
    _description = "QBO Payment Importer"

    # Class-level cache for lookups
    _lookup_cache = {}

    @ETL.extract("Payment")
    def extract_payments(self, ctx: ETLContext) -> List[Dict]:
        """Extract payments from QBO API with linked invoice/bill data."""
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
            f"Found {len(existing_payment_ids)} existing customer payment JEs in Odoo"
        )
        _logger.info(
            f"Found {len(existing_bill_payment_ids)} existing bill payment JEs in Odoo"
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
            f"Extracted {len(payments)} customer payments, {len(new_payments)} are new"
        )
        _logger.info(
            f"Extracted {len(bill_payments)} bill payments, {len(new_bill_payments)} are new"
        )

        # Build invoice/bill lookup maps
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

        # Get payment journal
        company = ctx.env.company
        payment_journal = ctx.env["account.journal"].search(
            [("type", "=", "general"), ("company_id", "=", company.id)],
            limit=1,
        )
        if not payment_journal:
            raise ValueError("No general journal found for payment entries")

        # Get bank account for offset
        bank_account = ctx.env["account.account"].search(
            [("account_type", "=", "asset_cash"), ("company_ids", "in", [company.id])],
            limit=1,
        )
        if not bank_account:
            raise ValueError("No bank account found for payment entries")

        # Store in class-level cache
        QboPaymentImporter._lookup_cache = {
            "invoice_map": invoice_map,
            "bill_map": bill_map,
            "journal_id": payment_journal.id,
            "bank_account_id": bank_account.id,
        }

        # Combine into single list for proper chunking
        all_payments = new_payments + new_bill_payments
        return all_payments

    @ETL.transform()
    def transform_payments(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO payments into journal entry values with reconciliation data."""
        all_payments = extracted.get("extract_payments", [])

        cache = QboPaymentImporter._lookup_cache
        invoice_map = cache.get("invoice_map", {})
        bill_map = cache.get("bill_map", {})
        journal_id = cache.get("journal_id")
        bank_account_id = cache.get("bank_account_id")

        if not journal_id or not bank_account_id:
            _logger.warning(
                "Missing journal or bank account in cache, skipping transform"
            )
            return []

        # Build partner lookups
        ctx.env.cr.execute(
            "SELECT qbo_customer_id, id FROM res_partner WHERE qbo_customer_id IS NOT NULL"
        )
        customer_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        ctx.env.cr.execute(
            "SELECT qbo_vendor_id, id FROM res_partner WHERE qbo_vendor_id IS NOT NULL"
        )
        vendor_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

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
                    journal_id,
                    bank_account_id,
                    company,
                    ctx,
                )
            else:
                result = self._transform_bill_payment(
                    data,
                    vendor_map,
                    bill_map,
                    journal_id,
                    bank_account_id,
                    company,
                    ctx,
                )

            if result:
                payment_data.extend(result)
            else:
                skipped += 1

        _logger.info(
            f"Transformed {len(payment_data)} payment allocations, skipped {skipped} payments"
        )
        return payment_data

    def _transform_customer_payment(
        self,
        payment: Dict,
        customer_map: Dict,
        invoice_map: Dict,
        journal_id: int,
        bank_account_id: int,
        company,
        ctx: ETLContext,
    ) -> Optional[List[Dict]]:
        """Transform a customer payment into journal entry values per linked invoice."""
        # Get customer
        customer_ref = payment.get("CustomerRef", {})
        qbo_customer_id = int(customer_ref.get("value", 0))
        partner_id = customer_map.get(qbo_customer_id)

        if not partner_id:
            _logger.warning(
                f"Customer not found for QBO ID {qbo_customer_id} "
                f"in payment {payment.get('Id')}"
            )
            return None

        # Parse date
        txn_date = payment.get("TxnDate")
        payment_date = None
        if txn_date:
            try:
                payment_date = datetime.strptime(txn_date, "%Y-%m-%d").date()
            except ValueError:
                payment_date = datetime.now().date()

        qbo_payment_id = int(payment.get("Id", 0))
        payment_ref = payment.get("PaymentRefNum", "") or f"QBO-{qbo_payment_id}"

        # Get linked invoices from payment lines
        lines = payment.get("Line", [])
        allocations = []

        for line in lines:
            linked_txns = line.get("LinkedTxn", [])
            line_amount = float(line.get("Amount", 0) or 0)

            for linked in linked_txns:
                txn_id = str(linked.get("TxnId", ""))
                txn_type = linked.get("TxnType", "")

                if txn_type == "Invoice" and txn_id in invoice_map:
                    allocations.append(
                        {
                            "move_id": invoice_map[txn_id],
                            "amount": line_amount,
                            "payment_date": payment_date,
                            "payment_ref": payment_ref,
                            "partner_id": partner_id,
                            "journal_id": journal_id,
                            "bank_account_id": bank_account_id,
                            "qbo_payment_id": qbo_payment_id,
                            "qbo_bill_payment_id": None,
                            "is_customer": True,
                        }
                    )

        if not allocations:
            _logger.debug(f"Payment {qbo_payment_id} has no linked invoices in Odoo")

        return allocations if allocations else None

    def _transform_bill_payment(
        self,
        bp: Dict,
        vendor_map: Dict,
        bill_map: Dict,
        journal_id: int,
        bank_account_id: int,
        company,
        ctx: ETLContext,
    ) -> Optional[List[Dict]]:
        """Transform a bill payment into journal entry values per linked bill."""
        # Get vendor
        vendor_ref = bp.get("VendorRef", {})
        qbo_vendor_id = int(vendor_ref.get("value", 0))
        partner_id = vendor_map.get(qbo_vendor_id)

        if not partner_id:
            _logger.warning(
                f"Vendor not found for QBO ID {qbo_vendor_id} "
                f"in bill payment {bp.get('Id')}"
            )
            return None

        # Parse date
        txn_date = bp.get("TxnDate")
        payment_date = None
        if txn_date:
            try:
                payment_date = datetime.strptime(txn_date, "%Y-%m-%d").date()
            except ValueError:
                payment_date = datetime.now().date()

        qbo_bill_payment_id = int(bp.get("Id", 0))
        payment_ref = bp.get("DocNumber", "") or f"QBO-BP-{qbo_bill_payment_id}"

        # Get linked bills from payment lines
        lines = bp.get("Line", [])
        allocations = []

        for line in lines:
            linked_txns = line.get("LinkedTxn", [])
            line_amount = float(line.get("Amount", 0) or 0)

            for linked in linked_txns:
                txn_id = str(linked.get("TxnId", ""))
                txn_type = linked.get("TxnType", "")

                if txn_type == "Bill" and txn_id in bill_map:
                    allocations.append(
                        {
                            "move_id": bill_map[txn_id],
                            "amount": line_amount,
                            "payment_date": payment_date,
                            "payment_ref": payment_ref,
                            "partner_id": partner_id,
                            "journal_id": journal_id,
                            "bank_account_id": bank_account_id,
                            "qbo_payment_id": None,
                            "qbo_bill_payment_id": qbo_bill_payment_id,
                            "is_customer": False,
                        }
                    )

        if not allocations:
            _logger.debug(
                f"Bill payment {qbo_bill_payment_id} has no linked bills in Odoo"
            )

        return allocations if allocations else None

    @ETL.load()
    def load_payments(self, ctx: ETLContext, transformed: Dict) -> None:
        """Create journal entries and reconcile with invoices/bills."""
        allocations = transformed.get("transform_payments", [])

        if not allocations:
            _logger.info("No payment allocations to process")
            return

        # Invalidate cache to ensure fresh data from DB
        ctx.env.invalidate_all()

        # Batch fetch all moves needed
        move_ids = list({a["move_id"] for a in allocations})
        moves = ctx.env["account.move"].browse(move_ids)
        moves_by_id = {m.id: m for m in moves}

        # Phase 1: Prepare all journal entry values
        je_vals_list = []
        reconciliation_pairs = []  # (invoice_line, je_vals_index)

        for alloc in allocations:
            move = moves_by_id.get(alloc["move_id"])
            if not move:
                continue

            amount = alloc["amount"]
            if amount <= 0:
                continue

            # Find the receivable/payable line on the invoice/bill
            line_to_reconcile = move.line_ids.filtered(
                lambda l: l.account_id.account_type
                in ("asset_receivable", "liability_payable")
                and not l.reconciled
            )

            if not line_to_reconcile:
                _logger.debug(f"No unreconciled line found on move {move.name}")
                continue

            # Determine debit/credit based on document type
            if alloc["is_customer"]:
                # Customer payment: credit receivable, debit bank
                recv_debit, recv_credit = 0, amount
                bank_debit, bank_credit = amount, 0
            else:
                # Vendor payment: debit payable, credit bank
                recv_debit, recv_credit = amount, 0
                bank_debit, bank_credit = 0, amount

            je_vals = {
                "journal_id": alloc["journal_id"],
                "date": alloc["payment_date"],
                "ref": alloc["payment_ref"],
                "qbo_payment_id": alloc["qbo_payment_id"],
                "qbo_bill_payment_id": alloc["qbo_bill_payment_id"],
                "line_ids": [
                    (
                        0,
                        0,
                        {
                            "account_id": line_to_reconcile[0].account_id.id,
                            "partner_id": alloc["partner_id"],
                            "debit": recv_debit,
                            "credit": recv_credit,
                            "name": f"Payment for {move.name}",
                        },
                    ),
                    (
                        0,
                        0,
                        {
                            "account_id": alloc["bank_account_id"],
                            "partner_id": alloc["partner_id"],
                            "debit": bank_debit,
                            "credit": bank_credit,
                            "name": f"Payment for {move.name}",
                        },
                    ),
                ],
            }

            je_vals_list.append(je_vals)
            # Store move_id instead of recordset - we'll re-fetch fresh before reconciling
            reconciliation_pairs.append((move.id, len(je_vals_list) - 1))

        if not je_vals_list:
            _logger.info("No valid payment journal entries to create")
            return

        # Phase 2: Batch create all journal entries
        _logger.info(f"Batch creating {len(je_vals_list)} payment journal entries")
        payment_moves = ctx.env["account.move"].create(je_vals_list)

        # Phase 3: Batch post all journal entries
        _logger.info(f"Batch posting {len(payment_moves)} payment journal entries")
        payment_moves.action_post()

        # Phase 4: Reconcile each pair
        reconciled_count = 0
        for original_move_id, je_idx in reconciliation_pairs:
            payment_move = payment_moves[je_idx]

            # Re-fetch the invoice/bill fresh - the original check may be stale
            # if the same invoice had multiple payment allocations in this batch
            original_move = ctx.env["account.move"].browse(original_move_id)
            line_to_reconcile = original_move.line_ids.filtered(
                lambda l: l.account_id.account_type
                in ("asset_receivable", "liability_payable")
                and not l.reconciled
            )

            if not line_to_reconcile:
                _logger.debug(
                    f"No unreconciled line on {original_move.name} for {payment_move.name}"
                )
                continue

            payment_line = payment_move.line_ids.filtered(
                lambda l: l.account_id.account_type
                in ("asset_receivable", "liability_payable")
            )

            if payment_line:
                (line_to_reconcile + payment_line).reconcile()
                reconciled_count += 1

        _logger.info(
            f"Created {len(payment_moves)} payment JEs, reconciled {reconciled_count}"
        )
