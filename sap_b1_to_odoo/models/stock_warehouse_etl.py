import logging
from typing import Dict, List

from odoo import fields, models

from odoo.addons.sap_b1_to_odoo.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)


class StockWarehouse(models.Model):
    _inherit = "stock.warehouse"

    sap_whs_code = fields.Char(
        string="SAP Warehouse Code",
        index=True,
        help="Original warehouse code from SAP B1 (OWHS.WhsCode)",
    )


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

        # Get existing warehouses by SAP code and name
        existing_warehouses = ctx.env["stock.warehouse"].search(
            [("company_id", "=", company_id)]
        )
        existing_sap_codes = {
            wh.sap_whs_code for wh in existing_warehouses if wh.sap_whs_code
        }
        # Map name to warehouse for updating existing ones missing sap_whs_code
        name_to_warehouse = {wh.name: wh for wh in existing_warehouses}

        sql = """
            SELECT whscode, whsname
            FROM owhs
            WHERE whscode IS NOT NULL
        """
        ctx.cr.execute(sql)
        sap_warehouses = ctx.cr.dictfetchall()

        # Separate into new warehouses and existing ones needing sap_whs_code update
        new_warehouses = []
        warehouses_to_update = []
        for wh in sap_warehouses:
            sap_code = (wh.get("whscode") or "").strip()
            name = (wh.get("whsname") or sap_code).strip()
            if not sap_code:
                continue
            if sap_code in existing_sap_codes:
                # Already has SAP code set
                continue
            if name in name_to_warehouse:
                # Exists by name but missing sap_whs_code - update it
                warehouses_to_update.append(
                    {
                        "warehouse": name_to_warehouse[name],
                        "sap_whs_code": sap_code,
                    }
                )
            else:
                # New warehouse
                new_warehouses.append(wh)

        _logger.info(
            f"Extracted {len(new_warehouses)} new warehouses from SAP OWHS "
            f"(filtered from {len(sap_warehouses)} total). "
            f"{len(warehouses_to_update)} existing warehouses need sap_whs_code update."
        )
        return {
            "warehouses": new_warehouses,
            "warehouses_to_update": warehouses_to_update,
            "company_id": company_id,
        }

    @ETL.transform()
    def transform_warehouses(self, ctx: ETLContext, extracted: Dict) -> Dict:
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
            sap_code = (sap_wh.get("whscode") or "").strip()
            name = (sap_wh.get("whsname") or sap_code).strip()
            # Odoo warehouse code is max 5 chars, so truncate
            odoo_code = sap_code[:5]

            vals = {
                "name": name,
                "code": odoo_code,
                "sap_whs_code": sap_code,
                "company_id": company_id,
            }
            warehouse_vals.append(vals)

        _logger.info(f"Transformed {len(warehouse_vals)} warehouse records.")
        return {
            "warehouse_vals": warehouse_vals,
            "warehouses_to_update": data.get("warehouses_to_update", []),
        }

    @ETL.load()
    def load_warehouses(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load warehouses into Odoo.

        Args:
            ctx: ETL context.
            transformed: Dictionary containing transformed data.
        """
        # Get data from transform phase
        transform_data = transformed.get("transform_warehouses") or {}
        warehouses_to_update = transform_data.get("warehouses_to_update") or []
        warehouse_vals = transform_data.get("warehouse_vals") or []

        # Update existing warehouses with missing sap_whs_code
        for update_info in warehouses_to_update:
            warehouse = update_info["warehouse"]
            sap_code = update_info["sap_whs_code"]
            warehouse.write({"sap_whs_code": sap_code})
            _logger.info(
                f"Updated warehouse {warehouse.name} with sap_whs_code={sap_code}"
            )

        if warehouses_to_update:
            _logger.info(
                f"Updated {len(warehouses_to_update)} existing warehouses with sap_whs_code"
            )

        # Create new warehouses

        if not warehouse_vals:
            _logger.info("No new warehouses to create.")
            return

        warehouses = ctx.env["stock.warehouse"].create(warehouse_vals)
        _logger.info(
            f"Created {len(warehouses)} stock.warehouse records: "
            f"{', '.join(warehouses.mapped('code'))}"
        )
