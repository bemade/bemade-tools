"""QBO GL-first Reconciliation Pipeline.

After the GL-first import creates all moves (enriched typed moves,
enriched ``account.payment`` records, and generic journal entries), this
pipeline reconciles payment AR/AP lines with invoice/bill AR/AP lines
using QBO's ``LinkedTxn`` data.

Uses Odoo's native ``reconcile()`` so that exchange-difference journal
entries are auto-generated for multi-currency transactions.

Parallelised: each reconciliation record is one payment with all its
linked invoices/bills, so workers never contend on the same AR/AP line.
"""

import logging
from collections import defaultdict
from typing import Dict, List

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext, ChunkableData

from .utils import get_api_client

_logger = logging.getLogger(__name__)


# LinkedTxn TxnType -> (qbo_id_field on account.move, account_type to match)
_LINKED_TYPE_MAP = {
    # Customer-side
    "Invoice": ("qbo_invoice_id", "asset_receivable"),
    "CreditMemo": ("qbo_credit_memo_id", "asset_receivable"),
    # Vendor-side
    "Bill": ("qbo_bill_id", "liability_payable"),
    "VendorCredit": ("qbo_vendor_credit_id", "liability_payable"),
    # Generic (could be either side — resolved at reconciliation time)
    "JournalEntry": ("qbo_journal_entry_id", None),
}


