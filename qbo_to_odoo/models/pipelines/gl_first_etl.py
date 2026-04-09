"""QBO GL-first Import Pipeline (Journal export XLSX as source of truth).

This pipeline imports QBO transactions using the Journal export XLSX
(uploaded on the QBO connection) as the primary source of GL postings:

- Each transaction is routed:
  - **Enrichable types** (Invoice/Bill/CreditMemo/VendorCredit) are imported as
    typed moves using the QBO API entity + ``QBOMoveBuilder``. Product-line
    accounts are restored from the export pre-posting, tax amounts are fixed
    from ``TxnTaxDetail`` pre-posting, and AR/AP accounts are corrected
    post-posting.
  - **Payment types** (Payment/BillPayment) are imported as enriched
    ``account.payment`` records from the QBO API entity.
  - Everything else is imported as ``move_type='entry'`` from the export
    lines. Foreign-currency amounts are derived from the QBO entity's
    exchange rate where available.
"""

import base64
import logging
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext, ChunkableData, post_lock

from .exchange_rate_helper import ExchangeRateEnsurer
from .extractor import QBOExtractor
from .gl_import_etl import _get_imported_qbo_ids, _parse_journal_export
from .utils import get_api_client

_logger = logging.getLogger(__name__)

# JournalReport API columns and indices — used by the validation report.
_JOURNAL_REPORT_COLUMNS = (
    "tx_date,txn_type,doc_num,name,memo,"
    "acct_num_with_extn,account_name,"
    "debt_home_amt,credit_home_amt,"
    "currency,debt_amt,credit_amt"
)
_JR_DATE, _JR_TYPE, _JR_NUM, _JR_NAME, _JR_MEMO = 0, 1, 2, 3, 4
_JR_ACCT_NUM, _JR_ACCT_NAME = 5, 6
_JR_DEBIT_HOME, _JR_CREDIT_HOME = 7, 8
_JR_CURRENCY, _JR_DEBIT_FGN, _JR_CREDIT_FGN = 9, 10, 11


def _parse_journal_report(report_data: Dict) -> List[Dict]:
    """Parse a QBO JournalReport API response into grouped transactions.

    Returns the same structure as the old ``_parse_journal_export`` but
    with additional ``currency``, ``debit_foreign``, and
    ``credit_foreign`` fields on each line.
    """
    rows = report_data.get("Rows", {}).get("Row", [])
    transactions: Dict[str, Dict] = {}  # "type-id" -> txn dict
    current_key = None

    def _float(val):
        try:
            return float(val) if val else 0.0
        except (ValueError, TypeError):
            return 0.0

    for row in rows:
        if row.get("type") != "Data":
            continue
        cd = row.get("ColData", [])
        if len(cd) < 12:
            continue

        date_val = cd[_JR_DATE]["value"]
        txn_type = cd[_JR_TYPE]["value"]
        txn_id = cd[_JR_TYPE].get("id", "")

        if not txn_id:
            continue

        # New transaction header (non-zero date + type present).
        # Store header metadata on the txn dict so continuation lines
        # can inherit even if the header row itself has no account data.
        # Use a composite key (type-id) because QBO uses separate ID
        # sequences per entity — Invoice 3661 and Payment 3661 are
        # different transactions.
        if date_val and date_val != "0-00-00" and txn_type:
            current_key = f"{txn_type}-{txn_id}"
            if current_key not in transactions:
                transactions[current_key] = {
                    "id": txn_id,
                    "header_date": date_val,
                    "header_type": txn_type,
                    "header_num": cd[_JR_NUM]["value"] or "",
                    "header_name": cd[_JR_NAME]["value"] or "",
                    "lines": [],
                }

        if not current_key or current_key not in transactions:
            continue

        acct_num = cd[_JR_ACCT_NUM]["value"].strip()
        if not acct_num:
            continue  # Summary / blank line
        # Normalize: QBO API returns e.g. "2020.10" but Odoo stores "2020.1"
        if "." in acct_num:
            base, ext = acct_num.rsplit(".", 1)
            ext = ext.rstrip("0")
            acct_num = f"{base}.{ext}" if ext else base

        debit_home = _float(cd[_JR_DEBIT_HOME]["value"])
        credit_home = _float(cd[_JR_CREDIT_HOME]["value"])
        if debit_home == 0 and credit_home == 0:
            continue

        currency = cd[_JR_CURRENCY]["value"] or ""
        debit_fgn = _float(cd[_JR_DEBIT_FGN]["value"])
        credit_fgn = _float(cd[_JR_CREDIT_FGN]["value"])

        txn = transactions[current_key]
        txn["lines"].append({
            "date": txn["header_date"],
            "type": txn["header_type"],
            "num": str(txn["header_num"]),
            "name": str(txn["header_name"]),
            "memo": cd[_JR_MEMO]["value"] or "",
            "account_code": acct_num,
            "account_name": cd[_JR_ACCT_NAME]["value"] or "",
            "debit": debit_home,
            "credit": credit_home,
            "currency": currency,
            "debit_foreign": debit_fgn,
            "credit_foreign": credit_fgn,
        })

    result = [t for t in transactions.values() if t["lines"]]
    empty = len(transactions) - len(result)
    type_counts = defaultdict(int)
    for t in result:
        type_counts[t["header_type"]] += 1

    # Debug: count header rows to verify grouping
    header_count = sum(
        1 for row in rows
        if row.get("type") == "Data"
        and len(row.get("ColData", [])) >= 12
        and row["ColData"][_JR_DATE]["value"] not in ("", "0-00-00")
        and row["ColData"][_JR_TYPE]["value"]
    )
    _logger.info(
        f"Parsed {len(result)} transactions from JournalReport API "
        f"({len(rows)} raw rows, {header_count} headers, "
        f"{len(transactions)} groups, {empty} empty) — "
        f"{', '.join(f'{t}: {c}' for t, c in sorted(type_counts.items()))}"
    )
    return result


