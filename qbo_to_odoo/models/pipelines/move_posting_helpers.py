"""Shared load-phase helpers for invoice-style QBO moves.

Implements the four-phase load pattern from the GL-first pipeline:

1. Pop GL metadata + create draft moves
2. Pre-posting fixes (tax amounts, GL account restore)
3. Post by journal
4. Post-posting AR/AP account correction

Used by invoice, bill, credit memo, and vendor credit pipelines to get
GL-accurate results without duplicating load logic.
"""

import logging
from collections import defaultdict
from typing import Dict, List

from odoo.addons.etl_framework import post_lock

from .exchange_rate_helper import ExchangeRateEnsurer

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Phase 1 helpers
# ---------------------------------------------------------------------------

def pop_gl_truth(move_vals_list: List[Dict]) -> Dict[int, Dict]:
    """Pop private ``_``-prefixed metadata from each vals dict.

    Odoo's ``create()`` raises ``ValueError`` on unknown keys, so these
    must be removed before creating moves.  The popped data is returned
    as ``{vals_index: {"arap": int, "tax_amounts": [...]}}`` for use in
    later phases.
    """
    gl_truth: Dict[int, Dict] = {}
    for i, vals in enumerate(move_vals_list):
        truth: Dict = {}
        if "_gl_arap_account_id" in vals:
            truth["arap"] = vals.pop("_gl_arap_account_id")
        if "_tax_amounts" in vals:
            truth["tax_amounts"] = vals.pop("_tax_amounts")
        if "_fx_currency_code" in vals:
            truth["fx_currency_code"] = vals.pop("_fx_currency_code")
            truth["fx_qbo_rate"] = vals.pop("_fx_qbo_rate", None)
        # invoice_currency_rate is a real account.move field — keep it.
        if truth:
            gl_truth[i] = truth
    return gl_truth


# ---------------------------------------------------------------------------
# Phase 2: pre-posting fixes
# ---------------------------------------------------------------------------

def fix_tax_amounts_orm(move, tax_amounts: List[Dict], move_type: str) -> int:
    """Adjust tax lines on a draft move to match QBO's exact amounts.

    For each entry in *tax_amounts*:

    - If a matching Odoo tax line exists (by ``tax_line_id``), adjust its
      ``amount_currency`` to match the QBO amount.
    - If no matching tax line exists (Odoo computed $0), add an explicit
      product line on the tax's repartition account.

    The payment_term line auto-rebalances via Odoo's inverse/sync stack.

    Returns the number of lines adjusted or created.
    """
    if not tax_amounts:
        return 0

    tax_lines = move.line_ids.filtered(
        lambda l: l.display_type == "tax" and l.tax_line_id
    )
    by_tax = defaultdict(lambda: move.env["account.move.line"])
    for line in tax_lines:
        by_tax[line.tax_line_id.id] |= line

    line_commands = []
    fixed = 0

    for ta in tax_amounts:
        odoo_tax_id = ta["tax_id"]
        qbo_amount = float(ta["amount"])  # source currency, always positive

        group = by_tax.get(odoo_tax_id)
        if group:
            target_ac = (
                qbo_amount
                if move_type in ("in_invoice", "out_refund")
                else -qbo_amount
            )
            current_ac = sum(group.mapped("amount_currency"))
            delta = round(target_ac - current_ac, 2)

            if abs(delta) < 0.005:
                continue

            first = group[0]
            line_commands.append(
                (1, first.id, {
                    "amount_currency": round(first.amount_currency + delta, 2),
                })
            )
            fixed += 1

        elif qbo_amount:
            tax = move.env["account.tax"].browse(odoo_tax_id)
            rep_line = tax.invoice_repartition_line_ids.filtered(
                lambda l: l.repartition_type == "tax" and l.account_id
            )
            if not rep_line:
                _logger.warning(
                    "Move %s: no repartition account for tax %s — skipping",
                    move.id, odoo_tax_id,
                )
                continue
            line_commands.append((0, 0, {
                "name": tax.name,
                "account_id": rep_line[0].account_id.id,
                "price_unit": qbo_amount,
                "quantity": 1,
                "tax_ids": [],
            }))
            fixed += 1

    if line_commands:
        move.write({"line_ids": line_commands})

    return fixed


