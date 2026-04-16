"""QuickBooks Online Product/Item ETL Pipeline

This module handles the migration of Items (Products/Services) from QBO to Odoo
using the ETL framework.
"""

import logging
from collections import Counter
from typing import Dict, List

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext
from odoo.addons.etl_framework.framework import ChunkableData
from odoo.addons.qbo_to_odoo.models.pipelines.utils import get_api_client

_logger = logging.getLogger(__name__)


def _build_income_to_categ(ctx: ETLContext) -> Dict[int, int]:
    """Return a mapping of Odoo income account ID → Odoo product category ID.

    Uses xTuple-imported products as the source of truth: for each income
    account, pick the product category that appears most frequently among
    xTuple products assigned to that account.  QBO products can then fall
    back to this mapping when they have no QBO-derived category.
    """
    company_key = str(ctx.env.company.id)
    ctx.env.cr.execute(
        """
        WITH ranked AS (
            SELECT
                (pt.property_account_income_id->>%s)::int AS account_id,
                pt.categ_id,
                COUNT(*) AS cnt,
                ROW_NUMBER() OVER (
                    PARTITION BY (pt.property_account_income_id->>%s)::int
                    ORDER BY COUNT(*) DESC
                ) AS rn
            FROM product_template pt
            JOIN product_product pp ON pp.product_tmpl_id = pt.id
            WHERE pp.xtuple_item_id IS NOT NULL AND pp.xtuple_item_id != 0
              AND pt.categ_id IS NOT NULL
              AND (pt.property_account_income_id->>%s) IS NOT NULL
            GROUP BY
                (pt.property_account_income_id->>%s)::int,
                pt.categ_id
        )
        SELECT account_id, categ_id FROM ranked WHERE rn = 1
        """,
        [company_key, company_key, company_key, company_key],
    )
    return {row[0]: row[1] for row in ctx.env.cr.fetchall()}


