"""ETL Pipeline for SAP Internal Reconciliation (OITR/ITR1).

SAP Internal Reconciliation is the equivalent of Odoo's
account.partial.reconcile / account.full.reconcile — it records which
journal items were matched together (e.g. an invoice with its payment).

This pipeline reads ITR groups from SAP and directly reconciles the
corresponding receivable/payable lines in Odoo using a FIFO-bipartite
allocator that walks members in SAP-recorded ``lineseq`` order.

Design
------
Each OITR group contains a set of debit-side and credit-side members
(determined by ``iscredit``).  The allocator walks both sides in
``lineseq`` order, consuming each member's ``reconsum`` amount, and
emits ``(debit_aml, credit_aml, amount)`` triples.  Each triple is
turned into an ``account.partial.reconcile`` directly — bypassing
Odoo's greedy ``_prepare_reconciliation_plan`` — using the same low-
level pattern as ``qbo_to_odoo.move_posting_helpers.reconcile_at_amount``
but generalised over an arbitrary bipartite group.

After all partials for the group are created, a single
``account.full.reconcile`` is emitted when every member's residual is
zero (or within currency epsilon).

Works with both enriched moves (sap_table = oinv/opch/etc.) and generic
JDT1-pipeline moves (sap_table = ojdt).
"""

import logging
from collections import defaultdict

from odoo import models
from odoo.addons.etl_framework import ETL, ETLContext, ChunkableData
from odoo.fields import Command

_logger = logging.getLogger(__name__)

