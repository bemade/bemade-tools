from odoo import models, fields, api


class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    sap_docentry = fields.Integer(
        index="btree", string="SAP Document Entry", copy=False
    )
    sap_docnum = fields.Integer(index="btree", string="SAP Document Number", copy=False)
    sap_atcentry = fields.Integer(index="btree", copy=False)

    _sap_docentry_unique = models.Constraint(
        "EXCLUDE USING btree (sap_docentry WITH =) WHERE (sap_docentry != 0)",
        "SAP docentry must be unique when set!",
    )


class PurchaseOrderLine(models.Model):
    _inherit = "purchase.order.line"

    sap_line_num = fields.Integer(
        index="btree",
        copy=False,
    )
    sap_aftlinenum = fields.Integer(
        index="btree",
        copy=False,
    )
    sap_lineseq = fields.Integer(
        index="btree",
        copy=False,
    )
    sap_docentry = fields.Integer(
        related="order_id.sap_docentry",
        store=True,
        index="btree",
        copy=False,
    )
    sap_table = fields.Char(
        index="btree",
        copy=False,
    )
    sap_qty_invoiced = fields.Float()

    _sap_line_type_check = models.Constraint(
        """CHECK(
            (sap_line_num != 0 AND sap_lineseq = 0 AND sap_aftlinenum = 0) OR
            (sap_line_num = 0 AND sap_lineseq != 0 AND sap_aftlinenum != 0)
        )""",
        "A line must have either a line_num (for product lines) or an aftlinenum (for text lines), but not both.",
    )
    _sap_line_docentry_table_unique = models.Constraint(
        "UNIQUE(sap_line_num, sap_aftlinenum, sap_lineseq, sap_docentry, sap_table)",
        "Another line with this line number and docentry already exists for this SAP table.",
    )

    @api.depends(
        "invoice_lines.move_id.state",
        "invoice_lines.quantity",
        "qty_received",
        "product_uom_qty",
        "order_id.state",
        "sap_qty_invoiced",
    )
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
            # Purchase order lines compute this second field right here
            line.qty_to_invoice -= line.sap_qty_invoiced
