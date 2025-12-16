import logging

from odoo import api, models
from odoo.addons.sap_b1_to_odoo.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.move",
    importer_name="account.payment.reconciliation",
    sap_source="oinv",  # We'll query both OINV and OPCH
    depends_on=[
        "account.move.invoice.post.processor",
        "account.move.bill.importer",
    ],
    multiprocessing_threshold=500,
    chunk_size=200,
)
class AccountPaymentReconciliation(models.AbstractModel):
    _name = "account.payment.reconciliation"
    _description = (
        "SAP Payment Reconciliation - Create Journal Entries for Paid Invoices/Bills"
    )

    # Class-level cache for multiprocessing (only primitive types!)
    _lookup_cache = {}

    @ETL.extract("oinv")
    def extract_payments(self, ctx: ETLContext):
        """Extract payments from SAP payment tables and build lookup maps."""
        _logger.info("[PaymentReconciliation] Extracting payment data from SAP...")

        # Get incoming payments (customer payments) with invoice allocations
        # rct2.baseabs links to oinv.docentry, invtype=13 is for A/R invoices
        ctx.cr.execute(
            """
            SELECT 
                p.docentry as payment_docentry,
                p.docnum as payment_docnum,
                p.docdate as payment_date,
                p.doctotal as payment_total,
                p.cashsum,
                p.trsfrsum,
                p.checksum,
                a.baseabs::integer as invoiceid,
                a.sumapplied,
                'customer' as payment_type
            FROM orct p
            JOIN rct2 a ON p.docentry = a.docentry
            WHERE a.invtype = '13' AND a.baseabs IS NOT NULL
            """
        )
        customer_payments = ctx.cr.dictfetchall()

        # Get outgoing payments (vendor payments) with bill allocations
        # vpm2.baseabs links to opch.docentry, invtype=18 is for A/P invoices (bills)
        ctx.cr.execute(
            """
            SELECT 
                p.docentry as payment_docentry,
                p.docnum as payment_docnum,
                p.docdate as payment_date,
                p.doctotal as payment_total,
                p.cashsum,
                p.trsfrsum,
                p.checksum,
                a.baseabs::integer as invoiceid,
                a.sumapplied,
                'vendor' as payment_type
            FROM ovpm p
            JOIN vpm2 a ON p.docentry = a.docnum
            WHERE a.invtype = '18' AND a.baseabs IS NOT NULL
            """
        )
        vendor_payments = ctx.cr.dictfetchall()

        _logger.info(
            f"[PaymentReconciliation] Found {len(customer_payments)} customer payment "
            f"allocations and {len(vendor_payments)} vendor payment allocations in SAP"
        )

        # Combine all payments into a single list for proper chunking
        all_payments = customer_payments + vendor_payments

        # Pre-load all invoices and bills - build ID maps (not recordsets!)
        invoice_docentries = [p["invoiceid"] for p in customer_payments]
        bill_docentries = [p["invoiceid"] for p in vendor_payments]

        # Map sap_docentry -> move_id for invoices
        invoices_map = {}
        if invoice_docentries:
            invoices = ctx.env["account.move"].search(
                [
                    ("sap_docentry", "in", invoice_docentries),
                    ("sap_table", "=", "oinv"),
                    ("state", "=", "posted"),
                ]
            )
            invoices_map = {inv.sap_docentry: inv.id for inv in invoices}

        # Map sap_docentry -> move_id for bills
        bills_map = {}
        if bill_docentries:
            bills = ctx.env["account.move"].search(
                [
                    ("sap_docentry", "in", bill_docentries),
                    ("sap_table", "=", "opch"),
                    ("state", "=", "posted"),
                ]
            )
            bills_map = {bill.sap_docentry: bill.id for bill in bills}

        _logger.info(
            f"[PaymentReconciliation] Pre-loaded {len(invoices_map)} invoices "
            f"and {len(bills_map)} bills"
        )

        # Get payment journal (created by account.journal.setup)
        payment_journal = ctx.env["account.journal"].search(
            [("code", "=", "SAPRC"), ("type", "=", "general")],
            limit=1,
        )
        if not payment_journal:
            _logger.error("[PaymentReconciliation] SAPRC journal not found!")
            return []

        # Get bank account
        bank_account = ctx.env["account.account"].search(
            [("account_type", "=", "asset_cash")],
            limit=1,
        )
        if not bank_account:
            _logger.error("[PaymentReconciliation] No bank account found!")
            return []

        # Store in class-level cache for workers
        AccountPaymentReconciliation._lookup_cache = {
            "invoices_map": invoices_map,
            "bills_map": bills_map,
            "journal_id": payment_journal.id,
            "bank_account_id": bank_account.id,
        }

        return all_payments

    @ETL.transform()
    def transform_payments(self, ctx: ETLContext, extracted):
        """Match SAP payments to Odoo move IDs and prepare reconciliation data."""
        payments = extracted.get("extract_payments", [])

        cache = AccountPaymentReconciliation._lookup_cache
        invoices_map = cache.get("invoices_map", {})
        bills_map = cache.get("bills_map", {})

        payment_data = []

        for payment in payments:
            payment_type = payment["payment_type"]

            # Look up move_id based on payment type
            if payment_type == "customer":
                move_id = invoices_map.get(payment["invoiceid"])
            else:  # vendor
                move_id = bills_map.get(payment["invoiceid"])

            if move_id:
                payment_data.append(
                    {
                        "move_id": move_id,
                        "payment_amount": float(payment["sumapplied"]),
                        "payment_date": str(payment["payment_date"]),
                        "payment_ref": f"SAP Payment {payment['payment_docnum']}",
                        "payment_type": payment_type,
                    }
                )

        _logger.info(
            f"[PaymentReconciliation] Prepared {len(payment_data)} payment reconciliations"
        )
        return payment_data

    @ETL.load()
    def load_payments(self, ctx: ETLContext, transformed):
        """Create journal entries to reconcile paid invoices/bills."""
        reconciliation_data = transformed.get("transform_payments", [])

        if not reconciliation_data:
            _logger.info("[PaymentReconciliation] No payments to reconcile in chunk")
            return

        cache = AccountPaymentReconciliation._lookup_cache
        journal_id = cache.get("journal_id")
        bank_account_id = cache.get("bank_account_id")

        if not journal_id or not bank_account_id:
            _logger.warning(
                "[PaymentReconciliation] Missing journal or bank account, skipping"
            )
            return

        # Batch fetch all moves needed for this chunk
        move_ids = [d["move_id"] for d in reconciliation_data]
        moves = ctx.env["account.move"].browse(move_ids)
        moves_by_id = {m.id: m for m in moves}

        reconciled_count = 0
        skipped_already_paid = 0
        skipped_no_line = 0

        for data in reconciliation_data:
            move = moves_by_id.get(data["move_id"])
            if not move:
                continue

            payment_amount = data["payment_amount"]
            payment_type = data["payment_type"]
            payment_date = data["payment_date"]
            payment_ref = data["payment_ref"]

            # Skip if already reconciled
            if move.payment_state in ["paid", "in_payment"]:
                skipped_already_paid += 1
                continue

            # Find the receivable/payable line to reconcile
            line_to_reconcile = move.line_ids.filtered(
                lambda l: l.account_id.account_type
                in ["asset_receivable", "liability_payable"]
                and not l.reconciled
            )

            if not line_to_reconcile:
                skipped_no_line += 1
                continue

            # Create journal entry for the payment
            payment_vals = {
                "journal_id": journal_id,
                "date": payment_date,
                "ref": payment_ref,
                "line_ids": [
                    (
                        0,
                        0,
                        {
                            "account_id": line_to_reconcile[0].account_id.id,
                            "partner_id": move.partner_id.id,
                            "debit": payment_amount if payment_type == "vendor" else 0,
                            "credit": (
                                payment_amount if payment_type == "customer" else 0
                            ),
                            "name": payment_ref,
                        },
                    ),
                    (
                        0,
                        0,
                        {
                            "account_id": bank_account_id,
                            "partner_id": move.partner_id.id,
                            "debit": (
                                payment_amount if payment_type == "customer" else 0
                            ),
                            "credit": payment_amount if payment_type == "vendor" else 0,
                            "name": payment_ref,
                        },
                    ),
                ],
            }

            payment_move = ctx.env["account.move"].create(payment_vals)
            payment_move.action_post()

            # Reconcile the payment with the invoice/bill
            payment_line = payment_move.line_ids.filtered(
                lambda l: l.account_id.account_type
                in ["asset_receivable", "liability_payable"]
            )

            if payment_line and line_to_reconcile:
                (line_to_reconcile + payment_line).reconcile()
                reconciled_count += 1

        _logger.info(
            f"[PaymentReconciliation] Chunk complete: {reconciled_count} reconciled, "
            f"{skipped_already_paid} already paid, {skipped_no_line} no line to reconcile"
        )
