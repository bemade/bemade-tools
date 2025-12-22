import logging

from odoo import api, models
from odoo.addons.sap_b1_to_odoo.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.move",
    importer_name="account.credit.memo.reconciliation",
    sap_source="orin",
    depends_on=[
        "account.move.invoice.post.processor",
    ],
    multiprocessing_threshold=500,
    chunk_size=200,
)
class AccountCreditMemoReconciliation(models.AbstractModel):
    _name = "account.credit.memo.reconciliation"
    _description = (
        "SAP Credit Memo Reconciliation - Apply Credit Memos to Invoices/Bills"
    )

    # Class-level cache for multiprocessing (only primitive types!)
    _lookup_cache = {}

    @ETL.extract("orin")
    def extract_credit_memos(self, ctx: ETLContext):
        """Extract credit memo allocations from SAP for both invoices and bills."""
        _logger.info(
            "[CreditMemoReconciliation] Extracting credit memo data from SAP..."
        )

        # Get A/R credit memo allocations (ORIN/RIN1) that pay off invoices
        # rin1.baseentry links to oinv.docentry, basetype=13 is for A/R invoices
        ctx.cr.execute(
            """
            SELECT 
                cm.docentry as cm_docentry,
                cm.docnum as cm_docnum,
                cm.docdate as cm_date,
                cm.doctotal as cm_total,
                l.baseentry::integer as doc_id,
                l.linetotal as applied_amount,
                'customer' as cm_type
            FROM orin cm
            JOIN rin1 l ON cm.docentry = l.docentry
            WHERE l.basetype = 13 AND l.baseentry IS NOT NULL
            """
        )
        customer_cms = ctx.cr.dictfetchall()

        # Get A/P credit memo allocations (ORPC/RPC1) that pay off bills
        # rpc1.baseentry links to opch.docentry, basetype=18 is for A/P invoices
        ctx.cr.execute(
            """
            SELECT 
                cm.docentry as cm_docentry,
                cm.docnum as cm_docnum,
                cm.docdate as cm_date,
                cm.doctotal as cm_total,
                l.baseentry::integer as doc_id,
                l.linetotal as applied_amount,
                'vendor' as cm_type
            FROM orpc cm
            JOIN rpc1 l ON cm.docentry = l.docentry
            WHERE l.basetype = 18 AND l.baseentry IS NOT NULL
            """
        )
        vendor_cms = ctx.cr.dictfetchall()

        all_cms = customer_cms + vendor_cms

        _logger.info(
            f"[CreditMemoReconciliation] Found {len(customer_cms)} customer and "
            f"{len(vendor_cms)} vendor credit memo allocations in SAP"
        )

        # Pre-load all invoices and bills - build ID maps
        invoice_docentries = [p["doc_id"] for p in customer_cms]
        bill_docentries = [p["doc_id"] for p in vendor_cms]

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
            f"[CreditMemoReconciliation] Pre-loaded {len(invoices_map)} invoices "
            f"and {len(bills_map)} bills"
        )

        # Get payment journal (created by account.journal.setup)
        payment_journal = ctx.env["account.journal"].search(
            [("code", "=", "SAPRC"), ("type", "=", "general")],
            limit=1,
        )
        if not payment_journal:
            _logger.error("[CreditMemoReconciliation] SAPRC journal not found!")
            return []

        # Get receivable and payable accounts
        receivable_account = ctx.env["account.account"].search(
            [("account_type", "=", "asset_receivable")],
            limit=1,
        )
        payable_account = ctx.env["account.account"].search(
            [("account_type", "=", "liability_payable")],
            limit=1,
        )
        if not receivable_account or not payable_account:
            _logger.error(
                "[CreditMemoReconciliation] No receivable/payable account found!"
            )
            return []

        # Store in class-level cache for workers
        AccountCreditMemoReconciliation._lookup_cache = {
            "invoices_map": invoices_map,
            "bills_map": bills_map,
            "journal_id": payment_journal.id,
            "receivable_account_id": receivable_account.id,
            "payable_account_id": payable_account.id,
        }

        return all_cms

    @ETL.transform()
    def transform_credit_memos(self, ctx: ETLContext, extracted):
        """Match SAP credit memos to Odoo invoice/bill IDs and prepare reconciliation data."""
        allocations = extracted.get("extract_credit_memos", [])

        cache = AccountCreditMemoReconciliation._lookup_cache
        invoices_map = cache.get("invoices_map", {})
        bills_map = cache.get("bills_map", {})

        reconciliation_data = []

        for alloc in allocations:
            cm_type = alloc["cm_type"]
            if cm_type == "customer":
                move_id = invoices_map.get(alloc["doc_id"])
            else:
                move_id = bills_map.get(alloc["doc_id"])

            if move_id:
                reconciliation_data.append(
                    {
                        "move_id": move_id,
                        "applied_amount": float(alloc["applied_amount"]),
                        "cm_date": str(alloc["cm_date"]),
                        "cm_ref": f"SAP Credit Memo {alloc['cm_docnum']}",
                        "cm_type": cm_type,
                    }
                )

        _logger.info(
            f"[CreditMemoReconciliation] Prepared {len(reconciliation_data)} "
            f"credit memo reconciliations"
        )
        return reconciliation_data

    @ETL.load()
    def load_credit_memos(self, ctx: ETLContext, transformed):
        """Create journal entries to reconcile credit memos with invoices/bills."""
        reconciliation_data = transformed.get("transform_credit_memos", [])

        if not reconciliation_data:
            _logger.info(
                "[CreditMemoReconciliation] No credit memos to reconcile in chunk"
            )
            return

        cache = AccountCreditMemoReconciliation._lookup_cache
        journal_id = cache.get("journal_id")
        receivable_account_id = cache.get("receivable_account_id")
        payable_account_id = cache.get("payable_account_id")

        if not journal_id or not receivable_account_id or not payable_account_id:
            _logger.warning(
                "[CreditMemoReconciliation] Missing journal or accounts, skipping"
            )
            return

        # Batch fetch all moves needed for this chunk
        move_ids = [d["move_id"] for d in reconciliation_data]
        moves = ctx.env["account.move"].browse(move_ids)
        moves_by_id = {m.id: m for m in moves}

        reconciled_count = 0
        skipped_no_line = 0

        for data in reconciliation_data:
            move = moves_by_id.get(data["move_id"])
            if not move:
                continue

            applied_amount = data["applied_amount"]
            cm_date = data["cm_date"]
            cm_ref = data["cm_ref"]
            cm_type = data["cm_type"]

            # Determine account type based on document type
            if cm_type == "customer":
                account_type = "asset_receivable"
                offset_account_id = receivable_account_id
            else:
                account_type = "liability_payable"
                offset_account_id = payable_account_id

            # Find the receivable/payable line to reconcile
            line_to_reconcile = move.line_ids.filtered(
                lambda l, at=account_type: l.account_id.account_type == at
                and not l.reconciled
            )

            if not line_to_reconcile:
                skipped_no_line += 1
                continue

            # Create journal entry for the credit memo application
            # For customer: credit receivable, debit offset
            # For vendor: debit payable, credit offset
            if cm_type == "customer":
                line1_debit, line1_credit = 0, applied_amount
                line2_debit, line2_credit = applied_amount, 0
            else:
                line1_debit, line1_credit = applied_amount, 0
                line2_debit, line2_credit = 0, applied_amount

            # Use the same account for both sides to ensure reconciliation works
            # (bills may have different payable accounts)
            reconcile_account_id = line_to_reconcile[0].account_id.id

            cm_vals = {
                "journal_id": journal_id,
                "date": cm_date,
                "ref": cm_ref,
                "line_ids": [
                    (
                        0,
                        0,
                        {
                            "account_id": reconcile_account_id,
                            "partner_id": move.partner_id.id,
                            "debit": line1_debit,
                            "credit": line1_credit,
                            "name": cm_ref,
                        },
                    ),
                    (
                        0,
                        0,
                        {
                            "account_id": reconcile_account_id,
                            "partner_id": move.partner_id.id,
                            "debit": line2_debit,
                            "credit": line2_credit,
                            "name": cm_ref,
                        },
                    ),
                ],
            }

            cm_move = ctx.env["account.move"].create(cm_vals)
            cm_move.action_post()

            # Reconcile the credit memo with the invoice/bill
            if cm_type == "customer":
                cm_line = cm_move.line_ids.filtered(
                    lambda l: l.account_id.account_type == "asset_receivable"
                    and l.credit > 0
                )
            else:
                cm_line = cm_move.line_ids.filtered(
                    lambda l: l.account_id.account_type == "liability_payable"
                    and l.debit > 0
                )

            if cm_line and line_to_reconcile:
                (line_to_reconcile + cm_line).reconcile()
                reconciled_count += 1

        _logger.info(
            f"[CreditMemoReconciliation] Chunk complete: {reconciled_count} reconciled, "
            f"{skipped_no_line} no line to reconcile"
        )
