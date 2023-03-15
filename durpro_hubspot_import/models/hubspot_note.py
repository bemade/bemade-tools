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
    author = fields.Many2one("res.partner", string="Note Author", compute="_compute_author")

    @api.depends("hs_created_by")
    def _compute_author(self):
        hs_users = self.env['durpro_hubspot_import.hubspot_owner'].search([('hs_id', 'in', self.mapped('hs_created_by'))])
        hs_users_dict = {u.hs_id: u.email for u in hs_users}
        odoo_users = self.env['res.users'].search([('email','in',hs_users.mapped('email'))])
        authors_dict = {u.email: u.partner_id for u in odoo_users}
        for rec in self:
            rec.author = authors_dict.get(hs_users_dict.get(rec.hs_created_by, False), False)


    @api.depends('hubspot_owner_id')
    def _compute_owner(self):
        for rec in self:
            if not rec.hubspot_owner_id:
                continue
            rec.owner = self.env['durpro_hubspot_import.hubspot_owner'].search([('hs_id', '=', rec.hubspot_owner_id)])
