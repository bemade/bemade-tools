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
#
# Types 13/14/18/19 have enriched moves (sap_table = oinv/orin/opch/orpc).
# All other types fall back to generic JDT1 moves (sap_table = 'ojdt')
# via the OJDT createdby → transid reverse lookup.
_SRCOBJTYP_MAP = {
    # Enriched document types
    "13": {"sap_table": "oinv", "transtype": "13"},   # A/R Invoice
    "14": {"sap_table": "orin", "transtype": "14"},   # A/R Credit Memo
    "18": {"sap_table": "opch", "transtype": "18"},   # A/P Invoice
    "19": {"sap_table": "orpc", "transtype": "19"},   # A/P Credit Memo
    # Payment types (no enriched moves — always use OJDT fallback)
    "24": {"sap_table": "orct", "transtype": "24"},   # Incoming Payment
    "46": {"sap_table": "ovpm", "transtype": "46"},   # Outgoing Payment
    # Inventory & production (OJDT fallback)
    "20": {"sap_table": "opdn", "transtype": "20"},   # Goods Receipt PO
    "21": {"sap_table": "orpd", "transtype": "21"},   # Goods Return
    "59": {"sap_table": "oign", "transtype": "59"},   # Goods Receipt
    "60": {"sap_table": "oige", "transtype": "60"},   # Goods Issue
    "-3": {"sap_table": "owtr", "transtype": "-3"},   # Inventory Transfer
    "-4": {"sap_table": "oiqr", "transtype": "-4"},   # Initial Quantities
    "-5": {"sap_table": "oiqr", "transtype": "-5"},   # Misc Inventory
    "202": {"sap_table": "owor", "transtype": "202"}, # Production Order
    # Financial types (OJDT fallback)
    "25": {"sap_table": "odpo", "transtype": "25"},   # A/P Down Payment
    "30": {"sap_table": "ojdt", "transtype": "30"},   # Journal Entry
    "203": {"sap_table": "orin", "transtype": "203"}, # Correction Invoice AR
    "204": {"sap_table": "orpc", "transtype": "204"}, # Correction Invoice AP
    "321": {"sap_table": "oitr", "transtype": "321"}, # Internal Reconciliation
}


