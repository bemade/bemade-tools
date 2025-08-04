# -*- coding: utf-8 -*-
"""
Sports Patient Model Extension for Migration
Adds tracking field for Odoo 16 patient IDs.
"""

from odoo import models, fields


class SportsPatient(models.Model):
    _inherit = 'sports.patient'
    
    odoo16_patient_id = fields.Integer(
        string='Odoo 16 Patient ID',
        help='Original patient ID from Odoo 16 database for migration tracking',
        readonly=True,
        index=True
    )
    
    _sql_constraints = [
        (
            "odoo16_patient_id_unique",
            "unique (odoo16_patient_id)",
            "Odoo 16 Patient ID must be unique"
        )
    ]
