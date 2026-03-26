"""QBO Extractor — Shared data extraction for QBO ETL pipelines.

Owns all SQL/ORM queries, lookup map construction, journal resolution,
and serialization.  Created in the **extract** phase so that transform
never touches the database::

    # ── extract ──
    extractor = QBOExtractor(ctx)
    extractor.preload("account", "customer", "sale_tax", "currency")
    extractor.preload_journals("sale")
    extractor.extra["estimate_map"] = extractor.qbo_id_map(
        "sale_order", "qbo_estimate_id",
    )
    return ChunkableData(
        records=new_records,
        context={"extractor": extractor.export()},
    )

    # ── transform ──
    data = extracted.get("extract_xxx")
    builder = QBOMoveBuilder(data.context["extractor"])
"""

import logging
from typing import Any, Dict, Optional, Set, Tuple

from odoo.addons.etl_framework import ETLContext

_logger = logging.getLogger(__name__)


class QBOExtractor:
    """Shared querying and map-preloading for QBO ETL pipelines.

    All database queries are performed eagerly during ``preload*()`` calls
    or via the generic query helpers.  The result is serialized with
    ``export()`` and passed to ``QBOMoveBuilder`` in the transform phase.
    """

    def __init__(self, ctx: ETLContext):
        self.env = ctx.env
        self.company = ctx.env.company
        self._company_id: int = self.company.id
        self._company_currency_id: int = self.company.currency_id.id

        # Lookup maps (None = not loaded)
        self._account_map: Optional[Dict[int, int]] = None
        self._account_currency_map: Optional[Dict[int, Optional[int]]] = None
        self._customer_map: Optional[Dict[int, int]] = None
        self._vendor_map: Optional[Dict[int, int]] = None
        self._product_map: Optional[Dict[int, int]] = None
        self._product_income_map: Optional[Dict[int, int]] = None
        self._product_expense_map: Optional[Dict[int, int]] = None
        self._sale_tax_map: Optional[Dict[str, int]] = None
        self._sale_tax_rate_map: Optional[Dict[str, int]] = None
        self._purchase_tax_map: Optional[Dict[str, int]] = None
        self._purchase_tax_rate_map: Optional[Dict[str, int]] = None
        self._currency_map: Optional[Dict[str, int]] = None

        # Journals: type → journal_id
        self._journal_ids: Dict[str, int] = {}
        # account_id → journal_id (for payment bank-journal matching)
        self._account_journal_map: Optional[Dict[int, int]] = None
        # Undeposited Funds account id
        self._undeposited_funds_id: Optional[int] = None

        # Pipeline-specific extras (arbitrary dict data)
        self.extra: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Preload helpers
    # ------------------------------------------------------------------

    _MAP_NAMES = (
        "account", "account_currency", "customer", "vendor", "product",
        "product_income", "product_expense", "sale_tax", "sale_tax_rate",
        "purchase_tax", "purchase_tax_rate", "currency",
    )

    def preload(self, *names: str) -> "QBOExtractor":
        """Eagerly load the named lookup maps.

        Accepts short names like ``"account"`` (without the ``_map`` suffix).
        """
        for name in names:
            prop = name + "_map" if not name.endswith("_map") else name
            getattr(self, prop)  # triggers the @property loader
        return self

    def preload_journals(self, *journal_types: str) -> "QBOExtractor":
        """Resolve and cache journal IDs by type.

        For ``"general"`` journals, creates one if it doesn't exist.
        """
        for jtype in journal_types:
            if jtype in self._journal_ids:
                continue
            journal = self.env["account.journal"].search(
                [("type", "=", jtype), ("company_id", "=", self._company_id)],
                limit=1,
            )
            if not journal and jtype == "general":
                journal = self.env["account.journal"].create({
                    "name": "General Journal",
                    "code": "GEN",
                    "type": "general",
                    "company_id": self._company_id,
                })
                _logger.info(
                    f"Created general journal for company {self._company_id}"
                )
            if not journal:
                raise ValueError(
                    f"No {jtype} journal found for company {self._company_id}"
                )
            self._journal_ids[jtype] = journal.id
        return self

    def preload_account_journal_map(self) -> "QBOExtractor":
        """Build account_id -> journal_id map for payment bank-journal matching."""
        self.env.cr.execute(
            "SELECT default_account_id, id FROM account_journal "
            "WHERE default_account_id IS NOT NULL AND company_id = %s",
            [self._company_id],
        )
        self._account_journal_map = {
            row[0]: row[1] for row in self.env.cr.fetchall()
        }
        return self

    def preload_undeposited_funds(self) -> "QBOExtractor":
        """Find and cache the Undeposited Funds account id."""
        account = self.env["account.account"].search(
            [
                ("code", "=like", "1408%"),
                ("company_ids", "in", [self._company_id]),
            ],
            limit=1,
        )
        if not account:
            account = self.env["account.account"].search(
                [
                    ("name", "ilike", "Undeposited Funds"),
                    ("company_ids", "in", [self._company_id]),
                ],
                limit=1,
            )
        self._undeposited_funds_id = account.id if account else None
        return self

    def preload_tax_rate_account_map(self) -> "QBOExtractor":
        """Build QBO tax rate ref → Odoo tax account map.

        Used by entry-style pipelines (expenses, JEs, deposits) to add
        explicit tax lines from QBO's TxnTaxDetail.

        QBO routes entry-style taxes (expenses, JEs, deposits) through
        the GlobalTaxSuspense account (2310), NOT the GlobalTaxPayable
        account (2615) used by invoices/bills via ``tax_ids``.  We use
        a single account for all tax rates since QBO doesn't distinguish
        per-rate accounts on entries.
        """
        # Find the QBO tax suspense account (GlobalTaxSuspense = 2310)
        suspense = self.env["account.account"].search(
            [("code", "=", "2310"), ("company_ids", "in", [self._company_id])],
            limit=1,
        )
        if not suspense:
            suspense = self.env["account.account"].search(
                [("name", "ilike", "Suspense"), ("name", "ilike", "GST"),
                 ("company_ids", "in", [self._company_id])],
                limit=1,
            )

        if not suspense:
            _logger.warning(
                "No GST/HST-QST Suspense (2310) account found — "
                "tax lines on expenses/JEs/deposits will not be created"
            )
            self.extra["tax_rate_account_map"] = {}
            return self

        # Map all tax rates to the suspense account
        self.env.cr.execute("""
            SELECT DISTINCT qbo_tax_rate_id
            FROM account_tax
            WHERE qbo_tax_rate_id IS NOT NULL
                AND qbo_tax_rate_id != ''
        """)
        tax_rate_account_map = {
            str(row[0]): suspense.id for row in self.env.cr.fetchall()
        }
        self.extra["tax_rate_account_map"] = tax_rate_account_map
        _logger.info(
            f"Mapped {len(tax_rate_account_map)} tax rates to "
            f"suspense account {suspense.code} ({suspense.name})"
        )
        return self

    # ------------------------------------------------------------------
    # Generic query helpers
    # ------------------------------------------------------------------

    def existing_qbo_ids(self, table: str, qbo_field: str) -> Set[str]:
        """Return the set of already-imported QBO IDs for a given table/field."""
        if not self.column_exists(table, qbo_field):
            _logger.warning(
                f"{qbo_field} column not found on {table} - module upgrade required"
            )
            return set()
        self.env.cr.execute(
            f"SELECT {qbo_field} FROM {table} WHERE {qbo_field} IS NOT NULL"  # noqa: S608
        )
        return {str(row[0]) for row in self.env.cr.fetchall()}

    def column_exists(self, table: str, column: str) -> bool:
        """Check whether a column exists in a table."""
        self.env.cr.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = %s AND column_name = %s",
            [table, column],
        )
        return bool(self.env.cr.fetchone())

    def qbo_id_map(
        self, table: str, qbo_field: str, *, where: str = ""
    ) -> Dict[str, int]:
        """Return {str(qbo_id): odoo_id} for a table with a QBO ID field."""
        extra = f" AND {where}" if where else ""
        self.env.cr.execute(
            f"SELECT {qbo_field}, id FROM {table} "  # noqa: S608
            f"WHERE {qbo_field} IS NOT NULL{extra}"
        )
        return {str(row[0]): row[1] for row in self.env.cr.fetchall()}

    def qbo_name_map(
        self, table: str, qbo_field: str
    ) -> Dict[str, str]:
        """Return {str(qbo_id): name} for a table with a QBO ID field."""
        self.env.cr.execute(
            f"SELECT {qbo_field}, name FROM {table} "  # noqa: S608
            f"WHERE {qbo_field} IS NOT NULL"
        )
        return {str(row[0]): row[1] for row in self.env.cr.fetchall()}

    # ------------------------------------------------------------------
    # Payment-specific pre-fetch helpers
    # ------------------------------------------------------------------

    def partner_receivable_map(self) -> Dict[int, int]:
        """Return {partner_id: receivable_account_id}."""
        company_key = str(self._company_id)
        self.env.cr.execute(
            "SELECT id, (property_account_receivable_id->>%s)::int "
            "FROM res_partner "
            "WHERE property_account_receivable_id IS NOT NULL "
            "AND property_account_receivable_id ? %s",
            [company_key, company_key],
        )
        return {row[0]: row[1] for row in self.env.cr.fetchall() if row[1]}

    def partner_payable_map(self) -> Dict[int, int]:
        """Return {partner_id: payable_account_id}."""
        company_key = str(self._company_id)
        self.env.cr.execute(
            "SELECT id, (property_account_payable_id->>%s)::int "
            "FROM res_partner "
            "WHERE property_account_payable_id IS NOT NULL "
            "AND property_account_payable_id ? %s",
            [company_key, company_key],
        )
        return {row[0]: row[1] for row in self.env.cr.fetchall() if row[1]}

    def invoice_receivable_map(self) -> Dict[str, int]:
        """Return {str(qbo_invoice_id): receivable_account_id}.

        Looks up the receivable line on each posted invoice.
        """
        self.env.cr.execute("""
            SELECT am.qbo_invoice_id, aml.account_id
            FROM account_move am
            JOIN account_move_line aml ON aml.move_id = am.id
            JOIN account_account aa ON aa.id = aml.account_id
            WHERE am.qbo_invoice_id IS NOT NULL
              AND am.state = 'posted'
              AND aa.account_type = 'asset_receivable'
            GROUP BY am.qbo_invoice_id, aml.account_id
        """)
        return {str(row[0]): row[1] for row in self.env.cr.fetchall()}

    def bill_payable_map(self) -> Dict[str, int]:
        """Return {str(qbo_bill_id): payable_account_id}.

        Looks up the payable line on each posted bill.
        """
        self.env.cr.execute("""
            SELECT am.qbo_bill_id, aml.account_id
            FROM account_move am
            JOIN account_move_line aml ON aml.move_id = am.id
            JOIN account_account aa ON aa.id = aml.account_id
            WHERE am.qbo_bill_id IS NOT NULL
              AND am.state = 'posted'
              AND aa.account_type = 'liability_payable'
            GROUP BY am.qbo_bill_id, aml.account_id
        """)
        return {str(row[0]): row[1] for row in self.env.cr.fetchall()}

    # ------------------------------------------------------------------
    # Lazy-loaded lookup maps (DB queries)
    # ------------------------------------------------------------------

    @property
    def account_map(self) -> Dict[int, int]:
        """qbo_id (int) -> account_account.id"""
        if self._account_map is None:
            self.env.cr.execute(
                "SELECT qbo_id, id FROM account_account "
                "WHERE qbo_id IS NOT NULL"
            )
            self._account_map = {
                int(row[0]): row[1] for row in self.env.cr.fetchall()
            }
        return self._account_map

    @property
    def account_currency_map(self) -> Dict[int, Optional[int]]:
        """qbo_id (int) -> currency_id or None"""
        if self._account_currency_map is None:
            self.env.cr.execute(
                "SELECT qbo_id, id, currency_id FROM account_account "
                "WHERE qbo_id IS NOT NULL"
            )
            self._account_currency_map = {}
            if self._account_map is None:
                self._account_map = {}
            for row in self.env.cr.fetchall():
                qbo_id = int(row[0])
                self._account_currency_map[qbo_id] = row[2]
                self._account_map[qbo_id] = row[1]
        return self._account_currency_map

    @property
    def customer_map(self) -> Dict[int, int]:
        if self._customer_map is None:
            self.env.cr.execute(
                "SELECT qbo_customer_id, id FROM res_partner "
                "WHERE qbo_customer_id IS NOT NULL"
            )
            self._customer_map = {
                int(row[0]): row[1] for row in self.env.cr.fetchall()
            }
        return self._customer_map

    @property
    def vendor_map(self) -> Dict[int, int]:
        if self._vendor_map is None:
            self.env.cr.execute(
                "SELECT qbo_vendor_id, id FROM res_partner "
                "WHERE qbo_vendor_id IS NOT NULL"
            )
            self._vendor_map = {
                int(row[0]): row[1] for row in self.env.cr.fetchall()
            }
        return self._vendor_map

    @property
    def product_map(self) -> Dict[int, int]:
        if self._product_map is None:
            self.env.cr.execute(
                "SELECT qbo_item_id, id FROM product_product "
                "WHERE qbo_item_id IS NOT NULL"
            )
            self._product_map = {
                int(row[0]): row[1] for row in self.env.cr.fetchall()
            }
        return self._product_map

    @property
    def product_income_map(self) -> Dict[int, int]:
        if self._product_income_map is None:
            company_key = str(self._company_id)
            self.env.cr.execute("""
                SELECT pp.id, COALESCE(
                    (pt.property_account_income_id->>%s)::int,
                    (pc.property_account_income_categ_id->>%s)::int
                )
                FROM product_product pp
                JOIN product_template pt ON pp.product_tmpl_id = pt.id
                LEFT JOIN product_category pc ON pt.categ_id = pc.id
                WHERE pp.qbo_item_id IS NOT NULL
            """, [company_key, company_key])
            self._product_income_map = {
                row[0]: row[1] for row in self.env.cr.fetchall() if row[1]
            }
        return self._product_income_map

    @property
    def product_expense_map(self) -> Dict[int, int]:
        if self._product_expense_map is None:
            company_key = str(self._company_id)
            self.env.cr.execute("""
                SELECT pp.id, COALESCE(
                    (pt.property_account_expense_id->>%s)::int,
                    (pc.property_account_expense_categ_id->>%s)::int
                )
                FROM product_product pp
                JOIN product_template pt ON pp.product_tmpl_id = pt.id
                LEFT JOIN product_category pc ON pt.categ_id = pc.id
                WHERE pp.qbo_item_id IS NOT NULL
            """, [company_key, company_key])
            self._product_expense_map = {
                row[0]: row[1] for row in self.env.cr.fetchall() if row[1]
            }
        return self._product_expense_map

    def _load_tax_maps(self, use: str) -> Tuple[Dict[str, int], Dict[str, int]]:
        self.env.cr.execute(
            "SELECT qbo_tax_id, id FROM account_tax "
            "WHERE qbo_tax_id IS NOT NULL AND type_tax_use = %s",
            [use],
        )
        tax_map = {str(row[0]): row[1] for row in self.env.cr.fetchall()}
        self.env.cr.execute(
            "SELECT qbo_tax_rate_id, id FROM account_tax "
            "WHERE qbo_tax_rate_id IS NOT NULL AND type_tax_use = %s",
            [use],
        )
        tax_rate_map = {str(row[0]): row[1] for row in self.env.cr.fetchall()}
        return tax_map, tax_rate_map

    @property
    def sale_tax_map(self) -> Dict[str, int]:
        if self._sale_tax_map is None:
            self._sale_tax_map, self._sale_tax_rate_map = self._load_tax_maps("sale")
        return self._sale_tax_map

    @property
    def sale_tax_rate_map(self) -> Dict[str, int]:
        if self._sale_tax_rate_map is None:
            self._sale_tax_map, self._sale_tax_rate_map = self._load_tax_maps("sale")
        return self._sale_tax_rate_map

    @property
    def purchase_tax_map(self) -> Dict[str, int]:
        if self._purchase_tax_map is None:
            self._purchase_tax_map, self._purchase_tax_rate_map = (
                self._load_tax_maps("purchase")
            )
        return self._purchase_tax_map

    @property
    def purchase_tax_rate_map(self) -> Dict[str, int]:
        if self._purchase_tax_rate_map is None:
            self._purchase_tax_map, self._purchase_tax_rate_map = (
                self._load_tax_maps("purchase")
            )
        return self._purchase_tax_rate_map

    @property
    def currency_map(self) -> Dict[str, int]:
        if self._currency_map is None:
            self.env.cr.execute(
                "SELECT name, id FROM res_currency WHERE active = true"
            )
            self._currency_map = {
                row[0]: row[1] for row in self.env.cr.fetchall()
            }
        return self._currency_map

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def export(self) -> dict:
        """Serialize all loaded state into a plain dict.

        Pass the result through ``ChunkableData.context`` so that
        ``QBOMoveBuilder(data)`` can be used in the transform phase
        without any database access.
        """
        maps: Dict[str, dict] = {}
        for name in self._MAP_NAMES:
            val = getattr(self, f"_{name}_map")
            if val is not None:
                maps[name] = val
        return {
            "company_id": self._company_id,
            "company_currency_id": self._company_currency_id,
            "maps": maps,
            "journal_ids": dict(self._journal_ids),
            "account_journal_map": self._account_journal_map,
            "undeposited_funds_id": self._undeposited_funds_id,
            "extra": dict(self.extra),
        }