_PAYMENT_TYPES: Dict[str, Tuple[str, Dict]] = {
    # export_type: (qbo_entity, payment_kwargs)
    "Payment": (
        "Payment",
        dict(
            payment_type="inbound",
            partner_type="customer",
            qbo_id_field="qbo_payment_id",
            partner_resolve="customer",
            account_type="asset_receivable",
        ),
    ),
    "Bill Payment (Cheque)": (
        "BillPayment",
        dict(
            payment_type="outbound",
            partner_type="supplier",
            qbo_id_field="qbo_bill_payment_id",
            partner_resolve="vendor",
            account_type="liability_payable",
        ),
    ),
    "Bill Payment (Credit Card)": (
        "BillPayment",
        dict(
            payment_type="outbound",
            partner_type="supplier",
            qbo_id_field="qbo_bill_payment_id",
            partner_resolve="vendor",
            account_type="liability_payable",
        ),
    ),
}


_ENRICHABLE: Dict[str, Tuple[str, Dict]] = {
    # export_type: (qbo_entity, builder_kwargs)
    "Invoice": (
        "Invoice",
        dict(
            move_type="out_invoice",
            journal_type="sale",
            partner_type="customer",
            qbo_id_field="qbo_invoice_id",
            line_detail_types=("SalesItemLineDetail", "DiscountLineDetail"),
            tax_use="sale",
            direction="income",
            memo_field="CustomerMemo",
            memo_key="value",
        ),
    ),
    "Bill": (
        "Bill",
        dict(
            move_type="in_invoice",
            journal_type="purchase",
            partner_type="vendor",
            qbo_id_field="qbo_bill_id",
            line_detail_types=(
                "ItemBasedExpenseLineDetail",
                "AccountBasedExpenseLineDetail",
            ),
            tax_use="purchase",
            direction="expense",
            memo_field="Memo",
            memo_key=None,
        ),
    ),
    "Credit Memo": (
        "CreditMemo",
        dict(
            move_type="out_refund",
            journal_type="sale",
            partner_type="customer",
            qbo_id_field="qbo_credit_memo_id",
            line_detail_types=("SalesItemLineDetail", "DiscountLineDetail"),
            tax_use="sale",
            direction="income",
            memo_field="CustomerMemo",
            memo_key="value",
        ),
    ),
    "Vendor Credit": (
        "VendorCredit",
        dict(
            move_type="in_refund",
            journal_type="purchase",
            partner_type="vendor",
            qbo_id_field="qbo_vendor_credit_id",
            line_detail_types=(
                "ItemBasedExpenseLineDetail",
                "AccountBasedExpenseLineDetail",
            ),
            tax_use="purchase",
            direction="expense",
            memo_field="Memo",
            memo_key=None,
        ),
    ),
}


def _build_code_maps(ctx: ETLContext, extractor: QBOExtractor) -> None:
    """Populate `extractor.extra` with account code, currency, and type maps."""
    ctx.env.cr.execute(
        """
        SELECT aa.id,
               aa.code_store::jsonb->>jsonb_object_keys(
                   aa.code_store::jsonb) as code,
               aa.currency_id,
               aa.account_type
        FROM account_account aa
        """
    )
    code_map = {}
    account_currency_map = {}
    account_type_map = {}  # account_id -> account_type string
    for acct_id, code, currency_id, acct_type in ctx.env.cr.fetchall():
        if code:
            code_map[code] = acct_id
            if currency_id:
                account_currency_map[acct_id] = currency_id
            if acct_type:
                account_type_map[acct_id] = acct_type
    extractor.extra["code_map"] = code_map
    extractor.extra["account_currency_map"] = account_currency_map
    extractor.extra["account_type_map"] = account_type_map
    extractor.extra["company_currency_id"] = ctx.env.company.currency_id.id

    # Currency name → ID map for JournalReport foreign-currency lines.
    ctx.env.cr.execute("SELECT id, name FROM res_currency WHERE active = true")
    extractor.extra["currency_name_map"] = {
        name: cid for cid, name in ctx.env.cr.fetchall()
    }



def _resolve_account(
    code_map: Dict[str, int],
    account_code: str,
    account_type_map: Optional[Dict[int, str]] = None,
) -> Optional[Tuple[int, Optional[str]]]:
    """Resolve account code to (account_id, account_type) or None."""
    for suffix in ("", ".1", ".2", ".3"):
        account_id = code_map.get(account_code + suffix)
        if account_id:
            acct_type = account_type_map.get(account_id) if account_type_map else None
            return account_id, acct_type
    return None


def _resolve_account_id(code_map: Dict[str, int], account_code: str) -> Optional[int]:
    result = _resolve_account(code_map, account_code)
    return result[0] if result else None


def _annotate_with_gl_accounts(
    vals: Dict,
    export_lines: List[Dict],
    code_map: Dict[str, int],
    account_type_map: Dict[int, str],
    exchange_rate: float = 1.0,
) -> Dict:
    """Annotate an enriched move's line vals with ``qbo_acct_id`` from the
    Journal export, so the GL account can be restored after Odoo's computes.

    For ``invoice_line_ids`` (product lines on typed moves), we match each
    builder-produced line to an export line by amount.  Unmatched lines keep
    whatever account the builder assigned.

    AR/AP account is extracted from the export only as a fallback — the builder
    may have already set ``_gl_arap_account_id`` from ``APAccountRef``.

    Returns the vals dict (mutated in place).
    """
    # Build a pool of non-AR/AP export lines with resolved account IDs.
    # Each entry: (signed_amount, account_id, used)
    export_pool: List[List] = []  # mutable list so we can mark "used"
    arap_accounts: List[Tuple[float, int]] = []

    for ld in export_lines:
        code = str(ld.get("account_code", "")).strip()
        resolved = _resolve_account(code_map, code, account_type_map)
        if not resolved:
            continue
        account_id, account_type = resolved
        signed = float(ld.get("debit") or 0) - float(ld.get("credit") or 0)

        if account_type in ("asset_receivable", "liability_payable"):
            arap_accounts.append((signed, account_id))
        else:
            export_pool.append([signed, account_id, False])  # [amount, id, used]

    # Store AR/AP on vals for post-posting correction (builder value takes priority).
    if arap_accounts and "_gl_arap_account_id" not in vals:
        arap_accounts.sort(key=lambda x: abs(x[0]), reverse=True)
        vals["_gl_arap_account_id"] = arap_accounts[0][1]

    # Annotate product lines (invoice_line_ids).
    line_key = "invoice_line_ids" if "invoice_line_ids" in vals else "line_ids"
    for cmd_tuple in vals.get(line_key, []):
        if not isinstance(cmd_tuple, (list, tuple)) or cmd_tuple[0] != 0:
            continue
        line_vals = cmd_tuple[2]

        # Compute the line's expected signed amount from price_unit * qty.
        # For sales (out_invoice/out_refund), revenue is a credit (negative).
        # For purchases (in_invoice/in_refund), expense is a debit (positive).
        qty = float(line_vals.get("quantity", 1) or 1)
        price = float(line_vals.get("price_unit", 0) or 0)
        # Convert source-currency amount to company currency for matching
        # against the Journal export (which is always in company currency).
        line_amount_ccy = qty * price * exchange_rate

        move_type = vals.get("move_type", "entry")
        if move_type in ("out_invoice", "out_refund"):
            target = -abs(line_amount_ccy)
        elif move_type in ("in_invoice", "in_refund"):
            target = abs(line_amount_ccy)
        else:
            target = line_amount_ccy

        # Find the closest unmatched export line.
        best_idx = None
        best_diff = float("inf")
        for i, (amt, _acct_id, used) in enumerate(export_pool):
            if used:
                continue
            diff = abs(amt - target)
            if diff < best_diff:
                best_diff = diff
                best_idx = i

        if best_idx is not None and best_diff < max(abs(target) * 0.05, 1.0):
            export_pool[best_idx][2] = True  # mark used
            line_vals["qbo_acct_id"] = export_pool[best_idx][1]

    return vals


