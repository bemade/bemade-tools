import logging

from odoo import api, models
from odoo.addons.sap_b1_to_odoo.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.move",
    importer_name="account.internal.reconciliation",
    sap_source="itr1",
    depends_on=[
        "account.move.invoice.post.processor",
    ],
    multiprocessing_threshold=500,
    chunk_size=200,
)
class AccountInternalReconciliation(models.AbstractModel):
    _name = "account.internal.reconciliation"
    _description = "SAP Internal Reconciliation - Apply ITR entries to Invoices/Bills"

    # Class-level cache for multiprocessing (only primitive types!)
    _lookup_cache = {}

    @ETL.extract("itr1")
    def extract_internal_reconciliations(self, ctx: ETLContext):
        """Extract internal reconciliation entries from SAP for invoices and bills."""
        _logger.info(
            "[InternalReconciliation] Extracting internal reconciliation data from SAP..."
        )

        # Get internal reconciliation entries (ITR1) for A/R invoices
        # srcobjtyp = '13' is for A/R invoices (OINV), iscredit = 'D' for debit side
        ctx.cr.execute(
            """
            SELECT 
                r.reconnum,
                r.srcobjabs::integer as doc_id,
                r.reconsum as reconciled_amount,
                r.shortname as partner_code,
                h.recondate as recon_date,
                'customer' as itr_type
            FROM itr1 r
            JOIN oitr h ON r.reconnum = h.reconnum
            WHERE r.srcobjtyp = '13'
            AND r.iscredit = 'D'
            """
        )
        customer_itr = ctx.cr.dictfetchall()

        # Get internal reconciliation entries (ITR1) for A/P invoices (bills)
        # srcobjtyp = '18' is for A/P invoices (OPCH), iscredit = 'C' for credit side
        ctx.cr.execute(
            """
            SELECT 
                r.reconnum,
                r.srcobjabs::integer as doc_id,
                r.reconsum as reconciled_amount,
                r.shortname as partner_code,
                h.recondate as recon_date,
                'vendor' as itr_type
            FROM itr1 r
            JOIN oitr h ON r.reconnum = h.reconnum
            WHERE r.srcobjtyp = '18'
            AND r.iscredit = 'C'
            """
        )
        vendor_itr = ctx.cr.dictfetchall()

        all_itr = customer_itr + vendor_itr

        _logger.info(
            f"[InternalReconciliation] Found {len(customer_itr)} customer and "
            f"{len(vendor_itr)} vendor ITR entries in SAP"
        )

        # Pre-load all invoices and bills - build ID maps
        invoice_docentries = [e["doc_id"] for e in customer_itr]
        bill_docentries = [e["doc_id"] for e in vendor_itr]

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
            f"[InternalReconciliation] Pre-loaded {len(invoices_map)} invoices "
            f"and {len(bills_map)} bills"
        )

        # Get payment journal (created by account.journal.setup)
        payment_journal = ctx.env["account.journal"].search(
            [("code", "=", "SAPRC"), ("type", "=", "general")],
            limit=1,
        )
        if not payment_journal:
            _logger.error("[InternalReconciliation] SAPRC journal not found!")
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
                "[InternalReconciliation] No receivable/payable account found!"
            )
            return []

        # Store in class-level cache for workers
        AccountInternalReconciliation._lookup_cache = {
            "invoices_map": invoices_map,
            "bills_map": bills_map,
            "journal_id": payment_journal.id,
            "receivable_account_id": receivable_account.id,
            "payable_account_id": payable_account.id,
        }

        return all_itr

    @ETL.transform()
    def transform_internal_reconciliations(self, ctx: ETLContext, extracted):
        """Match SAP ITR entries to Odoo invoice/bill IDs and prepare reconciliation data.

        Aggregates multiple ITR entries per document to avoid parallel workers
        trying to reconcile the same document simultaneously.
        """
        entries = extracted.get("extract_internal_reconciliations", [])

        cache = AccountInternalReconciliation._lookup_cache
        invoices_map = cache.get("invoices_map", {})
        bills_map = cache.get("bills_map", {})

        # Aggregate by document to avoid concurrent reconciliation
        doc_totals = {}
        for entry in entries:
            itr_type = entry["itr_type"]
            if itr_type == "customer":
                move_id = invoices_map.get(entry["doc_id"])
            else:
                move_id = bills_map.get(entry["doc_id"])

            if move_id:
                key = (move_id, itr_type)
                if key not in doc_totals:
                    doc_totals[key] = {
                        "move_id": move_id,
                        "reconciled_amount": 0.0,
                        "recon_date": (
                            str(entry["recon_date"]) if entry["recon_date"] else None
                        ),
                        "recon_refs": [],
                        "itr_type": itr_type,
                    }
                doc_totals[key]["reconciled_amount"] += float(
                    entry["reconciled_amount"]
                )
                doc_totals[key]["recon_refs"].append(str(entry["reconnum"]))

        # Convert to list and create combined ref
        reconciliation_data = []
        for data in doc_totals.values():
            data["recon_ref"] = f"SAP Internal Recon {','.join(data['recon_refs'][:3])}"
            if len(data["recon_refs"]) > 3:
                data["recon_ref"] += f" (+{len(data['recon_refs']) - 3} more)"
            del data["recon_refs"]
            reconciliation_data.append(data)

        _logger.info(
            f"[InternalReconciliation] Prepared {len(reconciliation_data)} "
            f"internal reconciliations (aggregated from {len(entries)} entries)"
        )
        return reconciliation_data

    @ETL.load()
    def load_internal_reconciliations(self, ctx: ETLContext, transformed):
        """Create journal entries to reconcile internal reconciliation entries."""
        reconciliation_data = transformed.get("transform_internal_reconciliations", [])

        if not reconciliation_data:
            _logger.info(
                "[InternalReconciliation] No internal reconciliations in chunk"
            )
            return

        cache = AccountInternalReconciliation._lookup_cache
        journal_id = cache.get("journal_id")
        receivable_account_id = cache.get("receivable_account_id")
        payable_account_id = cache.get("payable_account_id")

        if not journal_id or not receivable_account_id or not payable_account_id:
            _logger.warning(
                "[InternalReconciliation] Missing journal or accounts, skipping"
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

            reconciled_amount = data["reconciled_amount"]
            recon_date = data["recon_date"]
            recon_ref = data["recon_ref"]
            itr_type = data["itr_type"]

            # Determine account type based on document type
            if itr_type == "customer":
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

            # Create journal entry for the internal reconciliation
            # For customer: credit receivable, debit offset
            # For vendor: debit payable, credit offset
            if itr_type == "customer":
                line1_debit, line1_credit = 0, reconciled_amount
                line2_debit, line2_credit = reconciled_amount, 0
            else:
                line1_debit, line1_credit = reconciled_amount, 0
                line2_debit, line2_credit = 0, reconciled_amount

            # Use the same account for both sides to ensure reconciliation works
            # (bills may have different payable accounts)
            reconcile_account_id = line_to_reconcile[0].account_id.id

            recon_vals = {
                "journal_id": journal_id,
                "date": recon_date or move.date,
                "ref": recon_ref,
                "line_ids": [
                    (
                        0,
                        0,
                        {
                            "account_id": reconcile_account_id,
                            "partner_id": move.partner_id.id,
                            "debit": line1_debit,
                            "credit": line1_credit,
                            "name": recon_ref,
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
                            "name": recon_ref,
                        },
                    ),
                ],
            }

            recon_move = ctx.env["account.move"].create(recon_vals)
            recon_move.action_post()

            # Reconcile with the invoice/bill
            if itr_type == "customer":
                recon_line = recon_move.line_ids.filtered(
                    lambda l: l.account_id.account_type == "asset_receivable"
                    and l.credit > 0
                )
            else:
                recon_line = recon_move.line_ids.filtered(
                    lambda l: l.account_id.account_type == "liability_payable"
                    and l.debit > 0
                )

            if recon_line and line_to_reconcile:
                (line_to_reconcile + recon_line).reconcile()
                reconciled_count += 1

        _logger.info(
            f"[InternalReconciliation] Chunk complete: {reconciled_count} reconciled, "
            f"{skipped_no_line} no line to reconcile"
        )
