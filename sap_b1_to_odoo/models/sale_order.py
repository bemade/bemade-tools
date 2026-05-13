from odoo import api, fields, models


class SalesOrder(models.Model):
    _inherit = "sale.order"

    sap_docentry = fields.Integer(index="btree", copy=False)
    sap_docnum = fields.Integer(index="btree", copy=False)
    sap_atcentry = fields.Integer(index="btree", copy=False)
    sap_table = fields.Char(index="btree", copy=False)
    # Source-system DocStatus from SAP ORDR/OQUT: 'O' open, 'C' closed.
    # Closed orders in SAP are treated as fully invoiced even when there is
    # no inv1 row — this matches the manual-close behaviour from SAP.
    sap_docstatus = fields.Char(index="btree", copy=False)

    _sap_docnum_unique = models.Constraint(
        "EXCLUDE USING btree (sap_docnum WITH =) WHERE (sap_docnum != 0)",
        "Another sale order with this docnum already exists when set",
    )

    @api.depends("sap_docstatus")
    def _compute_invoice_status(self):
        super()._compute_invoice_status()
        for order in self.filtered(lambda o: o.sap_docstatus == "C"):
            if order.invoice_status != "invoiced":
                order.invoice_status = "invoiced"


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

    _sap_line_type_check = models.Constraint(
        """CHECK(
            (sap_line_num != 0 AND sap_lineseq = 0 AND sap_aftlinenum = 0) OR
            (sap_line_num = 0 AND sap_aftlinenum != 0 AND sap_lineseq != 0)
        )""",
        "A line must have either a line_num (for product lines) or an aftlinenum (for text lines), but not both.",
    )
    _sap_line_docentry_table_unique = models.Constraint(
        "UNIQUE(sap_line_num, sap_aftlinenum, sap_lineseq, sap_docentry, sap_table)",
        "Another line with this line number and docentry already exists for this SAP table.",
    )

    @api.depends(
        "invoice_lines.move_id.state",
        "invoice_lines.move_id.move_type",
        "invoice_lines.quantity",
        "sap_qty_invoiced",
    )
    def _compute_qty_invoiced(self):
        super()._compute_qty_invoiced()
        # When SAP-imported invoices/credit-memos are linked via sale_line_ids
        # (task 3334 wiring), super() already counts them in qty_invoiced — out_invoice
        # adds, out_refund subtracts.  We want the final qty_invoiced to reflect the
        # SAP-side net (sap_qty_invoiced) instead of those linked AML contributions,
        # so subtract the signed sum of sap-tagged invoice_lines and add sap_qty_invoiced
        # back.  Quantities on account.move.line are unsigned; use move_type to sign.
        sap_lines = self.filtered("sap_qty_invoiced")
        sap_lines.invoice_lines.filtered("sap_docentry").mapped("move_id.move_type")
        for line in sap_lines:
            sap_signed = 0.0
            for aml in line.invoice_lines.filtered("sap_docentry"):
                move = aml.move_id
                if move.state == "cancel" and move.payment_state != "invoicing_legacy":
                    continue
                if move.move_type == "out_invoice":
                    sap_signed += aml.quantity
                elif move.move_type == "out_refund":
                    sap_signed -= aml.quantity
            line.qty_invoiced = line.qty_invoiced - sap_signed + line.sap_qty_invoiced
