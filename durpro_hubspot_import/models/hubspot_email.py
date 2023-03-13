from odoo import models, fields, api, _


class HubSpotEmail(models.Model):
    _name = "durpro_hubspot_import.hubspot_email"
    _inherit = "durpro_hubspot_import.hubspot_model"
    _description = 'Carries information imported from Hubspot Emails'

    hubspot_model_name = "emails"
    hubspot_id_field = "hs_id"

    hs_id = fields.Char(string="Email ID", compute="_extract_hs_fields", store=True)
    hs_unique_id = fields.Char(string="HS Unique ID", compute="_extract_hs_fields", store=True)
    hs_email_message_id = fields.Char(string="HS Email Message ID", compute="_extract_hs_fields", store=True)
    hs_createdate = fields.Char(string="HS Create Date", compute="_extract_hs_fields", store=True)
    hs_email_from_email = fields.Char(string="HS Email From", compute="_extract_hs_fields", store=True)
    hs_email_cc_email = fields.Char(string="HS Email CC", compute="_extract_hs_fields", store=True)
    hs_email_direction = fields.Char(string="HS Email Direction", compute="_extract_hs_fields",
                                     store=True)
    hs_email_html = fields.Char(string="HS Email Html", compute="_extract_hs_fields", store=True)
    hs_email_subject = fields.Char(string="HS Email Subject", compute="_extract_hs_fields", store=True)
    hs_email_text = fields.Char(string="HS Email Text", compute="_extract_hs_fields", store=True)
    # attachment ids are semicolon separated
    hs_attachment_ids = fields.Char(string="HS Email Attachments", compute="_extract_hs_fields", store=True)
    hs_email_to_email = fields.Char(string="HS Email To Email", compute="_extract_hs_fields", store=True)
    hubspot_owner_id = fields.Char(string="HS Owner ID", compute="_extract_hs_fields", store=True)

    owner = fields.Many2one("durpro_hubspot_import.hubspot_owner", string="HubSpot Owner", compute="_compute_owner",
                            store=True)
    author = fields.Many2one("res.partner", string="Message Author", compute="_compute_sender_recipients")
    recipients = fields.Many2many("res.partner", string="Message Recipients", compute="_compute_sender_recipients")

    @api.depends('hubspot_owner_id')
    def _compute_owner(self):
        for rec in self:
            if not rec.hubspot_owner_id:
                continue
            rec.owner = self.env['durpro_hubspot_import.hubspot_owner'].search([('hs_id', '=', rec.hubspot_owner_id)])

    @api.depends("hs_email_from_email", "hs_email_cc_email", "hs_email_to_email")
    def _compute_sender_recipients(self):
        for rec in self:
            recipients = []
            if rec.hs_email_to_email:
                recipients.append(str.split(rec.hs_email_to_email, ";"))
            if rec.hs_email_cc_email:
                recipients.append(str.split(rec.hs_email_cc_email, ";"))
            partners = self.env['res.partner'].search([('email', 'in', recipients)])
            authors = self.env['res.partner'].search([('email', '=like', rec.hs_email_from_email)])
            rec.author = authors[0] if authors else False
            if partners:
                rec.write({'recipients': fields.Command.set([p.id for p in partners])})
            else:
                rec.recipients = False
