"""xTuple Partner Model Extensions

This module adds xTuple-specific fields to res.partner for tracking
imported customer, vendor, contact, and ship-to records.
"""

from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    xtuple_cust_id = fields.Integer(index=True, copy=False, help="xTuple Customer ID")
    xtuple_vend_id = fields.Integer(index=True, copy=False, help="xTuple Vendor ID")
    xtuple_cntct_id = fields.Integer(index=True, copy=False, help="xTuple Contact ID")
    xtuple_crmacct_id = fields.Integer(
        index=True, copy=False, help="xTuple CRM Account ID"
    )
    xtuple_parent_id = fields.Integer(
        index=True, copy=False, help="xTuple Parent CRM Account ID"
    )
    xtuple_addr_id = fields.Integer(index=True, copy=False, help="xTuple Address ID")
    xtuple_shipto_id = fields.Integer(index=True, copy=False, help="xTuple Ship-To ID")
    xtuple_partner_type = fields.Selection(
        [
            ("customer", "Customer"),
            ("vendor", "Vendor"),
            ("both", "Customer and Vendor"),
        ],
        string="xTuple Partner Type",
        index=True,
    )

    _xtuple_cust_id_unique = models.Constraint(
        "UNIQUE (xtuple_cust_id)",
        "A partner with that xTuple Customer ID already exists.",
    )
    _xtuple_vend_id_unique = models.Constraint(
        "UNIQUE (xtuple_vend_id)",
        "A partner with that xTuple Vendor ID already exists.",
    )
    _xtuple_cntct_id_unique = models.Constraint(
        "UNIQUE (xtuple_cntct_id)",
        "A partner with that xTuple Contact ID already exists.",
    )
    _xtuple_shipto_id_unique = models.Constraint(
        "UNIQUE (xtuple_shipto_id)",
        "A partner with that xTuple Ship-To ID already exists.",
    )
