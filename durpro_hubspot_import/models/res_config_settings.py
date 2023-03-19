from odoo import models, fields, api, _
from .. import constants


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    app_key = fields.Char("Private App Key")
    ticket_page_size = fields.Selection(selection=[
        ('1000', '1000'),
        ('500', '500'),
        ('250', '250'),
        ('100', '100'), ],
        string="Ticket Creation Page Size",
        help="How many tickets will be created before writing to the database. "
             "Smaller numbers are better for servers with short thread time limits.",
        required=True,
        default='250',
        config_parameter=constants.PAGE_SIZE_PARAM,
    )
    hubspot_auto_import = fields.Boolean(string="Automatic Import",
                                      help="Import HubSpot tickets and associated objects automatically.")

    tickets_imported = fields.Integer(string="HubSpot Tickets Imported", compute="_compute_import_totals")
    contacts_imported = fields.Integer(string="HubSpot Contacts Imported", compute="_compute_import_totals")
    companies_imported = fields.Integer(string="HubSpot Companies Imported", compute="_compute_import_totals")
    pipelines_imported = fields.Integer(string="HubSpot Pipelines Imported", compute="_compute_import_totals")
    emails_imported = fields.Integer(string="HubSpot Emails Imported", compute="_compute_import_totals")
    notes_imported = fields.Integer(string="HubSpot Notes Imported", compute="_compute_import_totals")
    owners_imported = fields.Integer(string="HubSpot Owners Imported", compute="_compute_import_totals")
    attachments_imported = fields.Integer(string="HubSpot Attachments Imported", compute="_compute_import_totals")

    attachments_remaining = fields.Integer(string="Attachments Remaining", compute="_compute_import_totals")

    tickets_converted = fields.Integer(string="Tickets Converted", compute="_compute_import_totals")

    auto_import_action = fields.Many2one("ir.cron", string="Scheduled Action for Import")

    def set_values(self):
        res = super(ResConfigSettings, self).set_values()
        self.env['ir.config_parameter'].set_param(constants.APPKEY_PARAM, self.app_key)
        self.env['ir.config_parameter'].set_param(constants.PAGE_SIZE_PARAM, self.ticket_page_size)
        self.env['ir.config_parameter'].set_param(constants.HS_AUTO_IMPORT_PARAM, self.hubspot_auto_import)
        if self.hubspot_auto_import:
            if not self.auto_import_action:
                self.auto_import_action = self.env['ir.cron'].create({
                    'name': 'HubSpot Automatic Import',
                    'interval_number': -1,
                })
        return res

    def get_values(self):
        res = super(ResConfigSettings, self).get_values()
        res.update(app_key=self.env['ir.config_parameter'].sudo().get_param(constants.APPKEY_PARAM),
                   ticket_page_size=self.env['ir.config_parameter'].sudo().get_param(constants.PAGE_SIZE_PARAM),
                   hubspot_auto_import=self.env['ir.config_parameter'].sudo().get_param(constants.HS_AUTO_IMPORT_PARAM))
        res.update(self._get_import_totals())
        return res

    def _get_import_totals(self):
        res = {
            'tickets_imported': self.env['durpro_hubspot_import.hubspot_ticket'].search_count([]),
            'contacts_imported': self.env['durpro_hubspot_import.hubspot_contact'].search_count([]),
            'companies_imported': self.env['durpro_hubspot_import.hubspot_company'].search_count([]),
            'pipelines_imported': self.env['durpro_hubspot_import.hubspot_contact'].search_count([]),
            'emails_imported': self.env['durpro_hubspot_import.hubspot_email'].search_count([]),
            'notes_imported': self.env['durpro_hubspot_import.hubspot_note'].search_count([]),
            'owners_imported': self.env['durpro_hubspot_import.hubspot_owner'].search_count([]),
            'attachments_imported': self.env['durpro_hubspot_import.hubspot_attachment'].search_count([]),
            'tickets_converted': self.env['helpdesk.ticket'].search_count([('hubspot_ticket_id', '!=', False)]),
        }
        # To calculate the attachments remaining to import, we see how many total distinct attachment IDs are present
        # in emails and notes, then subtract the number already imported.
        sql = """SELECT hs_attachment_ids 
                     FROM (select hs_attachment_ids from durpro_hubspot_import_hubspot_note) note 
                     UNION (select hs_attachment_ids from durpro_hubspot_import_hubspot_email)"""
        self.env.cr.execute(sql)
        result = self.env.cr.fetchall()
        all_attachment_ids = set()
        for r in result:
            ids = str.split(r[0]) if r[0] else None
            if ids:
                for i in ids:
                    all_attachment_ids.add(i)
        res['attachments_remaining'] = len(all_attachment_ids) - res['attachments_imported']
        return res

    def _compute_import_totals(self):
        for rec in self:
            rec.write(rec._get_import_totals())
