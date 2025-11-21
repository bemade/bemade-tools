import logging

from odoo import api, models
from odoo.tools.sql import SQL

from odoo.addons.sap_b1_to_odoo.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.move",
    importer_name="account.move.invoice.importer",
    sap_source="oinv",
    depends_on=[
        "account.journal.setup",  # Ensures CoA + journals are ready
        "res.partner.company.importer",
        "product.product.importer",
        "res.users.importer",
        "sale.order.post.processor",
    ],
    allow_multiprocessing=False,
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

    @ETL.extract("oinv")
    def extract_invoices(self, ctx: ETLContext):
        """Extract open SAP invoices and their lines, excluding already imported ones."""
        already_imported = ctx.env["account.move"].search(
            [
                ("sap_docnum", "!=", False),
                ("sap_table", "=", "oinv"),
            ]
        )

        where = "WHERE docstatus='O'"
        args = []
        if already_imported:
            where += " AND docentry not in %s"
            args = [tuple(already_imported.mapped("sap_docentry"))]

        ctx.cr.execute(SQL(f"SELECT * FROM oinv {where}", *args))
        open_docs = ctx.cr.dictfetchall()

        if not open_docs:
            return {"headers": [], "lines": {}}

        lines = self._get_lines(ctx.cr, "inv1", open_docs)
        lines_dict = {}
        for line in lines:
            lines_dict.setdefault(line["docentry"], []).append(line)

        return {"headers": open_docs, "lines": lines_dict}

    @ETL.transform()
    def transform_invoices(self, ctx: ETLContext, extracted):
        """Transform SAP invoice headers/lines into account.move create vals."""
        data = extracted["extract_invoices"]
        headers = data.get("headers", [])
        lines_dict = data.get("lines", {})

        if not headers:
            return []

        partners_dict = self._get_partners_dict()
        order_lines_dict = self._get_order_line_links(ctx.cr)

        moves_vals = []
        _logger.info(f"Creating {len(headers)} out_invoice moves via ETL...")
        for doc in headers:
            partner = partners_dict.get(doc["cardcode"])
            if not partner:
                _logger.warning(
                    "Could not find partner with cardcode %s", doc["cardcode"]
                )
                continue

            vals = self._get_move_vals(
                doc, partner, lines_dict, "inv1", order_lines_dict
            )
            vals.update({"move_type": "out_invoice"})
            moves_vals.append(vals)

        return moves_vals

    @ETL.load()
    def load_invoices(self, ctx: ETLContext, transformed):
        """Create and post account.move invoices, then recompute order invoiced qty."""
        move_vals = transformed.get("transform_invoices", [])
        if not move_vals:
            _logger.info("No SAP invoices to import via ETL.")
            return

        moves = ctx.env["account.move"].create(move_vals)
        _logger.info(
            "Created %s account.move invoices with %s lines via ETL.",
            len(moves),
            len(moves.mapped("line_ids")),
        )
        moves.action_post()

        self.import_order_invoiced_qty(ctx.cr)

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
