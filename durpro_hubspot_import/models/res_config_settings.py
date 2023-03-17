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

    def set_values(self):
        res = super(ResConfigSettings, self).set_values()
        self.env['ir.config_parameter'].set_param(constants.APPKEY_PARAM, self.app_key)
        self.env['ir.config_parameter'].set_param(constants.PAGE_SIZE_PARAM, self.ticket_page_size)
        return res

    def get_values(self):
        res = super(ResConfigSettings, self).get_values()
        res.update(app_key=self.env['ir.config_parameter'].sudo().get_param(constants.APPKEY_PARAM),
                   ticket_page_size=self.env['ir.config_parameter'].sudo().get_param(constants.PAGE_SIZE_PARAM))
        return res
