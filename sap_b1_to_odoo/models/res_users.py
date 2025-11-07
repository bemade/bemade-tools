import logging
from typing import Dict, List

from odoo import api, fields, models
from odoo.tools.sql import SQL

from odoo.addons.sap_b1_to_odoo.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)


class Users(models.Model):
    _inherit = "res.users"

    sap_slpcode = fields.Integer(
        string="SAP SLP Code",
        copy=False,
    )


@ETL.pipeline(
    target_model="res.users",
    importer_name="res.users.importer",
    sap_source="oslp",
    depends_on=[],
    allow_multiprocessing=False,  # Small dataset, always single-process
)
class ResUsersImporter(models.AbstractModel):
    _description = "SAP Users/Salespeople Importer"

    @ETL.extract("oslp")
    def extract_salespeople(self, ctx: ETLContext) -> List[Dict]:
        """Extract salespeople from SAP OSLP table.

        Args:
            ctx: ETL context with SAP cursor and Odoo environment.

        Returns:
            List of salesperson dictionaries from SAP.
        """
        # Get existing users to avoid duplicates
        existing_users = ctx.env["res.users"].search([("active", "in", [True, False])])
        _logger.info(f"Found {len(existing_users)} existing users.")
        existing_names = tuple(user.name for user in existing_users)

        # Build query
        sql = "SELECT * FROM oslp"
        if existing_names:
            sql += " WHERE slpname NOT IN %s"
            sql = SQL(sql, existing_names)
        else:
            sql = SQL(sql)

        # Execute extraction
        ctx.cr.execute(sql)
        salespeople = ctx.cr.dictfetchall()
        _logger.info(f"Extracted {len(salespeople)} salespeople from SAP.")

        return salespeople

    @ETL.transform()
    def transform_salespeople(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform SAP salespeople into Odoo user values.

        Args:
            ctx: ETL context.
            extracted: Dictionary containing extracted data.

        Returns:
            List of user value dictionaries ready for creation.
        """
        salespeople = extracted["extract_salespeople"]
        company = ctx.env.company

        user_vals = []
        for salesperson in salespeople:
            name = salesperson["slpname"]
            slp_code = salesperson["slpcode"]
            login = "_".join(name.split()).lower()

            user_vals.append(
                {
                    "name": name,
                    "login": login,
                    "company_id": company.id,
                    "sap_slpcode": slp_code,
                    "active": False,  # Inactive by default
                }
            )

        return user_vals

    @ETL.load()
    def load_salespeople(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load users into Odoo and deactivate their partner records.

        Args:
            ctx: ETL context.
            transformed: Dictionary containing transformed data.
        """
        user_vals = transformed["transform_salespeople"]

        # Create users
        users = ctx.env["res.users"].create(user_vals)
        _logger.info(f"Created {len(users)} users.")

        # Deactivate associated partner records
        partners = ctx.env["res.partner"].search([("user_ids", "in", users.ids)])
        partners.write({"active": False})
        _logger.info(f"Deactivated {len(partners)} associated partner records.")
