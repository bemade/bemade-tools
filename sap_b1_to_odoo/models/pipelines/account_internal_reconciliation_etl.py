"""ETL Pipeline for SAP Internal Reconciliation (OITR/ITR1).

SAP Internal Reconciliation is the equivalent of Odoo's
account.partial.reconcile / account.full.reconcile — it records which
journal items were matched together (e.g. an invoice with its payment).

This pipeline reads ITR groups from SAP and directly reconciles the
corresponding receivable/payable lines in Odoo.  No journal entries are
created; we simply call reconcile() on the existing lines, mirroring what
the payment and credit-memo pipelines may have already done for their
respective document types.

SAP document-type mapping (srcobjtyp → Odoo sap_table):
    13 → oinv   A/R Invoice
    14 → orin   A/R Credit Memo
    18 → opch   A/P Invoice (Bill)
    19 → orpc   A/P Credit Memo
    24 → rct2   Incoming Payment (allocation JEs)
    46 → vpm2   Outgoing Payment (allocation JEs)
"""

import logging
from collections import defaultdict

from odoo import models
from odoo.addons.etl_framework import ETL, ETLContext, ChunkableData

_logger = logging.getLogger(__name__)

# (odoo sap_table, is_payment_alloc)
# is_payment_alloc=True  → srcobjabs is a payment docentry; the Odoo moves
#                          are allocation JEs created by the payment pipeline
#                          (potentially several per payment).
# is_payment_alloc=False → srcobjabs is the document's own docentry.
_SRCOBJTYP_MAP = {
    "13": ("oinv", False),
    "14": ("orin", False),
    "18": ("opch", False),
    "19": ("orpc", False),
    "24": ("rct2", True),
    "46": ("vpm2", True),
}