def _fix_tax_amounts_orm(move, tax_amounts: List[Dict], move_type: str) -> int:
    """Fix tax line amounts on a draft move using ORM writes.

    For each ``_tax_amounts`` entry:

    - If a matching Odoo tax line exists (by ``tax_line_id``), adjust its
      ``amount_currency`` to match the QBO amount.  The inverse syncs
      ``balance``/``debit``/``credit`` and the payment_term auto-rebalances.
    - If **no** matching tax line exists (Odoo computed $0 tax because the
      product lines are tiny/dummy — common on CBSA customs and brokerage
      bills), add an explicit product line on the tax's repartition account
      for the exact amount.  The payment_term still auto-rebalances.

    Args:
        move: Draft ``account.move`` record.
        tax_amounts: ``[{"tax_id": int, "amount": float}]`` from QBO
            ``TxnTaxDetail`` (source-currency, always positive).
        move_type: Move type string (e.g. ``'in_invoice'``).

    Returns:
        Number of lines adjusted or created.
    """
    if not tax_amounts:
        return 0

    tax_lines = move.line_ids.filtered(
        lambda l: l.display_type == "tax" and l.tax_line_id
    )

    # Group tax lines by tax_line_id (child tax).
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
            # Normal path: adjust the existing tax line.
            target_ac = qbo_amount if move_type in ("in_invoice", "out_refund") else -qbo_amount
            current_ac = sum(group.mapped("amount_currency"))
            delta = round(target_ac - current_ac, 2)

            if abs(delta) < 0.005:
                continue

            first = group[0]
            line_commands.append(
                (1, first.id, {"amount_currency": round(first.amount_currency + delta, 2)})
            )
            fixed += 1
        elif qbo_amount:
            # No tax line exists (Odoo computed $0) — add a product line
            # on the tax's repartition account for the exact amount.
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


def _get_bank_journal(payment: Dict, builder) -> Optional[int]:
    """Resolve bank/cash journal ID from QBO payment entity."""
    account_ref = payment.get("DepositToAccountRef", {})
    if not account_ref or not account_ref.get("value"):
        check_payment = payment.get("CheckPayment", {})
        if check_payment:
            account_ref = check_payment.get("BankAccountRef", {})
    if not account_ref or not account_ref.get("value"):
        cc_payment = payment.get("CreditCardPayment", {})
        if cc_payment:
            account_ref = cc_payment.get("CCAccountRef", {})

    if not account_ref or not account_ref.get("value"):
        account_id = builder.undeposited_funds_id
        if not account_id:
            return None
    else:
        qbo_account_id = account_ref.get("value")
        try:
            account_id = builder.account_map.get(int(qbo_account_id))
        except (ValueError, TypeError):
            account_id = None
        if not account_id:
            return None

    return builder.get_journal_id_for_account(account_id, fallback_type=None)


