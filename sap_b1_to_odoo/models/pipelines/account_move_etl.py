import logging

from odoo import api, models
from odoo.tools.sql import SQL

from odoo.addons.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.move",
    importer_name="account.move.invoice.importer",
    sap_source="oinv",
    depends_on=[
        "account.journal.setup",  # Ensures CoA + journals are ready
        "account.tax.importer",  # Ensures taxes are imported
        "res.partner.company.importer",
        "product.product.importer",
        "res.users.importer",
        "sale.order.post.processor",
    ],
    multiprocessing_threshold=1000,
    chunk_size=50,
)
class AccountMoveInvoiceETLImporter(models.AbstractModel):
    _name = "account.move.invoice.importer"
    _description = "SAP Invoice Importer (ETL, OINV/INV1)"
    _inherit = "sap.account.move.importer.mixin"

    @api.model
    def _get_import_config(self):
        return {
            "header_table": "oinv",
            "line_table": "inv1",
            "move_type": "out_invoice",
            "invoice_status_method": "_compute_invoice_status",
        }

    @api.model
    def _get_order_line_link_config(self):
        return {
            "invoice_line_table": "inv1",
            "order_line_table": "rdr1",
            "picking_table": "dln1",
            "picking_basetype": 15,  # Deliveries have BaseType = 15
            "order_basetype": 17,  # Sales Orders have BaseType = 17
            "order_line_model": "sale.order.line",
        }

    @api.model
    def _get_order_line_link_vals(self, order_line_id):
        """Return values to link invoice line to sale order line."""
        return {"sale_line_ids": [(4, order_line_id)]}

    @ETL.extract("oinv")
    def extract_invoices(self, ctx: ETLContext):
        """Extract all SAP invoices and their lines, excluding already imported ones."""
        already_imported = ctx.env["account.move"].search(
            [
                ("sap_docnum", "!=", False),
                ("sap_table", "=", "oinv"),
            ]
        )

        where = ""
        args = []
        if already_imported:
            where = "WHERE docentry not in %s"
            args = [tuple(already_imported.mapped("sap_docentry"))]

        sql = f"SELECT * FROM oinv {where}" if where else "SELECT * FROM oinv"
        ctx.cr.execute(SQL(sql, *args) if args else sql)
        docs = ctx.cr.dictfetchall()

        if not docs:
            return {"headers": [], "lines": {}}

        lines = self._get_lines(ctx.cr, "inv1", docs)
        lines_dict = {}
        for line in lines:
            lines_dict.setdefault(line["docentry"], []).append(line)

        return {"headers": docs, "lines": lines_dict}

    @ETL.extract("rdr1")
    def extract_metadata(self, ctx: ETLContext):
        """Extract partners, order line links, and all lookups needed for transform."""
        # Get partners as ID dict (picklable for multiprocessing)
        partners = self.env["res.partner"].search_read(
            [("sap_card_code", "!=", False)],
            ["id", "sap_card_code"],
        )
        partners_dict = {p["sap_card_code"]: p["id"] for p in partners}

        order_lines_dict = self._get_order_line_links(ctx.cr)

        # Build all lookups once
        lookups = self._build_lookups()

        return {
            "partners": partners_dict,
            "order_lines": order_lines_dict,
            "lookups": lookups,
        }

    @ETL.transform()
    def transform_invoices(self, ctx: ETLContext, extracted):
        """Transform SAP invoice headers/lines into account.move create vals."""
        data = extracted["extract_invoices"]
        headers = data.get("headers", [])
        lines_dict = data.get("lines", {})

        if not headers:
            return {"move_vals": [], "lookups": {}}

        metadata = extracted["extract_metadata"]
        partners_id_dict = metadata["partners"]
        order_lines_dict = metadata["order_lines"]
        lookups = metadata["lookups"]

        moves_vals = []
        for doc in headers:
            partner_id = partners_id_dict.get(doc["cardcode"])
            if not partner_id:
                _logger.warning(
                    "Could not find partner with cardcode %s", doc["cardcode"]
                )
                continue

            vals = self._get_move_vals(
                doc, partner_id, lines_dict, "oinv", "inv1", order_lines_dict, lookups
            )

            # Skip invoices with no actual accounting lines
            if not vals.get("line_ids"):
                _logger.warning(
                    f"Skipping invoice docentry={doc['docentry']}: no line_ids generated"
                )
                continue

            self._normalize_move_type(vals, "out_invoice", "out_refund")
            moves_vals.append(vals)

        return {"move_vals": moves_vals, "lookups": lookups}

    @ETL.load()
    def load_invoices(self, ctx: ETLContext, transformed):
        """Create and post account.move invoices, then recompute order invoiced qty."""
        data = transformed.get("transform_invoices", {})
        move_vals = data.get("move_vals", [])
        lookups = data.get("lookups", {})

        if not move_vals:
            return

        # Batch-create any pending currency rates before creating moves
        self._create_pending_currency_rates(lookups)

        moves = ctx.env["account.move"].create(move_vals)
        ctx.env.flush_all()
        # Skip Odoo's automatic COGS generation - we've imported COGS from SAP
        moves.with_context(skip_cogs_generation=True).action_post()

    @api.model
    def _trigger_recomputation(self, lines):
        _logger.info(
            f"Triggering recalculation of invoiced quantity for {len(lines)} {lines._name} entries"
        )
        orders = lines.order_id
        lines._compute_qty_invoiced()
        lines._compute_qty_to_invoice()
        _logger.info(
            f"Triggering recalculation of invoice/billing status for {len(orders)} {orders._name}"
        )
        orders._compute_invoice_status()


@ETL.pipeline(
    target_model="sale.order.line",
    importer_name="account.move.invoice.post.processor",
    sap_source="oinv",
    depends_on=["account.move.invoice.importer"],
    allow_multiprocessing=False,
)
class AccountMoveInvoicePostProcessor(models.AbstractModel):
    _name = "account.move.invoice.post.processor"
    _description = "Invoice Post-Processor - Update Order Line Invoiced Quantities"
    _inherit = "sap.account.move.importer.mixin"

    def _get_order_line_link_config(self):
        """Return config for linking invoice lines to sale order lines."""
        return {
            "order_line_model": "sale.order.line",
            "invoice_line_table": "inv1",
            "order_line_table": "rdr1",
            "picking_table": "dln1",
            "picking_basetype": "13",
            "order_basetype": "17",
        }

    @api.model
    def _trigger_recomputation(self, lines):
        """Trigger recomputation of invoiced quantities and order status."""
        _logger.info(
            f"Triggering recalculation of invoiced quantity for {len(lines)} {lines._name} entries"
        )
        orders = lines.order_id
        lines._compute_qty_invoiced()
        lines._compute_qty_to_invoice()
        _logger.info(
            f"Triggering recalculation of invoice/billing status for {len(orders)} {orders._name}"
        )
        orders._compute_invoice_status()

    @ETL.extract("oinv")
    def extract_for_post_processing(self, ctx: ETLContext):
        """Trivial extract - just return empty dict to satisfy ETL contract."""
        return {}

    @ETL.transform()
    def transform_for_post_processing(self, ctx: ETLContext, extracted):
        """Trivial transform - pass through to satisfy ETL contract."""
        return {}

    @ETL.load()
    def update_order_invoiced_qty(self, ctx: ETLContext, transformed):
        """Update invoiced quantities on sale order lines after all invoices are imported."""
        _logger.info(
            "Post-processing invoices - updating order line invoiced quantities"
        )
        self.import_order_invoiced_qty(ctx.cr)