# ITR1.srcobjtyp -> (sap_table for enriched moves, transtype for OJDT lookup)
#
# Types 13/14/18/19 have enriched moves (sap_table = oinv/orin/opch/orpc).
# All other types fall back to generic JDT1 moves (sap_table = 'ojdt')
# via the OJDT createdby -> transid reverse lookup.
_SRCOBJTYP_MAP = {
    # Enriched document types
    "13": {"sap_table": "oinv", "transtype": "13"},   # A/R Invoice
    "14": {"sap_table": "orin", "transtype": "14"},   # A/R Credit Memo
    "18": {"sap_table": "opch", "transtype": "18"},   # A/P Invoice
    "19": {"sap_table": "orpc", "transtype": "19"},   # A/P Credit Memo
    # Payment types (no enriched moves -- always use OJDT fallback)
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


# ---------------------------------------------------------------------------
# Pure allocator -- no Odoo dependency; testable in isolation
# ---------------------------------------------------------------------------

def allocate_fifo(debits, credits):
    """FIFO bipartite allocator for a single OITR account bucket.

    Walks debit-side and credit-side members in SAP-recorded ``lineseq``
    order (callers must pre-sort by ``lineseq`` before calling), consuming
    each member's ``reconsum`` amount and emitting ``(debit_aml,
    credit_aml, amount)`` triples.

    Args:
        debits: list of ``{"aml": account.move.line, "reconsum": float}``
            dicts, **already sorted by lineseq**.
        credits: list of ``{"aml": account.move.line, "reconsum": float}``
            dicts, **already sorted by lineseq**.

    Returns:
        List of ``(debit_aml, credit_aml, amount)`` triples.  Amount is
        always positive and in the transaction currency (the same currency
        that SAP's ``reconsum`` / ``reconsumsc`` is expressed in).
    """
    d_caps = [d["reconsum"] for d in debits]
    c_caps = [c["reconsum"] for c in credits]

    triples = []
    di = 0
    ci = 0

    while di < len(debits) and ci < len(credits):
        # Advance past exhausted entries
        while di < len(debits) and d_caps[di] < 0.005:
            di += 1
        while ci < len(credits) and c_caps[ci] < 0.005:
            ci += 1

        if di >= len(debits) or ci >= len(credits):
            break

        amount = round(min(d_caps[di], c_caps[ci]), 2)
        triples.append((debits[di]["aml"], credits[ci]["aml"], amount))
        d_caps[di] = round(d_caps[di] - amount, 2)
        c_caps[ci] = round(c_caps[ci] - amount, 2)

    return triples


# ---------------------------------------------------------------------------
# ORM helpers -- require an Odoo environment
# ---------------------------------------------------------------------------

def _create_partial_for_triple(debit_aml, credit_aml, amount_currency):
    """Create one ``account.partial.reconcile`` for a FIFO triple.

    Mirrors ``reconcile_at_amount`` from
    ``qbo_to_odoo.move_posting_helpers`` but operates on a pre-determined
    (debit_aml, credit_aml, amount) triple rather than finding the pair
    itself.  Exchange difference entries are created by the standard Odoo
    machinery if the two lines are in different currencies.

    Args:
        debit_aml: The debit-side ``account.move.line``.
        credit_aml: The credit-side ``account.move.line``.
        amount_currency: Exact amount to reconcile in the transaction
            currency (from SAP ``reconsum``).

    Returns:
        The created ``account.partial.reconcile`` recordset (may be empty
        if Odoo's planner finds nothing to create).
    """
    AML = debit_aml.env["account.move.line"]
    amls = debit_aml + credit_aml

    plan_list = [{"amls": amls, "aml_ids": set(amls.ids)}]

    move_container = {"records": amls.move_id}
    with amls.move_id._check_balanced(move_container), \
         amls.move_id._sync_dynamic_lines(move_container):

        amls.move_id           # prefetch
        amls.matched_debit_ids
        amls.matched_credit_ids

        pre_hook_data = amls._reconcile_pre_hook()

        # Build values map -- cap both sides to `amount_currency` so the
        # standard plan builder produces exactly one partial at that amount.
        aml_values_map = {}
        for aml in amls:
            vals = {
                "aml": aml,
                "amount_residual": aml.amount_residual,
                "amount_residual_currency": aml.amount_residual_currency,
                "parent_state": aml.parent_state,
            }
            cap = abs(amount_currency)
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

        amls._reconcile_post_hook(pre_hook_data)

    return partials


def _stitch_full_reconcile(all_amls):
    """Emit ``account.full.reconcile`` if every member's residual is zero.

    Args:
        all_amls: ``account.move.line`` recordset containing all members
            of the group (across all account buckets that were reconciled).
    """
    AML = all_amls.env["account.move.line"]
    number2lines = all_amls._reconciled_by_number()
    involved = all_amls._filter_reconciled_by_number(number2lines)
    if not involved:
        involved = all_amls

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
        if not involved_partials:
            return
        AML.env["account.full.reconcile"].create({
            "partial_reconcile_ids": [
                Command.link(p.id) for p in involved_partials
            ],
            "reconciled_line_ids": [
                Command.link(a.id) for a in involved
            ],
        })


@ETL.pipeline(
    target_model="account.move",
    importer_name="account.internal.reconciliation",
    sap_source="itr1",
    depends_on=[
        "account.move.jdt1.importer",
    ],
    multiprocessing_threshold=500,
    chunk_size=200,
    max_workers=4,
)
class AccountInternalReconciliation(models.AbstractModel):
    _name = "account.internal.reconciliation"
    _description = "SAP Internal Reconciliation - Reconcile document groups from ITR"

    @ETL.extract("itr1")
    def extract_internal_reconciliations(self, ctx: ETLContext):
        """Extract all ITR1 lines grouped by reconnum.

        The query now also fetches ``lineseq`` (for FIFO ordering),
        ``account`` (GL account code, for per-account bucketing) and
        ``reconsumsc`` (source-currency reconsum, available for cross-
        currency defensive handling).
        """
        _logger.info("[ITR] Extracting reconciliation groups from SAP...")

        ctx.cr.execute(
            """
            SELECT
                r.reconnum,
                r.lineseq,
                r.srcobjtyp,
                r.srcobjabs::integer AS doc_id,
                r.iscredit,
                r.reconsum   AS reconciled_amount,
                r.reconsumsc AS reconciled_amount_sc,
                r.account
            FROM itr1 r
            JOIN oitr h ON r.reconnum = h.reconnum
            WHERE h.canceled = 'N'
            ORDER BY r.reconnum, r.lineseq
            """
        )
        all_lines = ctx.cr.dictfetchall()

        # Group by reconnum (ORDER BY reconnum, lineseq is preserved above)
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
        doc_move_map = {}  # (srcobjtyp, docentry) -> move_id

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
        """Map each reconnum group to Odoo move IDs with lineseq metadata.

        Each member now carries ``lineseq`` (for FIFO ordering),
        ``reconsum`` (absolute allocated amount), ``iscredit`` (side), and
        ``account`` (GL code used for per-account bucketing in load phase).

        Groups where *any* member's Odoo move is missing are dropped
        entirely with a warning: a partial allocation would corrupt the
        books by reconciling a sub-set of the group and leaving orphaned
        residuals.
        """
        data = extracted.get("extract_internal_reconciliations")
        groups = data.records if data else []
        cache = data.context if data else {}
        doc_move_map = cache.get("doc_move_map", {})

        reconciliation_groups = []
        dropped_partial = 0

        for group in groups:
            reconnum = group["reconnum"]
            members = []
            group_ok = True

            for line in group["lines"]:
                mid = doc_move_map.get((line["srcobjtyp"], line["doc_id"]))
                if mid is None:
                    # Member not in Odoo -- drop the whole group
                    group_ok = False
                    _logger.debug(
                        "[ITR] reconnum %s: member srcobjtyp=%s doc_id=%s not "
                        "found in Odoo -- dropping group",
                        reconnum, line["srcobjtyp"], line["doc_id"],
                    )
                    break
                members.append({
                    "move_id": mid,
                    "lineseq": int(line["lineseq"]),
                    "reconsum": abs(float(line["reconciled_amount"])),
                    "iscredit": line["iscredit"],
                    "account": line["account"],
                })

            if not group_ok:
                dropped_partial += 1
                continue

            # Need at least two distinct moves
            seen_move_ids = {m["move_id"] for m in members}
            if len(seen_move_ids) >= 2:
                reconciliation_groups.append({
                    "reconnum": reconnum,
                    "members": members,
                })

        _logger.info(
            "[ITR] %d groups ready (out of %d total); %d dropped "
            "(missing Odoo move)",
            len(reconciliation_groups), len(groups), dropped_partial,
        )
        return reconciliation_groups

    @ETL.load()
    def load_internal_reconciliations(self, ctx: ETLContext, transformed):
        """Reconcile receivable/payable lines within each ITR group.

        For each group:

        1. **Idempotency guard** -- skip if any ``account.partial.reconcile``
           already links two of the group's member AMLs.
        2. **AML selection** -- for each member, find the AR/AP line on its
           move that matches the ``iscredit`` side; warn if multiple
           candidates exist (but continue with the largest-residual one).
        3. **Per-account bucket** -- bucket AMLs by ``account_id``; OITR
           groups spanning multiple AR/AP accounts are handled by running
           the allocator independently per account.
        4. **FIFO allocate** -- call :func:`allocate_fifo` (sorted by
           ``lineseq``) to get ``(debit_aml, credit_aml, amount)`` triples.
        5. **Create partials** -- one ``account.partial.reconcile`` per
           triple via :func:`_create_partial_for_triple`.
        6. **Full reconcile** -- stitch a single ``account.full.reconcile``
           when every member's residual is zero.
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
            members = group["members"]

            # -- Step 1: per-group idempotency guard -----------------------
            # Collect all AR/AP AML candidates and check for existing
            # partials that already link two of them.
            candidate_aml_ids = set()
            for member in members:
                move = moves_by_id.get(member["move_id"])
                if not move:
                    continue
                for aml in move.line_ids:
                    if aml.account_id.account_type in (
                        "asset_receivable", "liability_payable"
                    ):
                        candidate_aml_ids.add(aml.id)

            if candidate_aml_ids:
                existing = ctx.env["account.partial.reconcile"].search([
                    ("debit_move_id", "in", list(candidate_aml_ids)),
                    ("credit_move_id", "in", list(candidate_aml_ids)),
                ], limit=1)
                if existing:
                    _logger.debug(
                        "[ITR] group %s already reconciled -- skipped",
                        reconnum,
                    )
                    already_done += 1
                    continue

            # -- Step 2: resolve AMLs from moves ---------------------------
            member_amls = []
            for member in members:
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

                # Sign-match: credit side -> credit > 0, debit -> debit > 0
                if len(arap_lines) > 1:
                    if member["iscredit"] == "C":
                        candidates = arap_lines.filtered(lambda l: l.credit > 0)
                    else:
                        candidates = arap_lines.filtered(lambda l: l.debit > 0)
                    if candidates and len(candidates) > 1:
                        _logger.warning(
                            "[ITR] group %s move %s: %d AR/AP candidates on "
                            "account -- using largest residual",
                            reconnum, member["move_id"], len(candidates),
                        )
                    arap_lines = candidates or arap_lines

                aml = max(arap_lines, key=lambda l: abs(l.amount_residual))
                member_amls.append({
                    "lineseq": member["lineseq"],
                    "reconsum": member["reconsum"],
                    "iscredit": member["iscredit"],
                    "aml": aml,
                })

            if not member_amls:
                already_done += 1
                continue

            # -- Step 3: bucket by account ---------------------------------
            by_account = defaultdict(lambda: {"debits": [], "credits": []})
            for m in member_amls:
                side = "credits" if m["iscredit"] == "C" else "debits"
                by_account[m["aml"].account_id.id][side].append({
                    "aml": m["aml"],
                    "lineseq": m["lineseq"],
                    "reconsum": m["reconsum"],
                })

            all_created_amls = ctx.env["account.move.line"]
            group_reconciled = False

            for account_id, bucket in by_account.items():
                debits = sorted(bucket["debits"], key=lambda x: x["lineseq"])
                credits = sorted(bucket["credits"], key=lambda x: x["lineseq"])

                if not debits or not credits:
                    _logger.debug(
                        "[ITR] group %s account %s: one-sided bucket "
                        "(debits=%d credits=%d) -- skipping bucket",
                        reconnum, account_id, len(debits), len(credits),
                    )
                    continue

                # -- Step 4: FIFO allocate ---------------------------------
                triples = allocate_fifo(debits, credits)
                if not triples:
                    continue

                # -- Step 5: create one partial per triple -----------------
                with ctx.skippable(
                    f"ITR group {reconnum} account {account_id}"
                ):
                    for debit_aml, credit_aml, amount in triples:
                        _create_partial_for_triple(
                            debit_aml, credit_aml, amount
                        )
                        all_created_amls |= debit_aml | credit_aml
                    group_reconciled = True

            if not group_reconciled:
                skipped_one_sided += 1
                continue

            # -- Step 6: stitch full reconcile -----------------------------
            if all_created_amls:
                with ctx.skippable(
                    f"ITR group {reconnum} full reconcile"
                ):
                    _stitch_full_reconcile(all_created_amls)

            reconciled_count += 1

        _logger.info(
            "[ITR] Chunk complete: %d reconciled, %d already done, "
            "%d skipped (one-sided)",
            reconciled_count, already_done, skipped_one_sided,
        )