def _build_payment_vals(
    entity: Dict,
    builder,
    *,
    payment_type: str,
    partner_type: str,
    qbo_id_field: str,
    partner_resolve: str,
    account_type: str,
    gl_lines: Optional[List[Dict]] = None,
    code_map: Optional[Dict[str, int]] = None,
    account_type_map: Optional[Dict[int, str]] = None,
) -> Optional[Dict]:
    """Build account.payment vals + reconciliation pairs from a QBO entity."""
    partner_id = builder.resolve_partner(entity, partner_resolve)
    if not partner_id:
        _logger.warning(
            f"Partner not found for {qbo_id_field} QBO#{entity.get('Id')}"
        )
        return None

    total_amt = float(entity.get("TotalAmt", 0) or 0)
    if total_amt <= 0:
        return None  # zero-amount = credit application, handled elsewhere

    qbo_id = int(entity.get("Id", 0))
    journal_id = _get_bank_journal(entity, builder)
    if not journal_id:
        _logger.warning(f"No bank journal for {qbo_id_field} QBO#{qbo_id}")
        return None

    # Resolve destination (AR/AP) account.
    # Priority: 1) QBO entity ARAccountRef/APAccountRef
    #           2) GL export lines (find the receivable/payable line)
    is_customer = payment_type == "inbound"
    linked_moves = []  # (odoo_move_id, qbo_line_amount)

    # QBO BillPayment → APAccountRef, Payment → ARAccountRef
    arap_ref = (
        entity.get("ARAccountRef", {}).get("value")
        if is_customer
        else entity.get("APAccountRef", {}).get("value")
    )
    dest_account_id = None
    if arap_ref:
        try:
            dest_account_id = builder.account_map.get(int(arap_ref))
        except (ValueError, TypeError):
            pass

    # Fallback: find the AR/AP account from the GL export lines.
    if not dest_account_id and gl_lines and code_map and account_type_map:
        target_types = (
            ("asset_receivable",) if is_customer else ("liability_payable",)
        )
        for ld in gl_lines:
            acct_code = str(ld.get("account_code", "")).strip()
            resolved = _resolve_account(code_map, acct_code, account_type_map)
            if resolved and resolved[1] in target_types:
                dest_account_id = resolved[0]
                break

    if is_customer:
        doc_map_key = "invoice_map"
        credit_map_keys = [("CreditMemo", "credit_memo_map"),
                           ("JournalEntry", "journal_entry_map")]
    else:
        doc_map_key = "bill_map"
        credit_map_keys = [("VendorCredit", "vendor_credit_map"),
                           ("JournalEntry", "journal_entry_map")]

    doc_map = builder.get_extra(doc_map_key) or {}
    credit_maps = {
        txn_type: builder.get_extra(key) or {}
        for txn_type, key in credit_map_keys
    }

    embedded_credit_links = []
    doc_type = "Invoice" if is_customer else "Bill"

    for line in entity.get("Line", []):
        line_amount = float(line.get("Amount", 0) or 0)
        for linked in line.get("LinkedTxn", []):
            txn_id = str(linked.get("TxnId", ""))
            txn_type = linked.get("TxnType")
            if txn_type == doc_type and txn_id in doc_map:
                linked_moves.append((doc_map[txn_id], line_amount))
            elif txn_type in credit_maps and txn_id in credit_maps[txn_type]:
                embedded_credit_links.append(
                    (credit_maps[txn_type][txn_id], line_amount)
                )

    currency_id, is_foreign, exchange_rate = builder.resolve_currency(entity)
    journal_bank_account_map = builder.get_extra("journal_bank_account_map") or {}
    outstanding_account_id = journal_bank_account_map.get(journal_id)

    payment_ref = (
        entity.get("PaymentRefNum", "")
        or entity.get("DocNumber", "")
        or f"QBO-{qbo_id}"
    )

    payment_vals = {
        "date": entity.get("TxnDate"),
        "journal_id": journal_id,
        "payment_type": payment_type,
        "partner_type": partner_type,
        "partner_id": partner_id,
        "amount": total_amt,
        "memo": payment_ref,
        "payment_reference": payment_ref,
        qbo_id_field: qbo_id,
        "outstanding_account_id": outstanding_account_id,
    }
    if dest_account_id:
        payment_vals["destination_account_id"] = dest_account_id
    # Always set currency explicitly so Odoo uses the imported QBO daily
    # rate rather than inferring from the journal (which may be multi-currency).
    payment_vals["currency_id"] = currency_id

    # Build embedded credit application pairs
    embedded_apps = []
    if embedded_credit_links and linked_moves:
        for cm_id, cm_amount in embedded_credit_links:
            for inv_id, _inv_amount in linked_moves:
                embedded_apps.append({
                    "invoice_move_id": inv_id,
                    "credit_memo_move_id": cm_id,
                    "amount": cm_amount,
                    "qbo_payment_id": qbo_id,
                })

    return {
        "payment_vals": payment_vals,
        "linked_moves": linked_moves,
        "is_customer": is_customer,
        "account_type": account_type,
        "embedded_credit_apps": embedded_apps,
        "currency_code": (
            entity.get("CurrencyRef", {}).get("value")
            if is_foreign else None
        ),
        "exchange_rate": exchange_rate if is_foreign else None,
    }


def _ensure_payment_method_lines(env, journal_ids):
    """Ensure target journals have manual inbound/outbound method lines."""
    if not journal_ids:
        return
    journals = env["account.journal"].browse(list(journal_ids))
    manual_in = env.ref(
        "account.account_payment_method_manual_in", raise_if_not_found=False,
    )
    manual_out = env.ref(
        "account.account_payment_method_manual_out", raise_if_not_found=False,
    )
    MethodLine = env["account.payment.method.line"]
    for journal in journals:
        if manual_in and not journal.inbound_payment_method_line_ids.filtered(
            lambda l, m=manual_in: l.payment_method_id == m
        ):
            MethodLine.create({"payment_method_id": manual_in.id, "journal_id": journal.id})
        if manual_out and not journal.outbound_payment_method_line_ids.filtered(
            lambda l, m=manual_out: l.payment_method_id == m
        ):
            MethodLine.create({"payment_method_id": manual_out.id, "journal_id": journal.id})


def _journal_entry_vals_from_export(
    *,
    txn_id: str,
    txn_type: str,
    txn_date: str,
    txn_num: str,
    txn_name: str,
    lines_data: List[Dict],
    code_map: Dict[str, int],
    account_currency_map: Dict[int, int],
    company_currency_id: int,
    txn_currency_id: Optional[int],
    txn_exchange_rate: float,
    journal_id: int,
    company_id: int,
) -> Optional[Dict]:
    """Build a generic journal entry from GL export lines.

    The QBO Journal export is always in company currency (CAD).  For
    lines on accounts with a foreign currency matching the transaction
    currency, we set ``currency_id`` and ``amount_currency`` using the
    transaction's exchange rate from QBO.
    """
    lines = []
    for ld in lines_data:
        account_code = ld["account_code"]
        account_id = _resolve_account_id(code_map, account_code)
        if not account_id:
            _logger.warning(
                f"Account {account_code} not found for export txn #{txn_id} ({txn_type})"
            )
            return None

        line_name = ld["memo"] or ld["name"] or txn_type
        line_vals = {
            "account_id": account_id,
            "debit": ld["debit"],
            "credit": ld["credit"],
            "name": line_name,
        }

        # If the account requires a foreign currency AND the transaction
        # is in that currency, set currency_id and reverse-convert the
        # CAD amount using the transaction's exchange rate.
        acct_currency = account_currency_map.get(account_id)
        if acct_currency and acct_currency != company_currency_id:
            cad_amount = ld["debit"] - ld["credit"]
            if txn_currency_id and txn_currency_id == acct_currency and txn_exchange_rate:
                # QBO ExchangeRate = home per 1 foreign (e.g. 1.35 = 1 USD → 1.35 CAD)
                line_vals["currency_id"] = acct_currency
                line_vals["amount_currency"] = round(cad_amount / txn_exchange_rate, 2)
            else:
                # Transaction currency doesn't match the account's — this
                # is a cross-currency entry (e.g. CAD payment on USD account).
                # Set the account's currency with the CAD amount as-is;
                # Odoo will treat it at rate 1.0.
                line_vals["currency_id"] = acct_currency
                line_vals["amount_currency"] = cad_amount

        lines.append((0, 0, line_vals))

    if not lines:
        return None

    total_debit = sum(l[2]["debit"] for l in lines)
    total_credit = sum(l[2]["credit"] for l in lines)
    diff = round(total_debit - total_credit, 2)
    if abs(diff) > 0.01:
        _logger.warning(
            f"Export txn #{txn_id} ({txn_type}) unbalanced by {diff:.2f} — skipping"
        )
        return None

    ref = f"JNL-{txn_type}-{txn_id}"
    narration = f"{txn_type}" + (f" #{txn_num}" if txn_num else "") + (
        f" — {txn_name}" if txn_name else ""
    )

    return {
        "move_type": "entry",
        "journal_id": journal_id,
        "date": txn_date,
        "ref": ref,
        "narration": narration,
        "company_id": company_id,
        "line_ids": lines,
    }


