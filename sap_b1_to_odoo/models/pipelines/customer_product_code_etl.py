import logging
from typing import Dict, List

from odoo import models

from odoo.addons.sap_b1_to_odoo.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="product.customer.code",
    importer_name="customer.product.code.importer",
    sap_source="oscn",
    depends_on=["product.product.importer", "res.partner.postprocess.importer"],
)
class CustomerProductCodeImporter(models.AbstractModel):
    _name = "customer.product.code.importer"
    _description = "SAP Customer Product Code Importer (OSCN)"

    @ETL.extract("oscn")
    def extract_customer_codes(self, ctx: ETLContext) -> Dict:
        """Extract customer product codes from SAP OSCN table.

        Args:
            ctx: ETL context with SAP cursor and Odoo environment.

        Returns:
            Dict containing SAP codes and lookup mappings.
        """
        # Query SAP for customer product codes with substitutes
        ctx.cr.execute(
            "SELECT * FROM OSCN WHERE substitute IS NOT NULL AND substitute <> ''"
        )
        sap_codes = ctx.cr.dictfetchall()

        # Build product lookup: sap_item_code -> product_tmpl_id
        item_codes = list({code["itemcode"] for code in sap_codes if code["itemcode"]})
        products_map = {}
        if item_codes:
            products = ctx.env["product.template"].search_read(
                [("sap_item_code", "in", item_codes)],
                ["id", "sap_item_code"],
            )
            products_map = {p["sap_item_code"]: p["id"] for p in products}

        # Build partner lookup: sap_card_code -> partner_id
        card_codes = list({code["cardcode"] for code in sap_codes if code["cardcode"]})
        partners_map = {}
        if card_codes:
            partners = ctx.env["res.partner"].search_read(
                [("sap_card_code", "in", card_codes)],
                ["id", "sap_card_code"],
            )
            partners_map = {p["sap_card_code"]: p["id"] for p in partners}

        _logger.info(
            f"Extracted {len(sap_codes)} customer product codes from SAP OSCN "
            f"(matched {len(products_map)} products, {len(partners_map)} partners)"
        )

        return {
            "sap_codes": sap_codes,
            "products_map": products_map,
            "partners_map": partners_map,
            "company_id": ctx.env.company.id,
        }

    @ETL.transform()
    def transform_customer_codes(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform SAP customer codes into Odoo product.customer.code values.

        Args:
            ctx: ETL context.
            extracted: Dictionary containing extracted data.

        Returns:
            List of customer code value dictionaries ready for creation.
        """
        data = extracted.get("extract_customer_codes") or {}
        sap_codes = data.get("sap_codes", [])
        products_map = data.get("products_map", {})
        partners_map = data.get("partners_map", {})
        company_id = data.get("company_id")

        code_vals = []
        skipped = 0
        for sap_code in sap_codes:
            product_id = products_map.get(sap_code["itemcode"])
            partner_id = partners_map.get(sap_code["cardcode"])

            if not product_id or not partner_id:
                skipped += 1
                continue

            vals = {
                "product_id": product_id,
                "partner_id": partner_id,
                "company_id": company_id,
                "product_code": sap_code["substitute"],
            }
            code_vals.append(vals)

        if skipped:
            _logger.warning(
                f"Skipped {skipped} customer codes due to missing product or partner"
            )

        _logger.info(f"Transformed {len(code_vals)} customer product code records.")
        return code_vals

    @ETL.load()
    def load_customer_codes(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load customer product codes into Odoo.

        Args:
            ctx: ETL context.
            transformed: Dictionary containing transformed data.
        """
        code_vals = transformed.get("transform_customer_codes") or []

        if code_vals:
            codes = ctx.env["product.customer.code"].create(code_vals)
            _logger.info(f"Created {len(codes)} customer product codes.")
        else:
            _logger.info("No customer product codes to create.")