def _reconcile_capped(lines_with_caps):
    """Reconcile lines with per-line amount caps from SAP reconsum.

    Like ``account.move.line.reconcile()`` but caps each line's residual
    to its SAP ``reconsum`` amount before the reconciliation algorithm
    runs.  This prevents Odoo's greedy matching from over-allocating one
    member at the expense of another.

    Args:
        lines_with_caps: list of ``(account.move.line, cap_amount)``
            tuples.  All lines must be on the same account.
            ``cap_amount`` is the absolute SAP reconsum for that member.
    """
    from odoo.fields import Command

    AML = lines_with_caps[0][0].env["account.move.line"]
    amls = AML.browse()
    for line, _ in lines_with_caps:
        amls |= line
    caps = {line.id: cap for line, cap in lines_with_caps}

    plan_list = [{"amls": amls, "aml_ids": set(amls.ids)}]

    move_container = {"records": amls.move_id}
    with amls.move_id._check_balanced(move_container), \
         amls.move_id._sync_dynamic_lines(move_container):

        # Prefetch (mirrors _reconcile_plan_with_sync)
        amls.move_id  # noqa: B018
        amls.matched_debit_ids  # noqa: B018
        amls.matched_credit_ids  # noqa: B018

        pre_hook_data = amls._reconcile_pre_hook()

        # Build values map with capped residuals
        aml_values_map = {}
        for aml in amls:
            vals = {
                "aml": aml,
                "amount_residual": aml.amount_residual,
                "amount_residual_currency": aml.amount_residual_currency,
                "parent_state": aml.parent_state,
            }
            cap = caps.get(aml.id, 0)
            if cap and abs(vals["amount_residual_currency"]) > cap + 0.005:
                sign = -1 if vals["amount_residual_currency"] < 0 else 1
                rate = (
                    vals["amount_residual"] / vals["amount_residual_currency"]
                    if vals["amount_residual_currency"]
                    else 1.0
                )
                vals["amount_residual_currency"] = sign * cap
                vals["amount_residual"] = round(sign * cap * rate, 2)
            aml_values_map[aml] = vals

        # Prepare partials + exchange diffs
        partials_values_list = []
        exchange_diff_values_list = []
        all_plan_results = []
        for plan in plan_list:
            plan_results = AML._prepare_reconciliation_plan(
                plan, aml_values_map
            )
            all_plan_results.append(plan_results)
            for results in plan_results:
                partials_values_list.append(results["partial_values"])
                if (
                    results.get("exchange_values")
                    and results["exchange_values"]["move_values"]["line_ids"]
                ):
                    exchange_diff_values_list.append(
                        results["exchange_values"]
                    )

        if not partials_values_list:
            amls._reconcile_post_hook(pre_hook_data)
            return

        # Create partials
        partials = AML.env["account.partial.reconcile"].create(
            partials_values_list
        )
        start_range = 0
        for plan_results, plan in zip(all_plan_results, plan_list):
            size = len(plan_results)
            plan["partials"] = partials[start_range:start_range + size]
            start_range += size

        # Create exchange difference moves
        exchange_moves = AML._create_exchange_difference_moves(
            exchange_diff_values_list
        )
        used_exchange_moves = set()
        used_partials = set()
        for partial in partials:
            for exchange_move in exchange_moves:
                linked = exchange_move.line_ids.reconciled_lines_ids
                if (
                    any(
                        line == partial.debit_move_id
                        or line == partial.credit_move_id
                        for line in linked
                    )
                    and exchange_move not in used_exchange_moves
                    and partial not in used_partials
                ):
                    partial.exchange_move_id = exchange_move
                    used_exchange_moves.add(exchange_move)
                    used_partials.add(partial)

        # Full reconcile: check if all lines are now fully matched
        number2lines = amls._reconciled_by_number()
        for plan in plan_list:
            involved = plan["amls"]._filter_reconciled_by_number(
                number2lines
            )
            has_multi = len(involved.currency_id) > 1
            if all(
                aml.reconciled
                or (
                    has_multi
                    and aml.company_currency_id.is_zero(aml.amount_residual)
                )
                or (
                    not has_multi
                    and aml.currency_id.is_zero(
                        aml.amount_residual_currency
                    )
                )
                for aml in involved
                if aml.matched_debit_ids or aml.matched_credit_ids
            ):
                involved_partials = (
                    involved.matched_debit_ids + involved.matched_credit_ids
                )
                AML.env["account.full.reconcile"].create({
                    "partial_reconcile_ids": [
                        Command.link(p.id) for p in involved_partials
                    ],
                    "reconciled_line_ids": [
                        Command.link(a.id) for a in involved
                    ],
                })

        amls._reconcile_post_hook(pre_hook_data)


