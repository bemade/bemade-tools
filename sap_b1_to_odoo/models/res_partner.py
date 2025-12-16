from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    sap_card_code = fields.Char(index="btree", copy=False)
    sap_parent_card = fields.Char(index="btree", copy=False)
    sap_address_linenum = fields.Integer(
        index="btree", copy=False
    )  # CRD1 linenum for addresses
    sap_cntct_code = fields.Integer(index="btree", copy=False)
    sap_atcentry = fields.Integer(index="btree", copy=False)
    sap_partner_type = fields.Char(index="btree", copy=False)

    _sql_constraints = [
        (
            "sap_cardcode_unique",
            "unique (sap_card_code)",
            "An partner with that SAP cardcode already exists.",
        ),
        (
            "sap_cntct_code_unique",
            "unique (sap_cntct_code)",
            "A partner with that SAP Contact Code already exists.",
        ),
    ]