@ETL.pipeline(
    target_model="account.move",
    importer_name="qbo.gl.first.reconciliation",
    sap_source="GLExport",
    depends_on=["qbo.gl.first.import", "qbo.gl.correction"],
    chunk_size=200,
    multiprocessing_threshold=100,
)
class QboGlFirstReconciliation(models.AbstractModel):
    _name = "qbo.gl.first.reconciliation"
    _description = "QBO GL-first payment reconciliation"

    @ETL.extract("GLExport")
    def extract_reconciliation_data(self, ctx: ETLContext) -> ChunkableData:
        """Fetch Payment and BillPayment entities from QBO API and build
        reconciliation records grouped by payment.
        """
        api_client = get_api_client(ctx)

        # Build move lookup maps: qbo_id (str) -> move_id (int)
        cr = ctx.env.cr
        move_maps: Dict[str, Dict[str, int]] = {}
        for field in (
            "qbo_invoice_id", "qbo_bill_id", "qbo_credit_memo_id",
            "qbo_vendor_credit_id", "qbo_journal_entry_id",
        ):
            cr.execute(
                f"SELECT {field}, id FROM account_move "
                f"WHERE {field} IS NOT NULL AND {field} != 0 AND state = 'posted'"
            )
            move_maps[field] = {str(row[0]): row[1] for row in cr.fetchall()}

        # Payment maps: prefer account.payment (enriched) → its move_id,
        # fall back to account.move (legacy JE-based payments).
        payment_map: Dict[str, int] = {}
        bill_payment_map: Dict[str, int] = {}

        # Enriched payments: account_payment.qbo_*_id → account_payment.move_id
        cr.execute(
            "SELECT qbo_payment_id, move_id FROM account_payment "
            "WHERE qbo_payment_id IS NOT NULL AND qbo_payment_id != 0 "
            "AND move_id IS NOT NULL"
        )
        for row in cr.fetchall():
            payment_map[str(row[0])] = row[1]

        cr.execute(
            "SELECT qbo_bill_payment_id, move_id FROM account_payment "
            "WHERE qbo_bill_payment_id IS NOT NULL AND qbo_bill_payment_id != 0 "
            "AND move_id IS NOT NULL"
        )
        for row in cr.fetchall():
            bill_payment_map[str(row[0])] = row[1]

        # Legacy fallback: account_move.qbo_*_id (JE-based payments)
        cr.execute(
            "SELECT qbo_payment_id, id FROM account_move "
            "WHERE qbo_payment_id IS NOT NULL AND qbo_payment_id != 0 "
            "AND state = 'posted'"
        )
        for row in cr.fetchall():
            payment_map.setdefault(str(row[0]), row[1])

        cr.execute(
            "SELECT qbo_bill_payment_id, id FROM account_move "
            "WHERE qbo_bill_payment_id IS NOT NULL AND qbo_bill_payment_id != 0 "
            "AND state = 'posted'"
        )
        for row in cr.fetchall():
            bill_payment_map.setdefault(str(row[0]), row[1])

        move_maps["qbo_payment_id"] = payment_map
        move_maps["qbo_bill_payment_id"] = bill_payment_map

        _logger.info(
            f"Move maps: {len(payment_map)} payments, "
            f"{len(bill_payment_map)} bill payments, "
            f"{len(move_maps['qbo_invoice_id'])} invoices, "
            f"{len(move_maps['qbo_bill_id'])} bills"
        )

        # ── Build reconciliation records ──
        # Each record is either a "payment" (payment + linked invoices/bills)
        # or a "credit_app" (credit memo + invoice).
        records: List[Dict] = []

        # -- Customer payments --
        _logger.info("Fetching QBO Payment entities...")
        payments = api_client.query_all(entity="Payment", order_by="Id")
        pmt_pairs = 0
        for pmt in payments:
            qbo_id = str(pmt.get("Id", ""))
            if qbo_id not in payment_map:
                continue
            payment_move_id = payment_map[qbo_id]
            total_amt = float(pmt.get("TotalAmt", 0) or 0)
            payment_date = pmt.get("TxnDate", "")

            for line in pmt.get("Line", []):
                line_amount = float(line.get("Amount", 0) or 0)
                for linked in line.get("LinkedTxn", []):
                    txn_id = str(linked.get("TxnId", ""))
                    txn_type = linked.get("TxnType", "")
                    type_info = _LINKED_TYPE_MAP.get(txn_type)
                    if not type_info:
                        continue
                    field, account_type = type_info
                    linked_move_id = move_maps.get(field, {}).get(txn_id)
                    if not linked_move_id:
                        continue

                    if total_amt == 0 and txn_type == "CreditMemo":
                        pass  # handled below as credit app
                    else:
                        records.append({
                            "type": "payment",
                            "payment_move_id": payment_move_id,
                            "linked_move_id": linked_move_id,
                            "account_type": account_type or "asset_receivable",
                            "payment_date": payment_date,
                        })
                        pmt_pairs += 1

            # Zero-amount payments: credit memo applications.
            if total_amt == 0:
                invoice_ids = []
                cm_ids = []
                for line in pmt.get("Line", []):
                    for linked in line.get("LinkedTxn", []):
                        txn_id = str(linked.get("TxnId", ""))
                        txn_type = linked.get("TxnType", "")
                        if txn_type == "Invoice":
                            inv_id = move_maps.get("qbo_invoice_id", {}).get(txn_id)
                            if inv_id:
                                invoice_ids.append(inv_id)
                        elif txn_type in ("CreditMemo", "JournalEntry"):
                            field = _LINKED_TYPE_MAP.get(txn_type, (None, None))[0]
                            if field:
                                cm_id = move_maps.get(field, {}).get(txn_id)
                                if cm_id:
                                    cm_ids.append(cm_id)

                for cm_id in cm_ids:
                    for inv_id in invoice_ids:
                        records.append({
                            "type": "credit_app",
                            "credit_move_id": cm_id,
                            "invoice_move_id": inv_id,
                            "account_type": "asset_receivable",
                            "payment_date": payment_date,
                        })

        _logger.info(f"Customer payments: {pmt_pairs} pairs")

        # -- Bill payments --
        _logger.info("Fetching QBO BillPayment entities...")
        bill_payments = api_client.query_all(entity="BillPayment", order_by="Id")
        bp_pairs = 0
        for bp in bill_payments:
            qbo_id = str(bp.get("Id", ""))
            if qbo_id not in bill_payment_map:
                continue
            bp_move_id = bill_payment_map[qbo_id]
            total_amt = float(bp.get("TotalAmt", 0) or 0)
            payment_date = bp.get("TxnDate", "")

            for line in bp.get("Line", []):
                line_amount = float(line.get("Amount", 0) or 0)
                for linked in line.get("LinkedTxn", []):
                    txn_id = str(linked.get("TxnId", ""))
                    txn_type = linked.get("TxnType", "")
                    type_info = _LINKED_TYPE_MAP.get(txn_type)
                    if not type_info:
                        continue
                    field, account_type = type_info
                    linked_move_id = move_maps.get(field, {}).get(txn_id)
                    if not linked_move_id:
                        continue

                    if total_amt == 0 and txn_type == "VendorCredit":
                        pass  # handled below as credit app
                    else:
                        records.append({
                            "type": "payment",
                            "payment_move_id": bp_move_id,
                            "linked_move_id": linked_move_id,
                            "account_type": account_type or "liability_payable",
                            "payment_date": payment_date,
                        })
                        bp_pairs += 1

            # Zero-amount bill payments: vendor credit applications.
            if total_amt == 0:
                bill_ids = []
                vc_ids = []
                for line in bp.get("Line", []):
                    for linked in line.get("LinkedTxn", []):
                        txn_id = str(linked.get("TxnId", ""))
                        txn_type = linked.get("TxnType", "")
                        if txn_type == "Bill":
                            b_id = move_maps.get("qbo_bill_id", {}).get(txn_id)
                            if b_id:
                                bill_ids.append(b_id)
                        elif txn_type in ("VendorCredit", "JournalEntry"):
                            field = _LINKED_TYPE_MAP.get(txn_type, (None, None))[0]
                            if field:
                                vc_id = move_maps.get(field, {}).get(txn_id)
                                if vc_id:
                                    vc_ids.append(vc_id)

                for vc_id in vc_ids:
                    for bill_id in bill_ids:
                        records.append({
                            "type": "credit_app",
                            "credit_move_id": vc_id,
                            "invoice_move_id": bill_id,
                            "account_type": "liability_payable",
                            "payment_date": payment_date,
                        })

        _logger.info(f"Bill payments: {bp_pairs} pairs")

        # Sort by date so older transactions reconcile first.
        records.sort(key=lambda r: r.get("payment_date", ""))

        _logger.info(
            f"Total reconciliation records: {len(records)} "
            f"({sum(1 for r in records if r['type'] == 'payment')} payments, "
            f"{sum(1 for r in records if r['type'] == 'credit_app')} credit apps)"
        )

        return ChunkableData(records=records, context={})

    @ETL.transform()
    def transform_reconciliation(self, ctx: ETLContext, extracted) -> List[Dict]:
        data = extracted.get("extract_reconciliation_data")
        if isinstance(data, ChunkableData):
            return data.records
        if isinstance(data, dict):
            return data.get("records", [])
        return list(data) if data else []

    @ETL.load()
    def load_reconciliation(self, ctx: ETLContext, transformed) -> None:
        records = transformed.get("transform_reconciliation") or []
        if not records:
            _logger.info("No reconciliation work to do")
            return

        Move = ctx.env["account.move"]

        reconciled = 0
        credit_applied = 0
        skipped = 0

        for rec in records:
            rec_type = rec.get("type")

            if rec_type == "payment":
                payment_move = Move.browse(rec["payment_move_id"])
                linked_move = Move.browse(rec["linked_move_id"])
                account_type = rec["account_type"]

                if not payment_move.exists() or not linked_move.exists():
                    skipped += 1
                    continue

                with ctx.skippable(
                    f"reconcile move#{payment_move.id} "
                    f"<-> move#{linked_move.id}"
                ):
                    pay_lines = payment_move.line_ids.filtered(
                        lambda l, at=account_type: (
                            l.account_id.account_type == at
                            and not l.reconciled
                        )
                    )
                    inv_lines = linked_move.line_ids.filtered(
                        lambda l, at=account_type: (
                            l.account_id.account_type == at
                            and not l.reconciled
                        )
                    )

                    if not pay_lines or not inv_lines:
                        skipped += 1
                        continue

                    # Pairwise: exactly one payment line + one invoice line.
                    # Odoo creates a partial for min(residuals) and auto-
                    # generates exchange difference entries for FX gaps.
                    (pay_lines[0] + inv_lines[0]).reconcile()
                    reconciled += 1

            elif rec_type == "credit_app":
                credit_move = Move.browse(rec["credit_move_id"])
                invoice_move_id = rec.get("invoice_move_id")
                if not invoice_move_id:
                    continue
                invoice_move = Move.browse(invoice_move_id)
                account_type = rec["account_type"]

                if not credit_move.exists() or not invoice_move.exists():
                    continue

                with ctx.skippable(
                    f"credit apply move#{credit_move.id} "
                    f"<-> move#{invoice_move.id}"
                ):
                    cm_lines = credit_move.line_ids.filtered(
                        lambda l, at=account_type: (
                            l.account_id.account_type == at
                            and not l.reconciled
                        )
                    )
                    inv_lines = invoice_move.line_ids.filtered(
                        lambda l, at=account_type: (
                            l.account_id.account_type == at
                            and not l.reconciled
                        )
                    )

                    if not cm_lines or not inv_lines:
                        continue

                    (cm_lines[0] + inv_lines[0]).reconcile()
                    credit_applied += 1

        _logger.info(
            f"Reconciliation: {reconciled} payments, "
            f"{credit_applied} credit apps, {skipped} skipped"
        )
