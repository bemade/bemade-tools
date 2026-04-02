"""QBO Move Builder — Pure factory for account.move vals dicts.

Builds Odoo ``account.move`` vals dicts from pre-fetched data.
**No ``env``, no ``cr``, no database access.**

The builder is initialized from the dict produced by
``QBOExtractor.export()``::

    # ── transform ──
    builder = QBOMoveBuilder(data.context["extractor"])
    vals = builder.build_invoice_move_vals(entry, ...)
"""

import logging
from datetime import date, datetime
from typing import Callable, Dict, List, Literal, Optional, Tuple

_logger = logging.getLogger(__name__)


class QBOMoveBuilder:
    """Pure factory for building account.move vals dicts from QBO data.

    Constructed from a plain dict (``QBOExtractor.export()`` output).
    All ``resolve_*`` / ``build_*`` methods operate purely on in-memory
    dicts and never query the database.
    """

    def __init__(self, data: dict):
        self._company_id: int = data["company_id"]
        self._company_currency_id: int = data["company_currency_id"]

        maps = data.get("maps", {})
        self._account_map: Dict[int, int] = maps.get("account", {})
        self._account_currency_map: Dict[int, Optional[int]] = maps.get(
            "account_currency", {}
        )
        self._customer_map: Dict[int, int] = maps.get("customer", {})
        self._vendor_map: Dict[int, int] = maps.get("vendor", {})
        self._product_map: Dict[int, int] = maps.get("product", {})
        self._product_income_map: Dict[int, int] = maps.get("product_income", {})
        self._product_expense_map: Dict[int, int] = maps.get("product_expense", {})
        self._sale_tax_map: Dict[str, int] = maps.get("sale_tax", {})
        self._sale_tax_rate_map: Dict[str, int] = maps.get("sale_tax_rate", {})
        self._purchase_tax_map: Dict[str, int] = maps.get("purchase_tax", {})
        self._purchase_tax_rate_map: Dict[str, int] = maps.get(
            "purchase_tax_rate", {}
        )
        self._currency_map: Dict[str, int] = maps.get("currency", {})

        self._journal_ids: Dict[str, int] = data.get("journal_ids", {})
        self._account_journal_map: Dict[int, int] = (
            data.get("account_journal_map") or {}
        )
        self._undeposited_funds_id: Optional[int] = data.get("undeposited_funds_id")
        self._extra: Dict = data.get("extra", {})

    # ------------------------------------------------------------------
    # Map accessors (read-only, no DB fallback)
    # ------------------------------------------------------------------

    @property
    def account_map(self) -> Dict[int, int]:
        return self._account_map

    @property
    def account_currency_map(self) -> Dict[int, Optional[int]]:
        return self._account_currency_map

    @property
    def customer_map(self) -> Dict[int, int]:
        return self._customer_map

    @property
    def vendor_map(self) -> Dict[int, int]:
        return self._vendor_map

    @property
    def product_map(self) -> Dict[int, int]:
        return self._product_map

    @property
    def product_income_map(self) -> Dict[int, int]:
        return self._product_income_map

    @property
    def product_expense_map(self) -> Dict[int, int]:
        return self._product_expense_map

    @property
    def sale_tax_map(self) -> Dict[str, int]:
        return self._sale_tax_map

    @property
    def sale_tax_rate_map(self) -> Dict[str, int]:
        return self._sale_tax_rate_map

    @property
    def purchase_tax_map(self) -> Dict[str, int]:
        return self._purchase_tax_map

    @property
    def purchase_tax_rate_map(self) -> Dict[str, int]:
        return self._purchase_tax_rate_map

    @property
    def currency_map(self) -> Dict[str, int]:
        return self._currency_map

    @property
    def undeposited_funds_id(self) -> Optional[int]:
        return self._undeposited_funds_id

    def get_extra(self, key: str, default=None):
        """Access pipeline-specific data stored by QBOExtractor."""
        return self._extra.get(key, default)

    # ------------------------------------------------------------------
    # Pure resolve / utility methods
    # ------------------------------------------------------------------

    @staticmethod
    def parse_date(
        qbo_str: Optional[str], default: Optional[date] = None
    ) -> Optional[date]:
        """Parse a QBO date string (YYYY-MM-DD) into a date object."""
        if not qbo_str:
            return default
        try:
            return datetime.strptime(qbo_str, "%Y-%m-%d").date()
        except ValueError:
            return default or datetime.now().date()

    def resolve_currency(self, entry: Dict) -> Tuple[int, bool, float]:
        """Resolve currency from a QBO entry.

        Returns (currency_id, is_foreign, exchange_rate).
        """
        currency_ref = entry.get("CurrencyRef", {})
        currency_code = currency_ref.get("value") if currency_ref else None
        exchange_rate = float(entry.get("ExchangeRate", 1.0) or 1.0)

        if currency_code:
            currency_id = self.currency_map.get(currency_code)
            if not currency_id:
                currency_id = self._company_currency_id
        else:
            currency_id = self._company_currency_id

        is_foreign = currency_id != self._company_currency_id
        return currency_id, is_foreign, exchange_rate

    @staticmethod
    def convert_to_company_currency(
        amount: float, rate: float, is_foreign: bool
    ) -> float:
        """Convert a foreign currency amount to company currency."""
        if is_foreign and rate:
            return round(amount * rate, 2)
        return amount

    def resolve_partner(
        self, entry: Dict, partner_type: Literal["customer", "vendor"]
    ) -> Optional[int]:
        """Resolve a partner from CustomerRef or VendorRef."""
        if partner_type == "customer":
            ref = entry.get("CustomerRef", {})
            pmap = self.customer_map
        else:
            ref = entry.get("VendorRef", {})
            pmap = self.vendor_map
        if not ref or not ref.get("value"):
            return None
        try:
            qbo_id = int(ref["value"])
        except (ValueError, TypeError):
            return None
        return pmap.get(qbo_id)

    def resolve_product(self, detail: Dict) -> Optional[int]:
        """Resolve a product from ItemRef."""
        item_ref = detail.get("ItemRef", {})
        if not item_ref:
            return None
        try:
            qbo_item_id = int(item_ref.get("value", "0"))
        except (ValueError, TypeError):
            return None
        return self.product_map.get(qbo_item_id)

    def resolve_account(
        self,
        detail: Dict,
        product_id: Optional[int],
        direction: Literal["income", "expense"],
    ) -> Optional[int]:
        """Resolve an account: AccountRef/ItemAccountRef -> product fallback -> None."""
        # SalesItemLineDetail uses ItemAccountRef; AccountBased uses AccountRef
        for ref_key in ("ItemAccountRef", "AccountRef"):
            account_ref = detail.get(ref_key, {})
            if account_ref and account_ref.get("value"):
                try:
                    account_id = self.account_map.get(int(account_ref["value"]))
                except (ValueError, TypeError):
                    account_id = None
                if account_id:
                    return account_id
        if product_id:
            if direction == "income":
                return self.product_income_map.get(product_id)
            return self.product_expense_map.get(product_id)
        return None

    def resolve_account_with_currency(
        self, detail: Dict
    ) -> Tuple[Optional[int], Optional[int]]:
        """Resolve account and its currency (for JE lines with secondary currency)."""
        account_ref = detail.get("AccountRef", {})
        if not account_ref or not account_ref.get("value"):
            return None, None
        try:
            qbo_account_id = int(account_ref["value"])
        except (ValueError, TypeError):
            return None, None
        return (
            self.account_map.get(qbo_account_id),
            self.account_currency_map.get(qbo_account_id),
        )

    def resolve_tax(
        self,
        detail: Dict,
        entry: Dict,
        tax_use: Literal["sale", "purchase"],
    ) -> List[int]:
        """Resolve tax IDs from TaxCodeRef with TxnTaxDetail fallback."""
        tax_map = self.sale_tax_map if tax_use == "sale" else self.purchase_tax_map
        tax_rate_map = (
            self.sale_tax_rate_map if tax_use == "sale" else self.purchase_tax_rate_map
        )
        tax_code_ref = detail.get("TaxCodeRef", {})
        if not tax_code_ref:
            return []
        tax_code_value = tax_code_ref.get("value")
        if not tax_code_value or tax_code_value in ("NON", ""):
            return []

        tax_id = tax_map.get(str(tax_code_value))
        if tax_id:
            return [tax_id]

        if tax_use == "purchase":
            tax_id = tax_rate_map.get(str(tax_code_value))
            if tax_id:
                return [tax_id]

        if str(tax_code_value) == "TAX":
            tax_ids = []
            for tax_line in entry.get("TxnTaxDetail", {}).get("TaxLine", []):
                ref_val = (
                    tax_line.get("TaxLineDetail", {})
                    .get("TaxRateRef", {})
                    .get("value")
                )
                if ref_val:
                    rid = tax_rate_map.get(str(ref_val))
                    if rid and rid not in tax_ids:
                        tax_ids.append(rid)
            return tax_ids
        return []

    def get_journal_id(self, journal_type: str) -> int:
        """Return a pre-resolved journal ID.  Raises if not preloaded."""
        jid = self._journal_ids.get(journal_type)
        if jid is None:
            raise RuntimeError(
                f"Journal type '{journal_type}' was not preloaded. "
                f"Call extractor.preload_journals('{journal_type}') in extract."
            )
        return jid

    def get_journal_id_for_account(
        self, account_id: int, fallback_type: Optional[str] = "general"
    ) -> Optional[int]:
        """Return the journal whose default_account matches, or a fallback."""
        if self._account_journal_map:
            jid = self._account_journal_map.get(account_id)
            if jid:
                return jid
        if fallback_type is None:
            return None
        return self._journal_ids.get(fallback_type)

    # ------------------------------------------------------------------
    # Line balancing
    # ------------------------------------------------------------------

    @staticmethod
    def balance_lines(
        line_vals: List[tuple], is_foreign: bool, ref: str = ""
    ) -> None:
        """Balance debit/credit (and amount_currency) on entry-style lines."""
        total_debit = sum(l[2]["debit"] for l in line_vals)
        total_credit = sum(l[2]["credit"] for l in line_vals)
        diff = round(total_debit - total_credit, 2)

        if diff != 0:
            if diff > 0:
                target = max(
                    (l for l in line_vals if l[2]["credit"] > 0),
                    key=lambda l: l[2]["credit"],
                    default=None,
                )
                if target:
                    target[2]["credit"] = round(target[2]["credit"] + diff, 2)
            else:
                target = max(
                    (l for l in line_vals if l[2]["debit"] > 0),
                    key=lambda l: l[2]["debit"],
                    default=None,
                )
                if target:
                    target[2]["debit"] = round(target[2]["debit"] - diff, 2)
            _logger.debug(f"Adjusted company currency by {diff} to balance {ref}")

        if is_foreign:
            total_ac = sum(l[2].get("amount_currency", 0) for l in line_vals)
            fc_diff = round(total_ac, 2)
            if fc_diff != 0:
                if fc_diff > 0:
                    target = min(
                        (l for l in line_vals if l[2].get("amount_currency", 0) < 0),
                        key=lambda l: l[2]["amount_currency"],
                        default=None,
                    )
                else:
                    target = max(
                        (l for l in line_vals if l[2].get("amount_currency", 0) > 0),
                        key=lambda l: l[2]["amount_currency"],
                        default=None,
                    )
                if target:
                    target[2]["amount_currency"] = round(
                        target[2]["amount_currency"] - fc_diff, 2
                    )
                _logger.debug(
                    f"Adjusted foreign currency by {fc_diff} to balance {ref}"
                )

    # ------------------------------------------------------------------
    # Tax line builders (for entry-style moves with TxnTaxDetail)
    # ------------------------------------------------------------------

    def build_tax_lines_from_detail(
        self,
        entry: Dict,
        currency_id: int,
        exchange_rate: float,
        is_foreign: bool,
        as_credit: bool = False,
    ) -> Tuple[List[tuple], float]:
        """Build explicit journal entry lines from QBO TxnTaxDetail.

        Invoice and bill pipelines use Odoo's ``tax_ids`` field, which
        computes tax lines automatically. But entry-style moves (journal
        entries, expenses, deposits) don't use ``tax_ids`` — they build
        raw debit/credit lines. When QBO puts taxes in ``TxnTaxDetail``
        rather than as regular ``Line`` entries, we need to add them as
        explicit lines using the tax rate's account from the preloaded
        ``tax_rate_account_map``.

        Args:
            as_credit: If True, positive tax amounts become credits
                (used for deposits where tax collected is a liability).
                If False (default), positive amounts become debits
                (used for expenses where tax paid is recoverable).

        Returns a tuple of (line_tuples, total_tax_company) where
        total_tax_company is positive for net debits, negative for
        net credits.
        """
        tax_rate_account_map = self.get_extra("tax_rate_account_map") or {}
        tax_lines = entry.get("TxnTaxDetail", {}).get("TaxLine", [])
        result = []
        total_tax_company = 0.0

        for tax_line in tax_lines:
            detail = tax_line.get("TaxLineDetail", {})
            tax_amount = float(tax_line.get("Amount", 0) or 0)
            if tax_amount == 0:
                continue
            rate_ref = detail.get("TaxRateRef", {}).get("value")
            if not rate_ref:
                continue
            tax_account_id = tax_rate_account_map.get(str(rate_ref))
            if not tax_account_id:
                _logger.warning(
                    f"No tax account for rate ref {rate_ref} in "
                    f"entry {entry.get('Id')}"
                )
                continue

            abs_amount = abs(tax_amount)
            company_amount = self.convert_to_company_currency(
                abs_amount, exchange_rate, is_foreign
            )

            # Determine debit/credit based on sign and direction
            is_credit_line = (tax_amount > 0 and as_credit) or (tax_amount < 0 and not as_credit)

            if is_credit_line:
                line_data = {
                    "account_id": tax_account_id,
                    "name": detail.get("TaxRateRef", {}).get("name", "Tax"),
                    "debit": 0.0,
                    "credit": round(company_amount, 2),
                }
                total_tax_company -= round(company_amount, 2)
            else:
                line_data = {
                    "account_id": tax_account_id,
                    "name": detail.get("TaxRateRef", {}).get("name", "Tax"),
                    "debit": round(company_amount, 2),
                    "credit": 0.0,
                }
                total_tax_company += round(company_amount, 2)

            if is_foreign:
                line_data["currency_id"] = currency_id
                line_data["amount_currency"] = (
                    -abs_amount if is_credit_line else abs_amount
                )

            result.append((0, 0, line_data))

        return result, total_tax_company

    # ------------------------------------------------------------------
    # Invoice-style line builders
    # ------------------------------------------------------------------

    def build_invoice_line(
        self,
        line: Dict,
        detail: Dict,
        detail_type: str,
        entry: Dict,
        tax_use: Literal["sale", "purchase"],
        direction: Literal["income", "expense"],
        force_positive: bool = False,
    ) -> Optional[Dict]:
        """Build an invoice_line_ids vals dict from a QBO line."""
        if detail_type == "SalesItemLineDetail":
            return self._build_sales_item_invoice_line(
                line, detail, entry, tax_use, direction, force_positive
            )
        elif detail_type == "DiscountLineDetail":
            return self._build_discount_invoice_line(
                line, detail, entry, tax_use
            )
        elif detail_type == "ItemBasedExpenseLineDetail":
            return self._build_item_expense_invoice_line(
                line, detail, entry, tax_use, force_positive
            )
        elif detail_type == "AccountBasedExpenseLineDetail":
            return self._build_account_expense_invoice_line(
                line, detail, entry, tax_use, force_positive
            )
        return None

    def _build_sales_item_invoice_line(
        self, line, detail, entry, tax_use, direction, force_positive
    ) -> Optional[Dict]:
        item_ref = detail.get("ItemRef", {})
        # QBO uses the magic value "SHIPPING_ITEM_ID" for shipping lines.
        # These have no ItemAccountRef and can't resolve to a product.
        # Look up the shipping account from QBO's Item.IncomeAccountRef.
        is_shipping = item_ref.get("value") == "SHIPPING_ITEM_ID"
        product_id = None if is_shipping else self.resolve_product(detail)
        if is_shipping:
            account_id = self.get_extra("shipping_account_id")
        else:
            account_id = self.resolve_account(detail, product_id, direction)
        qty = float(detail.get("Qty", 1) or 1)
        unit_price = float(detail.get("UnitPrice", 0) or 0)
        amount = float(line.get("Amount", 0) or 0)
        if not unit_price and amount and qty:
            unit_price = amount / qty
        if force_positive:
            unit_price = abs(unit_price)
        tax_ids = self.resolve_tax(detail, entry, tax_use)
        line_vals = {
            "name": line.get("Description", "") or item_ref.get("name", "") or "/",
            "quantity": qty,
            "price_unit": unit_price,
        }
        if product_id:
            line_vals["product_id"] = product_id
        if account_id:
            line_vals["account_id"] = account_id
        if tax_ids:
            line_vals["tax_ids"] = [(6, 0, tax_ids)]
        return line_vals

    def _build_discount_invoice_line(
        self, line, detail, entry, tax_use
    ) -> Optional[Dict]:
        """Build an invoice line for a QBO DiscountLineDetail.

        QBO discounts have a DiscountAccountRef and may be percentage
        or fixed amount.  They create a negative (contra-revenue) line.
        """
        amount = float(line.get("Amount", 0) or 0)
        if amount == 0:
            return None

        # Resolve discount account from DiscountAccountRef
        account_ref = detail.get("DiscountAccountRef", {})
        account_id = None
        if account_ref and account_ref.get("value"):
            try:
                account_id = self.account_map.get(int(account_ref["value"]))
            except (ValueError, TypeError):
                pass

        tax_ids = self.resolve_tax(detail, entry, tax_use)

        line_vals = {
            "name": line.get("Description", "") or "Discount",
            "quantity": 1,
            "price_unit": -abs(amount),  # Negative for contra-revenue
        }
        if account_id:
            line_vals["account_id"] = account_id
        if tax_ids:
            line_vals["tax_ids"] = [(6, 0, tax_ids)]
        return line_vals

    def _build_item_expense_invoice_line(
        self, line, detail, entry, tax_use, force_positive
    ) -> Optional[Dict]:
        item_ref = detail.get("ItemRef", {})
        product_id = self.resolve_product(detail)
        qty = float(detail.get("Qty", 1) or 1)
        unit_price = float(detail.get("UnitPrice", 0) or 0)
        amount = float(line.get("Amount", 0) or 0)
        if not unit_price and amount and qty:
            unit_price = amount / qty
        if force_positive:
            unit_price = abs(unit_price)
        tax_ids = self.resolve_tax(detail, entry, tax_use)
        line_vals = {
            "name": line.get("Description", "") or item_ref.get("name", "") or "/",
            "quantity": qty,
            "price_unit": unit_price,
        }
        if product_id:
            line_vals["product_id"] = product_id
            expense_account_id = self.product_expense_map.get(product_id)
            if expense_account_id:
                line_vals["account_id"] = expense_account_id
        if tax_ids:
            line_vals["tax_ids"] = [(6, 0, tax_ids)]
        return line_vals

    def _build_account_expense_invoice_line(
        self, line, detail, entry, tax_use, force_positive
    ) -> Optional[Dict]:
        account_ref = detail.get("AccountRef", {})
        account_id = None
        if account_ref and account_ref.get("value"):
            try:
                account_id = self.account_map.get(int(account_ref["value"]))
            except (ValueError, TypeError):
                pass
        amount = float(line.get("Amount", 0) or 0)
        if force_positive:
            amount = abs(amount)
        tax_ids = self.resolve_tax(detail, entry, tax_use)
        line_vals = {
            "name": line.get("Description", "")
            or account_ref.get("name", "")
            or "/",
            "quantity": 1,
            "price_unit": amount,
        }
        if account_id:
            line_vals["account_id"] = account_id
        if tax_ids:
            line_vals["tax_ids"] = [(6, 0, tax_ids)]
        return line_vals

    # ------------------------------------------------------------------
    # Entry-style line builder
    # ------------------------------------------------------------------

    def build_entry_line(
        self,
        line: Dict,
        detail: Dict,
        detail_type: str,
        currency_id: int,
        exchange_rate: float,
        is_foreign: bool,
        direction: Literal["income", "expense"],
    ) -> Optional[Dict]:
        """Build an entry-style (debit/credit) line vals dict.

        Returns the dict with an extra ``_amount_foreign`` key for
        counter-line computation (caller should pop it before saving).
        """
        amount_foreign = float(line.get("Amount", 0) or 0)
        if amount_foreign == 0:
            return None

        account_id = self._resolve_entry_line_account(detail, detail_type)
        if not account_id:
            return None

        abs_foreign = abs(amount_foreign)
        abs_company = self.convert_to_company_currency(
            abs_foreign, exchange_rate, is_foreign
        )

        is_credit = (
            (amount_foreign > 0 and direction == "income")
            or (amount_foreign < 0 and direction == "expense")
        )

        if is_credit:
            line_vals = {
                "account_id": account_id,
                "credit": abs_company,
                "debit": 0,
                "name": line.get("Description") or "/",
                "_amount_foreign": amount_foreign,
            }
            if is_foreign:
                line_vals["currency_id"] = currency_id
                line_vals["amount_currency"] = -abs_foreign
        else:
            line_vals = {
                "account_id": account_id,
                "credit": 0,
                "debit": abs_company,
                "name": line.get("Description") or "/",
                "_amount_foreign": amount_foreign,
            }
            if is_foreign:
                line_vals["currency_id"] = currency_id
                line_vals["amount_currency"] = abs_foreign

        return line_vals

    def _resolve_entry_line_account(
        self, detail: Dict, detail_type: str
    ) -> Optional[int]:
        """Resolve the account_id for an entry-style line detail."""
        if detail_type == "SalesItemLineDetail":
            product_id = self.resolve_product(detail)
            return self.resolve_account(detail, product_id, "income")

        if detail_type in (
            "AccountBasedExpenseLineDetail",
            "DepositLineDetail",
        ):
            account_ref = detail.get("AccountRef", {})
            qbo_val = account_ref.get("value") if account_ref else None
            if qbo_val:
                try:
                    aid = self.account_map.get(int(qbo_val))
                except (ValueError, TypeError):
                    aid = None
                if aid:
                    return aid
            if detail_type == "DepositLineDetail":
                return self._undeposited_funds_id
            return None

        if detail_type == "ItemBasedExpenseLineDetail":
            product_id = self.resolve_product(detail)
            if product_id:
                aid = self.product_expense_map.get(product_id)
                if aid:
                    return aid
            account_ref = detail.get("AccountRef", {})
            if account_ref and account_ref.get("value"):
                try:
                    return self.account_map.get(int(account_ref["value"]))
                except (ValueError, TypeError):
                    pass
            return None

        return None

    # ------------------------------------------------------------------
    # High-level move builders
    # ------------------------------------------------------------------

    def build_invoice_move_vals(
        self,
        entry: Dict,
        *,
        move_type: str,
        journal_type: str,
        partner_type: Literal["customer", "vendor"],
        qbo_id_field: str,
        line_detail_types: Tuple[str, ...],
        tax_use: Literal["sale", "purchase"],
        direction: Literal["income", "expense"],
        memo_field: str = "CustomerMemo",
        memo_key: Optional[str] = "value",
    ) -> Optional[Dict]:
        """Build vals for an invoice-style move."""
        qbo_id = entry.get("Id")
        partner_id = self.resolve_partner(entry, partner_type)
        if not partner_id:
            ref_key = "CustomerRef" if partner_type == "customer" else "VendorRef"
            _logger.warning(
                f"{partner_type.title()} not found for "
                f"QBO ID {entry.get(ref_key, {}).get('value')} "
                f"in {qbo_id_field.replace('qbo_', '').replace('_id', '')} {qbo_id}"
            )
            return None

        invoice_date = self.parse_date(entry.get("TxnDate"))
        invoice_date_due = self.parse_date(entry.get("DueDate"))
        currency_id, _, _ = self.resolve_currency(entry)

        force_positive = move_type in ("out_refund", "in_refund")
        line_vals = []
        for line in entry.get("Line", []):
            dt = line.get("DetailType", "")
            if dt not in line_detail_types:
                continue
            detail = line.get(dt, {})
            if not detail:
                continue
            ld = self.build_invoice_line(
                line, detail, dt, entry,
                tax_use=tax_use, direction=direction,
                force_positive=force_positive,
            )
            if ld:
                line_vals.append((0, 0, ld))

        if not line_vals:
            _logger.warning(
                f"No valid lines for "
                f"{qbo_id_field.replace('qbo_', '').replace('_id', '')} {qbo_id}"
            )
            return None

        actual_move_type = move_type
        if move_type in ("out_invoice", "in_invoice"):
            total_amt = float(entry.get("TotalAmt", 0) or 0)
            if total_amt < 0:
                actual_move_type = (
                    "out_refund" if move_type == "out_invoice" else "in_refund"
                )
                for lt in line_vals:
                    if "price_unit" in lt[2]:
                        lt[2]["price_unit"] = abs(lt[2]["price_unit"])

        narration = ""
        if memo_key:
            narration = entry.get(memo_field, {}).get(memo_key, "")
        else:
            narration = entry.get(memo_field, "")

        vals = {
            "move_type": actual_move_type,
            "journal_id": self.get_journal_id(journal_type),
            "partner_id": partner_id,
            "invoice_date": invoice_date,
            "currency_id": currency_id,
            "ref": entry.get("DocNumber", ""),
            "narration": narration,
            "invoice_line_ids": line_vals,
            qbo_id_field: int(qbo_id) if qbo_id else 0,
        }
        if invoice_date_due:
            vals["invoice_date_due"] = invoice_date_due

        # Extract exact tax amounts from QBO TxnTaxDetail for GL-first
        # pre-posting correction (avoids percentage-of-line rounding drift).
        tax_rate_map = (
            self._sale_tax_rate_map if tax_use == "sale"
            else self._purchase_tax_rate_map
        )
        tax_amounts = []
        for tax_line in entry.get("TxnTaxDetail", {}).get("TaxLine", []):
            tl_detail = tax_line.get("TaxLineDetail", {})
            rate_ref = tl_detail.get("TaxRateRef", {}).get("value")
            amount = float(tax_line.get("Amount", 0) or 0)
            if rate_ref and amount:
                odoo_tax_id = tax_rate_map.get(str(rate_ref))
                if odoo_tax_id:
                    tax_amounts.append({"tax_id": odoo_tax_id, "amount": amount})
                else:
                    _logger.warning(
                        "TaxRateRef %s not mapped to Odoo tax in %s %s",
                        rate_ref, qbo_id_field, qbo_id,
                    )
        if tax_amounts:
            vals["_tax_amounts"] = tax_amounts

        # Extract AP account from APAccountRef (bills/vendor credits).
        ap_ref = entry.get("APAccountRef", {}).get("value")
        if ap_ref:
            ap_account_id = self._account_map.get(int(ap_ref))
            if ap_account_id:
                vals["_gl_arap_account_id"] = ap_account_id

        return vals

    def build_entry_move_vals(
        self,
        entry: Dict,
        *,
        journal_type: str,
        qbo_id_field: str,
        line_builder_fn: Callable[
            [Dict, int, float, bool], Optional[List[tuple]]
        ],
        ref_prefix: str = "",
        qbo_id_as_str: bool = False,
        extra_vals: Optional[Dict] = None,
    ) -> Optional[Dict]:
        """Build vals for an entry-style move.

        The caller provides a ``line_builder_fn`` callback that receives
        ``(entry, currency_id, exchange_rate, is_foreign)`` and returns a
        list of ``(0, 0, line_vals)`` tuples (or ``None`` to skip).
        """
        qbo_id = str(entry.get("Id", ""))
        txn_date = entry.get("TxnDate")
        currency_id, is_foreign, exchange_rate = self.resolve_currency(entry)

        line_ids = line_builder_fn(entry, currency_id, exchange_rate, is_foreign)
        if not line_ids:
            return None

        ref = f"{ref_prefix}{qbo_id}" if ref_prefix else f"QBO-{qbo_id}"
        self.balance_lines(line_ids, is_foreign, ref)

        qbo_id_val = qbo_id if qbo_id_as_str else (int(qbo_id) if qbo_id else 0)
        vals = {
            "move_type": "entry",
            "journal_id": self.get_journal_id(journal_type),
            "date": txn_date,
            "ref": ref,
            qbo_id_field: qbo_id_val,
            "company_id": self._company_id,
            "currency_id": currency_id,
            "line_ids": line_ids,
        }
        if extra_vals:
            vals.update(extra_vals)
        return vals
