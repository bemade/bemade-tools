import logging

from odoo import api, models
from odoo.addons.etl_framework import ETL, ETLContext, ChunkableData
from odoo.addons.etl_framework.utils import post_lock

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.move",
    importer_name="account.payment.reconciliation",
    sap_source="oinv",  # We'll query both OINV and OPCH
    depends_on=[
        "account.move.invoice.post.processor",
        "account.move.bill.importer",
        "account.move.credit.memo.importer",
        "account.move.vendor.credit.memo.importer",
    ],
    multiprocessing_threshold=500,
    chunk_size=200,
)
class AccountPaymentReconciliation(models.AbstractModel):
    _name = "account.payment.reconciliation"
    _description = (
        "SAP Payment Reconciliation - Create Journal Entries for Paid Invoices/Bills"
    )

    @ETL.extract("oinv")
    def extract_payments(self, ctx: ETLContext):
        """Extract payments from SAP payment tables and build lookup maps."""
        _logger.info("[PaymentReconciliation] Extracting payment data from SAP...")

        # Get incoming payments (customer payments) with invoice/credit memo allocations
        # rct2.baseabs links to document docentry
        # invtype=13: A/R Invoice (OINV), invtype=14: A/R Credit Memo (ORIN)
        # Unique key: (docnum, docline) where docnum is payment docentry
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
                a.baseabs::integer as doc_id,
                a.docline::integer as alloc_line,
                a.sumapplied,
                a.invtype::integer as invtype,
                'customer' as payment_type,
                'rct2' as alloc_table
            FROM orct p
            JOIN rct2 a ON p.docentry = a.docnum
            WHERE a.invtype IN ('13', '14') AND a.baseabs IS NOT NULL
            """
        )
        customer_payments = ctx.cr.dictfetchall()

        # Get outgoing payments (vendor payments) with bill/credit memo allocations
        # vpm2.baseabs links to document docentry
        # invtype=18: A/P Invoice (OPCH), invtype=19: A/P Credit Memo (ORPC)
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
                a.baseabs::integer as doc_id,
                a.docline::integer as alloc_line,
                a.sumapplied,
                a.invtype::integer as invtype,
                'vendor' as payment_type,
                'vpm2' as alloc_table
            FROM ovpm p
            JOIN vpm2 a ON p.docentry = a.docnum
            WHERE a.invtype IN ('18', '19') AND a.baseabs IS NOT NULL
            """
        )
        vendor_payments = ctx.cr.dictfetchall()

        _logger.info(
            f"[PaymentReconciliation] Found {len(customer_payments)} customer payment "
            f"allocations and {len(vendor_payments)} vendor payment allocations in SAP"
        )

        # Combine all payments into a single list
        all_payments = customer_payments + vendor_payments

        # Check for already-imported payment allocations
        # We use sap_table (rct2/vpm2) and sap_docentry (payment_docentry)
        # and sap_docnum (alloc_line) to uniquely identify each allocation
        already_imported = ctx.env["account.move"].search(
            [
                ("sap_table", "in", ["rct2", "vpm2"]),
            ]
        )
        imported_keys = {
            (m.sap_table, m.sap_docentry, m.sap_docnum) for m in already_imported
        }

        # Filter out already-imported allocations
        new_payments = [
            p
            for p in all_payments
            if (p["alloc_table"], p["payment_docentry"], p["alloc_line"])
            not in imported_keys
        ]

        _logger.info(
            f"[PaymentReconciliation] {len(all_payments) - len(new_payments)} already imported, "
            f"{len(new_payments)} new allocations to process"
        )

        all_payments = new_payments

        # Pre-load documents by invtype - build ID maps (not recordsets!)
        # invtype 13 = A/R Invoice (oinv), 14 = A/R Credit Memo (orin)
        # invtype 18 = A/P Invoice (opch), 19 = A/P Credit Memo (orpc)
        ar_invoice_docentries = [
            p["doc_id"] for p in all_payments if p["invtype"] == 13
        ]
        ar_credit_memo_docentries = [
            p["doc_id"] for p in all_payments if p["invtype"] == 14
        ]
        ap_invoice_docentries = [
            p["doc_id"] for p in all_payments if p["invtype"] == 18
        ]
        ap_credit_memo_docentries = [
            p["doc_id"] for p in all_payments if p["invtype"] == 19
        ]

        # Map (sap_table, sap_docentry) -> move_id
        moves_map = {}

        if ar_invoice_docentries:
            invoices = ctx.env["account.move"].search(
                [
                    ("sap_docentry", "in", ar_invoice_docentries),
                    ("sap_table", "=", "oinv"),
                    ("state", "=", "posted"),
                ]
            )
            for inv in invoices:
                moves_map[("oinv", inv.sap_docentry)] = inv.id

        if ar_credit_memo_docentries:
            cms = ctx.env["account.move"].search(
                [
                    ("sap_docentry", "in", ar_credit_memo_docentries),
                    ("sap_table", "=", "orin"),
                    ("state", "=", "posted"),
                ]
            )
            for cm in cms:
                moves_map[("orin", cm.sap_docentry)] = cm.id

        if ap_invoice_docentries:
            bills = ctx.env["account.move"].search(
                [
                    ("sap_docentry", "in", ap_invoice_docentries),
                    ("sap_table", "=", "opch"),
                    ("state", "=", "posted"),
                ]
            )
            for bill in bills:
                moves_map[("opch", bill.sap_docentry)] = bill.id

        if ap_credit_memo_docentries:
            cms = ctx.env["account.move"].search(
                [
                    ("sap_docentry", "in", ap_credit_memo_docentries),
                    ("sap_table", "=", "orpc"),
                    ("state", "=", "posted"),
                ]
            )
            for cm in cms:
                moves_map[("orpc", cm.sap_docentry)] = cm.id

        _logger.info(
            f"[PaymentReconciliation] Pre-loaded {len(moves_map)} documents "
            f"(invoices, bills, credit memos)"
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

        return ChunkableData(
            records=all_payments,
            context={
                "moves_map": moves_map,
                "journal_id": payment_journal.id,
                "bank_account_id": bank_account.id,
            },
        )

    @ETL.transform()
    def transform_payments(self, ctx: ETLContext, extracted):
        """Match SAP payments to Odoo move IDs and prepare reconciliation data."""
        data = extracted.get("extract_payments")
        payments = data.records if data else []
        cache = data.context if data else {}
        moves_map = cache.get("moves_map", {})

        # Map invtype to SAP table name
        invtype_to_table = {
            13: "oinv",  # A/R Invoice
            14: "orin",  # A/R Credit Memo
            18: "opch",  # A/P Invoice (Bill)
            19: "orpc",  # A/P Credit Memo
        }

        payment_data = []

        for payment in payments:
            payment_type = payment["payment_type"]
            invtype = payment["invtype"]
            sap_table = invtype_to_table.get(invtype)

            if not sap_table:
                continue

            # Look up move_id using (sap_table, docentry) tuple
            move_id = moves_map.get((sap_table, payment["doc_id"]))

            if move_id:
                payment_data.append(
                    {
                        "move_id": move_id,
                        "payment_amount": float(payment["sumapplied"]),
                        "payment_date": str(payment["payment_date"]),
                        "payment_ref": f"SAP Payment {payment['payment_docnum']}",
                        "payment_type": payment_type,
                        "invtype": invtype,
                        # For preexistence tracking
                        "alloc_table": payment["alloc_table"],
                        "payment_docentry": payment["payment_docentry"],
                        "alloc_line": payment["alloc_line"],
                    }
                )

        _logger.info(
            f"[PaymentReconciliation] Prepared {len(payment_data)} payment reconciliations"
        )
        return {
            "data": payment_data,
            "journal_id": cache.get("journal_id"),
            "bank_account_id": cache.get("bank_account_id"),
        }

    @ETL.load()
    def load_payments(self, ctx: ETLContext, transformed):
        """Create journal entries to reconcile paid invoices/bills."""
        result = transformed.get("transform_payments", {})
        reconciliation_data = result.get("data", [])
        journal_id = result.get("journal_id")
        bank_account_id = result.get("bank_account_id")

        if not reconciliation_data:
            _logger.info("[PaymentReconciliation] No payments to reconcile in chunk")
            return

        if not journal_id or not bank_account_id:
            _logger.warning(
                "[PaymentReconciliation] Missing journal or bank account, skipping"
            )
            return

        # Batch fetch all moves needed for this chunk
        move_ids = [d["move_id"] for d in reconciliation_data]
        moves = ctx.env["account.move"].browse(move_ids)
        moves_by_id = {m.id: m for m in moves}

        # Phase 1: Prepare all payment journal entry values
        payment_vals_list = []
        reconciliation_pairs = []  # (original_line, payment_vals_index)

        for data in reconciliation_data:
            move = moves_by_id.get(data["move_id"])
            if not move:
                continue

            payment_amount = data["payment_amount"]
            payment_date = data["payment_date"]
            payment_ref = data["payment_ref"]
            invtype = data.get("invtype", 0)

            # Find the receivable/payable line to reconcile
            line_to_reconcile = move.line_ids.filtered(
                lambda l: l.account_id.account_type
                in ["asset_receivable", "liability_payable"]
                and not l.reconciled
            )

            if not line_to_reconcile:
                continue

            # Determine debit/credit based on document type (invtype)
            if invtype == 13:  # A/R Invoice
                recv_debit, recv_credit = 0, payment_amount
                bank_debit, bank_credit = payment_amount, 0
            elif invtype == 14:  # A/R Credit Memo
                recv_debit, recv_credit = payment_amount, 0
                bank_debit, bank_credit = 0, payment_amount
            elif invtype == 18:  # A/P Invoice (Bill)
                recv_debit, recv_credit = payment_amount, 0
                bank_debit, bank_credit = 0, payment_amount
            elif invtype == 19:  # A/P Credit Memo
                recv_debit, recv_credit = 0, payment_amount
                bank_debit, bank_credit = payment_amount, 0
            else:
                _logger.warning(f"Unknown invtype {invtype}, skipping")
                continue

            payment_vals = {
                "journal_id": journal_id,
                "date": payment_date,
                "ref": payment_ref,
                "sap_table": data["alloc_table"],
                "sap_docentry": data["payment_docentry"],
                "sap_docnum": data["alloc_line"],
                "line_ids": [
                    (
                        0,
                        0,
                        {
                            "account_id": line_to_reconcile[0].account_id.id,
                            "partner_id": move.partner_id.id,
                            "debit": recv_debit,
                            "credit": recv_credit,
                            "name": payment_ref,
                        },
                    ),
                    (
                        0,
                        0,
                        {
                            "account_id": bank_account_id,
                            "partner_id": move.partner_id.id,
                            "debit": bank_debit,
                            "credit": bank_credit,
                            "name": payment_ref,
                        },
                    ),
                ],
            }

            payment_vals_list.append(payment_vals)
            reconciliation_pairs.append((line_to_reconcile, len(payment_vals_list) - 1))

        if not payment_vals_list:
            _logger.info("[PaymentReconciliation] No valid payments to create")
            return

        # Phase 2: Batch create all payment journal entries
        _logger.info(
            f"[PaymentReconciliation] Batch creating {len(payment_vals_list)} payment entries"
        )
        payment_moves = ctx.env["account.move"].create(payment_vals_list)

        # Phase 3: Post grouped by journal under advisory lock to prevent deadlocks
        by_journal = {}
        for move in payment_moves:
            by_journal.setdefault(move.journal_id.id, self.env["account.move"])
            by_journal[move.journal_id.id] |= move
        for journal_id, journal_moves in sorted(by_journal.items()):
            _logger.info(
                f"[PaymentReconciliation] Posting {len(journal_moves)} entries for journal {journal_id}"
            )
            with post_lock(ctx.env.cr, journal_id):
                journal_moves.action_post()

        # Phase 4: Reconcile each pair (can't be batched)
        reconciled_count = 0
        for line_to_reconcile, payment_idx in reconciliation_pairs:
            payment_move = payment_moves[payment_idx]

            payment_line = payment_move.line_ids.filtered(
                lambda l: l.account_id.account_type
                in ["asset_receivable", "liability_payable"]
            )

            if payment_line and line_to_reconcile:
                with ctx.skippable(f"payment move {payment_move.id}"):
                    (line_to_reconcile + payment_line).reconcile()
                    reconciled_count += 1

        _logger.info(
            f"[PaymentReconciliation] Chunk complete: {reconciled_count} reconciled "
            f"out of {len(reconciliation_pairs)} pairs"
        )
