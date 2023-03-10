from odoo import models, fields, api, _
from .. import constants


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    app_key = fields.Char("Private App Key")

    def set_values(self):
        res = super(ResConfigSettings, self).set_values()
        self.env['ir.config_parameter'].set_param(constants.APPKEY_PARAM, self.app_key)
        return res

    def get_values(self):
        res = super(ResConfigSettings, self).get_values()
        value = self.env['ir.config_parameter'].sudo().get_param(constants.APPKEY_PARAM)
        res.update(app_key=value)
        return res
