"""One-pass AR/AP reconciliation replay from QBO payment applications.

QBO records every settlement — cash, credit memos, vendor credits, expenses
or deposits coded straight to the control account — as application lines on a
``Payment`` or ``BillPayment``: each ``Line`` carries the amount applied to
one linked transaction.  The amounts are per-document totals; QBO does not
say which credit cleared which invoice.

Replaying those applications faithfully therefore means treating each
(Bill)Payment as one *reconciliation group*: the payment's own AR/AP line
plus every linked document's AR/AP line, each capped at its QBO application
amount.  Any maximal fill of credits against debits that respects the caps
lands every document at exactly QBO's residual, because the per-document
totals are fixed.

This pass runs once, in the account finalizer, after every transaction
pipeline has committed — so every linked document exists and nothing has
been reconciled yet.  It replaces the old phase-3/4 reconciliation inside
the payment pipeline, whose split passes guessed at each other's state
(cash applied in QBO line order, credits blind-paired against invoices the
cash had already closed).
"""

import logging
from typing import Dict, List

from .move_posting_helpers import reconcile_at_amount
from .utils import get_api_client

_logger = logging.getLogger(__name__)

# QBO LinkedTxn.TxnType → the qbo_id_map (in ``maps``) that resolves it to an
# Odoo move.  A Purchase/Expense or Deposit coded to the control account
# participates in applications exactly like an invoice or credit note.
# QBO names a linked Purchase after its PaymentType — "Expense" (cash),
# "Check", "CreditCardCredit" — or generically "Purchase"; all four are the
# same entity behind qbo_expense_id.
LINK_TXN_MAPS = {
    "Invoice": "invoice_map",
    "Bill": "bill_map",
    "CreditMemo": "credit_memo_map",
    "VendorCredit": "vendor_credit_map",
    "JournalEntry": "journal_entry_map",
    "Purchase": "expense_map",
    "Expense": "expense_map",
    "Check": "expense_map",
    "CreditCardCredit": "expense_map",
    "Deposit": "deposit_map",
}

# Residuals and caps below half a cent are settled.
_EPS = 0.005


def build_application_groups(
    txns: List[Dict],
    kind: str,
    maps: Dict[str, Dict],
) -> List[Dict]:
    """Build reconciliation group specs from QBO (Bill)Payment payloads.

    Args:
        txns: raw QBO ``Payment`` or ``BillPayment`` dicts.
        kind: ``"Payment"`` or ``"BillPayment"`` — selects the map that
            resolves the payment's own Odoo move (``payment_move_map`` /
            ``bill_payment_move_map``) and stamps the group.
        maps: ``{map_name: {str(qbo_id): odoo_move_id}}`` for every entry in
            ``LINK_TXN_MAPS`` plus the two payment-move maps.

    Returns:
        One group per txn that has at least two resolved members::

            {
                "qbo_id": str,        # the (Bill)Payment's QBO id
                "kind": kind,
                "date": TxnDate,
                "members": [(odoo_move_id, cap_amount), ...],
                "unresolved": [(txn_type, txn_id, amount), ...],
            }

        The payment's own move is a member capped at ``TotalAmt`` (omitted
        for zero-total credit applications).  Caps for the same move are
        merged.  Groups with fewer than two members reconcile nothing and
        are dropped.
    """
    self_map = maps.get(
        "payment_move_map" if kind == "Payment" else "bill_payment_move_map"
    ) or {}
    groups = []
    for txn in txns:
        qbo_id = str(txn.get("Id", ""))
        total = float(txn.get("TotalAmt", 0) or 0)
        caps: Dict[int, float] = {}
        unresolved = []
        for line in txn.get("Line", []):
            amount = float(line.get("Amount", 0) or 0)
            for linked in line.get("LinkedTxn", []):
                ttype = linked.get("TxnType")
                tid = str(linked.get("TxnId", ""))
                map_name = LINK_TXN_MAPS.get(ttype)
                move_id = (maps.get(map_name) or {}).get(tid) if map_name else None
                if move_id:
                    caps[move_id] = round(caps.get(move_id, 0.0) + amount, 2)
                else:
                    unresolved.append((ttype, tid, amount))
        if total > _EPS:
            self_move = self_map.get(qbo_id)
            if self_move:
                caps[self_move] = round(caps.get(self_move, 0.0) + total, 2)
            else:
                unresolved.append((kind, qbo_id, total))
        if len(caps) < 2:
            if caps:
                _logger.debug(
                    "%s#%s: fewer than two resolved members "
                    "(resolved=%d, unresolved=%d) — nothing to reconcile",
                    kind, qbo_id, len(caps), len(unresolved),
                )
            continue
        groups.append({
            "qbo_id": qbo_id,
            "kind": kind,
            "date": txn.get("TxnDate"),
            "members": list(caps.items()),
            "unresolved": unresolved,
        })
    return groups