@ETL.pipeline(
    target_model="account.move",
    importer_name="qbo.gl.first.import",
    sap_source="GLExport",
    depends_on=[
        "qbo.account.importer",
        "qbo.bank.journal.processor",
        "qbo.customer.importer",
        "qbo.customer.linker",
        "qbo.vendor.importer",
        "qbo.vendor.linker",
        "qbo.item.importer",
        "qbo.tax.importer",
        "qbo.partner.account.linker",
        "qbo.category.account.fixer",
    ],
)
class QboGlFirstImporter(models.AbstractModel):
    _name = "qbo.gl.first.import"
    _description = "QBO GL-first importer (Journal export primary)"

    @ETL.extract("GLExport")
    def extract_gl_first(self, ctx: ETLContext) -> ChunkableData:
        extractor = QBOExtractor(ctx)

        connection = ctx.env["qbo.connection"].browse(ctx.get_config("source_id"))
        if not connection.gl_export_file:
            _logger.info("No Journal export file uploaded — skipping GL-first import")
            return ChunkableData(records=[], context={"extractor": extractor.export(), "entities": {}})

        file_content = base64.b64decode(connection.gl_export_file)
        transactions = _parse_journal_export(file_content)
        _logger.info(f"Parsed {len(transactions)} transactions from Journal export")

        # Fetch the TransactionList report to get per-transaction currency
        # and foreign-currency amounts.  This is a single API call that
        # returns one row per transaction with Currency, Foreign Debit,
        # and Foreign Credit — data not available in the XLSX export.
        api_client = get_api_client(ctx)
        _logger.info("Fetching TransactionList from QBO API for currency data...")
        txn_list = api_client.get_report("TransactionList", {
            "date_macro": "All",
            "columns": (
                "tx_date,txn_type,doc_num,name,account_name,"
                "subt_nat_home_amount,currency,debt_amt,credit_amt"
            ),
        })
        # Build txn_id → currency info map.  The id attribute on the
        # Transaction Type ColData is the unique QBO transaction ID.
        txn_currency_map: Dict[str, Dict] = {}
        for row in txn_list.get("Rows", {}).get("Row", []):
            cd = row.get("ColData", [])
            if len(cd) < 9:
                continue
            txn_id = cd[1].get("id", "")
            if not txn_id:
                continue
            currency = cd[6].get("value", "") if len(cd) > 6 else ""
            try:
                fgn_debit = float(cd[7].get("value") or 0)
            except (ValueError, TypeError):
                fgn_debit = 0.0
            try:
                fgn_credit = float(cd[8].get("value") or 0)
            except (ValueError, TypeError):
                fgn_credit = 0.0
            txn_currency_map[txn_id] = {
                "currency": currency,
                "foreign_debit": fgn_debit,
                "foreign_credit": fgn_credit,
            }
        _logger.info(
            f"TransactionList: {len(txn_currency_map)} transactions with currency data"
        )

        # Annotate each transaction with its currency info so it travels
        # with the chunk (no need for a separate large context map).
        for txn in transactions:
            cinfo = txn_currency_map.get(str(txn["id"]))
            if cinfo:
                txn["currency"] = cinfo["currency"]
                txn["foreign_debit"] = cinfo["foreign_debit"]
                txn["foreign_credit"] = cinfo["foreign_credit"]

        imported_ids = _get_imported_qbo_ids(ctx)
        _logger.info(f"Found {len(imported_ids)} already-imported QBO IDs")
        new_txns = [t for t in transactions if str(t["id"]) not in imported_ids]

        type_counts = defaultdict(int)
        for t in new_txns:
            if t["lines"]:
                type_counts[t["lines"][0]["type"]] += 1
        _logger.info(
            f"After filtering: {len(new_txns)} unimported transactions "
            f"({', '.join(f'{t}: {c}' for t, c in sorted(type_counts.items()))})"
        )

        # Preload everything needed to build typed moves when enriching.
        extractor.preload(
            "account",
            "customer",
            "vendor",
            "product",
            "product_income",
            "product_expense",
            "sale_tax",
            "sale_tax_rate",
            "purchase_tax",
            "purchase_tax_rate",
            "currency",
        )
        extractor.preload_journals("general", "sale", "purchase")
        extractor.preload_account_journal_map()
        extractor.preload_undeposited_funds()
        _build_code_maps(ctx, extractor)

        # Payment-specific preloads: bank account map, AR/AP account maps,
        # and invoice/bill QBO ID → move ID maps for LinkedTxn resolution.
        ctx.env.cr.execute(
            "SELECT id, default_account_id FROM account_journal "
            "WHERE default_account_id IS NOT NULL AND company_id = %s",
            [extractor._company_id],
        )
        extractor.extra["journal_bank_account_map"] = {
            row[0]: row[1] for row in ctx.env.cr.fetchall()
        }
        extractor.extra["invoice_map"] = extractor.qbo_id_map(
            "account_move", "qbo_invoice_id", where="state = 'posted'"
        )
        extractor.extra["bill_map"] = extractor.qbo_id_map(
            "account_move", "qbo_bill_id", where="state = 'posted'"
        )
        extractor.extra["credit_memo_map"] = extractor.qbo_id_map(
            "account_move", "qbo_credit_memo_id", where="state = 'posted'"
        )
        extractor.extra["vendor_credit_map"] = extractor.qbo_id_map(
            "account_move", "qbo_vendor_credit_id", where="state = 'posted'"
        )
        extractor.extra["journal_entry_map"] = extractor.qbo_id_map(
            "account_move", "qbo_journal_entry_id", where="state = 'posted'"
        )
        extractor.extra["invoice_receivable_map"] = extractor.invoice_receivable_map()
        extractor.extra["bill_payable_map"] = extractor.bill_payable_map()
        extractor.extra["partner_receivable_map"] = extractor.partner_receivable_map()
        extractor.extra["partner_payable_map"] = extractor.partner_payable_map()

        # Fetch QBO entities for enrichable transaction types only.
        api_client = get_api_client(ctx)
        entities: Dict[str, Dict[str, Dict]] = {k: {} for k in _ENRICHABLE}

        enrich_ids: Dict[str, Set[str]] = {k: set() for k in _ENRICHABLE}
        for t in new_txns:
            if not t.get("lines"):
                continue
            txn_type = t["lines"][0]["type"]
            if txn_type in _ENRICHABLE:
                enrich_ids[txn_type].add(str(t["id"]))

        total_to_fetch = sum(len(v) for v in enrich_ids.values())
        _logger.info(f"Need to fetch {total_to_fetch} enrichable QBO entities")

        for export_type, ids in enrich_ids.items():
            if not ids:
                continue
            entity_name, _ = _ENRICHABLE[export_type]
            wanted = set(ids)
            _logger.info(f"Bulk fetching {export_type} via query_all ({len(wanted)} needed)")
            with ctx.skippable(f"bulk fetch QBO {entity_name} ({len(wanted)} ids)"):
                # Much faster than per-id GET, and QBOApiClient already logs progress
                # every 1000 records.
                all_recs = api_client.query_all(entity=entity_name, order_by="Id")

            fetched = 0
            for rec in all_recs:
                rec_id = str(rec.get("Id") or "")
                if rec_id and rec_id in wanted:
                    entities[export_type][rec_id] = rec
                    fetched += 1
            _logger.info(f"Fetched {fetched}/{len(wanted)} {export_type} entities (filtered)")

        # Fetch Payment and BillPayment entities for enriched payment creation.
        payment_entities: Dict[str, Dict[str, Dict]] = {
            k: {} for k in _PAYMENT_TYPES
        }
        # Collect IDs, deduplicating by QBO entity name (both "Bill Payment
        # (Cheque)" and "Bill Payment (Credit Card)" map to "BillPayment").
        payment_ids_by_entity: Dict[str, Set[str]] = {}
        payment_type_for_id: Dict[str, str] = {}  # qbo_id -> export_type
        for t in new_txns:
            if not t.get("lines"):
                continue
            txn_type = t["lines"][0]["type"]
            if txn_type in _PAYMENT_TYPES:
                qbo_id = str(t["id"])
                entity_name, _ = _PAYMENT_TYPES[txn_type]
                payment_ids_by_entity.setdefault(entity_name, set()).add(qbo_id)
                payment_type_for_id[qbo_id] = txn_type

        for entity_name, wanted in payment_ids_by_entity.items():
            if not wanted:
                continue
            _logger.info(
                f"Bulk fetching {entity_name} via query_all ({len(wanted)} needed)"
            )
            with ctx.skippable(f"bulk fetch QBO {entity_name} ({len(wanted)} ids)"):
                all_recs = api_client.query_all(entity=entity_name, order_by="Id")
            fetched = 0
            for rec in all_recs:
                rec_id = str(rec.get("Id") or "")
                if rec_id and rec_id in wanted:
                    export_type = payment_type_for_id.get(rec_id)
                    if export_type:
                        payment_entities[export_type][rec_id] = rec
                        fetched += 1
            _logger.info(
                f"Fetched {fetched}/{len(wanted)} {entity_name} entities (filtered)"
            )

        # Ensure exchange rates exist for all enrichable foreign-currency txns
        # before the transform creates moves (otherwise Odoo falls back to 1.0).
        all_api_records = [
            rec
            for type_dict in entities.values()
            for rec in type_dict.values()
        ] + [
            rec
            for type_dict in payment_entities.values()
            for rec in type_dict.values()
        ]
        if all_api_records:
            inserted = ExchangeRateEnsurer(ctx.env).ensure_rates(all_api_records)
            _logger.info(f"Exchange rates: ensured from {len(all_api_records)} API records ({inserted} new)")

        return ChunkableData(
            records=new_txns,
            context={
                "extractor": extractor.export(),
                "entities": entities,
                "payment_entities": payment_entities,
            },
        )

    @ETL.transform()
    def transform_gl_first(self, ctx: ETLContext, extracted: Dict) -> Dict:
        data = extracted.get("extract_gl_first")
        if not data:
            return {"move_vals": [], "payment_vals": []}
        transactions = data.records if hasattr(data, "records") else data.get("records", [])
        context = data.context if hasattr(data, "context") else {}
        extractor_data = context.get("extractor", {})
        entities = context.get("entities", {}) or {}
        payment_entities = context.get("payment_entities", {}) or {}

        if not transactions:
            return {"move_vals": [], "payment_vals": []}

        from .move_builder import QBOMoveBuilder

        builder = QBOMoveBuilder(extractor_data)
        code_map = builder.get_extra("code_map") or {}
        account_type_map = builder.get_extra("account_type_map") or {}
        account_currency_map = builder.get_extra("account_currency_map") or {}
        company_currency_id = builder.get_extra("company_currency_id")
        currency_name_map = builder.get_extra("currency_name_map") or {}
        company_id = builder._company_id

        general_journal_id = builder.get_journal_id("general")

        move_vals: List[Dict] = []
        payment_vals: List[Dict] = []
        skipped = 0
        enriched = 0
        enriched_payments = 0
        fallback = 0
        failed_enrichment: Dict[str, List[str]] = defaultdict(list)

        for txn in transactions:
            qbo_id = str(txn["id"])
            lines_data = txn.get("lines") or []
            if not lines_data:
                skipped += 1
                continue

            first = lines_data[0]
            txn_type = first["type"]
            txn_date = first["date"]
            txn_num = first["num"]
            txn_name = first["name"]

            # Enriched typed moves (invoices, bills, credit/vendor credit).
            if txn_type in _ENRICHABLE:
                entity = (entities.get(txn_type) or {}).get(qbo_id)
                if entity:
                    _entity_name, kwargs = _ENRICHABLE[txn_type]
                    vals = builder.build_invoice_move_vals(entity, **kwargs)
                    if vals:
                        # QBO ExchangeRate = home currency per 1 foreign unit
                        # (e.g. 1.4 means 1 USD = 1.4 CAD).  Domestic = 1.0.
                        fx_rate = float(entity.get("ExchangeRate", 1.0) or 1.0)
                        _annotate_with_gl_accounts(
                            vals, lines_data, code_map, account_type_map,
                            exchange_rate=fx_rate,
                        )
                        move_vals.append(vals)
                        enriched += 1
                        continue
                # Log why enrichment failed for investigation.
                reason = "not in API" if not entity else "builder returned None"
                failed_enrichment[txn_type].append(f"{qbo_id} ({reason})")

            # Enriched payments (account.payment records).
            if txn_type in _PAYMENT_TYPES:
                entity = (payment_entities.get(txn_type) or {}).get(qbo_id)
                if entity:
                    _entity_name, kwargs = _PAYMENT_TYPES[txn_type]
                    result = _build_payment_vals(
                        entity, builder,
                        gl_lines=lines_data,
                        code_map=code_map,
                        account_type_map=account_type_map,
                        **kwargs,
                    )
                    if result:
                        payment_vals.append(result)
                        enriched_payments += 1
                        continue
                reason = "not in API" if not entity else "builder returned None"
                failed_enrichment[txn_type].append(f"{qbo_id} ({reason})")

            # Fallback: generic journal entry from GL export lines.
            # Compute exchange rate from TransactionList currency data
            # (annotated on the txn dict during extract).
            txn_currency = txn.get("currency", "")
            txn_currency_id = currency_name_map.get(txn_currency)
            fgn_total = txn.get("foreign_debit", 0) + txn.get("foreign_credit", 0)
            home_total = sum(ld["debit"] + ld["credit"] for ld in lines_data)
            txn_exchange_rate = (
                home_total / fgn_total if fgn_total else 1.0
            )

            entry_vals = _journal_entry_vals_from_export(
                txn_id=qbo_id,
                txn_type=txn_type,
                txn_date=txn_date,
                txn_num=txn_num,
                txn_name=txn_name,
                lines_data=lines_data,
                code_map=code_map,
                account_currency_map=account_currency_map,
                company_currency_id=company_currency_id,
                txn_currency_id=txn_currency_id,
                txn_exchange_rate=txn_exchange_rate,
                journal_id=general_journal_id,
                company_id=company_id,
            )
            if entry_vals:
                # Mark QBO ID field so downstream pipelines skip duplicates
                # and the reconciliation pipeline can find these moves.
                _QBO_TYPE_FIELD = {
                    "Invoice": "qbo_invoice_id",
                    "Bill": "qbo_bill_id",
                    "Credit Memo": "qbo_credit_memo_id",
                    "Vendor Credit": "qbo_vendor_credit_id",
                    "Payment": "qbo_payment_id",
                    "Bill Payment (Cheque)": "qbo_bill_payment_id",
                    "Bill Payment (Credit Card)": "qbo_bill_payment_id",
                    "Expense": "qbo_expense_id",
                    "Transfer": "qbo_transfer_id",
                    "Deposit": "qbo_deposit_id",
                    "Journal Entry": "qbo_journal_entry_id",
                    "Sales Receipt": "qbo_sales_receipt_id",
                    "Refund Receipt": "qbo_refund_receipt_id",
                    "Sales Tax Payment": "qbo_tax_payment_id",
                    "Tax Payment": "qbo_tax_payment_id",
                    "Credit Card Payment": "qbo_cc_payment_id",
                }
                field = _QBO_TYPE_FIELD.get(txn_type)
                if field:
                    entry_vals[field] = int(qbo_id)
                move_vals.append(entry_vals)
                if txn_type in _ENRICHABLE or txn_type in _PAYMENT_TYPES:
                    fallback += 1
            else:
                skipped += 1

        # Log enrichment stats.
        _logger.info(
            f"Transformed {len(move_vals)} moves, {len(payment_vals)} payments "
            f"(enriched={enriched}, enriched_payments={enriched_payments}, "
            f"fallback={fallback}, skipped={skipped})"
        )
        for etype, ids in failed_enrichment.items():
            _logger.info(
                f"  {etype}: {len(ids)} fell through to entry — "
                f"first 10: {ids[:10]}"
            )
        return {"move_vals": move_vals, "payment_vals": payment_vals}

    @ETL.load()
    def load_gl_first(self, ctx: ETLContext, transformed: Dict) -> None:
        transform_result = transformed.get("transform_gl_first", {})
        # Handle both old format (list) and new format (dict).
        if isinstance(transform_result, list):
            move_vals = transform_result
            payment_vals = []
        else:
            move_vals = transform_result.get("move_vals", [])
            payment_vals = transform_result.get("payment_vals", [])

        if not move_vals and not payment_vals:
            _logger.info("No GL-first transactions to create")
            return

        # Separate GL metadata that isn't a real field before create().
        gl_truth: Dict[int, Dict] = {}  # index in move_vals -> truth

        for i, vals in enumerate(move_vals):
            truth = {}
            if "_gl_arap_account_id" in vals:
                truth["arap"] = vals.pop("_gl_arap_account_id")
            if "_tax_amounts" in vals:
                truth["tax_amounts"] = vals.pop("_tax_amounts")
            # invoice_currency_rate (if present) is a real account.move
            # field — kept in vals for create().
            if truth:
                gl_truth[i] = truth

        moves = ctx.env["account.move"]
        move_index: Dict[int, int] = {}  # move.id -> vals index
        for i, vals in enumerate(move_vals):
            ref = vals.get("ref") or vals.get("name") or "?"
            with ctx.skippable(f"create move {ref}"):
                move = ctx.env["account.move"].create(vals)
                moves |= move
                move_index[move.id] = i

        _logger.info(f"Created {len(moves)} moves")

        # ── Pre-posting corrections (on draft moves) ──

        # 1. Fix tax amounts via ORM (writes amount_currency, inverse syncs
        #    balance/debit/credit, payment_term auto-rebalances).
        #    Must run BEFORE the qbo_acct_id SQL restore so the sync stack
        #    sees the same base-line state as create().
        fixed_tax = 0
        for move in moves:
            idx = move_index.get(move.id)
            truth = gl_truth.get(idx) if idx is not None else None
            if not truth or "tax_amounts" not in truth:
                continue
            with ctx.skippable(f"fix taxes on {move.ref or move.name or '?'}"):
                fixed_tax += _fix_tax_amounts_orm(
                    move,
                    truth["tax_amounts"],
                    move_vals[idx].get("move_type", "entry"),
                )
        if fixed_tax:
            _logger.info(f"Pre-posting tax fix: {fixed_tax} tax lines adjusted (ORM)")

        # 2. Restore GL accounts on product lines (bulk SQL — safe because
        #    account_id changes don't affect amounts or tax computation).
        if moves:
            ctx.env.cr.execute(
                """
                UPDATE account_move_line
                   SET account_id = qbo_acct_id
                 WHERE qbo_acct_id IS NOT NULL
                   AND account_id <> qbo_acct_id
                   AND move_id IN %s
                """,
                (tuple(moves.ids),),
            )
            restored = ctx.env.cr.rowcount
            if restored:
                _logger.info(f"Restored GL accounts on {restored} lines (pre-posting)")
                ctx.env.invalidate_all()

        # ── Post moves, grouped by journal ──
        posted = 0
        by_journal: Dict[int, "models.Model"] = {}
        for move in moves:
            by_journal.setdefault(move.journal_id.id, self.env["account.move"])
            by_journal[move.journal_id.id] |= move
        for journal_id, journal_moves in sorted(by_journal.items()):
            with post_lock(ctx.env.cr, journal_id):
                for move in journal_moves:
                    with ctx.skippable(f"post move {move.ref or move.name or '?'}"):
                        move.with_context(
                            skip_cogs_generation=True,
                        ).action_post()
                        posted += 1

        _logger.info(f"Posted {posted} moves")

        # ── Post-posting: fix payment_term account_id → GL truth ──
        corrected_arap = 0
        for move in moves:
            idx = move_index.get(move.id)
            if idx is None or idx not in gl_truth:
                continue
            arap_id = gl_truth[idx].get("arap")
            if arap_id:
                for line in move.line_ids:
                    if line.display_type == "payment_term" and line.account_id.id != arap_id:
                        ctx.env.cr.execute(
                            "UPDATE account_move_line SET account_id = %s WHERE id = %s",
                            (arap_id, line.id),
                        )
                        corrected_arap += 1

        if corrected_arap:
            ctx.env.invalidate_all()
            _logger.info(f"Post-posting: corrected {corrected_arap} AR/AP accounts")

        # ── Create and post enriched payments (account.payment) ──
        if payment_vals:
            _logger.info(f"Creating {len(payment_vals)} enriched payments")

            # Ensure all target journals have manual payment method lines.
            journal_ids = {
                pmt["payment_vals"]["journal_id"]
                for pmt in payment_vals
                if pmt["payment_vals"].get("journal_id")
            }
            _ensure_payment_method_lines(ctx.env, journal_ids)

            # Create payments.
            payments = []  # (payment_record, linked_moves, is_customer, account_type, fx_info)
            for pmt in payment_vals:
                pmt_vals = pmt["payment_vals"]
                qbo_id = (
                    pmt_vals.get("qbo_payment_id")
                    or pmt_vals.get("qbo_bill_payment_id")
                    or "?"
                )
                with ctx.skippable(f"create payment QBO#{qbo_id}"):
                    outstanding_id = pmt_vals.pop("outstanding_account_id", None)
                    payment = ctx.env["account.payment"].create(pmt_vals)
                    if outstanding_id:
                        payment.outstanding_account_id = outstanding_id
                    fx_info = (pmt.get("currency_code"), pmt.get("exchange_rate"))
                    payments.append((
                        payment,
                        pmt["linked_moves"],
                        pmt["is_customer"],
                        pmt["account_type"],
                        fx_info,
                    ))

            # Post payments, grouped by journal.
            # For foreign-currency payments, upsert the QBO per-transaction
            # rate before posting so currency._convert() uses the exact rate.
            rate_ensurer = ExchangeRateEnsurer(ctx.env)
            pmt_by_journal: Dict[int, list] = {}
            for payment, linked, is_cust, acct_type, fx_info in payments:
                jid = payment.journal_id.id
                pmt_by_journal.setdefault(jid, []).append(
                    (payment, linked, is_cust, acct_type, fx_info)
                )

            posted_payments = 0
            for journal_id, group in sorted(pmt_by_journal.items()):
                with post_lock(ctx.env.cr, journal_id):
                    for payment, _linked, _is_cust, _acct_type, fx_info in group:
                        qbo_id = (
                            payment.qbo_payment_id
                            or payment.qbo_bill_payment_id
                            or "?"
                        )
                        with ctx.skippable(f"post payment QBO#{qbo_id}"):
                            fx_code, fx_rate = fx_info
                            if fx_code and fx_rate:
                                rate_ensurer.set_rate(
                                    fx_code, str(payment.date), fx_rate,
                                )
                            payment.action_post()
                            posted_payments += 1

            _logger.info(
                f"Created and posted {posted_payments} enriched payments "
                f"(reconciliation deferred to gl.first.reconciliation pipeline)"
            )