@ETL.pipeline(
    target_model="account.move",
    importer_name="account.internal.reconciliation",
    sap_source="itr1",
    depends_on=[
        "account.move.jdt1.importer",
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
                no_ojdt = len(missing) - len(transids)
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
                    not_in_odoo = len(transids) - len(ojdt_moves)
                else:
                    not_in_odoo = 0

                _logger.info(
                    "[ITR] Type %s (%s): %d docs, %d enriched, "
                    "%d missing -> %d no OJDT mapping, %d OJDT not in Odoo",
                    srcobjtyp, sap_table, len(doc_ids), len(moves),
                    len(missing), no_ojdt, not_in_odoo,
                )

        _logger.info("[ITR] Pre-loaded %d document moves", len(doc_move_map))

        return ChunkableData(
            records=groups,
            context={"doc_move_map": doc_move_map},
        )

    @ETL.transform()
    def transform_internal_reconciliations(self, ctx: ETLContext, extracted):
        """Map each reconnum group to Odoo move IDs with reconsum caps.

        Only keeps groups where at least 2 distinct Odoo moves are found.
        Each member carries its SAP ``reconsum`` so the load phase can cap
        each line's contribution and avoid greedy over-allocation.
        """
        data = extracted.get("extract_internal_reconciliations")
        groups = data.records if data else []
        cache = data.context if data else {}
        doc_move_map = cache.get("doc_move_map", {})

        reconciliation_groups = []

        for group in groups:
            reconnum = group["reconnum"]
            members = []
            seen_move_ids = set()

            for line in group["lines"]:
                mid = doc_move_map.get((line["srcobjtyp"], line["doc_id"]))
                if mid:
                    members.append({
                        "move_id": mid,
                        "reconsum": abs(float(line["reconciled_amount"])),
                        "iscredit": line["iscredit"],
                    })
                    seen_move_ids.add(mid)

            if len(seen_move_ids) >= 2:
                reconciliation_groups.append({
                    "reconnum": reconnum,
                    "members": members,
                })

        _logger.info(
            "[ITR] %d groups have 2+ mapped moves (out of %d total)",
            len(reconciliation_groups), len(groups),
        )
        return reconciliation_groups

    @ETL.load()
    def load_internal_reconciliations(self, ctx: ETLContext, transformed):
        """Reconcile receivable/payable lines within each ITR group.

        Uses SAP's ``reconsum`` to cap each line's contribution, preventing
        Odoo's greedy reconciliation from misallocating amounts across
        members of the group.
        """
        groups = transformed.get("transform_internal_reconciliations", [])

        if not groups:
            _logger.info("[ITR] No reconciliation groups in chunk")
            return

        all_move_ids = list({
            m["move_id"] for g in groups for m in g["members"]
        })
        all_moves = ctx.env["account.move"].browse(all_move_ids)
        moves_by_id = {m.id: m for m in all_moves}

        reconciled_count = 0
        already_done = 0
        skipped_one_sided = 0

        for group in groups:
            reconnum = group["reconnum"]

            # Build (line, cap) pairs from members.
            # Each member maps to the unreconciled receivable/payable line(s)
            # on its move, capped at the SAP reconsum amount.
            lines_with_caps = []
            for member in group["members"]:
                move = moves_by_id.get(member["move_id"])
                if not move:
                    continue
                arap_lines = move.line_ids.filtered(
                    lambda l: l.account_id.account_type
                    in ("asset_receivable", "liability_payable")
                    and not l.reconciled
                )
                if not arap_lines:
                    continue
                # If multiple AR/AP lines on the same move (rare), pick the
                # one whose sign matches the member role (debit for 'D',
                # credit for 'C') and has the largest residual.
                if len(arap_lines) > 1:
                    if member["iscredit"] == "C":
                        candidates = arap_lines.filtered(lambda l: l.credit > 0)
                    else:
                        candidates = arap_lines.filtered(lambda l: l.debit > 0)
                    arap_lines = candidates or arap_lines
                line = max(arap_lines, key=lambda l: abs(l.amount_residual))
                lines_with_caps.append((line, member["reconsum"]))

            if not lines_with_caps:
                already_done += 1
                continue

            # Group by account — reconciliation requires same account
            by_account = defaultdict(list)
            for line, cap in lines_with_caps:
                by_account[line.account_id.id].append((line, cap))

            group_reconciled = False
            for account_id, account_pairs in by_account.items():
                has_debit = any(l.debit > 0 for l, _ in account_pairs)
                has_credit = any(l.credit > 0 for l, _ in account_pairs)
                if not (has_debit and has_credit):
                    continue

                with ctx.skippable(f"ITR group {reconnum} account {account_id}"):
                    _reconcile_capped(account_pairs)
                    group_reconciled = True

            if group_reconciled:
                reconciled_count += 1
            else:
                skipped_one_sided += 1

        _logger.info(
            "[ITR] Chunk complete: %d reconciled, %d already done, %d skipped (one-sided)",
            reconciled_count, already_done, skipped_one_sided,
        )
