"""Sale Quotation ETL Pipelines

This module contains 3 ETL pipelines for importing sale quotations from SAP B1:
1. Sale Quotation Headers (OQUT) - Creates sale.order records without lines
2. Product Lines (QUT1) - Creates sale.order.line records for products
3. Text Lines (QUT10) - Creates sale.order.line records for text/notes

Note: Quotations don't need post-processing (no confirmation, pickings, etc.)
"""

import logging
from typing import Dict, List, Any

from odoo import api, models, Command
from odoo.tools.sql import SQL

from odoo.addons.etl_framework import ETL, ETLContext, ChunkableData
from odoo.addons.sap_b1_to_odoo.tools import fix_tz

_logger = logging.getLogger(__name__)


# =============================================================================
# Pipeline 1: Sale Quotation Headers (OQUT)
# =============================================================================


@ETL.pipeline(
    target_model="sale.order",
    importer_name="sale.quotation.header.importer",
    sap_source="oqut",
    depends_on=[
        "res.partner.company.importer",
        "account.payment.term.importer",
        "res.users.importer",
    ],
    multiprocessing_threshold=500,
    chunk_size=500,
)
class SaleQuotationHeaderImporter(models.AbstractModel):
    _name = "sale.quotation.header.importer"
    _description = "SAP Sales Quotation Header Importer (OQUT)"
    _inherit = "sale.purchase.order.etl.mixin"

    @ETL.extract("oqut")
    def extract_headers(self, ctx: ETLContext) -> List[Dict]:
        """Extract sales quotation headers from SAP OQUT table."""
        # Uppercase cardcodes for consistency
        ctx.cr.execute("UPDATE oqut SET cardcode = UPPER(cardcode)")

        # Get existing quotations (idempotence)
        ctx.env.cr.execute(
            "SELECT DISTINCT sap_docnum FROM sale_order WHERE sap_docnum IS NOT NULL"
        )
        existing_docnums = tuple(row[0] for row in ctx.env.cr.fetchall())
        _logger.info(f"Found {len(existing_docnums)} existing quotations.")

        # Extract new quotation headers (exclude those converted to orders)
        sql = """
            SELECT * FROM oqut 
            WHERE docentry NOT IN (
                SELECT baseentry FROM rdr1 WHERE basetype = 23
            )
        """
        if existing_docnums:
            sql += " AND docnum NOT IN %s"
            ctx.cr.execute(SQL(sql, existing_docnums))
        else:
            ctx.cr.execute(sql)

        headers = ctx.cr.dictfetchall()
        _logger.info(f"Extracted {len(headers)} new quotation headers from OQUT.")

        if not headers:
            return ChunkableData(records=[], context={
                "partners_map": {},
                "partner_addresses_map": {},
                "contacts_map": {},
                "users_map": {},
                "terms_map": {},
                "pricelists_map": {},
                "carriers_map": {},
                "company_id": ctx.env.company.id,
            })

        # Pre-compute lookups (same as sale orders)
        _logger.info("Pre-computing lookup dictionaries...")

        # Partners, contacts, users, terms, pricelists, carriers
        cardcodes = [h["cardcode"] for h in headers]
        partners = ctx.env["res.partner"].search(
            [("sap_card_code", "in", cardcodes), ("active", "in", [True, False])]
        )
        partners_map = {partner.sap_card_code: partner.id for partner in partners}

        # Pre-compute partner addresses (delivery and invoice) for all partners
        partner_addresses_map = {}
        for partner in partners:
            # Get all potential address partners (commercial + children)
            all_partners = (
                partner.commercial_partner_id | partner.commercial_partner_id.child_ids
            )

            # Find delivery addresses
            delivery_partners = all_partners.filtered(lambda p: p.type == "delivery")
            invoice_partners = all_partners.filtered(lambda p: p.type == "invoice")

            # Store as dict of address type -> list of (id, address_string)
            partner_addresses_map[partner.id] = {
                "delivery": [
                    (p.id, self.extract_address_string(p)) for p in delivery_partners
                ],
                "invoice": [
                    (p.id, self.extract_address_string(p)) for p in invoice_partners
                ],
                "commercial_id": partner.commercial_partner_id.id,
            }

        cntctcodes = [h["cntctcode"] for h in headers if h.get("cntctcode")]
        contacts = ctx.env["res.partner"].search(
            [("sap_cntct_code", "in", cntctcodes), ("active", "in", [True, False])]
        )
        contacts_map = {
            contact.sap_cntct_code: (
                contact.parent_id.id if contact.parent_id else contact.id
            )
            for contact in contacts
        }

        slpcodes = [h["slpcode"] for h in headers if h.get("slpcode")]
        users = ctx.env["res.users"].search(
            [("sap_slpcode", "in", slpcodes), ("active", "in", [False, True])]
        )
        users_map = {user.sap_slpcode: user.id for user in users}

        groupnums = [h["groupnum"] for h in headers if h.get("groupnum")]
        terms = ctx.env["account.payment.term"].search(
            [("sap_groupnum", "in", groupnums)]
        )
        terms_map = {term.sap_groupnum: term.id for term in terms}

        cad_pricelist = ctx.env["product.pricelist"].search(
            [
                ("currency_id.name", "=", "CAD"),
                ("company_id", "=", ctx.env.company.id),
                ("name", "=", "Default CAD Pricelist"),
            ],
            limit=1,
        )
        usd_pricelist = ctx.env["product.pricelist"].search(
            [
                ("currency_id.name", "=", "USD"),
                ("company_id", "=", ctx.env.company.id),
                ("name", "=", "Default USD Pricelist"),
            ],
            limit=1,
        )
        pricelists_map = {
            "CAD": cad_pricelist.id if cad_pricelist else False,
            "USD": usd_pricelist.id if usd_pricelist else False,
        }

        carriers = ctx.env["sap.transporter"].search([])
        carriers_map = {
            tpt.sap_trnspcode: tpt.delivery_carrier_id.id
            for tpt in carriers
            if tpt.delivery_carrier_id
        }

        _logger.info("Lookup dictionaries ready.")

        return ChunkableData(records=headers, context={
            "partners_map": partners_map,
            "partner_addresses_map": partner_addresses_map,
            "contacts_map": contacts_map,
            "users_map": users_map,
            "terms_map": terms_map,
            "pricelists_map": pricelists_map,
            "carriers_map": carriers_map,
            "company_id": ctx.env.company.id,
        })

    @ETL.transform()
    def transform_headers(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform SAP quotation headers into Odoo sale.order values."""
        data = extracted["extract_headers"]
        headers = data.records
        cache = data.context

        if not headers:
            _logger.info("No headers to transform.")
            return []

        order_vals = []
        for header in headers:
            # Get partner ID (contact or company)
            partner_id = self.get_partner_id(header, cache)
            if not partner_id:
                _logger.warning(
                    f"Skipping quotation {header['docnum']}: partner not found "
                    f"(cardcode={header['cardcode']}, cntctcode={header.get('cntctcode')})"
                )
                continue

            # Get shipping and invoice addresses
            partner_shipping_id = self.find_partner_address_id(
                header, partner_id, "delivery", cache
            )
            partner_invoice_id = self.find_partner_address_id(
                header, partner_id, "invoice", cache
            )

            # Get pricelist based on currency
            pricelist_id = cache["pricelists_map"].get(
                header["doccur"], cache["pricelists_map"].get("CAD")
            )

            # Build quotation values (WITHOUT order_line field)
            vals = {
                "sap_docnum": header["docnum"],
                "sap_docentry": header["docentry"],
                "sap_atcentry": header["atcentry"],
                "partner_id": partner_id,
                "pricelist_id": pricelist_id,
                "partner_invoice_id": partner_invoice_id,
                "partner_shipping_id": partner_shipping_id,
                "payment_term_id": cache["terms_map"].get(header["groupnum"]),
                "date_order": fix_tz(header["docdate"]),
                "commitment_date": fix_tz(header["docduedate"]),
                "client_order_ref": header["numatcard"] or "N/A",
                "picking_policy": "direct" if header["partsupply"] == "Y" else "direct",
                "user_id": cache["users_map"].get(header["slpcode"]),
                "carrier_id": cache["carriers_map"].get(header["trnspcode"]),
                "state": "draft",  # Quotations start in draft state
            }

            order_vals.append(vals)

        _logger.info(f"Transformed {len(order_vals)} quotation headers.")
        return order_vals

    @ETL.load()
    def load_headers(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load quotation headers into Odoo."""
        order_vals = transformed["transform_headers"]

        if order_vals:
            orders = ctx.env["sale.order"].create(order_vals)
            _logger.info(f"Created {len(orders)} quotation headers.")
        else:
            _logger.info("No new quotation headers to create.")


# =============================================================================
# Pipeline 2: Sale Quotation Product Lines (QUT1)
# =============================================================================


@ETL.pipeline(
    target_model="sale.order.line",
    importer_name="sale.quotation.line.importer",
    sap_source="qut1",
    depends_on=[
        "sale.quotation.header.importer",
        "product.product.importer",
        "account.tax.importer",  # Need taxes loaded for line tax mapping
    ],
    multiprocessing_threshold=1000,
    chunk_size=500,
)
class SaleQuotationLineImporter(models.AbstractModel):
    _name = "sale.quotation.line.importer"
    _description = "SAP Sales Quotation Product Line Importer (QUT1)"

    @ETL.extract("qut1")
    def extract_lines(self, ctx: ETLContext) -> List[Dict]:
        """Extract product lines from SAP QUT1 table."""
        # Get existing lines (idempotence)
        ctx.env.cr.execute(
            """
            SELECT DISTINCT sap_docentry, sap_line_num 
            FROM sale_order_line 
            WHERE sap_docentry IS NOT NULL 
            AND sap_line_num != 0
            AND sap_table = 'qut1'
        """
        )
        existing_lines = set(ctx.env.cr.fetchall())
        _logger.info(f"Found {len(existing_lines)} existing product lines.")

        # Get quotations that exist in Odoo
        ctx.env.cr.execute(
            "SELECT DISTINCT sap_docentry FROM sale_order WHERE sap_docentry IS NOT NULL"
        )
        existing_docentries = tuple(row[0] for row in ctx.env.cr.fetchall())

        if not existing_docentries:
            _logger.info("No quotations found in Odoo. Skipping line import.")
            return []

        # Extract lines for existing quotations
        ctx.cr.execute(
            SQL(
                "SELECT * FROM qut1 WHERE docentry IN %s ORDER BY docentry, linenum",
                existing_docentries,
            )
        )
        all_lines = ctx.cr.dictfetchall()

        # Filter out existing lines
        lines = [
            line
            for line in all_lines
            if (line["docentry"], (line["linenum"] or 0) + 2) not in existing_lines
        ]

        _logger.info(
            f"Extracted {len(lines)} new product lines from QUT1 "
            f"(filtered from {len(all_lines)} total)."
        )

        if not lines:
            return ChunkableData(records=[], context={
                "products_map": {},
                "orders_map": {},
                "uom_unit_id": ctx.env.ref("uom.product_uom_unit").id,
            })

        # Group lines by order to prevent concurrent updates
        lines_by_order = {}
        for line in lines:
            docentry = line["docentry"]
            if docentry not in lines_by_order:
                lines_by_order[docentry] = []
            lines_by_order[docentry].append(line)

        _logger.info(f"Grouped into {len(lines_by_order)} quotations.")

        # Pre-compute lookups
        _logger.info("Pre-computing lookup dictionaries...")

        itemcodes = [line["itemcode"] for line in lines]
        products = ctx.env["product.product"].search(
            [("sap_item_code", "in", itemcodes), ("active", "in", [False, True])]
        )
        products_map = {product.sap_item_code: product.id for product in products}

        docentries = list(lines_by_order.keys())
        orders = ctx.env["sale.order"].search([("sap_docentry", "in", docentries)])
        orders_map = {order.sap_docentry: order.id for order in orders}

        # Pre-load all sale taxes for fast lookup
        taxes = ctx.env["account.tax"].search([("type_tax_use", "=", "sale")])
        taxes_map = {tax.sap_tax_code: tax.id for tax in taxes if tax.sap_tax_code}
        _logger.info(f"Pre-loaded {len(taxes_map)} sale taxes for lookup")

        _logger.info("Lookup dictionaries ready.")

        # Return list of orders with their lines
        return ChunkableData(
            records=[
                {"docentry": docentry, "lines": order_lines}
                for docentry, order_lines in lines_by_order.items()
            ],
            context={
                "products_map": products_map,
                "orders_map": orders_map,
                "taxes_map": taxes_map,
                "uom_unit_id": ctx.env.ref("uom.product_uom_unit").id,
            },
        )

    @api.model
    def _lookup_tax(self, ctx, vatgroup, cache):
        """Look up Odoo tax by SAP tax code (vatgroup) using pre-loaded cache.

        Args:
            ctx: ETL context
            vatgroup: SAP tax code (e.g., "CO", "WY 01")
            cache: lookup cache dict containing taxes_map

        Returns:
            account.tax ID or False
        """
        taxes_map = cache.get("taxes_map", {})

        if not taxes_map:
            _logger.error("Tax cache is empty! Taxes were not pre-loaded.")
            return False

        tax_id = taxes_map.get(vatgroup)

        if not tax_id:
            _logger.warning(
                f"Tax not found for SAP vatgroup '{vatgroup}' (sale). "
                f"Available tax codes: {list(taxes_map.keys())[:10]}..."
            )

        return tax_id

    @ETL.transform()
    def transform_lines(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform SAP product lines into Odoo sale.order.line values."""
        data = extracted["extract_lines"]
        orders_with_lines = data.records
        cache = data.context

        line_vals = []
        for order_data in orders_with_lines:
            docentry = order_data["docentry"]
            lines = order_data["lines"]

            order_id = cache["orders_map"].get(docentry)
            if not order_id:
                _logger.warning(
                    f"Skipping {len(lines)} lines: quotation not found for docentry={docentry}"
                )
                continue

            for line in lines:
                product_id = cache["products_map"].get(line["itemcode"])

                # Always derive effective price from SAP's linetotal (the authoritative final amount)
                # rather than trusting price + discprcnt which can have bogus values
                quantity = line["quantity"] if line["quantity"] else 0.0
                linetotal = line.get("linetotal") or 0.0

                if quantity and quantity != 0:
                    price_unit = linetotal / quantity
                else:
                    # Service/expense line: set quantity to 1 and use linetotal as price
                    quantity = 1.0
                    price_unit = linetotal

                vals = {
                    "order_id": order_id,
                    "product_id": product_id if product_id else False,
                    "product_uom_qty": quantity,
                    "price_unit": price_unit,
                    "sap_line_num": (line["linenum"] or 0) + 2,
                    "sap_aftlinenum": 0,
                    "sap_lineseq": 0,
                    "sap_docentry": line["docentry"],
                    "sap_table": "qut1",
                    "sequence": line["linenum"] * 100 if line["linenum"] else 0,
                }

                if not product_id:
                    vals["name"] = line["dscription"] or ""
                    vals["product_uom_id"] = cache["uom_unit_id"]

                # Map SAP tax code (vatgroup) to Odoo tax
                vatgroup = line.get("vatgroup")
                if vatgroup:
                    tax_id = self._lookup_tax(ctx, vatgroup, cache)
                    if tax_id:
                        vals["tax_ids"] = [Command.set([tax_id])]

                line_vals.append(vals)

        _logger.info(f"Transformed {len(line_vals)} product lines.")
        return line_vals

    @ETL.load()
    def load_lines(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load product lines into Odoo."""
        line_vals = transformed["transform_lines"]

        if line_vals:
            lines = ctx.env["sale.order.line"].create(line_vals)
            _logger.info(f"Created {len(lines)} product lines.")
        else:
            _logger.info("No new product lines to create.")


# =============================================================================
# Pipeline 3: Sale Quotation Text Lines (QUT10)
# =============================================================================


@ETL.pipeline(
    target_model="sale.order.line",
    importer_name="sale.quotation.text.line.importer",
    sap_source="qut10",
    depends_on=["sale.quotation.header.importer"],
    multiprocessing_threshold=1000,
    chunk_size=500,
)
class SaleQuotationTextLineImporter(models.AbstractModel):
    _name = "sale.quotation.text.line.importer"
    _description = "SAP Sales Quotation Text Line Importer (QUT10)"

    @ETL.extract("qut10")
    def extract_text_lines(self, ctx: ETLContext) -> List[Dict]:
        """Extract text lines from SAP QUT10 table."""
        # Get existing text lines (idempotence)
        ctx.env.cr.execute(
            """
            SELECT DISTINCT sap_docentry, sap_aftlinenum, sap_lineseq 
            FROM sale_order_line 
            WHERE sap_docentry IS NOT NULL 
            AND sap_aftlinenum != 0
            AND sap_lineseq != 0
            AND sap_table = 'qut10'
        """
        )
        existing_lines = set(ctx.env.cr.fetchall())
        _logger.info(f"Found {len(existing_lines)} existing text lines.")

        # Get quotations that exist in Odoo
        ctx.env.cr.execute(
            "SELECT DISTINCT sap_docentry FROM sale_order WHERE sap_docentry IS NOT NULL"
        )
        existing_docentries = tuple(row[0] for row in ctx.env.cr.fetchall())

        if not existing_docentries:
            _logger.info("No quotations found in Odoo. Skipping text line import.")
            return []

        # Extract text lines for existing quotations
        ctx.cr.execute(
            SQL(
                """
                SELECT * FROM qut10 
                WHERE docentry IN %s 
                AND linetext IS NOT NULL 
                AND linetext <> ''
                ORDER BY docentry, aftlinenum, lineseq
                """,
                existing_docentries,
            )
        )
        all_lines = ctx.cr.dictfetchall()

        # Filter out existing lines
        lines = [
            line
            for line in all_lines
            if (
                line["docentry"],
                (line["aftlinenum"] or 0) + 2,
                (line["lineseq"] or 0) + 2,
            )
            not in existing_lines
        ]

        _logger.info(
            f"Extracted {len(lines)} new text lines from QUT10 "
            f"(filtered from {len(all_lines)} total)."
        )

        if not lines:
            return ChunkableData(records=[], context={
                "orders_map": {},
            })

        # Group lines by order to prevent concurrent updates
        lines_by_order = {}
        for line in lines:
            docentry = line["docentry"]
            if docentry not in lines_by_order:
                lines_by_order[docentry] = []
            lines_by_order[docentry].append(line)

        _logger.info(f"Grouped into {len(lines_by_order)} quotations.")

        # Pre-compute lookups
        _logger.info("Pre-computing lookup dictionaries...")

        docentries = list(lines_by_order.keys())
        orders = ctx.env["sale.order"].search([("sap_docentry", "in", docentries)])
        orders_map = {order.sap_docentry: order.id for order in orders}

        _logger.info("Lookup dictionaries ready.")

        # Return list of orders with their lines
        return ChunkableData(
            records=[
                {"docentry": docentry, "lines": order_lines}
                for docentry, order_lines in lines_by_order.items()
            ],
            context={
                "orders_map": orders_map,
            },
        )

    @ETL.transform()
    def transform_text_lines(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform SAP text lines into Odoo sale.order.line values."""
        data = extracted["extract_text_lines"]
        orders_with_lines = data.records
        cache = data.context

        line_vals = []
        for order_data in orders_with_lines:
            docentry = order_data["docentry"]
            lines = order_data["lines"]

            order_id = cache["orders_map"].get(docentry)
            if not order_id:
                _logger.warning(
                    f"Skipping {len(lines)} text lines: quotation not found for docentry={docentry}"
                )
                continue

            for line in lines:
                vals = {
                    "order_id": order_id,
                    "display_type": "line_note",
                    "name": line["linetext"] or " ",
                    "product_id": False,
                    "product_uom_qty": 0.0,
                    "price_unit": 0.0,
                    "sap_line_num": 0,
                    "sap_aftlinenum": (line["aftlinenum"] or 0) + 2,
                    "sap_lineseq": (line["lineseq"] or 0) + 2,
                    "sap_docentry": line["docentry"],
                    "sap_table": "qut10",
                    "sequence": (
                        line["aftlinenum"] * 100 + line["lineseq"]
                        if line["aftlinenum"] and line["lineseq"]
                        else 0
                    ),
                }

                line_vals.append(vals)

        _logger.info(f"Transformed {len(line_vals)} text lines.")
        return line_vals

    @ETL.load()
    def load_text_lines(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load text lines into Odoo."""
        line_vals = transformed["transform_text_lines"]

        if line_vals:
            lines = ctx.env["sale.order.line"].create(line_vals)
            _logger.info(f"Created {len(lines)} text lines.")
        else:
            _logger.info("No new text lines to create.")
