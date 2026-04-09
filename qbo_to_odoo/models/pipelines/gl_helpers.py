"""Reusable GL-related helpers for QBO import pipelines.

Functions extracted from ``gl_first_etl.py`` and ``gl_import_etl.py`` for
use by the XLSX fallback pipeline and any other pipeline that needs
GL-level account resolution, XLSX parsing, or journal entry construction
from XLSX export data.
"""

import logging
from io import BytesIO
from typing import Dict, List, Optional, Set, Tuple

_logger = logging.getLogger(__name__)


def build_code_maps(ctx) -> Dict:
    """Build account code, currency, and type maps from Odoo.

    Returns a dict with keys: ``code_map``, ``account_currency_map``,
    ``account_type_map``, ``company_currency_id``, ``currency_name_map``.
    """
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
    code_map: Dict[str, int] = {}
    account_currency_map: Dict[int, int] = {}
    account_type_map: Dict[int, str] = {}
    for acct_id, code, currency_id, acct_type in ctx.env.cr.fetchall():
        if code:
            code_map[code] = acct_id
            if currency_id:
                account_currency_map[acct_id] = currency_id
            if acct_type:
                account_type_map[acct_id] = acct_type

    ctx.env.cr.execute("SELECT id, name FROM res_currency WHERE active = true")
    currency_name_map = {name: cid for cid, name in ctx.env.cr.fetchall()}

    return {
        "code_map": code_map,
        "account_currency_map": account_currency_map,
        "account_type_map": account_type_map,
        "company_currency_id": ctx.env.company.currency_id.id,
        "currency_name_map": currency_name_map,
    }


def resolve_account(
    code_map: Dict[str, int],
    account_code: str,
    account_type_map: Optional[Dict[int, str]] = None,
) -> Optional[Tuple[int, Optional[str]]]:
    """Resolve account code to (account_id, account_type) or None.

    Tries the code as-is, then with ``.1``, ``.2``, ``.3`` suffixes
    (QBO occasionally exports sub-account variants).
    """
    for suffix in ("", ".1", ".2", ".3"):
        account_id = code_map.get(account_code + suffix)
        if account_id:
            acct_type = account_type_map.get(account_id) if account_type_map else None
            return account_id, acct_type
    return None


def resolve_account_id(code_map: Dict[str, int], account_code: str) -> Optional[int]:
    """Resolve account code to an account_id or None."""
    result = resolve_account(code_map, account_code)
    return result[0] if result else None