def restore_gl_accounts(cr, move_ids: List[int]) -> int:
    """Bulk-restore GL accounts on product lines from ``qbo_acct_id``.

    Safe pre-posting because account_id changes on product lines don't
    affect amounts or tax computation.

    Returns the number of lines updated.
    """
    if not move_ids:
        return 0
    cr.execute(
        """
        UPDATE account_move_line
           SET account_id = qbo_acct_id
         WHERE qbo_acct_id IS NOT NULL
           AND account_id <> qbo_acct_id
           AND move_id IN %s
        """,
        (tuple(move_ids),),
    )
    return cr.rowcount


# ---------------------------------------------------------------------------
# Phase 4: post-posting AR/AP correction
# ---------------------------------------------------------------------------

def correct_arap_accounts(ctx, moves, move_index, gl_truth) -> int:
    """Post-posting: fix payment_term line account to match QBO AR/AP.

    Posting creates the ``payment_term`` display-type line with the
    partner's default receivable/payable account, which may differ from
    QBO's ``APAccountRef`` / ``ARAccountRef``.  This corrects it via SQL.

    Returns the number of lines corrected.
    """
    corrected = 0
    for move in moves:
        idx = move_index.get(move.id)
        if idx is None or idx not in gl_truth:
            continue
        arap_id = gl_truth[idx].get("arap")
        if arap_id:
            for line in move.line_ids:
                if (
                    line.display_type == "payment_term"
                    and line.account_id.id != arap_id
                ):
                    ctx.env.cr.execute(
                        "UPDATE account_move_line SET account_id = %s "
                        "WHERE id = %s",
                        (arap_id, line.id),
                    )
                    corrected += 1
    return corrected


# ---------------------------------------------------------------------------
# Amount-constrained reconciliation
# ---------------------------------------------------------------------------

def reconcile_at_amount(pay_line, inv_line, amount_currency):
    """Reconcile exactly *amount_currency* (foreign) between two lines.

    Mimics ``account.move.line.reconcile()`` but caps the payment line's
    residual to the QBO line amount so that partial payments don't greedily
    consume the entire invoice.  Exchange difference entries are created
    by the standard Odoo machinery.

    Args:
        pay_line: The payment-side ``account.move.line`` (AR/AP).
        inv_line: The invoice-side ``account.move.line`` (AR/AP).
        amount_currency: Exact amount to reconcile in the transaction
            currency (from QBO ``Line.Amount``).
    """
    AML = pay_line.env["account.move.line"]
    amls = pay_line + inv_line

    # Build the plan structure that _reconcile_plan_with_sync expects.
    plan_list = [{"amls": amls, "aml_ids": set(amls.ids)}]

    move_container = {"records": amls.move_id}
    with amls.move_id._check_balanced(move_container), \
         amls.move_id._sync_dynamic_lines(move_container):

        # ── Prefetch (same as _reconcile_plan_with_sync) ──
        amls.move_id
        amls.matched_debit_ids
        amls.matched_credit_ids

        pre_hook_data = amls._reconcile_pre_hook()

        # ── Collect residuals ──
        aml_values_map = {
            aml: {
                "aml": aml,
                "amount_residual": aml.amount_residual,
                "amount_residual_currency": aml.amount_residual_currency,
                "parent_state": aml.parent_state,
            }
            for aml in amls
        }

        # ── THE CONSTRAINT: cap payment residual to QBO amount ──
        pv = aml_values_map[pay_line]
        cap = abs(amount_currency)
        if cap and abs(pv["amount_residual_currency"]) > cap + 0.005:
            sign = -1 if pv["amount_residual_currency"] < 0 else 1
            rate = (
                pv["amount_residual"] / pv["amount_residual_currency"]
                if pv["amount_residual_currency"]
                else 1.0
            )
            pv["amount_residual_currency"] = sign * cap
            pv["amount_residual"] = round(sign * cap * rate, 2)

        # ── Prepare partials + exchange diffs ──
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
            return

        # ── Create partials ──
        partials = pay_line.env["account.partial.reconcile"].create(
            partials_values_list
        )
        start_range = 0
        for plan_results, plan in zip(all_plan_results, plan_list):
            size = len(plan_results)
            plan["partials"] = partials[start_range : start_range + size]
            start_range += size

        # ── Create exchange difference moves ──
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

        # ── Full reconcile creation ──
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
                from odoo.fields import Command

                pay_line.env["account.full.reconcile"].create(
                    {
                        "partial_reconcile_ids": [
                            Command.link(p.id) for p in involved_partials
                        ],
                        "reconciled_line_ids": [
                            Command.link(a.id) for a in involved
                        ],
                    }
                )

        amls._reconcile_post_hook(pre_hook_data)


