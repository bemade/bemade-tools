from odoo import models, fields, api, _


class HubSpotNote(models.Model):
    _name = "durpro_hubspot_import.hubspot_note"
    _inherit = "durpro_hubspot_import.hubspot_model"
    _description = 'Carries information imported from Hubspot Notes'

    hubspot_model_name = "notes"
    hubspot_id_field = "hs_object_id"

    hs_object_id = fields.Char("HS Object ID", compute="_extract_hs_fields", store=True)
    hs_created_by = fields.Char("HS Created By", compute="_extract_hs_fields", store=True)
    hs_created_date = fields.Char("HS Created Date", compute="_extract_hs_fields", store=True)
    hs_note_body = fields.Char("HS Note Body", compute="_extract_hs_fields", store=True)
