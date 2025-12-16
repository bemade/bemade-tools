from odoo import api, fields, models


class SalesOrder(models.Model):
    _inherit = "sale.order"

    sap_docentry = fields.Integer(index="btree", copy=False)
    sap_docnum = fields.Integer(index="btree", copy=False)
    sap_atcentry = fields.Integer(index="btree", copy=False)

    _sql_constraints = [
        (
            "sap_docnum_unique",
            "EXCLUDE USING btree (sap_docnum WITH =) WHERE (sap_docnum != 0)",
            "Another sale order with this docnum already exists when set",
        )
    ]


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"
    sap_line_num = fields.Integer(index="btree", copy=False)
    sap_aftlinenum = fields.Integer(index="btree", copy=False)
    sap_lineseq = fields.Integer(index="btree", copy=False)
    sap_docentry = fields.Integer(
        index="btree", related="order_id.sap_docentry", store=True, copy=False
    )
    sap_table = fields.Char(index="btree", copy=False)
    sap_qty_invoiced = fields.Float(copy=False)

    _sql_constraints = [
        (
            "sap_line_type_check",
            """CHECK(
                (sap_line_num != 0 AND sap_lineseq = 0 AND sap_aftlinenum = 0) OR  -- Product lines have line_num
                (sap_line_num = 0 AND sap_aftlinenum != 0 AND sap_lineseq != 0)     -- Text lines have aftlinenum and lineseq
            )""",
            "A line must have either a line_num (for product lines) or an aftlinenum (for text lines), but not both.",
        ),
        (
            "sap_line_docentry_table_unique",
            "UNIQUE(sap_line_num, sap_aftlinenum, sap_lineseq, sap_docentry, sap_table)",
            "Another line with this line number and docentry already exists for this SAP table.",
        ),
    ]

    @api.depends("invoice_lines.move_id.state", "invoice_lines.quantity")
    def _compute_qty_invoiced(self):
        super()._compute_qty_invoiced()
        # Pre-fetch quantities
        sap_lines = self.filtered("sap_qty_invoiced")
        # Prefetch the quantity instead of running one query per line later
        _ = sap_lines.invoice_lines.filtered("sap_docentry").mapped("quantity")
        for line in sap_lines:
            open_sap_qty = line.invoice_lines.filtered("sap_docentry").mapped(
                "quantity"
            )
            line.qty_invoiced += line.sap_qty_invoiced - sum(open_sap_qty)
