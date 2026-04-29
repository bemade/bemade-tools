"""Reconciliation helpers for the SAP B1 to Odoo pipelines.

The single public function here, :func:`reconcile_at_amount`, creates an
``account.partial.reconcile`` of an exact amount between two journal items,
bypassing Odoo's standard greedy planner.  This is the same mechanism used by
the ``qbo_to_odoo.move_posting_helpers`` module: when the source system
records pairwise applications with explicit amounts (QBO's ``LinkedTxn``,
SAP's RCT2/VPM2 ``SumApplied``), we want exactly that amount on the partial,
not whatever Odoo's planner would compute from full residuals.

For the ITR1 fallback case (where SAP only records a group-shaped match
without per-pair amounts), prefer ``account.move.line.reconcile()`` directly
-- it does the same FIFO + partial creation + exchange diffs + full-reconcile
stitching out of the box and produces correct books as long as the AMLs'
residuals already reflect what SAP wants reconciled.
"""

import logging
from collections import defaultdict

from odoo.fields import Command

_logger = logging.getLogger(__name__)


# SAP object-type code -> (enriched sap_table, OJDT transtype).
#
# ``sap_table`` is the table name that an *enriched* importer would set on
# the resulting ``account.move``.  For object types that have no enriched
# importer in this codebase, the enriched lookup just returns 0 rows and
# the resolver falls through to the OJDT path (createdby -> transid).
#
# CRITICAL: the ``sap_table`` value here MUST NOT collide with the table of
# any *unrelated* enriched importer, because the lookup uses ``sap_table``
# as a discriminator on ``account.move``.  In particular:
#   * 203 (Correction Invoice AR) is *not* the same as 14 (AR Credit Memo)
#     -- they live in OCRN vs ORIN.  Mapping 203 to 'orin' here would have
#     the resolver pick up an unrelated AR credit memo with the same
#     docentry, silently creating wrong reconciliations.
#   * Same caution for 204 (Correction Invoice AP) vs 19 (AP Credit Memo).
SRCOBJTYP_MAP = {
    # Enriched document types
    "13": {"sap_table": "oinv", "transtype": "13"},   # A/R Invoice
    "14": {"sap_table": "orin", "transtype": "14"},   # A/R Credit Memo
    "18": {"sap_table": "opch", "transtype": "18"},   # A/P Invoice
    "19": {"sap_table": "orpc", "transtype": "19"},   # A/P Credit Memo
    # Payment types (no enriched importer; OJDT fallback)
    "24": {"sap_table": "orct", "transtype": "24"},   # Incoming Payment
    "46": {"sap_table": "ovpm", "transtype": "46"},   # Outgoing Payment
    # Inventory & production (no enriched importer; OJDT fallback)
    "20": {"sap_table": "opdn", "transtype": "20"},   # Goods Receipt PO
    "21": {"sap_table": "orpd", "transtype": "21"},   # Goods Return
    "59": {"sap_table": "oign", "transtype": "59"},   # Goods Receipt
    "60": {"sap_table": "oige", "transtype": "60"},   # Goods Issue
    "-3": {"sap_table": "owtr", "transtype": "-3"},   # Inventory Transfer
    "-4": {"sap_table": "oiqr", "transtype": "-4"},   # Initial Quantities
    "-5": {"sap_table": "oiqr", "transtype": "-5"},   # Misc Inventory
    "202": {"sap_table": "owor", "transtype": "202"}, # Production Order
    # Financial / correction types (no enriched importer; OJDT fallback)
    "25": {"sap_table": "odpo", "transtype": "25"},   # A/P Down Payment
    "30": {"sap_table": "ojdt", "transtype": "30"},   # Journal Entry
    "203": {"sap_table": "ocrn", "transtype": "203"}, # Correction Invoice AR (NOT orin)
    "204": {"sap_table": "ocpv", "transtype": "204"}, # Correction Invoice AP (NOT orpc)
    "321": {"sap_table": "oitr", "transtype": "321"}, # Internal Reconciliation
}


def resolve_doc_to_move_map(cr, env, doc_ids_by_type):
    """Build a ``(srcobjtyp, sap_docentry) -> account.move id`` map.

    Tries enriched moves first (``sap_table = oinv|orin|opch|orpc|orct|ovpm``)
    and falls back to generic JDT1 moves (``sap_table = 'ojdt'``) using the
    OJDT ``createdby -> transid`` reverse lookup for any missing docs.

    Args:
        cr: Database cursor (used to query OJDT for the fallback lookup).
        env: Odoo environment for searching ``account.move``.
        doc_ids_by_type: ``{srcobjtyp: set(int doc_ids)}``.

    Returns:
        ``{(srcobjtyp, doc_id): move_id}`` -- only entries that resolved.
    """
    # OJDT fallback table for non-enriched types
    ojdt_transid_map = {}
    for srcobjtyp, doc_ids in doc_ids_by_type.items():
        if not doc_ids or srcobjtyp not in SRCOBJTYP_MAP:
            continue
        transtype = SRCOBJTYP_MAP[srcobjtyp]["transtype"]
        cr.execute(
            "SELECT createdby, transid FROM ojdt"
            " WHERE transtype = %s AND createdby IN %s",
            (transtype, tuple(doc_ids)),
        )
        for row in cr.fetchall():
            ojdt_transid_map[(srcobjtyp, row[0])] = row[1]

    doc_move_map = {}
    for srcobjtyp, doc_ids in doc_ids_by_type.items():
        if not doc_ids or srcobjtyp not in SRCOBJTYP_MAP:
            continue
        sap_table = SRCOBJTYP_MAP[srcobjtyp]["sap_table"]

        moves = env["account.move"].search([
            ("sap_docentry", "in", list(doc_ids)),
            ("sap_table", "=", sap_table),
            ("state", "=", "posted"),
        ])
        for m in moves:
            doc_move_map[(srcobjtyp, m.sap_docentry)] = m.id

        missing = doc_ids - {m.sap_docentry for m in moves}
        if not missing:
            continue
        transids = [
            ojdt_transid_map[(srcobjtyp, did)]
            for did in missing
            if (srcobjtyp, did) in ojdt_transid_map
        ]
        if not transids:
            continue
        ojdt_moves = env["account.move"].search([
            ("sap_docentry", "in", transids),
            ("sap_table", "=", "ojdt"),
            ("state", "=", "posted"),
        ])
        transid_to_move = {m.sap_docentry: m.id for m in ojdt_moves}
        for did in missing:
            tid = ojdt_transid_map.get((srcobjtyp, did))
            if tid and tid in transid_to_move:
                doc_move_map[(srcobjtyp, did)] = transid_to_move[tid]

    return doc_move_map


