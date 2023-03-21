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

    hubspot_auto_import_controller = fields.Many2one("durpro_hubspot_import.auto_importer",
                                                     string="Auto Import Controller",
                                                     compute="_get_hubspot_controller")
    tickets_imported = fields.Integer(string="HubSpot Tickets Imported",
                                      related="hubspot_auto_import_controller.tickets_imported")
    contacts_imported = fields.Integer(string="HubSpot Contacts Imported",
                                       related="hubspot_auto_import_controller.contacts_imported")
    companies_imported = fields.Integer(string="HubSpot Companies Imported",
                                        related="hubspot_auto_import_controller.companies_imported")
    pipelines_imported = fields.Integer(string="HubSpot Pipelines Imported",
                                        related="hubspot_auto_import_controller.pipelines_imported")
    emails_imported = fields.Integer(string="HubSpot Emails Imported",
                                     related="hubspot_auto_import_controller.emails_imported")
    notes_imported = fields.Integer(string="HubSpot Notes Imported",
                                    related="hubspot_auto_import_controller.notes_imported")
    owners_imported = fields.Integer(string="HubSpot Owners Imported",
                                     related="hubspot_auto_import_controller.owners_imported")
    attachments_imported = fields.Integer(string="HubSpot Attachments Imported",
                                          related="hubspot_auto_import_controller.attachments_imported")
    attachments_remaining = fields.Integer(string="Attachments Remaining",
                                           related="hubspot_auto_import_controller.attachments_remaining")
    tickets_converted = fields.Integer(string="Tickets Converted",
                                       related="hubspot_auto_import_controller.tickets_converted")

    @api.depends('hubspot_auto_import_controller')
    def set_values(self):
        res = super(ResConfigSettings, self).set_values()
        self.env['ir.config_parameter'].set_param(constants.APPKEY_PARAM, self.app_key)
        self.env['ir.config_parameter'].set_param(constants.PAGE_SIZE_PARAM, self.ticket_page_size)
        self.env['ir.config_parameter'].set_param(constants.HS_AUTO_IMPORT_PARAM, self.hubspot_auto_import)
        if self.hubspot_auto_import and not self.hubspot_auto_import_controller.active:
            self.hubspot_auto_import_controller.activate()
        elif not self.hubspot_auto_import and self.hubspot_auto_import_controller.active:
            self.hubspot_auto_import_controller.deactivate()
        return res

    @api.depends('hubspot_auto_import_controller')
    def get_values(self):
        res = super(ResConfigSettings, self).get_values()
        res.update(app_key=self.env['ir.config_parameter'].sudo().get_param(constants.APPKEY_PARAM),
                   ticket_page_size=self.env['ir.config_parameter'].sudo().get_param(constants.PAGE_SIZE_PARAM),
                   hubspot_auto_import=self.env['ir.config_parameter'].sudo().get_param(constants.HS_AUTO_IMPORT_PARAM))
        res.update({
            'tickets_imported': self.tickets_imported,
            'contacts_imported': self.contacts_imported,
            'companies_imported': self.companies_imported,
            'pipelines_imported': self.pipelines_imported,
            'emails_imported': self.emails_imported,
            'notes_imported': self.notes_imported,
            'owners_imported': self.owners_imported,
            'attachments_imported': self.attachments_imported,
            'attachments_remaining': self.attachments_remaining,
            'tickets_converted': self.tickets_converted,
        })
        return res

    def _get_hubspot_controller(self):
        self.hubspot_auto_import_controller = self.env['durpro_hubspot_import.auto_importer'].search([], limit=1)
