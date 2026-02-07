import logging

from odoo import api, models
from odoo.addons.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.move",
    importer_name="account.force.paid.reconciliation",
    sap_source="oinv",
    depends_on=[
        "account.internal.reconciliation",
    ],
    multiprocessing_threshold=500,
    chunk_size=200,
)
class AccountForcePaidReconciliation(models.AbstractModel):
    _name = "account.force.paid.reconciliation"
    _description = "SAP Force Paid - Reconcile invoices/bills marked paid in SAP without payment records"

    # Class-level cache for multiprocessing (only primitive types!)
    _lookup_cache = {}

    @ETL.extract("oinv")
    def extract_force_paid(self, ctx: ETLContext):
        """Extract invoices/bills that are fully paid in SAP but not in Odoo."""
        _logger.info(
            "[ForcePaidReconciliation] Extracting force-paid documents from SAP..."
        )

        # Get invoices where paidtodate = doctotal (fully paid in SAP)
        ctx.cr.execute(
            """
            SELECT 
                i.docentry,
                i.docnum,
                i.doctotal,
                i.docdate,
                'customer' as doc_type
            FROM oinv i
            WHERE i.paidtodate = i.doctotal
            AND i.doctotal > 0
            """
        )
        paid_invoices = ctx.cr.dictfetchall()

        # Get bills where paidtodate = doctotal (fully paid in SAP)
        ctx.cr.execute(
            """
            SELECT 
                b.docentry,
                b.docnum,
                b.doctotal,
                b.docdate,
                'vendor' as doc_type
            FROM opch b
            WHERE b.paidtodate = b.doctotal
            AND b.doctotal > 0
            """
        )
        paid_bills = ctx.cr.dictfetchall()

        all_paid = paid_invoices + paid_bills

        _logger.info(
            f"[ForcePaidReconciliation] Found {len(paid_invoices)} paid invoices "
            f"and {len(paid_bills)} paid bills in SAP"
        )

        # Pre-load documents that are NOT yet fully paid in Odoo
        invoice_docentries = [p["docentry"] for p in paid_invoices]
        bill_docentries = [p["docentry"] for p in paid_bills]

        # Map sap_docentry -> (move_id, amount_residual) for invoices not yet paid
        invoices_map = {}
        if invoice_docentries:
            invoices = ctx.env["account.move"].search(
                [
                    ("sap_docentry", "in", invoice_docentries),
                    ("sap_table", "=", "oinv"),
                    ("state", "=", "posted"),
                    ("payment_state", "in", ["not_paid", "partial"]),
                ]
            )
            invoices_map = {
                inv.sap_docentry: {
                    "move_id": inv.id,
                    "amount_residual": inv.amount_residual,
                }
                for inv in invoices
            }

        # Map sap_docentry -> (move_id, amount_residual) for bills not yet paid
        bills_map = {}
        if bill_docentries:
            bills = ctx.env["account.move"].search(
                [
                    ("sap_docentry", "in", bill_docentries),
                    ("sap_table", "=", "opch"),
                    ("state", "=", "posted"),
                    ("payment_state", "in", ["not_paid", "partial"]),
                ]
            )
            bills_map = {
                bill.sap_docentry: {
                    "move_id": bill.id,
                    "amount_residual": bill.amount_residual,
                }
                for bill in bills
            }

        _logger.info(
            f"[ForcePaidReconciliation] Found {len(invoices_map)} invoices and "
            f"{len(bills_map)} bills that need force-paid reconciliation"
        )

        # Get payment journal
        payment_journal = ctx.env["account.journal"].search(
            [("code", "=", "SAPRC"), ("type", "=", "general")],
            limit=1,
        )
        if not payment_journal:
            _logger.error("[ForcePaidReconciliation] SAPRC journal not found!")
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
                "[ForcePaidReconciliation] No receivable/payable account found!"
            )
            return []

        # Store in class-level cache for workers
        AccountForcePaidReconciliation._lookup_cache = {
            "invoices_map": invoices_map,
            "bills_map": bills_map,
            "journal_id": payment_journal.id,
            "receivable_account_id": receivable_account.id,
            "payable_account_id": payable_account.id,
        }

        # Only return documents that need reconciliation
        result = []
        for p in paid_invoices:
            if p["docentry"] in invoices_map:
                result.append(p)
        for p in paid_bills:
            if p["docentry"] in bills_map:
                result.append(p)
        return result

    @ETL.transform()
    def transform_force_paid(self, ctx: ETLContext, extracted):
        """Prepare force-paid reconciliation data."""
        documents = extracted.get("extract_force_paid", [])

        cache = AccountForcePaidReconciliation._lookup_cache
        invoices_map = cache.get("invoices_map", {})
        bills_map = cache.get("bills_map", {})

        reconciliation_data = []

        for doc in documents:
            doc_type = doc["doc_type"]
            if doc_type == "customer":
                doc_data = invoices_map.get(doc["docentry"])
                doc_label = "Invoice"
            else:
                doc_data = bills_map.get(doc["docentry"])
                doc_label = "Bill"

            if doc_data:
                reconciliation_data.append(
                    {
                        "move_id": doc_data["move_id"],
                        "amount_residual": float(doc_data["amount_residual"]),
                        "docdate": str(doc["docdate"]) if doc["docdate"] else None,
                        "ref": f"SAP Write-off/Force Paid ({doc_label} {doc['docnum']})",
                        "doc_type": doc_type,
                    }
                )

        _logger.info(
            f"[ForcePaidReconciliation] Prepared {len(reconciliation_data)} "
            f"force-paid reconciliations"
        )
        return reconciliation_data

    @ETL.load()
    def load_force_paid(self, ctx: ETLContext, transformed):
        """Create journal entries to force-reconcile remaining invoices/bills."""
        reconciliation_data = transformed.get("transform_force_paid", [])

        if not reconciliation_data:
            _logger.info(
                "[ForcePaidReconciliation] No force-paid reconciliations in chunk"
            )
            return

        cache = AccountForcePaidReconciliation._lookup_cache
        journal_id = cache.get("journal_id")
        receivable_account_id = cache.get("receivable_account_id")
        payable_account_id = cache.get("payable_account_id")

        if not journal_id or not receivable_account_id or not payable_account_id:
            _logger.warning(
                "[ForcePaidReconciliation] Missing journal or accounts, skipping"
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

            amount_residual = data["amount_residual"]
            docdate = data["docdate"]
            ref = data["ref"]
            doc_type = data["doc_type"]

            # Skip if already fully reconciled
            if amount_residual <= 0:
                continue

            # Determine account type based on document type
            if doc_type == "customer":
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

            # Create journal entry to write off the remaining balance
            # For customer: credit receivable, debit offset
            # For vendor: debit payable, credit offset
            if doc_type == "customer":
                line1_debit, line1_credit = 0, amount_residual
                line2_debit, line2_credit = amount_residual, 0
            else:
                line1_debit, line1_credit = amount_residual, 0
                line2_debit, line2_credit = 0, amount_residual

            # Use the same account for both sides to ensure reconciliation works
            # (bills may have different payable accounts)
            reconcile_account_id = line_to_reconcile[0].account_id.id

            writeoff_vals = {
                "journal_id": journal_id,
                "date": docdate or move.date,
                "ref": ref,
                "line_ids": [
                    (
                        0,
                        0,
                        {
                            "account_id": reconcile_account_id,
                            "partner_id": move.partner_id.id,
                            "debit": line1_debit,
                            "credit": line1_credit,
                            "name": ref,
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
                            "name": ref,
                        },
                    ),
                ],
            }

            with ctx.skippable(ref):
                writeoff_move = ctx.env["account.move"].create(writeoff_vals)
                writeoff_move.action_post()

                # Reconcile with the invoice/bill
                if doc_type == "customer":
                    writeoff_line = writeoff_move.line_ids.filtered(
                        lambda l: l.account_id.account_type == "asset_receivable"
                        and l.credit > 0
                    )
                else:
                    writeoff_line = writeoff_move.line_ids.filtered(
                        lambda l: l.account_id.account_type == "liability_payable"
                        and l.debit > 0
                    )

                if writeoff_line and line_to_reconcile:
                    (line_to_reconcile + writeoff_line).reconcile()
                    reconciled_count += 1

        _logger.info(
            f"[ForcePaidReconciliation] Chunk complete: {reconciled_count} reconciled, "
            f"{skipped_no_line} no line to reconcile"
        )