def solve_group(env, group: Dict) -> int:
    """Reconcile one application group; return the number of pairs applied.

    Collects the unreconciled receivable/payable lines of every member move,
    buckets them by account (reconciliation is per-account; e.g. AR-USD and
    AR-CAD never cross), splits each bucket into debit/credit sides by
    residual sign, and greedily fills credits against debits — every partial
    capped at ``min(remaining debit cap, remaining credit cap)`` in the
    transaction currency via :func:`reconcile_at_amount`.

    Idempotent: caps are also bounded by the lines' current residuals, so a
    re-run applies nothing.
    """
    caps: Dict[int, float] = {}
    for move_id, cap in group["members"]:
        caps[move_id] = round(caps.get(move_id, 0.0) + cap, 2)

    moves = env["account.move"].browse(list(caps))
    buckets: Dict[int, List] = {}  # account_id -> [(move_id, line)]
    for move in moves:
        control_lines = move.line_ids.filtered(
            lambda l: (
                l.account_id.account_type
                in ("asset_receivable", "liability_payable")
                and l.account_id.reconcile
                and not l.reconciled
                and l.parent_state == "posted"
            )
        )
        for line in control_lines:
            buckets.setdefault(line.account_id.id, []).append((move.id, line))

    qbo_ref = f"QBO_EXCH:{group['kind']}:{group['qbo_id']}"
    qbo_date = group.get("date")
    applied = 0
    for entries in buckets.values():
        debits = [e for e in entries if e[1].amount_residual > 0]
        credits = [e for e in entries if e[1].amount_residual < 0]
        ci = di = 0
        while ci < len(credits) and di < len(debits):
            cmid, cline = credits[ci]
            dmid, dline = debits[di]
            c_avail = min(
                caps.get(cmid, 0.0), abs(cline.amount_residual_currency)
            )
            d_avail = min(
                caps.get(dmid, 0.0), abs(dline.amount_residual_currency)
            )
            if c_avail < _EPS:
                ci += 1
                continue
            if d_avail < _EPS:
                di += 1
                continue
            amount = min(c_avail, d_avail)
            reconcile_at_amount(
                cline, dline, amount, qbo_ref=qbo_ref, qbo_date=qbo_date,
            )
            applied += 1
            caps[cmid] = round(caps[cmid] - amount, 2)
            caps[dmid] = round(caps[dmid] - amount, 2)
    return applied


def run_reconciliation_pass(ctx) -> None:
    """Replay all QBO payment applications against the imported moves.

    Queries every ``Payment`` and ``BillPayment`` from QBO, resolves the
    linked transactions through the ``qbo_*_id`` stamps on ``account.move``,
    and solves the groups in transaction-date order.  Exchange-difference
    moves are stamped ``QBO_EXCH:<Kind>:<qbo_id>`` and dated at the QBO
    transaction date, exactly as the old per-phase reconciliation did, so
    the FX true-up finalizer keeps its per-transaction traceability.
    """
    try:
        api_client = get_api_client(ctx)
    except ValueError as exc:
        # No QBO source configured on this context (e.g. a finalizer test
        # exercising only the archival logic) — nothing to replay.
        _logger.warning("Reconciliation pass skipped: %s", exc)
        return
    payments = api_client.query_all(entity="Payment", order_by="Id")
    bill_payments = api_client.query_all(entity="BillPayment", order_by="Id")

    maps = _build_move_maps(ctx.env)
    groups = build_application_groups(payments, "Payment", maps)
    groups += build_application_groups(bill_payments, "BillPayment", maps)
    # Date order for determinism only — per-document caps make the final
    # residuals order-independent.
    groups.sort(key=lambda g: (g.get("date") or "", g["kind"], int(g["qbo_id"])))

    applied = 0
    unresolved = []
    for group in groups:
        with ctx.skippable(
            f"reconcile group {group['kind']}#{group['qbo_id']}"
        ):
            applied += solve_group(ctx.env, group)
            unresolved.extend(
                (group["kind"], group["qbo_id"]) + u
                for u in group["unresolved"]
            )
    _logger.info(
        "Reconciliation pass: %d partials applied across %d groups "
        "(%d payments, %d bill payments)",
        applied, len(groups), len(payments), len(bill_payments),
    )
    if unresolved:
        _logger.warning(
            "Reconciliation pass: %d unresolved application links "
            "(first 20): %s",
            len(unresolved), unresolved[:20],
        )


def _build_move_maps(env) -> Dict[str, Dict[str, int]]:
    """Build ``{map_name: {str(qbo_id): move_id}}`` for group resolution.

    Linked documents resolve through the ``qbo_*_id`` stamps on posted
    ``account.move`` records; the payments' own moves resolve through the
    stamps on ``account.payment``.
    """
    move_fields = {
        "invoice_map": "qbo_invoice_id",
        "bill_map": "qbo_bill_id",
        "credit_memo_map": "qbo_credit_memo_id",
        "vendor_credit_map": "qbo_vendor_credit_id",
        "journal_entry_map": "qbo_journal_entry_id",
        "expense_map": "qbo_expense_id",
        "deposit_map": "qbo_deposit_id",
    }
    maps: Dict[str, Dict[str, int]] = {}
    for map_name, field in move_fields.items():
        env.cr.execute(
            f"SELECT {field}, id FROM account_move "  # noqa: S608
            f"WHERE {field} IS NOT NULL AND state = 'posted'"
        )
        maps[map_name] = {str(r[0]): r[1] for r in env.cr.fetchall()}
    for map_name, field in (
        ("payment_move_map", "qbo_payment_id"),
        ("bill_payment_move_map", "qbo_bill_payment_id"),
    ):
        env.cr.execute(
            f"SELECT p.{field}, p.move_id FROM account_payment p "  # noqa: S608
            f"JOIN account_move m ON m.id = p.move_id "
            f"WHERE p.{field} IS NOT NULL AND m.state = 'posted'"
        )
        maps[map_name] = {str(r[0]): r[1] for r in env.cr.fetchall()}
    return maps
