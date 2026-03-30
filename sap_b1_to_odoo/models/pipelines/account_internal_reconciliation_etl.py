"""ETL Pipeline for SAP Internal Reconciliation (OITR/ITR1).

SAP Internal Reconciliation is the equivalent of Odoo's
account.partial.reconcile / account.full.reconcile — it records which
journal items were matched together (e.g. an invoice with its payment).

This pipeline reads ITR groups from SAP and directly reconciles the
corresponding receivable/payable lines in Odoo.  No journal entries are
created; we simply call reconcile() on the existing lines.

Works with both enriched moves (sap_table = oinv/opch/etc.) and generic
JDT1-pipeline moves (sap_table = ojdt).
"""

import logging
from collections import defaultdict

from odoo import models
from odoo.addons.etl_framework import ETL, ETLContext, ChunkableData

_logger = logging.getLogger(__name__)

# ITR1.srcobjtyp -> (sap_table for enriched moves, transtype for OJDT lookup)
_SRCOBJTYP_MAP = {
    "13": {"sap_table": "oinv", "transtype": "13"},
    "14": {"sap_table": "orin", "transtype": "14"},
    "18": {"sap_table": "opch", "transtype": "18"},
    "19": {"sap_table": "orpc", "transtype": "19"},
    "24": {"sap_table": "orct", "transtype": "24"},
    "46": {"sap_table": "ovpm", "transtype": "46"},
}


@ETL.pipeline(
    target_model="account.move",
    importer_name="account.internal.reconciliation",
    sap_source="itr1",
    depends_on=[
        "account.move.jdt1.enricher",
    ],
    multiprocessing_threshold=500,
    chunk_size=200,
    max_workers=8,
)
class AccountInternalReconciliation(models.AbstractModel):
    _name = "account.internal.reconciliation"
    _description = "SAP Internal Reconciliation - Reconcile document groups from ITR"

    @ETL.extract("itr1")
    def extract_internal_reconciliations(self, ctx: ETLContext):
        """Extract all ITR1 lines grouped by reconnum."""
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
            "[ITR] Found %d active reconciliation groups (%d total lines)",
            len(groups), len(all_lines),
        )

        # Build a map from (srcobjtyp, srcobjabs) -> OJDT transid
        # so we can find generic JDT1-pipeline moves (sap_table='ojdt')
        doc_ids_by_type = defaultdict(set)
        for line in all_lines:
            if line["srcobjtyp"] in _SRCOBJTYP_MAP:
                doc_ids_by_type[line["srcobjtyp"]].add(line["doc_id"])

        # Batch-query OJDT for createdby -> transid mapping per transtype
        ojdt_transid_map = {}  # (srcobjtyp, srcobjabs) -> transid
        for srcobjtyp, doc_ids in doc_ids_by_type.items():
            if not doc_ids:
                continue
            transtype = _SRCOBJTYP_MAP[srcobjtyp]["transtype"]
            ctx.cr.execute(
                "SELECT createdby, transid FROM ojdt"
                " WHERE transtype = %s AND createdby IN %s",
                (transtype, tuple(doc_ids)),
            )
            for row in ctx.cr.fetchall():
                ojdt_transid_map[(srcobjtyp, row[0])] = row[1]

        # Pre-load Odoo moves: try enriched table first, fall back to ojdt
        doc_move_map = {}  # "table:docentry" -> move_id

        for srcobjtyp, doc_ids in doc_ids_by_type.items():
            if not doc_ids:
                continue
            config = _SRCOBJTYP_MAP[srcobjtyp]
            sap_table = config["sap_table"]

            # Try enriched moves (sap_table = oinv/opch/orct/etc.)
            moves = ctx.env["account.move"].search([
                ("sap_docentry", "in", list(doc_ids)),
                ("sap_table", "=", sap_table),
                ("state", "=", "posted"),
            ])
            for m in moves:
                doc_move_map[(srcobjtyp, m.sap_docentry)] = m.id

            # For any not found, try generic JDT1 moves (sap_table='ojdt')
            missing = doc_ids - {m.sap_docentry for m in moves}
            if missing:
                transids = [
                    ojdt_transid_map[(srcobjtyp, did)]
                    for did in missing
                    if (srcobjtyp, did) in ojdt_transid_map
                ]
                if transids:
                    ojdt_moves = ctx.env["account.move"].search([
                        ("sap_docentry", "in", transids),
                        ("sap_table", "=", "ojdt"),
                        ("state", "=", "posted"),
                    ])
                    # Map back: we need (srcobjtyp, original_doc_id) -> move_id
                    transid_to_move = {m.sap_docentry: m.id for m in ojdt_moves}
                    for did in missing:
                        transid = ojdt_transid_map.get((srcobjtyp, did))
                        if transid and transid in transid_to_move:
                            doc_move_map[(srcobjtyp, did)] = transid_to_move[transid]

        _logger.info("[ITR] Pre-loaded %d document moves", len(doc_move_map))

        return ChunkableData(
            records=groups,
            context={"doc_move_map": doc_move_map},
        )

    @ETL.transform()
    def transform_internal_reconciliations(self, ctx: ETLContext, extracted):
        """Map each reconnum group to Odoo move IDs.

        Only keeps groups where at least 2 distinct Odoo moves are found.
        """
        data = extracted.get("extract_internal_reconciliations")
        groups = data.records if data else []
        cache = data.context if data else {}
        doc_move_map = cache.get("doc_move_map", {})

        reconciliation_groups = []

        for group in groups:
            reconnum = group["reconnum"]
            move_ids = set()

            for line in group["lines"]:
                mid = doc_move_map.get((line["srcobjtyp"], line["doc_id"]))
                if mid:
                    move_ids.add(mid)

            if len(move_ids) >= 2:
                reconciliation_groups.append({
                    "reconnum": reconnum,
                    "move_ids": list(move_ids),
                })

        _logger.info(
            "[ITR] %d groups have 2+ mapped moves (out of %d total)",
            len(reconciliation_groups), len(groups),
        )
        return reconciliation_groups

    @ETL.load()
    def load_internal_reconciliations(self, ctx: ETLContext, transformed):
        """Reconcile receivable/payable lines within each ITR group."""
        groups = transformed.get("transform_internal_reconciliations", [])

        if not groups:
            _logger.info("[ITR] No reconciliation groups in chunk")
            return

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

            # Collect unreconciled receivable/payable lines
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
            "[ITR] Chunk complete: %d reconciled, %d already done, %d skipped (one-sided)",
            reconciled_count, already_done, skipped_one_sided,
        )
