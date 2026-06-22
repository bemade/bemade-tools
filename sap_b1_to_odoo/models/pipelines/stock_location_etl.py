#
#    Bemade Inc.
#
#    Copyright (C) 2024-June Bemade Inc. (<https://www.bemade.org>).
#    Author: Marc Durepos (Contact : marc@bemade.org)
#
#    This program is under the terms of the GNU Lesser General Public License,
#    version 3.
#
#    For full license details, see https://www.gnu.org/licenses/lgpl-3.0.en.html.
#
import logging
from typing import Dict, List

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="stock.location",
    importer_name="stock.location.importer",
    sap_source="oitm",
    depends_on=["stock.warehouse.importer"],
    allow_multiprocessing=False,
)
class StockLocationImporter(models.AbstractModel):
    _name = "stock.location.importer"
    _description = "SAP Stock Location Importer (OITM.sww)"

    @ETL.extract("oitm")
    def extract_stock_locations(self, ctx: ETLContext) -> Dict:
        """Extract distinct non-empty sww codes from SAP OITM.

        Args:
            ctx: ETL context with SAP cursor and Odoo environment.

        Returns:
            Dict containing distinct sww codes and the warehouse lot_stock_id mapping.
        """
        sql = """
            SELECT DISTINCT sww
            FROM oitm
            WHERE sww IS NOT NULL
              AND sww <> ''
        """
        ctx.cr.execute(sql)
        rows = ctx.cr.dictfetchall()
        sww_codes = [r["sww"].strip() for r in rows if (r.get("sww") or "").strip()]

        # Build warehouse → lot_stock_id map (keyed by sap_whs_code)
        warehouses = ctx.env["stock.warehouse"].search([("active", "=", True)])
        warehouse_lot_stock_map = {
            wh.sap_whs_code: wh.lot_stock_id.id
            for wh in warehouses
            if wh.sap_whs_code and wh.lot_stock_id
        }

        _logger.info(
            "Extracted %d distinct non-empty sww codes from SAP OITM.", len(sww_codes)
        )
        return {
            "sww_codes": sww_codes,
            "warehouse_lot_stock_map": warehouse_lot_stock_map,
        }

    @ETL.transform()
    def transform_stock_locations(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform sww codes into stock.location value dicts.

        Each distinct sww code becomes one internal location.  The parent
        location is the warehouse's lot_stock_id.  If the warehouse map has
        only one entry (single-warehouse setup), that entry is used for all
        locations.  If the map is empty, a warning is logged and no locations
        are produced.

        Args:
            ctx: ETL context.
            extracted: Dict from extract_stock_locations.

        Returns:
            List of stock.location value dicts ready for upsert.
        """
        data = extracted.get("extract_stock_locations") or {}
        sww_codes = data.get("sww_codes", [])
        warehouse_lot_stock_map = data.get("warehouse_lot_stock_map", {})

        if not warehouse_lot_stock_map:
            _logger.warning(
                "stock_location transform: no warehouses with sap_whs_code found — "
                "cannot determine parent lot_stock_id; skipping all sww locations."
            )
            return []

        # For a single-warehouse installation, use that warehouse's lot_stock_id
        # as the parent for every sww location.
        if len(warehouse_lot_stock_map) == 1:
            default_lot_stock_id = next(iter(warehouse_lot_stock_map.values()))
        else:
            # Multi-warehouse: use the first entry as default (caller can override
            # via a client overlay if needed).
            default_lot_stock_id = next(iter(warehouse_lot_stock_map.values()))
            _logger.info(
                "stock_location transform: multiple warehouses found; using first "
                "warehouse lot_stock_id (%d) as parent for all sww locations.",
                default_lot_stock_id,
            )

        location_vals = []
        for sww in sww_codes:
            vals = {
                "name": sww,
                "usage": "internal",
                "location_id": default_lot_stock_id,
                "sap_sww_code": sww,
                "active": True,
            }
            location_vals.append(vals)

        _logger.info(
            "Transformed %d stock.location records for sww codes.", len(location_vals)
        )
        return location_vals

    @ETL.load()
    def load_stock_locations(self, ctx: ETLContext, transformed: Dict) -> None:
        """Upsert stock.location records keyed on sap_sww_code.

        Existing locations are updated; new sww codes create new locations.
        Idempotent: re-running with the same data produces no duplicates.

        Args:
            ctx: ETL context.
            transformed: Dict from transform_stock_locations.
        """
        location_vals = transformed.get("transform_stock_locations") or []

        if not location_vals:
            _logger.info("No sww stock locations to upsert.")
            return

        # Build lookup of existing locations keyed by sap_sww_code
        existing = {
            loc["sap_sww_code"]: loc["id"]
            for loc in ctx.env["stock.location"].with_context(active_test=False).search_read(
                [("sap_sww_code", "!=", False)],
                ["sap_sww_code"],
            )
        }

        to_create = []
        to_write = []  # list of (id, vals)
        for vals in location_vals:
            sww = vals["sap_sww_code"]
            if sww in existing:
                to_write.append((existing[sww], vals))
            else:
                to_create.append(vals)

        if to_create:
            locations = ctx.env["stock.location"].create(to_create)
            _logger.info("Created %d stock.location records for sww codes.", len(locations))

        if to_write:
            for loc_id, vals in to_write:
                ctx.env["stock.location"].browse(loc_id).write(vals)
            _logger.info("Updated %d existing stock.location records.", len(to_write))
