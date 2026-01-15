"""xTuple MRP BOM Model Extensions

This module adds xTuple-specific fields to MRP BOM models for tracking
imported bills of materials.
"""

from odoo import fields, models


class MrpBom(models.Model):
    _inherit = "mrp.bom"

    xtuple_bomhead_id = fields.Integer(string="xTuple BOM ID", index=True)
    xtuple_bomhead_item_id = fields.Integer(
        string="xTuple BOM Head Item ID", index=True
    )
    xtuple_revision = fields.Char(string="xTuple Revision")
    xtuple_revision_date = fields.Date(string="xTuple Revision Date")
    xtuple_batch_size = fields.Float(string="xTuple Batch Size", default=1.0)


class MrpBomLine(models.Model):
    _inherit = "mrp.bom.line"

    xtuple_bomitem_id = fields.Integer(string="xTuple BOM Item ID", index=True)


class MrpProduction(models.Model):
    _inherit = "mrp.production"

    xtuple_wo_id = fields.Integer(string="xTuple Work Order ID", index=True, copy=False)
    xtuple_wo_number = fields.Integer(string="xTuple WO Number", copy=False)
