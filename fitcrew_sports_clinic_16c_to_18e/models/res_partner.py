# -*- coding: utf-8 -*-
"""
Extension to res.partner model to store original Odoo 16 partner IDs for migration mapping.
"""

from odoo import models, fields, api
from odoo.exceptions import ValidationError
import logging

_logger = logging.getLogger(__name__)


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
            "unique (odoo16_partner_id) WHERE odoo16_partner_id IS NOT NULL",
            "A partner with that Odoo 16 Partner ID already exists.",
        ),
    ]

    def write(self, vals):
        """Override write to handle odoo16_partner_id conflicts during partner merges."""
        if 'odoo16_partner_id' in vals and vals['odoo16_partner_id']:
            # Check if we're in a partner merge context
            if self.env.context.get('bypass_audit'):
                # During partner merge, if the odoo16_partner_id already exists on the destination,
                # don't try to overwrite it to avoid constraint violations
                for record in self:
                    existing_partner = self.env['res.partner'].search([
                        ('odoo16_partner_id', '=', vals['odoo16_partner_id']),
                        ('id', '!=', record.id)
                    ], limit=1)
                    
                    if existing_partner:
                        _logger.info(f"Partner merge: Skipping odoo16_partner_id update for partner {record.id} "
                                   f"because ID {vals['odoo16_partner_id']} already exists on partner {existing_partner.id}")
                        # Remove the conflicting field from the update
                        vals = vals.copy()
                        del vals['odoo16_partner_id']
                        break
        
        return super().write(vals)
