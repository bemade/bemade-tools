"""QuickBooks Online Payment Reconciliation Pipeline

This module reconciles imported payments with their corresponding
invoices/bills using QBO LinkedTxn data.
"""

import logging
from typing import Dict, List

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.payment",
    importer_name="qbo.payment.reconciler",
    sap_source="Payment",
    depends_on=["qbo.payment.importer"],
)
class QboPaymentReconciler(models.AbstractModel):
    """Post-processing pipeline to reconcile payments with invoices/bills."""

    _name = "qbo.payment.reconciler"
    _description = "QBO Payment Reconciler"

    @ETL.extract("Payment")
    def extract_unreconciled_payments(self, ctx: ETLContext) -> List[Dict]:
        """Find payments that need reconciliation and fetch their QBO data."""
        api_client = ctx.get_config("api_client")
        if not api_client:
            raise ValueError("API client not found in ETL context")

        company = ctx.env.company

        # Find customer payments that are not reconciled
        # Include 'in_process' state as payments may be waiting for bank reconciliation
        customer_payments = ctx.env["account.payment"].search(
            [
                ("qbo_payment_id", "!=", False),
                ("state", "in", ["posted", "in_process"]),
                ("is_reconciled", "=", False),
                ("company_id", "=", company.id),
            ]
        )

        # Find bill payments that are not reconciled
        bill_payments = ctx.env["account.payment"].search(
            [
                ("qbo_bill_payment_id", "!=", False),
                ("state", "in", ["posted", "in_process"]),
                ("is_reconciled", "=", False),
                ("company_id", "=", company.id),
            ]
        )

        _logger.info(
            f"Found {len(customer_payments)} unreconciled customer payments, "
            f"{len(bill_payments)} unreconciled bill payments"
        )

        # Batch fetch QBO data for payments
        payment_data = []

        # Build maps of QBO ID -> Odoo payment ID
        customer_qbo_ids = {p.qbo_payment_id: p.id for p in customer_payments}
        vendor_qbo_ids = {p.qbo_bill_payment_id: p.id for p in bill_payments}

        # Batch fetch customer payments from QBO
        if customer_qbo_ids:
            qbo_payments = api_client.query_all(entity="Payment", order_by="Id")
            for qbo_pmt in qbo_payments:
                qbo_id = int(qbo_pmt.get("Id", 0))
                if qbo_id in customer_qbo_ids:
                    payment_data.append(
                        {
                            "odoo_payment_id": customer_qbo_ids[qbo_id],
                            "qbo_data": qbo_pmt,
                            "payment_type": "customer",
                        }
                    )

        # Batch fetch bill payments from QBO
        if vendor_qbo_ids:
            qbo_bill_payments = api_client.query_all(
                entity="BillPayment", order_by="Id"
            )
            for qbo_pmt in qbo_bill_payments:
                qbo_id = int(qbo_pmt.get("Id", 0))
                if qbo_id in vendor_qbo_ids:
                    payment_data.append(
                        {
                            "odoo_payment_id": vendor_qbo_ids[qbo_id],
                            "qbo_data": qbo_pmt,
                            "payment_type": "vendor",
                        }
                    )

        _logger.info(f"Fetched QBO data for {len(payment_data)} payments")
        return payment_data

    @ETL.transform()
    def transform_reconciliations(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO LinkedTxn data into reconciliation instructions."""
        payment_data = extracted.get("extract_unreconciled_payments", [])

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

        reconcile_instructions = []

        for pmt in payment_data:
            odoo_payment_id = pmt["odoo_payment_id"]
            qbo_data = pmt["qbo_data"]
            payment_type = pmt["payment_type"]

            # Get LinkedTxn from QBO payment lines
            lines = qbo_data.get("Line", [])
            linked_move_ids = []

            _logger.debug(f"Payment {qbo_data.get('Id')} has {len(lines)} lines")

            for line in lines:
                linked_txns = line.get("LinkedTxn", [])
                _logger.debug(f"  Line has {len(linked_txns)} LinkedTxn entries")
                for linked in linked_txns:
                    txn_id = str(linked.get("TxnId", ""))
                    txn_type = linked.get("TxnType", "")
                    _logger.debug(f"    LinkedTxn: {txn_type} {txn_id}")

                    if txn_type == "Invoice" and txn_id in invoice_map:
                        linked_move_ids.append(invoice_map[txn_id])
                        _logger.debug(
                            f"      -> Found invoice in Odoo: {invoice_map[txn_id]}"
                        )
                    elif txn_type == "Bill" and txn_id in bill_map:
                        linked_move_ids.append(bill_map[txn_id])
                        _logger.debug(
                            f"      -> Found bill in Odoo: {bill_map[txn_id]}"
                        )
                    elif txn_type in ("Invoice", "Bill"):
                        _logger.debug(f"      -> NOT found in Odoo maps")

            if linked_move_ids:
                reconcile_instructions.append(
                    {
                        "payment_id": odoo_payment_id,
                        "move_ids": linked_move_ids,
                        "payment_type": payment_type,
                    }
                )
            else:
                _logger.info(
                    f"Payment {qbo_data.get('Id')} has no linked invoices/bills in Odoo (lines: {len(lines)})"
                )

        _logger.info(
            f"Prepared {len(reconcile_instructions)} payments for reconciliation "
            f"(invoice_map has {len(invoice_map)} entries, bill_map has {len(bill_map)} entries)"
        )
        return reconcile_instructions

    @ETL.load()
    def load_reconciliations(self, ctx: ETLContext, transformed: Dict) -> None:
        """Reconcile payments with invoices/bills by creating journal entries.

        Similar to SAP approach - create a journal entry that offsets the
        invoice receivable/payable, then reconcile the lines.
        """
        instructions = transformed.get("transform_reconciliations", [])

        if not instructions:
            _logger.info("No payments to reconcile")
            return

        company = ctx.env.company

        # Find a misc journal for reconciliation entries
        journal = ctx.env["account.journal"].search(
            [("type", "=", "general"), ("company_id", "=", company.id)],
            limit=1,
        )
        if not journal:
            _logger.warning("No general journal found for reconciliation entries")
            return

        # Find bank account for offset
        bank_account = ctx.env["account.account"].search(
            [("account_type", "=", "asset_cash"), ("company_ids", "in", [company.id])],
            limit=1,
        )
        if not bank_account:
            _logger.warning("No bank account found for reconciliation entries")
            return

        reconciled = 0

        for instr in instructions:
            payment = ctx.env["account.payment"].browse(instr["payment_id"])
            move_ids = instr["move_ids"]

            # Get the invoices/bills to reconcile with
            invoices = ctx.env["account.move"].browse(move_ids)

            for invoice in invoices:
                # Find the receivable/payable line on the invoice
                invoice_line = invoice.line_ids.filtered(
                    lambda l: l.account_id.account_type
                    in ("asset_receivable", "liability_payable")
                    and not l.reconciled
                )

                if not invoice_line:
                    continue

                # Determine amount and direction
                amount = abs(invoice_line.amount_residual)
                if amount <= 0:
                    continue

                # For customer invoice: credit receivable, debit bank
                # For vendor bill: debit payable, credit bank
                if invoice_line.account_id.account_type == "asset_receivable":
                    recv_debit, recv_credit = 0, amount
                    bank_debit, bank_credit = amount, 0
                else:  # liability_payable
                    recv_debit, recv_credit = amount, 0
                    bank_debit, bank_credit = 0, amount

                # Create reconciliation journal entry
                je_vals = {
                    "journal_id": journal.id,
                    "date": payment.date or invoice.invoice_date,
                    "ref": f"QBO Payment {payment.qbo_payment_id or payment.qbo_bill_payment_id}",
                    "line_ids": [
                        (
                            0,
                            0,
                            {
                                "account_id": invoice_line.account_id.id,
                                "partner_id": invoice.partner_id.id,
                                "debit": recv_debit,
                                "credit": recv_credit,
                                "name": f"Payment for {invoice.name}",
                            },
                        ),
                        (
                            0,
                            0,
                            {
                                "account_id": bank_account.id,
                                "partner_id": invoice.partner_id.id,
                                "debit": bank_debit,
                                "credit": bank_credit,
                                "name": f"Payment for {invoice.name}",
                            },
                        ),
                    ],
                }

                je = ctx.env["account.move"].create(je_vals)
                je.action_post()

                # Reconcile the lines
                je_recv_line = je.line_ids.filtered(
                    lambda l: l.account_id.account_type
                    in ("asset_receivable", "liability_payable")
                )

                if je_recv_line and invoice_line:
                    (je_recv_line + invoice_line).reconcile()
                    reconciled += 1

        _logger.info(f"Reconciled {reconciled} invoice/payment pairs")
