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

