from odoo import models, fields


class MrpBom(models.Model):
    _inherit = "mrp.bom"

    sap_code = fields.Char(index="btree", copy=False)


class MrpBomLine(models.Model):
    _inherit = "mrp.bom.line"

    sap_comment = fields.Char(
        "SAP Comment",
        help="Comment/description from SAP BOM line (ITT1.comment)",
    )