@ETL.pipeline(
    target_model="account.move",
    importer_name="account.internal.reconciliation",
    sap_source="itr1",
    depends_on=[
        "account.payment.reconciliation",
        "account.credit.memo.reconciliation",
    ],
    allow_multiprocessing=False,
)
class AccountInternalReconciliation(models.AbstractModel):
    _name = "account.internal.reconciliation"
    _description = "SAP Internal Reconciliation - Reconcile document groups from ITR"

    @ETL.extract("itr1")
    def extract_internal_reconciliations(self, ctx: ETLContext):
        """Extract all ITR1 lines grouped by reconnum (both sides)."""
        _logger.info("[ITR] Extracting reconciliation groups from SAP...")

        ctx.cr.execute(
            """
            SELECT
                r.reconnum,
                r.srcobjtyp,
                r.srcobjabs::integer AS doc_id,
                r.iscredit,
                r.reconsum AS reconciled_amount
            FROM itr1 r
            JOIN oitr h ON r.reconnum = h.reconnum
            WHERE h.canceled = 'N'
            ORDER BY r.reconnum, r.lineseq
            """
        )
        all_lines = ctx.cr.dictfetchall()

        # Group by reconnum
        groups_by_reconnum = defaultdict(list)
        for line in all_lines:
            groups_by_reconnum[line["reconnum"]].append(line)

        groups = [
            {"reconnum": reconnum, "lines": lines}
            for reconnum, lines in groups_by_reconnum.items()
        ]

        _logger.info(
            f"[ITR] Found {len(groups)} active reconciliation groups "
            f"({len(all_lines)} total lines)"
        )

        # Collect document references to pre-load from Odoo
        doc_ids_by_table = defaultdict(set)
        payment_ids_by_table = defaultdict(set)

        for line in all_lines:
            mapping = _SRCOBJTYP_MAP.get(line["srcobjtyp"])
            if not mapping:
                continue
            sap_table, is_payment = mapping
            if is_payment:
                payment_ids_by_table[sap_table].add(line["doc_id"])
            else:
                doc_ids_by_table[sap_table].add(line["doc_id"])

        # Pre-load Odoo moves for document types
        # Key: "sap_table:sap_docentry" -> move_id
        doc_move_map = {}
        for sap_table, doc_ids in doc_ids_by_table.items():
            if not doc_ids:
                continue
            moves = ctx.env["account.move"].search(
                [
                    ("sap_docentry", "in", list(doc_ids)),
                    ("sap_table", "=", sap_table),
                    ("state", "=", "posted"),
                ]
            )
            for m in moves:
                doc_move_map[f"{sap_table}:{m.sap_docentry}"] = m.id

        # Pre-load Odoo moves for payment allocations
        # Key: "sap_table:payment_docentry" -> [move_ids]
        payment_move_map = {}
        for sap_table, payment_docentries in payment_ids_by_table.items():
            if not payment_docentries:
                continue
            moves = ctx.env["account.move"].search(
                [
                    ("sap_docentry", "in", list(payment_docentries)),
                    ("sap_table", "=", sap_table),
                    ("state", "=", "posted"),
                ]
            )
            for m in moves:
                key = f"{sap_table}:{m.sap_docentry}"
                payment_move_map.setdefault(key, []).append(m.id)

        _logger.info(
            f"[ITR] Pre-loaded {len(doc_move_map)} document moves and "
            f"{sum(len(v) for v in payment_move_map.values())} payment moves"
        )

        return ChunkableData(
            records=groups,
            context={
                "doc_move_map": doc_move_map,
                "payment_move_map": payment_move_map,
            },
        )

    @ETL.transform()
    def transform_internal_reconciliations(self, ctx: ETLContext, extracted):
        """Map each reconnum group's ITR lines to Odoo move IDs.

        Only keeps groups where at least 2 distinct Odoo moves are found
        (need both sides to reconcile).
        """
        data = extracted.get("extract_internal_reconciliations")
        groups = data.records if data else []
        cache = data.context if data else {}
        doc_move_map = cache.get("doc_move_map", {})
        payment_move_map = cache.get("payment_move_map", {})

        reconciliation_groups = []

        for group in groups:
            reconnum = group["reconnum"]
            move_ids = set()

            for line in group["lines"]:
                mapping = _SRCOBJTYP_MAP.get(line["srcobjtyp"])
                if not mapping:
                    continue
                sap_table, is_payment = mapping
                key = f"{sap_table}:{line['doc_id']}"
                if is_payment:
                    for mid in payment_move_map.get(key, []):
                        move_ids.add(mid)
                else:
                    mid = doc_move_map.get(key)
                    if mid:
                        move_ids.add(mid)

            if len(move_ids) >= 2:
                reconciliation_groups.append(
                    {
                        "reconnum": reconnum,
                        "move_ids": list(move_ids),
                    }
                )

        _logger.info(
            f"[ITR] {len(reconciliation_groups)} groups have 2+ mapped moves "
            f"(out of {len(groups)} total)"
        )
        return reconciliation_groups

    @ETL.load()
    def load_internal_reconciliations(self, ctx: ETLContext, transformed):
        """Directly reconcile receivable/payable lines within each ITR group."""
        groups = transformed.get("transform_internal_reconciliations", [])

        if not groups:
            _logger.info("[ITR] No reconciliation groups in chunk")
            return

        # Batch-browse all moves referenced by this chunk
        all_move_ids = list({mid for g in groups for mid in g["move_ids"]})
        all_moves = ctx.env["account.move"].browse(all_move_ids)
        moves_by_id = {m.id: m for m in all_moves}

        reconciled_count = 0
        already_done = 0
        skipped_one_sided = 0

        for group in groups:
            reconnum = group["reconnum"]
            moves = [
                moves_by_id[mid]
                for mid in group["move_ids"]
                if mid in moves_by_id
            ]

            # Collect unreconciled receivable/payable lines from all moves
            lines = self.env["account.move.line"]
            for move in moves:
                lines |= move.line_ids.filtered(
                    lambda l: l.account_id.account_type
                    in ("asset_receivable", "liability_payable")
                    and not l.reconciled
                )

            if not lines:
                already_done += 1
                continue

            # Group by account — reconcile() requires a single account
            lines_by_account = {}
            for line in lines:
                lines_by_account.setdefault(
                    line.account_id.id, self.env["account.move.line"]
                )
                lines_by_account[line.account_id.id] |= line

            group_reconciled = False
            for account_id, account_lines in lines_by_account.items():
                has_debit = any(l.debit > 0 for l in account_lines)
                has_credit = any(l.credit > 0 for l in account_lines)
                if not (has_debit and has_credit):
                    continue

                with ctx.skippable(f"ITR group {reconnum} account {account_id}"):
                    account_lines.reconcile()
                    group_reconciled = True

            if group_reconciled:
                reconciled_count += 1
            else:
                skipped_one_sided += 1

        _logger.info(
            f"[ITR] Chunk complete: {reconciled_count} reconciled, "
            f"{already_done} already done, {skipped_one_sided} skipped (one-sided)"
        )
