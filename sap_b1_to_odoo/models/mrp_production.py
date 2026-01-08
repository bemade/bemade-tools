from odoo import fields, models


class MrpProduction(models.Model):
    _inherit = "mrp.production"

    sap_docentry = fields.Integer("SAP DocEntry", index=True, copy=False)
    sap_docnum = fields.Char("SAP DocNum", index=True, copy=False)
    sap_atcentry = fields.Integer(index="btree", copy=False)


class StockMove(models.Model):
    _inherit = "stock.move"

    # WOR1 reference: docentry + linenum uniquely identifies a component line
    sap_docentry = fields.Integer("SAP DocEntry", index=True, copy=False)
    sap_linenum = fields.Integer("SAP LineNum", copy=False)
    sap_comment = fields.Char(
        "SAP Comment",
        help="Work instructions from SAP WOR1 (u_nbs_wrkinstr)",
    )


class MrpWorkorder(models.Model):
    _inherit = "mrp.workorder"

    # WOR1 reference for labor lines
    sap_docentry = fields.Integer("SAP DocEntry", index=True, copy=False)
    sap_linenum = fields.Integer("SAP LineNum", copy=False)