# ---------------------------------------------------------------------------
# Orchestrator: four-phase load
# ---------------------------------------------------------------------------

def load_and_post_invoice_moves(ctx, move_vals_list: List[Dict]) -> None:
    """Create, fix, post, and correct invoice-style moves.

    Implements the four-phase load:

    1. Pop GL metadata, create draft moves
    2. Pre-posting: fix tax amounts (ORM), restore GL accounts (SQL)
    3. Post by journal with ``post_lock``
    4. Post-posting: correct AR/AP accounts (SQL)

    Works for ``out_invoice``, ``in_invoice``, ``out_refund``,
    ``in_refund``.  Entry-type moves (JEs, deposits, etc.) should use
    a simpler create-then-post flow since they don't have tax/AR/AP
    corrections.
    """
    if not move_vals_list:
        _logger.info("No moves to create")
        return

    # ── Phase 1: pop metadata + create ──
    gl_truth = pop_gl_truth(move_vals_list)

    moves = ctx.env["account.move"]
    move_index: Dict[int, int] = {}  # move.id -> vals index
    for i, vals in enumerate(move_vals_list):
        qbo_id = "?"
        for field in (
            "qbo_invoice_id", "qbo_bill_id",
            "qbo_credit_memo_id", "qbo_vendor_credit_id",
        ):
            if vals.get(field):
                qbo_id = vals[field]
                break
        with ctx.skippable(f"create move QBO#{qbo_id}"):
            move = ctx.env["account.move"].create(vals)
            moves |= move
            move_index[move.id] = i

    _logger.info(f"Created {len(moves)} moves")

    # ── Phase 2: pre-posting corrections ──

    # 2a. Fix tax amounts via ORM.  Must run BEFORE the qbo_acct_id SQL
    # restore so the sync stack sees the same base-line state as create().
    fixed_tax = 0
    for move in moves:
        idx = move_index.get(move.id)
        truth = gl_truth.get(idx) if idx is not None else None
        if not truth or "tax_amounts" not in truth:
            continue
        with ctx.skippable(f"fix taxes on {move.ref or move.name or '?'}"):
            fixed_tax += fix_tax_amounts_orm(
                move,
                truth["tax_amounts"],
                move_vals_list[idx].get("move_type", "entry"),
            )
    if fixed_tax:
        _logger.info(f"Pre-posting tax fix: {fixed_tax} tax lines adjusted")

    # 2b. Restore GL accounts on product lines (bulk SQL).
    if moves:
        restored = restore_gl_accounts(ctx.env.cr, moves.ids)
        if restored:
            _logger.info(
                f"Restored GL accounts on {restored} lines (pre-posting)"
            )
            ctx.env.invalidate_all()

    # ── Phase 3: post by journal ──
    # Upsert per-transaction exchange rates into the global rate table
    # immediately before posting so reconcile() has accurate rates.
    rate_ensurer = ExchangeRateEnsurer(ctx.env)
    posted = 0
    by_journal: Dict[int, list] = {}
    for move in moves:
        by_journal.setdefault(move.journal_id.id, ctx.env["account.move"])
        by_journal[move.journal_id.id] |= move
    for journal_id, journal_moves in sorted(by_journal.items()):
        with post_lock(ctx.env.cr, journal_id):
            for move in journal_moves:
                with ctx.skippable(
                    f"post move {move.ref or move.name or '?'}"
                ):
                    idx = move_index.get(move.id)
                    truth = gl_truth.get(idx) if idx is not None else None
                    if truth and truth.get("fx_currency_code"):
                        rate_ensurer.set_rate(
                            truth["fx_currency_code"],
                            str(move.invoice_date or move.date),
                            truth["fx_qbo_rate"],
                        )
                    move.action_post()
                    posted += 1

    _logger.info(f"Posted {posted} moves")

    # ── Phase 4: post-posting AR/AP correction ──
    corrected = correct_arap_accounts(ctx, moves, move_index, gl_truth)
    if corrected:
        ctx.env.invalidate_all()
        _logger.info(f"Post-posting: corrected {corrected} AR/AP accounts")
