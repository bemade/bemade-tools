import logging
from typing import Any, Dict, List

from odoo import models
from odoo.sql_db import SQL

from odoo.addons.sap_b1_to_odoo.etl_framework import ETL, ETLContext
from odoo.addons.sap_b1_to_odoo.tools import fix_quotes

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="product.category",
    importer_name="product.category.importer",
    sap_source="oitb",
    depends_on=["account.account.importer"],
)
class ProductCategoryImporter(models.AbstractModel):
    _name = "product.category.importer"
    _description = "SAP Product Category Importer (OITB)"

    @ETL.extract("oitb")
    def extract_categories(self, ctx: ETLContext) -> Dict[str, Any]:
        """Extract product categories from SAP OITB table.

        Args:
            ctx: ETL context with SAP cursor and Odoo environment.

        Returns:
            List of category dictionaries from SAP.
        """
        # Get existing categories to avoid duplicates
        ctx.env.cr.execute(
            "SELECT sap_itms_grp_cod FROM product_category WHERE sap_itms_grp_cod IS NOT NULL"
        )
        existing_codes = tuple(row[0] for row in ctx.env.cr.fetchall())

        # Get account mappings from OACT to join with category accounts
        ctx.cr.execute("SELECT AcctCode, FormatCode FROM oact")
        account_mappings = {row[0]: row[1] for row in ctx.cr.fetchall()}

        # Pre-load Odoo accounts by SAP format code for efficient lookup
        ctx.env.cr.execute(
            """
            SELECT id, sap_acct_code 
            FROM account_account 
            WHERE sap_acct_code IS NOT NULL
        """
        )
        odoo_accounts = {row[1]: row[0] for row in ctx.env.cr.fetchall()}

        # Query SAP - filter out empty names, include account fields
        sql = """
            SELECT itmsgrpcod, itmsgrpnam, 
                   balinvntac, salecostac, transferac, revenuesac, 
                   varianceac, decresglac, incresglac, shpdgdsact
            FROM oitb 
            WHERE itmsgrpnam <> '' AND itmsgrpnam IS NOT NULL
        """
        if existing_codes:
            sql += " AND itmsgrpcod NOT IN %s"
            ctx.cr.execute(SQL(sql, existing_codes))
        else:
            ctx.cr.execute(sql)

        sap_categories = ctx.cr.dictfetchall()

        # Add account format codes to each category
        for category in sap_categories:
            for account_field in [
                "balinvntac",
                "salecostac",
                "transferac",
                "revenuesac",
                "varianceac",
                "decresglac",
                "incresglac",
                "shpdgdsact",
            ]:
                acct_code = category.get(account_field)
                category[f"{account_field}_formatcode"] = account_mappings.get(
                    acct_code, False
                )

        _logger.info(f"Extracted {len(sap_categories)} categories from SAP OITB.")
        return {
            "categories": sap_categories,
            "odoo_accounts": odoo_accounts,
        }

    @ETL.transform()
    def transform_categories(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform SAP categories into Odoo category values.

        Args:
            ctx: ETL context.
            extracted: Dictionary containing extracted data.

        Returns:
            List of category value dictionaries ready for creation.
        """
        data = extracted.get("extract_categories") or {}
        sap_categories = data.get("categories", [])
        odoo_accounts = data.get("odoo_accounts", {})

        category_vals = []
        for sap_cat in sap_categories:
            vals = {
                "sap_itms_grp_cod": sap_cat["itmsgrpcod"],
                "name": fix_quotes(sap_cat["itmsgrpnam"]),
                "property_cost_method": "fifo",
                "property_valuation": "real_time",
            }

            # Map SAP accounts to Odoo accounts using format codes
            # Inventory Account (balinvntac) -> property_stock_valuation_account_id
            if sap_cat.get("balinvntac_formatcode"):
                inventory_account_id = odoo_accounts.get(
                    sap_cat["balinvntac_formatcode"]
                )
                if inventory_account_id:
                    vals["property_stock_valuation_account_id"] = inventory_account_id

            # COGS Account (salecostac) -> property_account_expense_categ_id
            if sap_cat.get("salecostac_formatcode"):
                expense_account_id = odoo_accounts.get(sap_cat["salecostac_formatcode"])
                if expense_account_id:
                    vals["property_account_expense_categ_id"] = expense_account_id

            # Revenue Account (revenuesac) -> property_account_income_categ_id
            if sap_cat.get("revenuesac_formatcode"):
                income_account_id = odoo_accounts.get(sap_cat["revenuesac_formatcode"])
                if income_account_id:
                    vals["property_account_income_categ_id"] = income_account_id

            category_vals.append(vals)

        _logger.info(
            f"Transformed {len(category_vals)} category records with SAP account mappings."
        )
        return category_vals

    @ETL.load()
    def load_categories(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load categories into Odoo.

        Args:
            ctx: ETL context.
            transformed: Dictionary containing transformed data.
        """
        category_vals = transformed["transform_categories"]

        if category_vals:
            categories = ctx.env["product.category"].create(category_vals)
            _logger.info(f"Created {len(categories)} product categories.")
        else:
            _logger.info("No new categories to create.")
