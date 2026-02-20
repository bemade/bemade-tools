"""Purchase Order ETL Pipelines

This module contains 4 ETL pipelines for importing purchases orders from SAP B1:
1. purchase Order Headers (OPOR) - Creates purchase.order records without lines
2. Product Lines (POR1) - Creates purchase.order.line records for products
3. Text Lines (POR10) - Creates purchase.order.line records for text/notes
4. Post-Processor - Confirms orders, sets quantities, validates pickings, etc.

This replaces the legacy SapPurchaseOrderImporter with a declarative ETL approach.
"""

import logging
from typing import Dict, List, Any
from fuzzywuzzy import process

from odoo import api, models, Command
from odoo.tools.sql import SQL

from odoo.addons.etl_framework import ETL, ETLContext
from odoo.addons.sap_b1_to_odoo.tools import fix_tz

_logger = logging.getLogger(__name__)


# =============================================================================
# Pipeline 1: purchase Order Headers (OPOR)
# =============================================================================


@ETL.pipeline(
    target_model="purchase.order",
    importer_name="purchase.order.header.importer",
    sap_source="opor",
    depends_on=[
        "res.partner.company.importer",
        "account.payment.term.importer",
        "res.users.importer",
    ],
    multiprocessing_threshold=500,
    chunk_size=500,
)
class PurchaseOrderHeaderImporter(models.AbstractModel):
    _name = "purchase.order.header.importer"
    _description = "SAP Purchase Order Header Importer (OPOR)"
    _inherit = "sale.purchase.order.etl.mixin"

    _lookup_cache = {}

    @ETL.extract("opor")
    def extract_headers(self, ctx: ETLContext) -> List[Dict]:
        """Extract purchases order headers from SAP OPOR table."""
        # Uppercase cardcodes for consistency
        ctx.cr.execute("UPDATE opor SET cardcode = UPPER(cardcode)")

        # Get existing orders (idempotence) - use docentry as unique key
        ctx.env.cr.execute(
            "SELECT DISTINCT sap_docentry FROM purchase_order WHERE sap_docentry IS NOT NULL"
        )
        existing_docentries = tuple(row[0] for row in ctx.env.cr.fetchall())
        _logger.info(f"Found {len(existing_docentries)} existing purchases orders.")

        # Extract new order headers
        sql = "SELECT * FROM opor"
        if existing_docentries:
            sql += " WHERE docentry NOT IN %s"
            ctx.cr.execute(SQL(sql, existing_docentries))
        else:
            ctx.cr.execute(sql)

        headers = ctx.cr.dictfetchall()
        _logger.info(f"Extracted {len(headers)} new order headers from OPOR.")

        if headers:
            _logger.info(
                f"Sample header cardcodes: {[h['cardcode'] for h in headers[:5]]}"
            )

        if not headers:
            # Initialize empty cache for transform phase
            PurchaseOrderHeaderImporter._lookup_cache = {
                "partners_map": {},
                "partner_addresses_map": {},
                "contacts_map": {},
                "users_map": {},
                "terms_map": {},
                "pricelists_map": {},
                "carriers_map": {},
                "company_id": ctx.env.company.id,
            }
            return []

        # Pre-compute lookups
        _logger.info("Pre-computing lookup dictionaries...")

        # Partners, contacts, users, terms, pricelists, carriers
        cardcodes = [h["cardcode"] for h in headers]
        partners = ctx.env["res.partner"].search(
            [("sap_card_code", "in", cardcodes), ("active", "in", [True, False])]
        )
        partners_map = {partner.sap_card_code: partner.id for partner in partners}
        _logger.info(
            f"Found {len(partners)} partners out of {len(cardcodes)} cardcodes"
        )

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

        groupnums = [h["groupnum"] for h in headers if h.get("groupnum")]
        terms = ctx.env["account.payment.term"].search(
            [("sap_groupnum", "in", groupnums)]
        )
        terms_map = {term.sap_groupnum: term.id for term in terms}

        carriers = ctx.env["sap.transporter"].search([])
        carriers_map = {
            tpt.sap_trnspcode: tpt.delivery_carrier_id.id
            for tpt in carriers
            if tpt.delivery_carrier_id
        }

        # Store in cache
        PurchaseOrderHeaderImporter._lookup_cache = {
            "partners_map": partners_map,
            "partner_addresses_map": partner_addresses_map,
            "contacts_map": contacts_map,
            "terms_map": terms_map,
            "carriers_map": carriers_map,
            "company_id": ctx.env.company.id,
        }
        _logger.info("Lookup dictionaries ready.")

        return headers

    @ETL.transform()
    def transform_headers(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform SAP order headers into Odoo purchase.order values."""
        headers = extracted["extract_headers"]
        cache = PurchaseOrderHeaderImporter._lookup_cache

        if not headers:
            _logger.info("No headers to transform.")
            return []

        order_vals = []
        for header in headers:
            # Get partner ID (contact or company)
            partner_id = self.get_partner_id(header, cache)
            if not partner_id:
                _logger.warning(
                    f"Skipping order {header['docnum']}: partner not found "
                    f"(cardcode={header['cardcode']}, cntctcode={header.get('cntctcode')})"
                )
                continue

            # For purchase orders, use commercial partner (from legacy code)
            partner_data = cache["partner_addresses_map"].get(partner_id, {})
            commercial_id = partner_data.get("commercial_id", partner_id)

            order_date = fix_tz(header["docdate"])

            # Build order values (WITHOUT order_line field)
            # Fields match legacy _get_order_vals in purchase_order.py
            vals = {
                "sap_docnum": header["docnum"],
                "sap_docentry": header["docentry"],
                "sap_atcentry": header["atcentry"],
                "partner_id": commercial_id,
                "payment_term_id": cache["terms_map"].get(header["groupnum"]),
                "date_approve": order_date,
                "date_order": order_date,
                "date_planned": fix_tz(header["docduedate"]),
                "note": f"SAP Order {header['numatcard']}",
                "carrier_id": cache["carriers_map"].get(header["trnspcode"]),
            }

            order_vals.append(vals)

        _logger.info(f"Transformed {len(order_vals)} order headers.")
        return order_vals

    @ETL.load()
    def load_headers(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load order headers into Odoo."""
        order_vals = transformed["transform_headers"]

        if order_vals:
            orders = ctx.env["purchase.order"].create(order_vals)
            _logger.info(f"Created {len(orders)} order headers.")
        else:
            _logger.info("No new order headers to create.")


# =============================================================================
# Pipeline 2: purchase Order Product Lines (POR1)
# =============================================================================


@ETL.pipeline(
    target_model="purchase.order.line",
    importer_name="purchase.order.line.importer",
    sap_source="por1",
    depends_on=[
        "purchase.order.header.importer",
        "product.product.importer",
        "account.tax.importer",  # Need taxes loaded for line tax mapping
    ],
    multiprocessing_threshold=1000,
    chunk_size=500,
)
class PurchaseOrderLineImporter(models.AbstractModel):
    _name = "purchase.order.line.importer"
    _description = "SAP Purchase Order Product Line Importer (POR1)"

    _lookup_cache = {}

    @ETL.extract("por1")
    def extract_lines(self, ctx: ETLContext) -> List[Dict]:
        """Extract product lines from SAP POR1 table."""
        # Get existing lines (idempotence)
        ctx.env.cr.execute(
            """
            SELECT DISTINCT sap_docentry, sap_line_num 
            FROM purchase_order_line 
            WHERE sap_docentry IS NOT NULL 
            AND sap_line_num != 0
            AND sap_table = 'por1'
        """
        )
        existing_lines = set(ctx.env.cr.fetchall())
        _logger.info(f"Found {len(existing_lines)} existing product lines.")

        # Get orders that exist in Odoo
        ctx.env.cr.execute(
            "SELECT DISTINCT sap_docentry FROM purchase_order WHERE sap_docentry IS NOT NULL"
        )
        existing_docentries = tuple(row[0] for row in ctx.env.cr.fetchall())

        if not existing_docentries:
            _logger.info("No orders found in Odoo. Skipping line import.")
            return []

        # Extract lines for existing orders
        ctx.cr.execute(
            SQL(
                "SELECT * FROM por1 WHERE docentry IN %s ORDER BY docentry, linenum",
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
            f"Extracted {len(lines)} new product lines from POR1 "
            f"(filtered from {len(all_lines)} total)."
        )

        if not lines:
            # Initialize empty cache for transform phase
            PurchaseOrderLineImporter._lookup_cache = {
                "products_map": {},
                "orders_map": {},
                "uom_unit_id": ctx.env.ref("uom.product_uom_unit").id,
            }
            return []

        # Group lines by order to prevent concurrent updates
        lines_by_order = {}
        for line in lines:
            docentry = line["docentry"]
            if docentry not in lines_by_order:
                lines_by_order[docentry] = []
            lines_by_order[docentry].append(line)

        _logger.info(f"Grouped into {len(lines_by_order)} orders.")

        # Pre-compute lookups
        _logger.info("Pre-computing lookup dictionaries...")

        itemcodes = [line["itemcode"] for line in lines]
        products = ctx.env["product.product"].search(
            [("sap_item_code", "in", itemcodes), ("active", "in", [False, True])]
        )
        products_map = {product.sap_item_code: product.id for product in products}

        docentries = list(lines_by_order.keys())
        orders = ctx.env["purchase.order"].search([("sap_docentry", "in", docentries)])
        orders_map = {order.sap_docentry: order.id for order in orders}

        # Pre-load all purchase taxes for fast lookup
        taxes = ctx.env["account.tax"].search([("type_tax_use", "=", "purchase")])
        taxes_map = {tax.sap_tax_code: tax.id for tax in taxes if tax.sap_tax_code}
        _logger.info(f"Pre-loaded {len(taxes_map)} purchase taxes for lookup")

        PurchaseOrderLineImporter._lookup_cache = {
            "products_map": products_map,
            "orders_map": orders_map,
            "taxes_map": taxes_map,
            "uom_unit_id": ctx.env.ref("uom.product_uom_unit").id,
        }
        _logger.info("Lookup dictionaries ready.")

        # Return list of orders with their lines
        return [
            {"docentry": docentry, "lines": order_lines}
            for docentry, order_lines in lines_by_order.items()
        ]

    @api.model
    def _lookup_tax(self, ctx, vatgroup):
        """Look up Odoo tax by SAP tax code (vatgroup) using pre-loaded cache.

        Args:
            ctx: ETL context
            vatgroup: SAP tax code (e.g., "CO", "WY 01")

        Returns:
            account.tax ID or False
        """
        cache = PurchaseOrderLineImporter._lookup_cache
        taxes_map = cache.get("taxes_map", {})

        if not taxes_map:
            _logger.error("Tax cache is empty! Taxes were not pre-loaded.")
            return False

        tax_id = taxes_map.get(vatgroup)

        if not tax_id:
            _logger.warning(
                f"Tax not found for SAP vatgroup '{vatgroup}' (purchase). "
                f"Available tax codes: {list(taxes_map.keys())[:10]}..."
            )

        return tax_id

    @ETL.transform()
    def transform_lines(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform SAP product lines into Odoo purchase.order.line values."""
        orders_with_lines = extracted["extract_lines"]
        cache = PurchaseOrderLineImporter._lookup_cache

        if not cache:
            raise RuntimeError("Cache is empty in transform!")

        line_vals = []
        for order_data in orders_with_lines:
            docentry = order_data["docentry"]
            lines = order_data["lines"]

            order_id = cache["orders_map"].get(docentry)
            if not order_id:
                _logger.warning(
                    f"Skipping {len(lines)} lines: order not found for docentry={docentry}"
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
                    "product_qty": quantity,
                    "price_unit": price_unit,
                    "sap_line_num": (line["linenum"] or 0) + 2,
                    "sap_aftlinenum": 0,
                    "sap_lineseq": 0,
                    "sap_docentry": line["docentry"],
                    "sap_table": "por1",
                    "sequence": line["linenum"] * 100 if line["linenum"] else 0,
                }

                if not product_id:
                    vals["name"] = line["dscription"] or ""
                    vals["product_uom_id"] = cache["uom_unit_id"]

                # Map SAP tax code (vatgroup) to Odoo tax
                vatgroup = line.get("vatgroup")
                if vatgroup:
                    tax_id = self._lookup_tax(ctx, vatgroup)
                    if tax_id:
                        vals["taxes_id"] = [Command.set([tax_id])]

                line_vals.append(vals)

        _logger.info(f"Transformed {len(line_vals)} product lines.")
        return line_vals

    @ETL.load()
    def load_lines(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load product lines into Odoo."""
        line_vals = transformed["transform_lines"]

        if line_vals:
            lines = ctx.env["purchase.order.line"].create(line_vals)
            _logger.info(f"Created {len(lines)} product lines.")
        else:
            _logger.info("No new product lines to create.")


# =============================================================================
# Pipeline 3: purchase Order Text Lines (POR10)
# =============================================================================


@ETL.pipeline(
    target_model="purchase.order.line",
    importer_name="purchase.order.text.line.importer",
    sap_source="por10",
    depends_on=["purchase.order.header.importer"],
    multiprocessing_threshold=1000,
    chunk_size=500,
)
class PurchaseOrderTextLineImporter(models.AbstractModel):
    _name = "purchase.order.text.line.importer"
    _description = "SAP Purchase Order Text Line Importer (POR10)"

    _lookup_cache = {}

    @ETL.extract("por10")
    def extract_text_lines(self, ctx: ETLContext) -> List[Dict]:
        """Extract text lines from SAP POR10 table."""
        # Get existing text lines (idempotence)
        ctx.env.cr.execute(
            """
            SELECT DISTINCT sap_docentry, sap_aftlinenum, sap_lineseq 
            FROM purchase_order_line 
            WHERE sap_docentry IS NOT NULL 
            AND sap_aftlinenum != 0
            AND sap_lineseq != 0
            AND sap_table = 'por10'
        """
        )
        existing_lines = set(ctx.env.cr.fetchall())
        _logger.info(f"Found {len(existing_lines)} existing text lines.")

        # Get orders that exist in Odoo
        ctx.env.cr.execute(
            "SELECT DISTINCT sap_docentry FROM purchase_order WHERE sap_docentry IS NOT NULL"
        )
        existing_docentries = tuple(row[0] for row in ctx.env.cr.fetchall())

        if not existing_docentries:
            _logger.info("No orders found in Odoo. Skipping text line import.")
            return []

        # Extract text lines for existing orders
        ctx.cr.execute(
            SQL(
                """
                SELECT * FROM por10 
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
            f"Extracted {len(lines)} new text lines from POR10 "
            f"(filtered from {len(all_lines)} total)."
        )

        if not lines:
            # Initialize empty cache for transform phase
            PurchaseOrderTextLineImporter._lookup_cache = {
                "orders_map": {},
            }
            return []

        # Group lines by order to prevent concurrent updates
        lines_by_order = {}
        for line in lines:
            docentry = line["docentry"]
            if docentry not in lines_by_order:
                lines_by_order[docentry] = []
            lines_by_order[docentry].append(line)

        _logger.info(f"Grouped into {len(lines_by_order)} orders.")

        # Pre-compute lookups
        _logger.info("Pre-computing lookup dictionaries...")

        docentries = list(lines_by_order.keys())
        orders = ctx.env["purchase.order"].search([("sap_docentry", "in", docentries)])
        orders_map = {order.sap_docentry: order.id for order in orders}

        PurchaseOrderTextLineImporter._lookup_cache = {
            "orders_map": orders_map,
        }
        _logger.info("Lookup dictionaries ready.")

        # Return list of orders with their lines
        return [
            {"docentry": docentry, "lines": order_lines}
            for docentry, order_lines in lines_by_order.items()
        ]

    @ETL.transform()
    def transform_text_lines(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform SAP text lines into Odoo purchase.order.line values."""
        orders_with_lines = extracted["extract_text_lines"]
        cache = PurchaseOrderTextLineImporter._lookup_cache

        if not cache:
            raise RuntimeError("Cache is empty in transform!")

        line_vals = []
        for order_data in orders_with_lines:
            docentry = order_data["docentry"]
            lines = order_data["lines"]

            order_id = cache["orders_map"].get(docentry)
            if not order_id:
                _logger.warning(
                    f"Skipping {len(lines)} text lines: order not found for docentry={docentry}"
                )
                continue

            for line in lines:
                vals = {
                    "order_id": order_id,
                    "display_type": "line_note",
                    "name": line["linetext"] or " ",
                    "product_id": False,
                    "product_qty": 0.0,
                    "price_unit": 0.0,
                    "sap_line_num": 0,
                    "sap_aftlinenum": (line["aftlinenum"] or 0) + 2,
                    "sap_lineseq": (line["lineseq"] or 0) + 2,
                    "sap_docentry": line["docentry"],
                    "sap_table": "por10",
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
            lines = ctx.env["purchase.order.line"].create(line_vals)
            _logger.info(f"Created {len(lines)} text lines.")
        else:
            _logger.info("No new text lines to create.")


# =============================================================================
# Pipeline 4: Purchase Order Post-Processor
# =============================================================================


@ETL.pipeline(
    target_model="purchase.order",
    importer_name="purchase.order.post.processor",
    sap_source="opor",
    depends_on=[
        "purchase.order.line.importer",
        "purchase.order.text.line.importer",
    ],
    allow_multiprocessing=False,
)
class PurchaseOrderPostProcessor(models.AbstractModel):
    _name = "purchase.order.post.processor"
    _description = "SAP Purchase Order Post-Processor"

    @ETL.extract("opor")
    def extract_sap_order_data(self, ctx: ETLContext) -> Dict[str, Any]:
        """Extract SAP order status data needed for post-processing."""
        _logger.info("Extracting SAP order data for post-processing...")

        # Get closed orders (confirmed and closed, no delivery)
        ctx.cr.execute(
            """
            SELECT docnum FROM opor 
            WHERE docstatus = 'C' 
            AND invntsttus = 'C' 
            AND canceled = 'N'
            """
        )
        closed_orders = [row[0] for row in ctx.cr.fetchall()]

        # Get open orders (to confirm)
        ctx.cr.execute(
            """
            SELECT docnum FROM opor 
            WHERE canceled='N' AND confirmed='Y' 
            AND (
                (docstatus='O' AND invntsttus='O')
                OR docstatus='C'
            )
            """
        )
        open_orders = [row[0] for row in ctx.cr.fetchall()]

        # Get canceled orders
        ctx.cr.execute(
            """
            SELECT docnum FROM opor
            WHERE canceled = 'Y' 
            OR (confirmed='N' AND (docstatus='C' OR invntsttus='C'))
            """
        )
        canceled_orders = [row[0] for row in ctx.cr.fetchall()]

        # Get SAP line quantities for pickings validation
        ctx.cr.execute(
            """
            SELECT o.docnum, l.itemcode, l.linenum, 
                   (l.quantity - l.openqty) as quantity
            FROM opor o
            JOIN por1 l ON l.docentry = o.docentry
            WHERE o.docstatus = 'O'
              AND o.canceled = 'N'
              AND o.confirmed = 'Y'
            ORDER BY o.docnum, l.linenum
            """
        )
        sap_line_quantities = ctx.cr.dictfetchall()

        # Get order dates
        ctx.cr.execute("SELECT docnum, docdate, createdate FROM opor")
        order_dates = ctx.cr.fetchall()

        _logger.info(
            f"Extracted: {len(closed_orders)} closed, {len(open_orders)} open, "
            f"{len(canceled_orders)} canceled, {len(sap_line_quantities)} line quantities, "
            f"{len(order_dates)} order dates"
        )

        return {
            "closed_orders": closed_orders,
            "open_orders": open_orders,
            "canceled_orders": canceled_orders,
            "sap_line_quantities": sap_line_quantities,
            "order_dates": order_dates,
        }

    @ETL.transform()
    def transform_sap_data(self, ctx: ETLContext, extracted: Dict) -> Dict:
        """Trivial transform, just pass along extracted data."""
        return extracted.get("extract_sap_order_data", {})

    @ETL.load()
    def post_process_orders(self, ctx: ETLContext, transformed: Dict) -> None:
        """Post-process all purchases orders using extracted SAP data."""
        # Get extracted SAP data (no transform phase, so it's in transformed dict)
        sap_data = transformed.get("transform_sap_data", {})

        _logger.info("Starting post-processing of purchases orders...")

        _logger.info("Confirming closed orders (no delivery order)...")
        self._confirm_closed_orders(sap_data.get("closed_orders", []))

        _logger.info("Setting delivered quantities for closed orders...")
        self._set_delivered_qty_for_closed_orders(sap_data.get("closed_orders", []))

        _logger.info("Confirming open orders...")
        self._confirm_open_orders(ctx, sap_data.get("open_orders", []))

        _logger.info("Canceling canceled orders...")
        self._cancel_canceled_orders(sap_data.get("canceled_orders", []))

        _logger.info("Recomputing delivery status for all orders...")
        self._recompute_receipt_status()

        _logger.info("Validating pickings with SAP quantities...")
        self._validate_pickings_with_sap_quantities(
            sap_data.get("sap_line_quantities", [])
        )

        _logger.info("Setting order dates...")
        self._set_order_dates(sap_data.get("order_dates", []))

        _logger.info("Post-processing complete.")

    @api.model
    def _confirm_closed_orders(self, closed_orders):
        """Mark orders as confirmed and closed (no delivery order)."""
        if closed_orders:
            _logger.info(
                f"Marking {len(closed_orders)} orders as confirmed and closed."
            )
            self.env.flush_all()
            self.env.cr.commit()
            self.env.cr.execute(
                SQL(
                    "UPDATE purchase_order SET state = 'purchase' WHERE sap_docnum IN %s",
                    tuple(closed_orders),
                )
            )

    @api.model
    def _set_delivered_qty_for_closed_orders(self, closed_orders):
        """Set delivered quantities equal to ordered quantities for closed orders."""
        if not closed_orders:
            return

        _logger.info(f"Setting delivered quantities for {len(closed_orders)} orders")

        orders = self.env["purchase.order"].search(
            [("sap_docnum", "in", closed_orders)]
        )

        for order in orders:
            for line in order.order_line:
                if line.product_id:
                    line.write(
                        {
                            "qty_received": line.product_qty,
                            "qty_received_method": "manual",
                        }
                    )

        self.env.cr.commit()

    @api.model
    def _confirm_open_orders(self, ctx, open_orders):
        """Confirm open orders (creates delivery orders)."""

        # Disable automations during confirmation
        active_automations = self.env["base.automation"].search([("active", "=", True)])
        active_automations.active = False
        self.env["base.automation"].flush_model()

        if open_orders:
            _logger.info(f"Confirming {len(open_orders)} open orders")
            orders = self.env["purchase.order"].search(
                [("sap_docnum", "in", open_orders), ("state", "in", ["draft", "sent"])]
            )
            for order in orders:
                with ctx.skippable(f"confirm PO {order.name}"):
                    order.button_confirm()
            self.env.cr.commit()

        active_automations.active = True

    @api.model
    def _cancel_canceled_orders(self, canceled_orders):
        """Mark canceled orders as cancelled."""
        if canceled_orders:
            _logger.info(f"Cancelling {len(canceled_orders)} cancelled orders")
            self.env.cr.execute(
                SQL(
                    "UPDATE purchase_order SET state='cancel' WHERE sap_docnum IN %s",
                    tuple(canceled_orders),
                )
            )

    @api.model
    def _recompute_receipt_status(self):
        """Recompute delivery status for all orders."""
        self.env.flush_all()
        self.env.cr.execute(
            """
            UPDATE purchase_order
            SET receipt_status = CASE
                WHEN NOT EXISTS (
                    SELECT 1 FROM purchase_order_line
                    WHERE purchase_order_line.order_id = purchase_order.id
                      AND purchase_order_line.product_qty != purchase_order_line.qty_received
                )
                THEN 'full'
                WHEN EXISTS (
                    SELECT 1 FROM purchase_order_line
                    WHERE purchase_order_line.order_id = purchase_order.id
                      AND purchase_order_line.qty_received > 0
                )
                THEN 'partial'
                ELSE 'pending'
            END
            WHERE sap_docentry IS NOT NULL
            """
        )
        self.env.cr.commit()

    @api.model
    def _validate_pickings_with_sap_quantities(self, sap_lines):
        """Validate stock pickings based on SAP delivered quantities."""
        if not sap_lines:
            return

        # Group lines by order
        order_lines = {}
        for line in sap_lines:
            if line["docnum"] not in order_lines:
                order_lines[line["docnum"]] = []
            order_lines[line["docnum"]].append(line)

        # Get corresponding Odoo orders
        orders = self.env["purchase.order"].search(
            [
                ("sap_docnum", "in", list(order_lines.keys())),
                ("state", "=", "purchase"),
            ]
        )

        # Process each order's pickings
        for order in orders:
            pickings = order.picking_ids.filtered(
                lambda p: p.state in ["waiting", "confirmed", "assigned"]
            )
            if not pickings:
                continue

            sap_lines = order_lines[order.sap_docnum]
            for picking in pickings:
                for move in picking.move_ids:
                    order_line = move.purchase_line_id
                    if not order_line:
                        move.quantity = 0
                        continue

                    # Find corresponding SAP line
                    sap_line = next(
                        (
                            l
                            for l in sap_lines
                            if l["linenum"] + 2 == order_line.sap_line_num
                        ),
                        None,
                    )
                    if sap_line:
                        move.quantity = sap_line["quantity"]

                # Validate picking if any moves have quantities
                if any(move.quantity > 0 for move in picking.move_ids):
                    picking.with_context(skip_backorder=True).button_validate()

        _logger.info(
            f"Validated pickings for {len(orders)} orders based on SAP quantities"
        )

    @api.model
    def _set_order_dates(self, sap_orders):
        """Set order dates from SAP."""
        if not sap_orders:
            return

        # Create temp table
        self.env.cr.execute("DROP TABLE IF EXISTS sap_order_dates")
        self.env.cr.execute(
            "CREATE TEMP TABLE sap_order_dates (docnum INT, docdate TIMESTAMP, createdate TIMESTAMP)"
        )

        # Insert values
        values = [
            (
                order[0],
                fix_tz(order[1]) if order[1] else None,
                fix_tz(order[2]) if order[2] else None,
            )
            for order in sap_orders
        ]
        insert_query = b",".join(
            self.env.cr.mogrify("(%s, %s, %s)", value) for value in values
        ).decode("utf-8")
        self.env.cr.execute(
            f"INSERT INTO sap_order_dates (docnum, docdate, createdate) VALUES {insert_query}"
        )

        # Update orders
        self.env.cr.execute(
            """
            UPDATE purchase_order orders
            SET create_date=temp.createdate, date_approve=temp.docdate
            FROM sap_order_dates temp
            WHERE orders.sap_docnum=temp.docnum
            """
        )
        self.env.cr.commit()
