"""Product Supplier Info import pipeline.

Imports vendor-item relationships from SAP ITM2 (Multiple Preferred Vendors)
into Odoo product.supplierinfo records. Uses OITM.lastpurprc as the vendor
price when available.
"""

import logging
from typing import Dict, List

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)

# SAP currency code → Odoo currency name
_SAP_CURRENCY_MAP = {
    "$": "USD",
    "CAN": "CAD",
    "EUR": "EUR",
}


@ETL.pipeline(
    target_model="product.supplierinfo",
    importer_name="product.supplierinfo.importer",
    sap_source="itm2,oitm",
    depends_on=["product.product.importer", "res.partner.company.importer"],
    allow_multiprocessing=False,
)
class ProductSupplierinfoImporter(models.AbstractModel):
    _name = "product.supplierinfo.importer"
    _description = "SAP Product Supplier Info Importer (ITM2/OITM)"

    _lookup_cache = {}

    @ETL.extract("itm2,oitm")
    def extract_supplierinfo(self, ctx: ETLContext) -> List[Dict]:
        """Extract vendor-item links from ITM2 joined with OITM pricing.

        Returns:
            List of dicts with itemcode, vendorcode, lastpurprc, lastpurcur.
        """
        ctx.cr.execute(
            """
            SELECT
                itm2.itemcode,
                itm2.vendorcode,
                oitm.lastpurprc,
                oitm.lastpurcur
            FROM itm2
            JOIN oitm ON itm2.itemcode = oitm.itemcode
            """
        )
        rows = ctx.cr.dictfetchall()
        _logger.info(f"Extracted {len(rows)} vendor-item links from ITM2.")

        # Pre-compute lookups
        products = ctx.env["product.product"].search(
            [("sap_item_code", "!=", False), ("active", "in", [True, False])]
        )
        product_tmpl_map = {
            p.sap_item_code: p.product_tmpl_id.id for p in products
        }

        partners = ctx.env["res.partner"].search(
            [("sap_card_code", "!=", False), ("active", "in", [True, False])]
        )
        partner_map = {p.sap_card_code: p.id for p in partners}

        currencies = ctx.env["res.currency"].search([])
        currency_map = {c.name: c.id for c in currencies}

        ProductSupplierinfoImporter._lookup_cache = {
            "product_tmpl_map": product_tmpl_map,
            "partner_map": partner_map,
            "currency_map": currency_map,
            "company_id": ctx.env.company.id,
            "company_currency_id": ctx.env.company.currency_id.id,
        }

        return rows

    @ETL.transform()
    def transform_supplierinfo(
        self, ctx: ETLContext, extracted: Dict
    ) -> List[Dict]:
        """Transform ITM2 rows into product.supplierinfo values.

        Uses OITM.lastpurprc as the vendor price. Rows without a price
        are skipped (subclasses may override to provide a fallback).
        """
        rows = extracted["extract_supplierinfo"]
        cache = ProductSupplierinfoImporter._lookup_cache
        product_tmpl_map = cache["product_tmpl_map"]
        partner_map = cache["partner_map"]
        currency_map = cache["currency_map"]
        company_id = cache["company_id"]
        company_currency_id = cache["company_currency_id"]

        supplierinfo_vals = []
        skipped_no_product = 0
        skipped_no_partner = 0
        skipped_no_price = 0

        for row in rows:
            product_tmpl_id = product_tmpl_map.get(row["itemcode"])
            if not product_tmpl_id:
                skipped_no_product += 1
                continue

            partner_id = partner_map.get(row["vendorcode"])
            if not partner_id:
                skipped_no_partner += 1
                continue

            price = self._get_vendor_price(row)
            if not price:
                skipped_no_price += 1
                continue

            # Resolve currency
            sap_curr = row.get("lastpurcur") or ""
            odoo_curr = _SAP_CURRENCY_MAP.get(sap_curr)
            currency_id = currency_map.get(odoo_curr) if odoo_curr else None
            if not currency_id:
                currency_id = company_currency_id

            supplierinfo_vals.append(
                {
                    "partner_id": partner_id,
                    "product_tmpl_id": product_tmpl_id,
                    "price": price,
                    "currency_id": currency_id,
                    "company_id": company_id,
                }
            )

        _logger.info(
            f"Transformed {len(supplierinfo_vals)} supplierinfo records "
            f"(skipped: {skipped_no_product} no product, "
            f"{skipped_no_partner} no partner, "
            f"{skipped_no_price} no price)."
        )
        return supplierinfo_vals

    def _get_vendor_price(self, row: Dict) -> float:
        """Return the vendor price for a given ITM2+OITM row.

        Base implementation uses OITM.lastpurprc directly.
        Override in subclass to provide fallback logic.

        Args:
            row: Dict with at least 'lastpurprc'.

        Returns:
            Vendor price as float, or 0.0 if unavailable.
        """
        return float(row.get("lastpurprc") or 0.0)

    @ETL.load()
    def load_supplierinfo(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load supplier info records into Odoo."""
        vals_list = transformed["transform_supplierinfo"]

        if vals_list:
            ctx.env["product.supplierinfo"].create(vals_list)
            _logger.info(f"Created {len(vals_list)} product.supplierinfo records.")
        else:
            _logger.info("No supplier info records to create.")
