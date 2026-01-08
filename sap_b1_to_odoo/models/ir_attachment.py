"""SAP B1 fields for ir.attachment model."""

from odoo import models, fields


class IrAttachment(models.Model):
    _inherit = "ir.attachment"

    sap_absentry = fields.Integer(index="btree", copy=False)
    sap_line = fields.Integer(index="btree", copy=False)

    _sap_absentry_line_unique = models.Constraint(
        "EXCLUDE USING btree (sap_absentry WITH =, sap_line WITH =) WHERE (sap_absentry != 0 AND sap_line != 0)",
        "SAP AbsEntry and Line combination must be unique when both are set",
    )
