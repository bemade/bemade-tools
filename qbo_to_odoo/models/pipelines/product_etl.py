"""QuickBooks Online Product/Item ETL Pipeline

This module handles the migration of Items (Products/Services) from QBO to Odoo
using the ETL framework.
"""

import logging
from typing import Any, Dict, List, Optional

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext
from odoo.addons.qbo_to_odoo.models.pipelines.utils import get_api_client

_logger = logging.getLogger(__name__)


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

        # Get existing default_codes for deduplication (cross-system matching)
        ctx.env.cr.execute(
            "SELECT default_code FROM product_product WHERE default_code IS NOT NULL AND default_code != ''"
        )
        existing_default_codes = {row[0] for row in ctx.env.cr.fetchall()}
        _logger.info(
            f"Found {len(existing_default_codes)} existing products by default_code for deduplication"
        )

        # Fetch all items from QBO
        items = api_client.query_all(
            entity="Item", where="Active IN (true, false)", order_by="Id"
        )

        # Filter out already imported by QBO ID
        new_items = [item for item in items if str(item.get("Id")) not in existing_ids]

        # Filter out items that match existing products by SKU (deduplication)
        deduped_items = [
            item
            for item in new_items
            if not item.get("Sku") or item.get("Sku") not in existing_default_codes
        ]

        _logger.info(
            f"Extracted {len(items)} items from QBO, {len(new_items)} new by ID, "
            f"{len(deduped_items)} after deduplication by SKU"
        )
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

            # Get category from ParentRef
            categ_id = None
            parent_ref = item.get("ParentRef", {})
            if parent_ref:
                qbo_categ_id = parent_ref.get("value")
                categ_id = category_map.get(qbo_categ_id)

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

        # Update last sync timestamp
        connection = ctx.env["qbo.connection"].browse(ctx.get_config("source_id"))
        if connection:
            connection.last_product_sync = ctx.env.cr.now()


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
            SELECT id, default_code FROM product_product
            WHERE default_code IS NOT NULL AND default_code != ''
            AND qbo_item_id IS NULL
            """
        )
        product_by_code = {row[1]: row[0] for row in ctx.env.cr.fetchall()}

        link_updates = []
        for item in items:
            sku = item.get("Sku")
            if sku and sku in product_by_code:
                link_updates.append(
                    {
                        "product_id": product_by_code[sku],
                        "qbo_item_id": int(item.get("Id")),
                        "default_code": sku,
                    }
                )

        _logger.info(f"Found {len(link_updates)} products to link by default_code")
        return link_updates

    @ETL.load()
    def load_item_links(self, ctx: ETLContext, transformed: Dict) -> None:
        """Update existing products with QBO item IDs."""
        link_updates = transformed.get("transform_items_for_linking", [])

        if not link_updates:
            _logger.info("No products to link")
            return

        for update in link_updates:
            ctx.env.cr.execute(
                "UPDATE product_product SET qbo_item_id = %s WHERE id = %s",
                (update["qbo_item_id"], update["product_id"]),
            )
            _logger.debug(
                f"Linked product {update['product_id']} (default_code={update['default_code']}) "
                f"to QBO item {update['qbo_item_id']}"
            )

        _logger.info(f"Linked {len(link_updates)} existing products to QBO items")