def journal_entry_vals_from_export(
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
    """Build a generic journal entry from XLSX GL export lines.

    The QBO Journal export is always in company currency (CAD).  For
    lines on accounts with a foreign currency matching the transaction
    currency, we set ``currency_id`` and ``amount_currency`` using the
    transaction's exchange rate from QBO.
    """
    lines = []
    for ld in lines_data:
        account_code = ld["account_code"]
        account_id = resolve_account_id(code_map, account_code)
        if not account_id:
            _logger.warning(
                "Account %s not found for export txn #%s (%s)",
                account_code, txn_id, txn_type,
            )
            return None

        line_name = ld["memo"] or ld["name"] or txn_type
        line_vals = {
            "account_id": account_id,
            "debit": ld["debit"],
            "credit": ld["credit"],
            "name": line_name,
        }

        acct_currency = account_currency_map.get(account_id)
        if acct_currency and acct_currency != company_currency_id:
            cad_amount = ld["debit"] - ld["credit"]
            if txn_currency_id and txn_currency_id == acct_currency and txn_exchange_rate:
                line_vals["currency_id"] = acct_currency
                line_vals["amount_currency"] = round(cad_amount / txn_exchange_rate, 2)
            else:
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
            "Export txn #%s (%s) unbalanced by %.2f — skipping",
            txn_id, txn_type, diff,
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


# ---------------------------------------------------------------------------
# XLSX parsing (from gl_import_etl.py)
# ---------------------------------------------------------------------------

def parse_journal_export(file_content: bytes) -> List[Dict]:
    """Parse a QBO Journal XLSX export into grouped transactions.

    Returns a list of transactions, each with an ``id`` and ``lines``
    (list of dicts with date, type, num, name, memo, account_code,
    account_name, debit, credit).
    """
    import openpyxl

    wb = openpyxl.load_workbook(BytesIO(file_content), read_only=True)
    ws = wb.active

    transactions: List[Dict] = []
    current_id: Optional[str] = None
    current_lines: List[Dict] = []
    has_txn_id_col = False

    for row in ws.iter_rows(min_row=5, max_row=5, values_only=True):
        if len(row) > 10 and row[10] and "Transaction ID" in str(row[10]):
            has_txn_id_col = True
        break

    for row in ws.iter_rows(min_row=6, values_only=True):
        col0 = row[0]
        txn_date = row[1]
        txn_type = row[2]
        txn_num = row[3]
        name = row[4]
        memo = row[5]
        acct_code = row[6]
        acct_name = row[7]
        debit = row[8]
        credit = row[9]
        txn_id_col = row[10] if len(row) > 10 else None

        if col0 and not txn_date:
            col0_str = str(col0).strip()
            if col0_str.startswith("Total for"):
                if current_id and current_lines:
                    transactions.append({"id": current_id, "lines": current_lines})
                current_id = None
                current_lines = []
            elif col0_str.startswith("TOTAL"):
                continue
            else:
                current_id = col0_str
                current_lines = []
            continue

        if txn_type and acct_code is not None:
            if has_txn_id_col and txn_id_col:
                line_txn_id = (
                    str(int(txn_id_col))
                    if isinstance(txn_id_col, float)
                    else str(txn_id_col)
                )
                current_id = line_txn_id

            date_str = str(txn_date) if txn_date else None
            if date_str and "/" in date_str:
                parts = date_str.split("/")
                if len(parts) == 3:
                    date_str = f"{parts[2]}-{parts[1]}-{parts[0]}"

            try:
                d = float(debit) if debit else 0.0
            except (ValueError, TypeError):
                d = 0.0
            try:
                c = float(credit) if credit else 0.0
            except (ValueError, TypeError):
                c = 0.0

            if d == 0 and c == 0:
                continue

            current_lines.append({
                "date": date_str,
                "type": str(txn_type),
                "num": str(txn_num or ""),
                "name": str(name or ""),
                "memo": str(memo or ""),
                "account_code": str(acct_code).strip(),
                "account_name": str(acct_name or ""),
                "debit": d,
                "credit": c,
            })

    wb.close()
    return transactions


# ---------------------------------------------------------------------------
# Imported QBO ID collection (from gl_import_etl.py)
# ---------------------------------------------------------------------------

_QBO_ID_FIELDS = [
    "qbo_invoice_id",
    "qbo_bill_id",
    "qbo_expense_id",
    "qbo_transfer_id",
    "qbo_deposit_id",
    "qbo_journal_entry_id",
    "qbo_credit_memo_id",
    "qbo_vendor_credit_id",
    "qbo_sales_receipt_id",
    "qbo_refund_receipt_id",
    "qbo_tax_payment_id",
    "qbo_cc_payment_id",
    "qbo_payment_id",
    "qbo_bill_payment_id",
]

_QBO_PAYMENT_FIELDS = [
    "qbo_payment_id",
    "qbo_bill_payment_id",
]


def get_imported_qbo_ids(ctx) -> Set[str]:
    """Collect all QBO transaction IDs already in Odoo."""
    imported: Set[str] = set()
    cr = ctx.env.cr

    for field in _QBO_ID_FIELDS:
        cr.execute(
            f"SELECT {field} FROM account_move "
            f"WHERE {field} IS NOT NULL AND {field} != 0"
        )
        imported.update(str(row[0]) for row in cr.fetchall())

    for field in _QBO_PAYMENT_FIELDS:
        cr.execute(
            f"SELECT {field} FROM account_payment "
            f"WHERE {field} IS NOT NULL AND {field} != 0"
        )
        imported.update(str(row[0]) for row in cr.fetchall())

    return imported
