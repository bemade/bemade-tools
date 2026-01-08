import logging
from datetime import datetime, timezone
from typing import Dict, List

from odoo import Command, api, models
from odoo.tools.sql import SQL

from odoo.addons.sap_b1_to_odoo.etl_framework import ETL, ETLContext
from odoo.addons.sap_b1_to_odoo.tools import fix_tz

utc = timezone.utc

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="product.pricelist",
    importer_name="product.pricelist.item.importer",
    sap_source="opln,ooat,oat1",
    depends_on=["product.product.importer", "res.partner.company.importer"],
    allow_multiprocessing=False,  # Small dataset, always single-process
)
class ProductPricelistItemImporter(models.AbstractModel):
    _name = "product.pricelist.item.importer"
    _description = "SAP Product Pricelist Items Importer (OPLN/OOAT/OAT1)"

    _lookup_cache = {}

    @ETL.extract("opln,ooat,oat1")
    def extract_pricelists_and_blankets(self, ctx: ETLContext) -> Dict:
        """Extract pricelists and blanket orders from SAP.

        Args:
            ctx: ETL context with SAP cursor and Odoo environment.

        Returns:
            Dictionary containing basic pricelists, blanket orders, and blanket lines.
        """
        # Extract basic pricelists from OPLN
        ctx.cr.execute("SELECT * FROM opln")
        basic_pricelists = ctx.cr.dictfetchall()
        _logger.info(f"Extracted {len(basic_pricelists)} basic pricelists from OPLN.")

        # Extract blanket orders from OOAT
        ctx.cr.execute("SELECT * FROM ooat")
        blanket_orders = ctx.cr.dictfetchall()
        _logger.info(f"Extracted {len(blanket_orders)} blanket orders from OOAT.")

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
        product_tmpl_map = {
            product.sap_item_code: product.product_tmpl_id.id for product in products
        }

        # Get partners
        cardcodes = [blanket["bpcode"] for blanket in blanket_orders]
        partners = ctx.env["res.partner"].search(
            [("sap_card_code", "in", cardcodes), ("active", "in", [True, False])]
        )
        partners_map = {partner.sap_card_code: partner.id for partner in partners}
        partner_type_map = {
            partner.sap_card_code: partner.sap_partner_type for partner in partners
        }
        partner_pricelist_map = {
            partner.sap_card_code: partner.property_product_pricelist.id
            for partner in partners
        }

        # Get currencies
        currencies = ctx.env["res.currency"].search([])
        currencies_map = {currency.name: currency.id for currency in currencies}

        # Get agreement to cardcode mapping for purchase blankets
        ctx.cr.execute(
            "SELECT cardcode, dflagrmnt FROM ocrd WHERE dflagrmnt IS NOT NULL"
        )
        agreements_to_cardcode = ctx.cr.fetchall()
        agreement_partners_dict = {}
        for cardcode, agreement_id in agreements_to_cardcode:
            partner_id = partners_map.get(cardcode)
            if partner_id:
                agreement_partners_dict.setdefault(agreement_id, []).append(partner_id)

        ProductPricelistItemImporter._lookup_cache = {
            "products_map": products_map,
            "product_tmpl_map": product_tmpl_map,
            "partners_map": partners_map,
            "partner_type_map": partner_type_map,
            "partner_pricelist_map": partner_pricelist_map,
            "currencies_map": currencies_map,
            "agreement_partners_dict": agreement_partners_dict,
            "company_id": ctx.env.company.id,
        }
        _logger.info("Lookup dictionaries ready.")

        return {
            "basic_pricelists": basic_pricelists,
            "blanket_orders": blanket_orders,
            "blanket_lines_dict": lines_dict,
        }

    @ETL.transform()
    def transform_pricelists_and_blankets(
        self, ctx: ETLContext, extracted: Dict
    ) -> Dict:
        """Transform SAP pricelists and blankets into Odoo values.

        Args:
            ctx: ETL context.
            extracted: Dictionary containing extracted data.

        Returns:
            Dictionary with basic_pricelist_vals, customer_pricelist_vals, and purchase_blanket_vals.
        """
        data = extracted["extract_pricelists_and_blankets"]
        basic_pricelists = data["basic_pricelists"]
        blanket_orders = data["blanket_orders"]
        blanket_lines_dict = data["blanket_lines_dict"]

        cache = ProductPricelistItemImporter._lookup_cache
        products_map = cache["products_map"]
        product_tmpl_map = cache["product_tmpl_map"]
        partners_map = cache["partners_map"]
        partner_type_map = cache["partner_type_map"]
        partner_pricelist_map = cache["partner_pricelist_map"]
        currencies_map = cache["currencies_map"]
        company_id = cache["company_id"]

        # Transform basic pricelists (OPLN)
        basic_pricelist_vals = []
        for pricelist in basic_pricelists:
            if pricelist["listnum"] != 1:  # Skip listnum 1, it's the public pricelist
                basic_pricelist_vals.append(
                    {
                        "sap_listnum": pricelist["listnum"],
                        "name": pricelist["listname"],
                    }
                )

        _logger.info(f"Transformed {len(basic_pricelist_vals)} basic pricelists.")

        # Transform customer pricelists (blanket orders for customers)
        customer_pricelist_vals = []
        now = datetime.now(utc)

        for blanket in blanket_orders:
            partner_id = partners_map.get(blanket["bpcode"])
            if not partner_id:
                continue

            partner_type = partner_type_map.get(blanket["bpcode"])
            if partner_type == "S":  # Skip suppliers
                continue

            start = fix_tz(blanket["startdate"])
            end = fix_tz(blanket["enddate"])
            active = now <= end

            lines = blanket_lines_dict.get(blanket["absid"], [])
            item_vals = []

            for line in lines:
                product_tmpl_id = product_tmpl_map.get(line["itemcode"])
                if not product_tmpl_id:
                    continue

                item_vals.append(
                    {
                        "applied_on": "1_product",
                        "product_tmpl_id": product_tmpl_id,
                        "compute_price": "fixed",
                        "fixed_price": line["unitprice"],
                        "company_id": company_id,
                        "date_start": start,
                        "date_end": end,
                    }
                )

            # Add fallback to base pricelist
            base_pricelist_id = partner_pricelist_map.get(blanket["bpcode"])
            if base_pricelist_id:
                item_vals.append(
                    {
                        "applied_on": "3_global",
                        "base": "pricelist",
                        "base_pricelist_id": base_pricelist_id,
                    }
                )

            currency_code = "USD" if blanket["bpcurr"] == "USD" else "CAD"
            currency_id = currencies_map.get(currency_code)

            # Get partner name for pricelist name
            partner = ctx.env["res.partner"].browse(partner_id)
            name = blanket["descript"] or partner.name

            customer_pricelist_vals.append(
                {
                    "sap_abs_id": blanket["absid"],
                    "name": f"{partner.name} - {name}",
                    "active": active,
                    "currency_id": currency_id,
                    "item_ids": [Command.create(val) for val in item_vals],
                    "company_id": company_id,
                    "_partner_id": partner_id,  # Store for later use
                    "_is_active": active,
                }
            )

        _logger.info(f"Transformed {len(customer_pricelist_vals)} customer pricelists.")

        return {
            "basic_pricelist_vals": basic_pricelist_vals,
            "customer_pricelist_vals": customer_pricelist_vals,
        }

    @ETL.load()
    def load_pricelists_and_blankets(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load pricelists and blankets into Odoo.

        Args:
            ctx: ETL context.
            transformed: Dictionary containing transformed data.
        """
        data = transformed["transform_pricelists_and_blankets"]
        basic_pricelist_vals = data["basic_pricelist_vals"]
        customer_pricelist_vals = data["customer_pricelist_vals"]

        # Load basic pricelists
        if basic_pricelist_vals:
            # Get public pricelist to use as base
            public_pricelist = ctx.env["product.pricelist"].search(
                [
                    ("name", "ilike", "public"),
                    ("currency_id", "=", ctx.env.ref("base.CAD").id),
                ],
                limit=1,
            )

            if not public_pricelist:
                _logger.warning(
                    "Public pricelist not found. Creating basic pricelists without base pricelist."
                )
                # Create without base pricelist - they'll be standalone
                ctx.env["product.pricelist"].create(basic_pricelist_vals)
            else:
                # Add base pricelist reference to each
                for vals in basic_pricelist_vals:
                    vals["item_ids"] = [
                        Command.create(
                            {
                                "applied_on": "3_global",
                                "base": "pricelist",
                                "base_pricelist_id": public_pricelist.id,
                            }
                        )
                    ]

                ctx.env["product.pricelist"].create(basic_pricelist_vals)

            _logger.info(f"Created {len(basic_pricelist_vals)} basic pricelists.")

        # Load customer pricelists
        if customer_pricelist_vals:
            # Remove temporary fields
            partner_mappings = []
            for vals in customer_pricelist_vals:
                partner_mappings.append(
                    {
                        "partner_id": vals.pop("_partner_id"),
                        "is_active": vals.pop("_is_active"),
                    }
                )

            pricelists = ctx.env["product.pricelist"].create(customer_pricelist_vals)
            _logger.info(f"Created {len(pricelists)} customer pricelists.")

            # Set partner default pricelists
            now = datetime.now(utc)
            default_pricelists = ctx.env["product.pricelist"].search(
                [("name", "ilike", "Default")]
            )

            for pricelist, mapping in zip(pricelists, partner_mappings):
                partner = ctx.env["res.partner"].browse(mapping["partner_id"])

                if pricelist.active and mapping["is_active"]:
                    partner.property_product_pricelist = pricelist
                elif (
                    not pricelist.active
                    and pricelist.currency_id != ctx.env.company.currency_id
                ):
                    # Set to default pricelist with matching currency
                    applicable = default_pricelists.filtered(
                        lambda pl: pl.currency_id == pricelist.currency_id
                    )
                    if applicable:
                        partner.property_product_pricelist = applicable[0]

            _logger.info("Set partner default pricelists.")

        # Set USD pricelist for USD partners
        self._set_usd_pricelist_partners(ctx)

    def _set_usd_pricelist_partners(self, ctx: ETLContext) -> None:
        """Set USD pricelist for partners with USD currency."""
        ctx.cr.execute("SELECT cardcode FROM ocrd WHERE currency = 'USD'")
        cardcodes = [item[0] for item in ctx.cr.fetchall()]

        if not cardcodes:
            return

        cad_pricelist = ctx.env["product.pricelist"].search(
            [("name", "=", "Default CAD Pricelist")], limit=1
        )
        usd_pricelist = ctx.env["product.pricelist"].search(
            [("name", "=", "Default USD Pricelist")], limit=1
        )

        if not usd_pricelist:
            return

        partners = ctx.env["res.partner"].search(
            [("sap_card_code", "in", cardcodes), ("active", "in", [True, False])]
        )

        # Only update partners that have the CAD pricelist
        partners_to_update = partners.filtered(
            lambda p: p.property_product_pricelist == cad_pricelist
        )

        if partners_to_update:
            partners_to_update.write({"property_product_pricelist": usd_pricelist.id})
            _logger.info(f"Set USD pricelist for {len(partners_to_update)} partners.")


@ETL.pipeline(
    target_model="product.pricelist",
    importer_name="product.pricelist.importer",
    depends_on=[],
    allow_multiprocessing=False,  # Small dataset, always single-process
)
class ProductPricelistInitImporter(models.AbstractModel):
    _name = "product.pricelist.importer"
    _description = "Initialize Default Pricelists for Active Currencies"

    @ETL.extract("res.currency")
    def extract_active_currencies(self, ctx: ETLContext) -> List:
        """Extract active currencies from Odoo (not SAP).

        Args:
            ctx: ETL context.

        Returns:
            List of active currency records.
        """
        active_currencies = ctx.env["res.currency"].search([("active", "=", True)])
        _logger.info(f"Found {len(active_currencies)} active currencies.")
        return active_currencies

    @ETL.transform()
    def transform_pricelist_vals(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform currencies into pricelist values.

        Args:
            ctx: ETL context.
            extracted: Dictionary containing extracted data.

        Returns:
            List of pricelist value dictionaries for creation.
        """
        currencies = extracted["extract_active_currencies"]
        company = ctx.env.company

        pricelist_vals = []
        for currency in currencies:
            pricelist_name = f"Default {currency.name} Pricelist"

            # Check if pricelist already exists
            existing_pricelist = ctx.env["product.pricelist"].search(
                [
                    ("currency_id", "=", currency.id),
                    ("company_id", "=", company.id),
                    ("name", "=", pricelist_name),
                ],
                limit=1,
            )

            if not existing_pricelist:
                pricelist_vals.append(
                    {
                        "name": pricelist_name,
                        "currency_id": currency.id,
                        "company_id": company.id,
                    }
                )

        return pricelist_vals

    @ETL.load()
    def load_pricelists(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load pricelists into Odoo.

        Args:
            ctx: ETL context.
            transformed: Dictionary containing transformed data.
        """
        pricelist_vals = transformed["transform_pricelist_vals"]

        if pricelist_vals:
            pricelists = ctx.env["product.pricelist"].create(pricelist_vals)
            _logger.info(f"Created {len(pricelists)} default pricelists.")
        else:
            _logger.info("No new pricelists to create (all already exist).")
