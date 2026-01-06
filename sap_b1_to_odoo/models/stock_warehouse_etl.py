import logging
from typing import Dict, List

from odoo import models

from odoo.addons.sap_b1_to_odoo.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="stock.warehouse",
    importer_name="stock.warehouse.importer",
    sap_source="owhs",
    depends_on=["res.company.importer"],
    allow_multiprocessing=False,
)
class StockWarehouseImporter(models.AbstractModel):
    _name = "stock.warehouse.importer"
    _description = "SAP Stock Warehouse Importer (OWHS)"

    @ETL.extract("owhs")
    def extract_warehouses(self, ctx: ETLContext) -> Dict:
        """Extract warehouses from SAP OWHS table.

        Args:
            ctx: ETL context with SAP cursor and Odoo environment.

        Returns:
            Dict containing SAP warehouses filtered by existing.
        """
        company_id = ctx.env.company.id

        # Get existing warehouses by code and name
        existing_warehouses = ctx.env["stock.warehouse"].search(
            [("company_id", "=", company_id)]
        )
        existing_codes = {wh.code for wh in existing_warehouses}
        existing_names = {wh.name for wh in existing_warehouses}

        sql = """
            SELECT whscode, whsname
            FROM owhs
            WHERE whscode IS NOT NULL
        """
        ctx.cr.execute(sql)
        sap_warehouses = ctx.cr.dictfetchall()

        # Filter out existing warehouses (by code or name - unique constraint is on name)
        new_warehouses = []
        for wh in sap_warehouses:
            code = (wh.get("whscode") or "").strip()
            name = (wh.get("whsname") or code).strip()
            if code and code not in existing_codes and name not in existing_names:
                new_warehouses.append(wh)

        _logger.info(
            f"Extracted {len(new_warehouses)} new warehouses from SAP OWHS "
            f"(filtered from {len(sap_warehouses)} total)."
        )
        return {
            "warehouses": new_warehouses,
            "company_id": company_id,
        }

    @ETL.transform()
    def transform_warehouses(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform SAP warehouses into Odoo stock.warehouse values.

        Args:
            ctx: ETL context.
            extracted: Dictionary containing extracted data.

        Returns:
            List of warehouse value dictionaries ready for creation.
        """
        data = extracted.get("extract_warehouses") or {}
        sap_warehouses = data.get("warehouses", [])
        company_id = data.get("company_id")

        warehouse_vals = []
        for sap_wh in sap_warehouses:
            code = (sap_wh.get("whscode") or "").strip()
            name = (sap_wh.get("whsname") or code).strip()

            vals = {
                "name": name,
                "code": code,
                "company_id": company_id,
            }
            warehouse_vals.append(vals)

        _logger.info(f"Transformed {len(warehouse_vals)} warehouse records.")
        return warehouse_vals

    @ETL.load()
    def load_warehouses(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load warehouses into Odoo.

        Args:
            ctx: ETL context.
            transformed: Dictionary containing transformed data.
        """
        warehouse_vals = transformed.get("transform_warehouses") or []

        if not warehouse_vals:
            _logger.info("No new warehouses to create.")
            return

        warehouses = ctx.env["stock.warehouse"].create(warehouse_vals)
        _logger.info(
            f"Created {len(warehouses)} stock.warehouse records: "
            f"{', '.join(warehouses.mapped('code'))}"
        )
