# -*- coding: utf-8 -*-
"""
Sports Patient Injury Model Extension for Migration
Adds tracking field for Odoo 16 injury IDs.
"""

from odoo import models, fields


class SportsPatientInjury(models.Model):
    _inherit = 'sports.patient.injury'
    
    odoo16_injury_id = fields.Integer(
        string='Odoo 16 Injury ID',
        help='Original injury ID from Odoo 16 database for migration tracking',
        readonly=True,
        index=True
    )
    
    _sql_constraints = [
        (
            "odoo16_injury_id_unique",
            "unique (odoo16_injury_id)",
            "Odoo 16 Injury ID must be unique"
        )
    ]
