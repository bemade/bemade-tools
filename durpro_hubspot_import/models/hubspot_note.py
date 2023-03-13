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
    hubspot_owner_id = fields.Char("HS Owner ID", compute="_extract_hs_fields", store=True)

    hs_attachment_ids = fields.Char("HS Attachment IDs", compute="_extract_hs_fields", store=True)

    owner = fields.Many2one("durpro_hubspot_import.hubspot_owner", string="HubSpot Owner", compute="_compute_owner",
                            store=True)

    @api.depends('hubspot_owner_id')
    def _compute_owner(self):
        for rec in self:
            if not rec.hubspot_owner_id:
                continue
            rec.owner = self.env['durpro_hubspot_import.hubspot_owner'].search('hs_id', '=', rec.hubspot_owner_id)