def pick_open_arap_line(move):
    """Return the single unreconciled AR/AP line with the largest residual.

    Used by the RCT2/VPM2 pairwise importer where each payment-to-target
    application targets one AR/AP control line per move.  Multi-line cases
    (e.g. internal-reconciliation bridge JEs that post on both AR and AP
    control accounts) need :func:`pick_open_arap_lines` instead.
    """
    arap = move.line_ids.filtered(
        lambda l: l.account_id.account_type
        in ("asset_receivable", "liability_payable")
        and not l.reconciled
    )
    if not arap:
        return move.env["account.move.line"]
    if len(arap) == 1:
        return arap
    return arap.sorted(
        key=lambda l: abs(l.amount_residual_currency), reverse=True
    )[:1]


def pick_open_arap_lines(move):
    """Return *all* unreconciled AR/AP control lines on ``move``.

    Required for OITR groups whose members include the OITR bridge JE
    (transtype 321 "Manual Reconciliation Transaction"): that JE posts a
    debit on one AR/AP control account and a credit on another, and both
    lines must participate in reconciliation -- otherwise the bucket on
    one of the two accounts ends up one-sided and fails to reconcile.
    """
    return move.line_ids.filtered(
        lambda l: l.account_id.account_type
        in ("asset_receivable", "liability_payable")
        and not l.reconciled
    )


def reconcile_at_amount(line_a, line_b, amount_currency):
    """Create one ``account.partial.reconcile`` of exact ``amount_currency``.

    Caps both lines' residuals to ``amount_currency`` so that Odoo's standard
    plan builder produces a single partial at exactly that amount.  Exchange
    difference moves are created by Odoo when the two lines are in different
    currencies and linked back to the new partial.

    Both arguments are interchangeable with respect to debit/credit -- Odoo
    sorts the pair internally.

    Args:
        line_a: First ``account.move.line`` (AR/AP control line).
        line_b: Second ``account.move.line`` (AR/AP control line).
        amount_currency: Amount to reconcile, in the transaction currency
            (the currency in which ``amount_residual_currency`` is expressed
            on the lines).
    """
    AML = line_a.env["account.move.line"]
    amls = line_a + line_b
    if len(amls) < 2:
        return AML.env["account.partial.reconcile"]

    plan_list = [{"amls": amls, "aml_ids": set(amls.ids)}]

    move_container = {"records": amls.move_id}
    with amls.move_id._check_balanced(move_container), \
         amls.move_id._sync_dynamic_lines(move_container):

        amls.move_id           # prefetch
        amls.matched_debit_ids
        amls.matched_credit_ids

        pre_hook_data = amls._reconcile_pre_hook()

        cap = abs(amount_currency)
        aml_values_map = {}
        for aml in amls:
            vals = {
                "aml": aml,
                "amount_residual": aml.amount_residual,
                "amount_residual_currency": aml.amount_residual_currency,
                "parent_state": aml.parent_state,
            }
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
                    exchange_diff_values_list.append(results["exchange_values"])

        if not partials_values_list:
            amls._reconcile_post_hook(pre_hook_data)
            return AML.env["account.partial.reconcile"]

        partials = AML.env["account.partial.reconcile"].create(
            partials_values_list
        )
        start_range = 0
        for plan_results, plan in zip(all_plan_results, plan_list):
            size = len(plan_results)
            plan["partials"] = partials[start_range:start_range + size]
            start_range += size

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

        # Stitch a full reconcile if the pair is now zeroed.
        number2lines = amls._reconciled_by_number()
        involved = amls._filter_reconciled_by_number(number2lines) or amls
        has_multi = len(involved.currency_id) > 1
        if all(
            aml.reconciled
            or (
                has_multi
                and aml.company_currency_id.is_zero(aml.amount_residual)
            )
            or (
                not has_multi
                and aml.currency_id.is_zero(aml.amount_residual_currency)
            )
            for aml in involved
            if aml.matched_debit_ids or aml.matched_credit_ids
        ):
            involved_partials = (
                involved.matched_debit_ids | involved.matched_credit_ids
            )
            if involved_partials:
                AML.env["account.full.reconcile"].create({
                    "partial_reconcile_ids": [
                        Command.link(p.id) for p in involved_partials
                    ],
                    "reconciled_line_ids": [
                        Command.link(a.id) for a in involved
                    ],
                })

        amls._reconcile_post_hook(pre_hook_data)

    return partials
