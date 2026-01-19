"""QuickBooks Online Product Category ETL Pipeline

This module handles the migration of Item Categories from QBO to Odoo
using the ETL framework.
"""

import logging
from typing import Dict, List

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="product.category",
    importer_name="qbo.category.importer",
    sap_source="Item",
    depends_on=["qbo.account.importer"],
)
class QboCategoryImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO Item Categories."""

    _name = "qbo.category.importer"
    _description = "QBO Category Importer"

    @ETL.extract("Category")
    def extract_categories(self, ctx: ETLContext) -> List[Dict]:
        """Extract categories from QBO API."""
        api_client = ctx.get_config("api_client")
        if not api_client:
            raise ValueError("API client not found in ETL context")

        # Get existing QBO category IDs
        ctx.env.cr.execute(
            "SELECT qbo_category_id FROM product_category WHERE qbo_category_id IS NOT NULL"
        )
        existing_ids = {str(row[0]) for row in ctx.env.cr.fetchall()}
        _logger.info(f"Found {len(existing_ids)} existing categories in Odoo")

        # Fetch all items from QBO and filter for Category type
        items = api_client.query_all(
            entity="Item", where="Type = 'Category'", order_by="Id"
        )

        # Filter out already imported
        new_categories = [
            item for item in items if str(item.get("Id")) not in existing_ids
        ]

        _logger.info(
            f"Extracted {len(items)} categories from QBO, {len(new_categories)} are new"
        )
        return new_categories

    @ETL.transform()
    def transform_categories(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO categories into Odoo product category values."""
        categories = extracted.get("extract_categories", [])

        # Build account lookup
        ctx.env.cr.execute(
            "SELECT qbo_id, id FROM account_account WHERE qbo_id IS NOT NULL"
        )
        account_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        # Build existing category lookup for parent references
        ctx.env.cr.execute(
            "SELECT qbo_category_id, id FROM product_category WHERE qbo_category_id IS NOT NULL"
        )
        category_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        category_vals = []

        for category in categories:
            # Get income/expense accounts
            income_account_id = None
            expense_account_id = None

            income_ref = category.get("IncomeAccountRef", {})
            if income_ref:
                qbo_income_id = int(income_ref.get("value", 0))
                income_account_id = account_map.get(qbo_income_id)

            expense_ref = category.get("ExpenseAccountRef", {})
            if expense_ref:
                qbo_expense_id = int(expense_ref.get("value", 0))
                expense_account_id = account_map.get(qbo_expense_id)

            # Get parent category
            parent_id = None
            parent_ref = category.get("ParentRef", {})
            if parent_ref:
                qbo_parent_id = parent_ref.get("value")
                parent_id = category_map.get(qbo_parent_id)

            vals = {
                "name": category.get("Name", ""),
                "qbo_category_id": str(category.get("Id")),
            }

            if income_account_id:
                vals["property_account_income_categ_id"] = income_account_id
            if expense_account_id:
                vals["property_account_expense_categ_id"] = expense_account_id
            if parent_id:
                vals["parent_id"] = parent_id

            category_vals.append(vals)

        _logger.info(f"Transformed {len(category_vals)} category records")
        return category_vals

    @ETL.load()
    def load_categories(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load categories into Odoo."""
        category_vals = transformed.get("transform_categories", [])

        if not category_vals:
            _logger.info("No new categories to create")
            return

        categories = ctx.env["product.category"].create(category_vals)
        _logger.info(f"Created {len(categories)} product categories")
