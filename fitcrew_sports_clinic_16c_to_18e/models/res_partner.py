# -*- coding: utf-8 -*-
"""
Extension to res.partner model to store original Odoo 16 partner IDs for migration mapping.
"""

from odoo import models, fields


class ResPartner(models.Model):
    _inherit = "res.partner"

    odoo16_partner_id = fields.Integer(
        string="Odoo 16 Partner ID",
        index=True,
        copy=False,
        help="Original partner ID from Odoo 16 database for migration mapping"
    )

    _sql_constraints = [
        (
            "odoo16_partner_id_unique",
            "unique (odoo16_partner_id)",
            "A partner with that Odoo 16 Partner ID already exists.",
        ),
    ]
