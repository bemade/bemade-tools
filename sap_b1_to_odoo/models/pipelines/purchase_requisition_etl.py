import logging
from datetime import datetime, timezone
from typing import Dict, List

from odoo import Command, api, models
from odoo.tools.sql import SQL

from odoo.addons.etl_framework import ETL, ETLContext
from odoo.addons.sap_b1_to_odoo.tools import fix_tz

utc = timezone.utc

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="purchase.requisition",
    importer_name="purchase.requisition.importer",
    sap_source="ooat,oat1",
    depends_on=["product.product.importer", "res.partner.company.importer"],
    allow_multiprocessing=False,  # Small dataset, always single-process
)
class PurchaseRequisitionImporter(models.AbstractModel):
    _name = "purchase.requisition.importer"
    _description = "SAP Purchase Blanket Order Importer (OOAT/OAT1)"

    _lookup_cache = {}

    @ETL.extract("ooat,oat1")
    def extract_blanket_orders(self, ctx: ETLContext) -> Dict:
        """Extract purchase blanket orders from SAP.

        Args:
            ctx: ETL context with SAP cursor and Odoo environment.

        Returns:
            Dictionary containing blanket orders and blanket lines.
        """
        # Check for existing blanket orders
        ctx.env.cr.execute(
            "SELECT DISTINCT sap_abs_id FROM purchase_requisition WHERE sap_abs_id IS NOT NULL"
        )
        existing_ids = set(row[0] for row in ctx.env.cr.fetchall())
        _logger.info(f"Found {len(existing_ids)} existing purchase blanket orders.")

        # Extract blanket orders from OOAT
        ctx.cr.execute("SELECT * FROM ooat")
        all_blanket_orders = ctx.cr.dictfetchall()

        # Filter out existing and non-supplier blankets
        blanket_orders = [
            blanket
            for blanket in all_blanket_orders
            if blanket["absid"] not in existing_ids
        ]

        _logger.info(
            f"Extracted {len(blanket_orders)} new blanket orders from OOAT "
            f"(filtered from {len(all_blanket_orders)} total)."
        )

        # Extract blanket lines from OAT1
        ctx.cr.execute("SELECT * FROM oat1")
        blanket_lines = ctx.cr.dictfetchall()
        _logger.info(f"Extracted {len(blanket_lines)} blanket lines from OAT1.")

        # Group lines by agreement number
        lines_dict = {}
        for line in blanket_lines:
            lines_dict.setdefault(line["agrno"], []).append(line)

        # Pre-compute lookup dictionaries
        _logger.info("Pre-computing lookup dictionaries...")

        # Get products
        itemcodes = [line["itemcode"] for line in blanket_lines]
        products = ctx.env["product.product"].search(
            [("sap_item_code", "in", itemcodes), ("active", "in", [False, True])]
        )
        products_map = {product.sap_item_code: product.id for product in products}

        # Get partners
        cardcodes = [blanket["bpcode"] for blanket in blanket_orders]
        partners = ctx.env["res.partner"].search(
            [("sap_card_code", "in", cardcodes), ("active", "in", [True, False])]
        )
        partners_map = {partner.sap_card_code: partner.id for partner in partners}
        partner_type_map = {
            partner.sap_card_code: partner.sap_partner_type for partner in partners
        }

        # Get currencies
        currencies = ctx.env["res.currency"].search([])
        currencies_map = {currency.name: currency.id for currency in currencies}

        # Get agreement to customer mapping
        ctx.cr.execute(
            "SELECT cardcode, dflagrmnt FROM ocrd WHERE dflagrmnt IS NOT NULL"
        )
        agreements_to_cardcode = ctx.cr.fetchall()
        agreement_customers_dict = {}
        for cardcode, agreement_id in agreements_to_cardcode:
            partner_id = partners_map.get(cardcode)
            if partner_id:
                agreement_customers_dict.setdefault(agreement_id, []).append(partner_id)

        PurchaseRequisitionImporter._lookup_cache = {
            "products_map": products_map,
            "partners_map": partners_map,
            "partner_type_map": partner_type_map,
            "currencies_map": currencies_map,
            "agreement_customers_dict": agreement_customers_dict,
        }
        _logger.info("Lookup dictionaries ready.")

        return {
            "blanket_orders": blanket_orders,
            "blanket_lines_dict": lines_dict,
        }

    @ETL.transform()
    def transform_blanket_orders(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform SAP blanket orders into Odoo purchase requisition values.

        Args:
            ctx: ETL context.
            extracted: Dictionary containing extracted data.

        Returns:
            List of purchase requisition value dictionaries.
        """
        data = extracted["extract_blanket_orders"]
        blanket_orders = data["blanket_orders"]
        blanket_lines_dict = data["blanket_lines_dict"]

        cache = PurchaseRequisitionImporter._lookup_cache
        products_map = cache["products_map"]
        partners_map = cache["partners_map"]
        partner_type_map = cache["partner_type_map"]
        currencies_map = cache["currencies_map"]
        agreement_customers_dict = cache["agreement_customers_dict"]

        def _get_status(blanket):
            """Map SAP status to Odoo state."""
            match blanket["status"]:
                case "A" | "X" | "P":
                    return "confirmed"
                case "B" | "D" | "F":
                    return "draft"
                case "T":
                    return "done"
                case "C":
                    return "cancel"
                case _:
                    return "draft"

        blanket_vals = []
        for blanket in blanket_orders:
            partner_id = partners_map.get(blanket["bpcode"])
            if not partner_id:
                continue

            # Only create for suppliers
            partner_type = partner_type_map.get(blanket["bpcode"])
            if partner_type != "S":
                continue

            start = fix_tz(blanket["startdate"])
            end = fix_tz(blanket["enddate"])
            reference = blanket["descript"]
            status = _get_status(blanket)

            currency_code = "USD" if blanket["bpcurr"] == "USD" else "CAD"
            currency_id = currencies_map.get(currency_code)

            # Transform blanket lines
            lines = blanket_lines_dict.get(blanket["absid"], [])
            line_vals = []
            for line in lines:
                product_id = products_map.get(line["itemcode"])
                if not product_id:
                    _logger.warning(
                        f"Skipping blanket line for unknown product: {line['itemcode']}"
                    )
                    continue

                line_vals.append(
                    {
                        "product_id": product_id,
                        "product_qty": line["planqty"],
                        "price_unit": line["unitprice"],
                    }
                )

            # Get customer IDs for this agreement
            customer_ids = agreement_customers_dict.get(blanket["absid"], [])

            blanket_vals.append(
                {
                    "sap_abs_id": blanket["absid"],
                    "vendor_id": partner_id,
                    "reference": reference,
                    "date_start": start,
                    "date_end": end,
                    "state": status,
                    "currency_id": currency_id,
                    "line_ids": [Command.create(val) for val in line_vals],
                    "requisition_type": "blanket_order",
                    "customer_ids": [Command.set(customer_ids)] if customer_ids else [],
                }
            )

        _logger.info(f"Transformed {len(blanket_vals)} purchase blanket orders.")
        return blanket_vals

    @ETL.load()
    def load_blanket_orders(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load purchase blanket orders into Odoo.

        Args:
            ctx: ETL context.
            transformed: Dictionary containing transformed data.
        """
        blanket_vals = transformed["transform_blanket_orders"]

        if not blanket_vals:
            _logger.info("No new purchase blanket orders to create.")
            return

        blankets = ctx.env["purchase.requisition"].create(blanket_vals)
        _logger.info(f"Created {len(blankets)} purchase blanket orders.")
