"""QBO GL-first Reconciliation Pipeline.

After the GL-first import creates all moves (enriched typed moves and
generic journal entries), this pipeline reconciles payment AR/AP lines
with invoice/bill AR/AP lines using QBO's ``LinkedTxn`` data.

In the GL-first flow, payments are generic entries (``move_type='entry'``)
identified by ``qbo_payment_id`` or ``qbo_bill_payment_id`` on
``account.move``.  Invoices/bills are either enriched typed moves or
generic entries identified by ``qbo_invoice_id``, ``qbo_bill_id``, etc.

Uses ``account.partial.reconcile`` directly (NOT ``reconcile()``) to
avoid greedy matching side-effects.
"""

import logging
from typing import Dict, List, Optional, Tuple

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext

from .utils import get_api_client

_logger = logging.getLogger(__name__)


def _partial_reconcile(ctx, credit_line, debit_line, amount):
    """Create an exact partial reconcile between two move lines.

    Extracted from the legacy payment pipeline.  Uses
    ``account.partial.reconcile`` directly to avoid greedy matching.
    """
    # Ensure correct debit/credit orientation.
    if credit_line.balance > 0 and debit_line.balance < 0:
        credit_line, debit_line = debit_line, credit_line

    credit_curr = credit_line.currency_id
    debit_curr = debit_line.currency_id
    company_curr = credit_line.company_currency_id

    credit_open = abs(
        credit_line.amount_residual_currency
        if credit_curr and credit_curr != company_curr
        else credit_line.amount_residual
    )
    debit_open = abs(
        debit_line.amount_residual_currency
        if debit_curr and debit_curr != company_curr
        else debit_line.amount_residual
    )

    apply_amount = min(amount, credit_open, debit_open) if amount else min(credit_open, debit_open)
    if apply_amount <= 0:
        return False

    # Company-currency equivalent from the credit line's rate.
    credit_cad = abs(credit_line.amount_residual)
    credit_foreign = abs(credit_line.amount_residual_currency) if credit_curr else credit_cad
    rate = credit_cad / credit_foreign if credit_foreign else 1.0
    company_amount = round(apply_amount * rate, 2)

    ctx.env["account.partial.reconcile"].create({
        "debit_move_id": debit_line.id,
        "credit_move_id": credit_line.id,
        "amount": company_amount,
        "debit_amount_currency": apply_amount,
        "credit_amount_currency": apply_amount,
        "company_id": credit_line.company_id.id,
    })
    (credit_line + debit_line).invalidate_recordset(
        ["amount_residual", "amount_residual_currency", "reconciled"]
    )
    return True


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
)
class QboGlFirstReconciliation(models.AbstractModel):
    _name = "qbo.gl.first.reconciliation"
    _description = "QBO GL-first payment reconciliation"

    @ETL.extract("GLExport")
    def extract_reconciliation_data(self, ctx: ETLContext) -> Dict:
        """Fetch Payment and BillPayment entities from QBO API and build
        reconciliation pairs by cross-referencing with imported moves.
        """
        api_client = get_api_client(ctx)

        # Build move lookup maps: qbo_id (str) -> move_id (int)
        cr = ctx.env.cr
        move_maps: Dict[str, Dict[str, int]] = {}
        for field in (
            "qbo_invoice_id", "qbo_bill_id", "qbo_credit_memo_id",
            "qbo_vendor_credit_id", "qbo_journal_entry_id",
            "qbo_payment_id", "qbo_bill_payment_id",
        ):
            cr.execute(
                f"SELECT {field}, id FROM account_move "
                f"WHERE {field} IS NOT NULL AND {field} != 0 AND state = 'posted'"
            )
            move_maps[field] = {str(row[0]): row[1] for row in cr.fetchall()}

        payment_map = move_maps["qbo_payment_id"]
        bill_payment_map = move_maps["qbo_bill_payment_id"]

        _logger.info(
            f"Move maps: {len(payment_map)} payments, "
            f"{len(bill_payment_map)} bill payments, "
            f"{len(move_maps['qbo_invoice_id'])} invoices, "
            f"{len(move_maps['qbo_bill_id'])} bills"
        )

        # Fetch QBO Payment entities (customer payments).
        reconciliation_pairs: List[Dict] = []
        credit_applications: List[Dict] = []

        _logger.info("Fetching QBO Payment entities...")
        payments = api_client.query_all(entity="Payment", order_by="Id")
        for pmt in payments:
            qbo_id = str(pmt.get("Id", ""))
            if qbo_id not in payment_map:
                continue  # Payment move not imported
            payment_move_id = payment_map[qbo_id]
            total_amt = float(pmt.get("TotalAmt", 0) or 0)

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
                        # Zero-amount payment = credit memo application.
                        credit_applications.append({
                            "credit_move_id": linked_move_id,
                            "invoice_move_id": None,  # filled below
                            "amount": line_amount,
                            "account_type": "asset_receivable",
                        })
                    else:
                        reconciliation_pairs.append({
                            "payment_move_id": payment_move_id,
                            "linked_move_id": linked_move_id,
                            "amount": line_amount,
                            "account_type": account_type or "asset_receivable",
                        })

            # For zero-amount payments, pair credit memos with invoices.
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
                                invoice_ids.append((inv_id, float(line.get("Amount", 0) or 0)))
                        elif txn_type in ("CreditMemo", "JournalEntry"):
                            field = _LINKED_TYPE_MAP.get(txn_type, (None, None))[0]
                            if field:
                                cm_id = move_maps.get(field, {}).get(txn_id)
                                if cm_id:
                                    cm_ids.append((cm_id, float(line.get("Amount", 0) or 0)))

                for cm_id, cm_amount in cm_ids:
                    for inv_id, _inv_amount in invoice_ids:
                        credit_applications.append({
                            "credit_move_id": cm_id,
                            "invoice_move_id": inv_id,
                            "amount": cm_amount,
                            "account_type": "asset_receivable",
                        })

        _logger.info(
            f"Customer payments: {len(reconciliation_pairs)} pairs, "
            f"{len(credit_applications)} credit applications"
        )

        # Fetch QBO BillPayment entities (vendor payments).
        _logger.info("Fetching QBO BillPayment entities...")
        bill_payments = api_client.query_all(entity="BillPayment", order_by="Id")
        bp_pairs = 0
        bp_credits = 0
        for bp in bill_payments:
            qbo_id = str(bp.get("Id", ""))
            if qbo_id not in bill_payment_map:
                continue
            bp_move_id = bill_payment_map[qbo_id]
            total_amt = float(bp.get("TotalAmt", 0) or 0)

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
                        credit_applications.append({
                            "credit_move_id": linked_move_id,
                            "invoice_move_id": None,
                            "amount": line_amount,
                            "account_type": "liability_payable",
                        })
                        bp_credits += 1
                    else:
                        reconciliation_pairs.append({
                            "payment_move_id": bp_move_id,
                            "linked_move_id": linked_move_id,
                            "amount": line_amount,
                            "account_type": account_type or "liability_payable",
                        })
                        bp_pairs += 1

            # Zero-amount bill payments: pair vendor credits with bills.
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
                                bill_ids.append((b_id, float(line.get("Amount", 0) or 0)))
                        elif txn_type in ("VendorCredit", "JournalEntry"):
                            field = _LINKED_TYPE_MAP.get(txn_type, (None, None))[0]
                            if field:
                                vc_id = move_maps.get(field, {}).get(txn_id)
                                if vc_id:
                                    vc_ids.append((vc_id, float(line.get("Amount", 0) or 0)))

                for vc_id, vc_amount in vc_ids:
                    for bill_id, _bill_amount in bill_ids:
                        credit_applications.append({
                            "credit_move_id": vc_id,
                            "invoice_move_id": bill_id,
                            "amount": vc_amount,
                            "account_type": "liability_payable",
                        })

        _logger.info(
            f"Bill payments: {bp_pairs} pairs, {bp_credits} credit applications"
        )

        return {
            "pairs": reconciliation_pairs,
            "credit_applications": credit_applications,
        }

    @ETL.transform()
    def transform_reconciliation(self, ctx: ETLContext, extracted: Dict) -> Dict:
        data = extracted.get("extract_reconciliation_data", {})
        # Pass through — all work done in extract (needs DB access for maps).
        return data

    @ETL.load()
    def load_reconciliation(self, ctx: ETLContext, transformed: Dict) -> None:
        data = transformed.get("transform_reconciliation", {})
        pairs = data.get("pairs") or []
        credit_applications = data.get("credit_applications") or []

        if not pairs and not credit_applications:
            _logger.info("No reconciliation work to do")
            return

        Move = ctx.env["account.move"]

        # Phase 1: Payment <-> Invoice/Bill reconciliation.
        reconciled = 0
        skipped = 0
        for pair in pairs:
            payment_move = Move.browse(pair["payment_move_id"])
            linked_move = Move.browse(pair["linked_move_id"])
            amount = float(pair["amount"])
            account_type = pair["account_type"]

            if not payment_move.exists() or not linked_move.exists():
                skipped += 1
                continue

            with ctx.skippable(
                f"reconcile payment move#{payment_move.id} "
                f"<-> move#{linked_move.id} ({amount})"
            ):
                # Find unreconciled AR/AP lines on each side.
                pay_lines = payment_move.line_ids.filtered(
                    lambda l, at=account_type: (
                        l.account_id.account_type == at and not l.reconciled
                    )
                )
                inv_lines = linked_move.line_ids.filtered(
                    lambda l, at=account_type: (
                        l.account_id.account_type == at and not l.reconciled
                    )
                )

                if not pay_lines or not inv_lines:
                    skipped += 1
                    continue

                if _partial_reconcile(ctx, pay_lines[0], inv_lines[0], amount):
                    reconciled += 1

        _logger.info(f"Payment reconciliation: {reconciled} paired, {skipped} skipped")

        # Phase 2: Credit memo / vendor credit applications.
        applied = 0
        for app in credit_applications:
            credit_move = Move.browse(app["credit_move_id"])
            invoice_move_id = app.get("invoice_move_id")
            if not invoice_move_id:
                continue
            invoice_move = Move.browse(invoice_move_id)
            amount = float(app["amount"])
            account_type = app["account_type"]

            if not credit_move.exists() or not invoice_move.exists():
                continue

            with ctx.skippable(
                f"credit apply move#{credit_move.id} "
                f"<-> move#{invoice_move.id} ({amount})"
            ):
                cm_lines = credit_move.line_ids.filtered(
                    lambda l, at=account_type: (
                        l.account_id.account_type == at and not l.reconciled
                    )
                )
                inv_lines = invoice_move.line_ids.filtered(
                    lambda l, at=account_type: (
                        l.account_id.account_type == at and not l.reconciled
                    )
                )

                if not cm_lines or not inv_lines:
                    continue

                if _partial_reconcile(ctx, cm_lines[0], inv_lines[0], amount):
                    applied += 1

        _logger.info(f"Credit applications: {applied} applied")