@ETL.pipeline(
    target_model="product.product",
    importer_name="qbo.item.importer",
    sap_source="Item",
    depends_on=["qbo.category.importer"],
)
class QboItemImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO Items (Products/Services)."""

    _name = "qbo.item.importer"
    _description = "QBO Item Importer"

    @ETL.extract("Item")
    def extract_items(self, ctx: ETLContext) -> List[Dict]:
        """Extract items from QBO API."""
        api_client = get_api_client(ctx)

        # Get existing QBO item IDs
        ctx.env.cr.execute(
            "SELECT qbo_item_id FROM product_product WHERE qbo_item_id IS NOT NULL"
        )
        existing_ids = {str(row[0]) for row in ctx.env.cr.fetchall()}
        _logger.info(f"Found {len(existing_ids)} existing items in Odoo")


        # Fetch all items from QBO
        items = api_client.query_all(
            entity="Item", where="Active IN (true, false)", order_by="Id"
        )
        # Filter out already imported by QBO ID
        new_items = [item for item in items if str(item.get("Id")) not in existing_ids]
        deduped_items = self._dedup_items(ctx, new_items)

        _logger.info(
            f"Extracted {len(items)} items from QBO, {len(new_items)} new by ID, "
            f"{len(deduped_items)} after deduplication by SKU"
        )
        return deduped_items


    def _dedup_items(self, ctx: ETLContext, items) -> List[Dict]:
        # Get existing default_codes for deduplication (cross-system matching)
        ctx.env.cr.execute(
            "SELECT default_code FROM product_product WHERE default_code IS NOT NULL AND default_code != ''"
        )
        existing_default_codes = {row[0] for row in ctx.env.cr.fetchall()}
        _logger.info(
            f"Found {len(existing_default_codes)} existing products by default_code for deduplication"
        )


        # Filter out items that match existing products by SKU (deduplication)
        deduped_items = [
            item
            for item in items
            if not item.get("Sku") or item.get("Sku") not in existing_default_codes
        ]
        return deduped_items

    @ETL.transform()
    def transform_items(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO items into Odoo product values."""
        items = extracted.get("extract_items", [])

        # Build account lookup
        ctx.env.cr.execute(
            "SELECT qbo_id, id FROM account_account WHERE qbo_id IS NOT NULL"
        )
        account_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        # Build category lookup
        ctx.env.cr.execute(
            "SELECT qbo_category_id, id FROM product_category WHERE qbo_category_id IS NOT NULL"
        )
        category_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        # Fallback: income account → best xTuple category.
        # QBO often has no Category-type items, so category_map may be empty.
        # Instead, use each QBO item's IncomeAccountRef to infer the right
        # category by finding which xTuple category's products share that account.
        income_to_categ = _build_income_to_categ(ctx)
        _logger.info(
            f"Built income→category fallback map with {len(income_to_categ)} entries"
        )

        product_vals = []

        for item in items:
            item_type = item.get("Type", "")

            # Skip Category type items - they're product categories, not products
            if item_type == "Category":
                continue

            # Determine Odoo product type
            # Default to storable for most items, only Service is non-storable
            if item_type == "Service":
                product_type = "service"
                is_storable = False
            else:
                product_type = "consu"
                is_storable = True

            # Get income/expense accounts
            income_account_id = None
            expense_account_id = None

            income_ref = item.get("IncomeAccountRef", {})
            if income_ref:
                qbo_income_id = int(income_ref.get("value", 0))
                income_account_id = account_map.get(qbo_income_id)

            expense_ref = item.get("ExpenseAccountRef", {})
            if expense_ref:
                qbo_expense_id = int(expense_ref.get("value", 0))
                expense_account_id = account_map.get(qbo_expense_id)

            # Get category: try ParentRef → qbo_category_id first, then fall
            # back to income account matching against xTuple products.
            categ_id = None
            parent_ref = item.get("ParentRef", {})
            if parent_ref:
                qbo_categ_id = parent_ref.get("value")
                categ_id = category_map.get(qbo_categ_id)
            if not categ_id and income_account_id:
                categ_id = income_to_categ.get(income_account_id)

            vals = {
                "name": item.get("Name", ""),
                "description_sale": item.get("Description", ""),
                "description_purchase": item.get("PurchaseDesc", ""),
                "default_code": item.get("Sku") or None,
                "type": product_type,
                "is_storable": is_storable,
                "list_price": float(item.get("UnitPrice", 0) or 0),
                "standard_price": float(item.get("PurchaseCost", 0) or 0),
                "active": item.get("Active", True),
                "sale_ok": item_type in ("Service", "NonInventory", "Inventory"),
                "purchase_ok": bool(item.get("PurchaseCost")),
                "qbo_item_id": int(item.get("Id")),
            }

            if income_account_id:
                vals["property_account_income_id"] = income_account_id
            if expense_account_id:
                vals["property_account_expense_id"] = expense_account_id
            if categ_id:
                vals["categ_id"] = categ_id

            product_vals.append(vals)

        _logger.info(f"Transformed {len(product_vals)} product records")
        return product_vals

    @ETL.load()
    def load_items(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load items into Odoo."""
        product_vals = transformed.get("transform_items", [])

        if not product_vals:
            _logger.info("No new items to create")
            return

        products = ctx.env["product.product"].create(product_vals)
        _logger.info(f"Created {len(products)} products")



@ETL.pipeline(
    target_model="product.product",
    importer_name="qbo.item.linker",
    sap_source="Item",
    depends_on=["qbo.item.importer"],
)
class QboItemLinker(models.AbstractModel):
    """ETL Pipeline for linking existing products to QBO Items by default_code."""

    _name = "qbo.item.linker"
    _description = "QBO Item Linker"

    @ETL.extract("Item")
    def extract_items_for_linking(self, ctx: ETLContext) -> List[Dict]:
        """Extract items from QBO API that need linking."""
        api_client = get_api_client(ctx)

        # Fetch all items from QBO that have a SKU
        items = api_client.query_all(
            entity="Item", where="Active IN (true, false)", order_by="Id"
        )

        # Filter to items with SKU that don't have qbo_item_id set yet
        items_with_sku = [item for item in items if item.get("Sku")]

        _logger.info(f"Extracted {len(items_with_sku)} items with SKU for linking")
        return items_with_sku

    @ETL.transform()
    def transform_items_for_linking(
        self, ctx: ETLContext, extracted: Dict
    ) -> List[Dict]:
        """Find existing products by default_code and prepare link updates."""
        items = extracted.get("extract_items_for_linking", [])

        # Build lookup of existing products by default_code that don't have qbo_item_id
        ctx.env.cr.execute(
            """
            SELECT pp.id, pp.default_code, pp.product_tmpl_id
            FROM product_product pp
            WHERE pp.default_code IS NOT NULL AND pp.default_code != ''
            AND pp.qbo_item_id IS NULL
            """
        )
        product_by_code = {
            row[1]: {"id": row[0], "tmpl_id": row[2]}
            for row in ctx.env.cr.fetchall()
        }

        # Build account lookup (QBO ID -> Odoo ID)
        ctx.env.cr.execute(
            "SELECT qbo_id, id FROM account_account WHERE qbo_id IS NOT NULL"
        )
        account_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        link_updates = []
        for item in items:
            sku = item.get("Sku")
            if sku and sku in product_by_code:
                product_info = product_by_code[sku]
                update = {
                    "product_id": product_info["id"],
                    "product_tmpl_id": product_info["tmpl_id"],
                    "qbo_item_id": int(item.get("Id")),
                    "default_code": sku,
                }

                # Resolve income/expense accounts from QBO item refs
                income_ref = item.get("IncomeAccountRef", {})
                if income_ref:
                    qbo_income_id = int(income_ref.get("value", 0))
                    account_id = account_map.get(qbo_income_id)
                    if account_id:
                        update["property_account_income_id"] = account_id

                expense_ref = item.get("ExpenseAccountRef", {})
                if expense_ref:
                    qbo_expense_id = int(expense_ref.get("value", 0))
                    account_id = account_map.get(qbo_expense_id)
                    if account_id:
                        update["property_account_expense_id"] = account_id

                link_updates.append(update)

        _logger.info(f"Found {len(link_updates)} products to link by default_code")
        return link_updates

    @ETL.load()
    def load_item_links(self, ctx: ETLContext, transformed: Dict) -> None:
        """Update existing products with QBO item IDs and accounts."""
        link_updates = transformed.get("transform_items_for_linking", [])

        if not link_updates:
            _logger.info("No products to link")
            return

        ProductTemplate = ctx.env["product.template"]

        for update in link_updates:
            ctx.env.cr.execute(
                "UPDATE product_product SET qbo_item_id = %s WHERE id = %s",
                (update["qbo_item_id"], update["product_id"]),
            )

            # Update income/expense accounts via ORM (company_dependent fields)
            tmpl_vals = {}
            if update.get("property_account_income_id"):
                tmpl_vals["property_account_income_id"] = update[
                    "property_account_income_id"
                ]
            if update.get("property_account_expense_id"):
                tmpl_vals["property_account_expense_id"] = update[
                    "property_account_expense_id"
                ]
            if tmpl_vals:
                ProductTemplate.browse(update["product_tmpl_id"]).write(tmpl_vals)

            _logger.debug(
                f"Linked product {update['product_id']} (default_code={update['default_code']}) "
                f"to QBO item {update['qbo_item_id']}"
                + (f", set accounts: {tmpl_vals}" if tmpl_vals else "")
            )

        _logger.info(f"Linked {len(link_updates)} existing products to QBO items")


@ETL.pipeline(
    target_model="product.category",
    importer_name="qbo.category.account.fixer",
    sap_source="Item",
    depends_on=["qbo.item.linker"],
)
class QboCategoryAccountFixer(models.AbstractModel):
    """Set category default accounts from product data, or null them out.

    For each Odoo category, determines the best income, expense, and stock
    valuation accounts by examining the products it contains.  Categories
    with no usable product data get their account properties cleared so
    stale defaults don't cause posting errors.
    """

    _name = "qbo.category.account.fixer"
    _description = "QBO Category Account Fixer"

    @ETL.extract("CategoryAccountFix")
    def extract_lookups(self, ctx: ETLContext) -> ChunkableData:
        """Build all lookup tables needed by the transform."""
        company_key = str(ctx.env.company.id)

        # All Odoo categories
        ctx.env.cr.execute(
            "SELECT id, name FROM product_category"
        )
        categories = [{"id": r[0], "name": r[1]} for r in ctx.env.cr.fetchall()]

        # QBO account map (qbo_id -> odoo account id)
        ctx.env.cr.execute(
            "SELECT qbo_id, id FROM account_account WHERE qbo_id IS NOT NULL"
        )
        account_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        # Most common income account per category (from product templates)
        ctx.env.cr.execute(
            """
            SELECT pt.categ_id,
                   (pt.property_account_income_id->>%s)::int AS account_id,
                   COUNT(*) AS cnt
            FROM product_template pt
            JOIN product_product pp ON pp.product_tmpl_id = pt.id
            WHERE (pt.property_account_income_id->>%s)::int IS NOT NULL
            GROUP BY pt.categ_id, (pt.property_account_income_id->>%s)::int
            ORDER BY pt.categ_id, cnt DESC
            """,
            [company_key, company_key, company_key],
        )
        best_income = {}
        for row in ctx.env.cr.fetchall():
            best_income.setdefault(row[0], row[1])

        # Most common expense account per category
        ctx.env.cr.execute(
            """
            SELECT pt.categ_id,
                   (pt.property_account_expense_id->>%s)::int AS account_id,
                   COUNT(*) AS cnt
            FROM product_template pt
            JOIN product_product pp ON pp.product_tmpl_id = pt.id
            WHERE (pt.property_account_expense_id->>%s)::int IS NOT NULL
            GROUP BY pt.categ_id, (pt.property_account_expense_id->>%s)::int
            ORDER BY pt.categ_id, cnt DESC
            """,
            [company_key, company_key, company_key],
        )
        best_expense = {}
        for row in ctx.env.cr.fetchall():
            best_expense.setdefault(row[0], row[1])

        # Product → category mapping for QBO-linked products
        ctx.env.cr.execute(
            """
            SELECT pp.qbo_item_id, pt.categ_id
            FROM product_product pp
            JOIN product_template pt ON pp.product_tmpl_id = pt.id
            WHERE pp.qbo_item_id IS NOT NULL
            """
        )
        item_categ = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        _logger.info(
            f"Extracted {len(categories)} categories, "
            f"{len(account_map)} QBO accounts, "
            f"{len(item_categ)} QBO-linked products"
        )
        return ChunkableData(
            records=categories,
            context={
                "account_map": account_map,
                "best_income": best_income,
                "best_expense": best_expense,
                "item_categ": item_categ,
            },
        )

    @ETL.extract("Item")
    def extract_qbo_items(self, ctx: ETLContext) -> List[Dict]:
        """Fetch QBO inventory items for AssetAccountRef (stock valuation)."""
        api_client = get_api_client(ctx)
        items = api_client.query_all(
            entity="Item",
            where="Type = 'Inventory' AND Active IN (true, false)",
            order_by="Id",
        )
        _logger.info(f"Extracted {len(items)} inventory items for asset accounts")
        return items

    @ETL.transform()
    def transform_categories(
        self, ctx: ETLContext, extracted: Dict
    ) -> List[Dict]:
        """Build update dicts: category ID → accounts (or False to null)."""
        lookups = extracted.get("extract_lookups", ChunkableData(records=[]))
        qbo_items = extracted.get("extract_qbo_items", [])

        categories = lookups.records
        account_map = lookups.context.get("account_map", {})
        best_income = lookups.context.get("best_income", {})
        best_expense = lookups.context.get("best_expense", {})
        item_categ = lookups.context.get("item_categ", {})

        if not categories:
            return []

        # Stock valuation: most common AssetAccountRef per category
        asset_counts = {}  # categ_id -> Counter of odoo account_id
        for item in qbo_items:
            qbo_id = int(item.get("Id", 0))
            categ_id = item_categ.get(qbo_id)
            if not categ_id:
                continue
            asset_ref = item.get("AssetAccountRef", {})
            if asset_ref:
                odoo_account_id = account_map.get(int(asset_ref.get("value", 0)))
                if odoo_account_id:
                    asset_counts.setdefault(categ_id, Counter())[
                        odoo_account_id
                    ] += 1

        best_stock = {
            cid: counter.most_common(1)[0][0]
            for cid, counter in asset_counts.items()
        }

        updates = []
        for categ in categories:
            cid = categ["id"]
            vals = {
                "categ_id": cid,
                "categ_name": categ["name"],
                "property_account_income_categ_id": best_income.get(cid, False),
                "property_account_expense_categ_id": best_expense.get(cid, False),
                "property_stock_valuation_account_id": best_stock.get(cid, False),
            }
            updates.append(vals)

        set_count = sum(
            1 for u in updates
            if u["property_account_income_categ_id"]
            or u["property_account_expense_categ_id"]
            or u["property_stock_valuation_account_id"]
        )
        _logger.info(
            f"Prepared updates for {len(updates)} categories "
            f"({set_count} with accounts, {len(updates) - set_count} nulled)"
        )
        return updates

    @ETL.load()
    def load_category_accounts(self, ctx: ETLContext, transformed: Dict) -> None:
        """Write accounts onto categories via ORM."""
        updates = transformed.get("transform_categories", [])
        if not updates:
            _logger.info("No category accounts to update")
            return

        ProductCategory = ctx.env["product.category"]
        account_fields = {
            "property_account_income_categ_id",
            "property_account_expense_categ_id",
            "property_stock_valuation_account_id",
        }

        for upd in updates:
            categ = ProductCategory.browse(upd["categ_id"])
            vals = {k: v for k, v in upd.items() if k in account_fields}
            categ.write(vals)
            _logger.debug(
                f"Category '{upd['categ_name']}' (id={upd['categ_id']}): "
                f"set {vals}"
            )

        _logger.info(
            f"Updated accounts on {len(updates)} categories"
        )

        # Update company-level stock valuation account from the most common
        # QBO-derived valuation account across all categories.
        best_valuation = None
        valuation_counts = Counter()
        for upd in updates:
            acct = upd.get("property_stock_valuation_account_id")
            if acct:
                valuation_counts[acct] += 1
        if valuation_counts:
            best_valuation = valuation_counts.most_common(1)[0][0]
            company = ctx.env.company
            if company.account_stock_valuation_id.id != best_valuation:
                company.account_stock_valuation_id = best_valuation
                _logger.info(
                    f"Updated company stock valuation account to {best_valuation}"
                )


@ETL.pipeline(
    target_model="product.product",
    importer_name="qbo.product.category.fixer",
    sap_source="Item",
    depends_on=["qbo.item.importer"],
)
class QboProductCategoryFixer(models.AbstractModel):
    """Assign xTuple-derived categories to QBO-only products that have none.

    Runs after qbo.item.importer.  For each QBO product that has no category
    (typically because QBO has no Category-type items), infers the best
    product category by matching the product's income account against the
    categories used by xTuple products with the same account.
    """

    _name = "qbo.product.category.fixer"
    _description = "QBO Product Category Fixer"

    @ETL.extract("UncategorisedQboProducts")
    def extract_uncategorised(self, ctx: ETLContext) -> Dict:
        """Find QBO-only products with no category and build income→category map."""
        ctx.env.cr.execute(
            """
            SELECT pp.id, (pt.property_account_income_id->>%s)::int AS income_account_id
            FROM product_product pp
            JOIN product_template pt ON pp.product_tmpl_id = pt.id
            WHERE pp.qbo_item_id IS NOT NULL AND pp.qbo_item_id != 0
              AND (pp.xtuple_item_id IS NULL OR pp.xtuple_item_id = 0)
              AND pt.categ_id IS NULL
            """,
            [str(ctx.env.company.id)],
        )
        products = [
            {"id": row[0], "income_account_id": row[1]}
            for row in ctx.env.cr.fetchall()
        ]
        _logger.info(f"Found {len(products)} QBO-only products with no category")

        income_to_categ = _build_income_to_categ(ctx)
        _logger.info(
            f"Built income→category map with {len(income_to_categ)} entries"
        )

        return {"products": products, "income_to_categ": income_to_categ}

    @ETL.transform()
    def transform_uncategorised(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Map each product to the best available category."""
        data = extracted.get("extract_uncategorised", {})
        products = data.get("products", [])
        income_to_categ = data.get("income_to_categ", {})

        updates = []
        skipped = 0
        for product in products:
            categ_id = income_to_categ.get(product["income_account_id"])
            if categ_id:
                updates.append({"id": product["id"], "categ_id": categ_id})
            else:
                skipped += 1

        if skipped:
            _logger.warning(
                f"{skipped} QBO-only products have no income account match — "
                f"they will remain uncategorised"
            )
        _logger.info(f"Prepared category assignment for {len(updates)} products")
        return updates

    @ETL.load()
    def load_category_fixes(self, ctx: ETLContext, transformed: Dict) -> None:
        """Write categ_id onto the matching product templates."""
        updates = transformed.get("transform_uncategorised", [])
        if not updates:
            _logger.info("No QBO product categories to fix")
            return

        for upd in updates:
            ctx.env.cr.execute(
                """
                UPDATE product_template pt
                SET categ_id = %s
                FROM product_product pp
                WHERE pp.product_tmpl_id = pt.id
                  AND pp.id = %s
                """,
                (upd["categ_id"], upd["id"]),
            )

        _logger.info(f"Fixed categories for {len(updates)} QBO-only products")
