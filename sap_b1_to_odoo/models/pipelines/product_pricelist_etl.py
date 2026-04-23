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
    target_model="product.pricelist",
    importer_name="product.pricelist.item.importer",
    sap_source="opln,ooat,oat1",
    depends_on=["product.product.importer", "res.partner.company.importer"],
)
class ProductPricelistItemImporter(models.AbstractModel):
    _name = "product.pricelist.item.importer"
    _description = "SAP Product Pricelist Items Importer (OPLN/OOAT/OAT1)"

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

        # Extract customer default pricelist assignments (OCRD.listnum)
        ctx.cr.execute(
            "SELECT cardcode, listnum FROM ocrd WHERE cardtype = 'C' AND listnum IS NOT NULL"
        )
        customer_listnum_map = {row[0]: row[1] for row in ctx.cr.fetchall()}
        _logger.info(
            f"Extracted {len(customer_listnum_map)} customer pricelist assignments from OCRD."
        )

        # Get partners (blanket agreement partners + all customers with pricelist)
        cardcodes = list(
            set([blanket["bpcode"] for blanket in blanket_orders])
            | set(customer_listnum_map.keys())
        )
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

        _logger.info("Lookup dictionaries ready.")

        return {
            "basic_pricelists": basic_pricelists,
            "blanket_orders": blanket_orders,
            "blanket_lines_dict": lines_dict,
            "products_map": products_map,
            "product_tmpl_map": product_tmpl_map,
            "partners_map": partners_map,
            "partner_type_map": partner_type_map,
            "partner_pricelist_map": partner_pricelist_map,
            "currencies_map": currencies_map,
            "agreement_partners_dict": agreement_partners_dict,
            "customer_listnum_map": customer_listnum_map,
            "company_id": ctx.env.company.id,
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
        products_map = data["products_map"]
        product_tmpl_map = data["product_tmpl_map"]
        partners_map = data["partners_map"]
        partner_type_map = data["partner_type_map"]
        partner_pricelist_map = data["partner_pricelist_map"]
        currencies_map = data["currencies_map"]
        company_id = data["company_id"]

        # Transform basic pricelists (OPLN)
        # Separate into base pricelists (self-referencing) and derived
        # pricelists (referencing another list with a factor).
        base_pricelist_vals = []
        derived_pricelist_vals = []
        for pricelist in basic_pricelists:
            listnum = pricelist["listnum"]
            base_num = pricelist["base_num"]
            factor = float(pricelist["factor"] or 1.0)
            vals = {
                "sap_listnum": listnum,
                "name": pricelist["listname"],
                "company_id": company_id,
            }
            if base_num == listnum:
                # Self-referencing: standalone base pricelist
                base_pricelist_vals.append(vals)
            else:
                # Derived: references another pricelist with a factor
                vals["_base_listnum"] = base_num
                vals["_factor"] = factor
                derived_pricelist_vals.append(vals)

        _logger.info(
            f"Transformed {len(base_pricelist_vals)} base pricelists "
            f"and {len(derived_pricelist_vals)} derived pricelists."
        )

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
            "base_pricelist_vals": base_pricelist_vals,
            "derived_pricelist_vals": derived_pricelist_vals,
            "customer_pricelist_vals": customer_pricelist_vals,
            "customer_listnum_map": data["customer_listnum_map"],
            "partners_map": data["partners_map"],
        }

    @ETL.load()
    def load_pricelists_and_blankets(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load pricelists and blankets into Odoo.

        Args:
            ctx: ETL context.
            transformed: Dictionary containing transformed data.
        """
        data = transformed["transform_pricelists_and_blankets"]
        base_pricelist_vals = data["base_pricelist_vals"]
        derived_pricelist_vals = data["derived_pricelist_vals"]
        customer_pricelist_vals = data["customer_pricelist_vals"]

        Pricelist = ctx.env["product.pricelist"]

        # Build existing pricelist maps for deduplication
        existing_by_listnum = {}
        existing_by_abs_id = {}
        all_existing = Pricelist.with_context(active_test=False).search([
            "|",
            ("sap_listnum", "!=", False),
            ("sap_abs_id", "!=", False),
        ])
        for pl in all_existing:
            if pl.sap_listnum:
                existing_by_listnum[pl.sap_listnum] = pl
            if pl.sap_abs_id:
                existing_by_abs_id[pl.sap_abs_id] = pl

        # 1. Load base pricelists (upsert by sap_listnum)
        for vals in base_pricelist_vals:
            existing = existing_by_listnum.get(vals["sap_listnum"])
            if existing:
                write_vals = {k: v for k, v in vals.items() if k != "sap_listnum"}
                # Clear old items before re-creating if transform added item_ids
                if "item_ids" in write_vals:
                    write_vals["item_ids"] = [Command.clear()] + write_vals["item_ids"]
                existing.write(write_vals)
            else:
                new_pl = Pricelist.create(vals)
                existing_by_listnum[vals["sap_listnum"]] = new_pl

        created_base = sum(
            1 for v in base_pricelist_vals
            if v["sap_listnum"] not in {pl.sap_listnum for pl in all_existing}
        )
        _logger.info(
            f"Loaded {len(base_pricelist_vals)} base pricelists "
            f"({created_base} created, {len(base_pricelist_vals) - created_base} updated)."
        )

        # Build listnum → pricelist.id map for derived pricelists
        # Re-fetch to include any newly created base pricelists
        all_sap_pricelists = Pricelist.with_context(active_test=False).search([
            ("sap_listnum", "!=", False),
        ])
        listnum_to_id = {pl.sap_listnum: pl.id for pl in all_sap_pricelists}

        # 2. Load derived pricelists with factor-based formula rules (upsert)
        derived_created = 0
        derived_updated = 0
        for vals in derived_pricelist_vals:
            base_listnum = vals.pop("_base_listnum")
            factor = vals.pop("_factor")
            base_pl_id = listnum_to_id.get(base_listnum)
            if not base_pl_id:
                _logger.warning(
                    f"Base pricelist listnum {base_listnum} not found "
                    f"for derived pricelist '{vals['name']}'. Skipping."
                )
                continue
            # Factor is a markup multiplier: Retail = Base × 1.75
            # Odoo formula: price = base - (base * discount / 100)
            # So discount = -(factor - 1) * 100  (negative = markup)
            discount = -(factor - 1.0) * 100.0
            item_vals = {
                "applied_on": "3_global",
                "compute_price": "formula",
                "base": "pricelist",
                "base_pricelist_id": base_pl_id,
                "price_discount": discount,
            }

            existing = existing_by_listnum.get(vals["sap_listnum"])
            if existing:
                # Replace existing items and update fields
                vals["item_ids"] = [
                    Command.clear(),
                    Command.create(item_vals),
                ]
                existing.write(vals)
                derived_updated += 1
            else:
                vals["item_ids"] = [Command.create(item_vals)]
                new_pl = Pricelist.create(vals)
                existing_by_listnum[vals["sap_listnum"]] = new_pl
                derived_created += 1

        _logger.info(
            f"Loaded derived pricelists "
            f"({derived_created} created, {derived_updated} updated)."
        )

        # 3. Load customer pricelists (upsert by sap_abs_id)
        cust_created = 0
        cust_updated = 0
        if customer_pricelist_vals:
            default_pricelists = Pricelist.search([("name", "ilike", "Default")])

            for vals in customer_pricelist_vals:
                partner_id = vals.pop("_partner_id")
                is_active = vals.pop("_is_active")

                existing = existing_by_abs_id.get(vals.get("sap_abs_id"))
                if existing:
                    # Replace items and update
                    vals["item_ids"] = [Command.clear()] + vals.get("item_ids", [])
                    existing.write(vals)
                    pricelist = existing
                    cust_updated += 1
                else:
                    pricelist = Pricelist.create(vals)
                    if vals.get("sap_abs_id"):
                        existing_by_abs_id[vals["sap_abs_id"]] = pricelist
                    cust_created += 1

                # Set partner default pricelist
                partner = ctx.env["res.partner"].browse(partner_id)
                if pricelist.active and is_active:
                    partner.property_product_pricelist = pricelist
                elif (
                    not pricelist.active
                    and pricelist.currency_id != ctx.env.company.currency_id
                ):
                    applicable = default_pricelists.filtered(
                        lambda pl: pl.currency_id == pricelist.currency_id
                    )
                    if applicable:
                        partner.property_product_pricelist = applicable[0]

            _logger.info(
                f"Loaded customer pricelists "
                f"({cust_created} created, {cust_updated} updated)."
            )

        # 5. Apply house-default pricelist sequencing and archive empty shell.
        #    This MUST run before the OCRD loop (step 4) so that:
        #    (a) listnum_to_id is already populated (steps 1-2 done above), and
        #    (b) sequence changes precede partner writes.
        house_default = self._get_house_default_pricelist(ctx)
        self._apply_house_default_pricelist(ctx, house_default)

        # 4. Set customer default pricelists from OCRD.listnum, skipping partners
        #    whose SAP listnum maps to the house-default pricelist (they resolve
        #    correctly via the sequence-based resolver; writing explicit = house
        #    would be a wasted no-op due to _inverse_product_pricelist collapse).
        customer_listnum_map = data["customer_listnum_map"]
        partners_map = data["partners_map"]
        if customer_listnum_map:
            Partner = ctx.env["res.partner"]
            updated_count = 0
            skipped_count = 0
            for cardcode, listnum in customer_listnum_map.items():
                partner_id = partners_map.get(cardcode)
                pricelist_id = listnum_to_id.get(listnum)
                if not partner_id or not pricelist_id:
                    skipped_count += 1
                    continue
                if house_default and pricelist_id == house_default.id:
                    # Walk-up/retail customer — let them fall through to the
                    # house default via the resolver; don't write explicit specific.
                    skipped_count += 1
                    continue
                Partner.browse(partner_id).property_product_pricelist = pricelist_id
                updated_count += 1
            _logger.info(
                f"Set default pricelists for {updated_count} customers "
                f"({skipped_count} skipped — missing partner/pricelist or house-default)."
            )

        # Set USD pricelist for USD partners
        self._set_usd_pricelist_partners(ctx)

    def _get_house_default_pricelist(self, ctx: ETLContext):
        """Return the pricelist that should serve as the company-wide house default.

        When non-empty, the returned pricelist will be given the lowest sequence
        among active company-scoped pricelists so that Odoo's resolver
        (``_get_country_pricelist_multi`` step 1: lowest-sequence active match)
        routes all partners without a specific pricelist to it.

        Override this method in a client module to return a client-specific
        pricelist.  The base implementation returns an empty recordset (no-op).

        Args:
            ctx: ETL context with Odoo environment.

        Returns:
            A ``product.pricelist`` singleton, or an empty recordset if no
            house default should be configured.
        """
        return ctx.env["product.pricelist"]

    def _apply_house_default_pricelist(self, ctx: ETLContext, house_default) -> None:
        """Apply house-default pricelist: lower its sequence and archive empty shells.

        When ``house_default`` is non-empty:

        1. Sets ``house_default.sequence`` to ``min(other_active_sequences) - 1``
           (or 1 if there are no other active pricelists in the company domain),
           ensuring it wins step (1) of Odoo 19's sequence-first resolver.
        2. Archives every OTHER active pricelist in the company domain that has:
           - zero ``item_ids``
           - no ``sap_listnum`` and no ``sap_abs_id``
           (targets the Odoo-auto "Default" id 1 shell; the predicate keeps this
           client-agnostic and safe).

        When ``house_default`` is empty (base default), this method is a no-op.

        Args:
            ctx: ETL context with Odoo environment.
            house_default: Singleton returned by ``_get_house_default_pricelist``,
                or an empty recordset.
        """
        if not house_default:
            return

        Pricelist = ctx.env["product.pricelist"]
        company_id = ctx.env.company.id
        pl_domain = [
            "|",
            ("company_id", "=", company_id),
            ("company_id", "=", False),
        ]

        # Find all other active pricelists in the company domain (excluding house_default)
        other_active = Pricelist.search(
            pl_domain + [("id", "!=", house_default.id), ("active", "=", True)]
        )
        if other_active:
            min_seq = min(other_active.mapped("sequence"))
            new_seq = min_seq - 1
        else:
            new_seq = 1

        if house_default.sequence != new_seq:
            house_default.sequence = new_seq
            _logger.info(
                f"Set house-default pricelist '{house_default.name}' "
                f"(id={house_default.id}) sequence to {new_seq}."
            )

        # Archive empty, unlinked active pricelists that would otherwise win the resolver
        shells_to_archive = Pricelist.search(
            pl_domain + [
                ("id", "!=", house_default.id),
                ("active", "=", True),
                ("sap_listnum", "=", False),
                ("sap_abs_id", "=", False),
            ]
        ).filtered(lambda pl: not pl.item_ids)
        if shells_to_archive:
            shells_to_archive.write({"active": False})
            names = ", ".join(
                f"'{pl.name}' (id={pl.id})" for pl in shells_to_archive
            )
            _logger.info(
                f"Archived {len(shells_to_archive)} empty shell pricelist(s): {names}."
            )

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
