from odoo import fields, models, api


class ModelName(models.Model):
    _name = 'durpro_hubspot_import.hubspot_owner'
    _description = 'HubSpot Owner Records'
    _inherit = 'durpro_hubspot_import.hubspot_model'
    _description = 'Carries information imported from Hubspot Owners (users)'

    hubspot_model_name = 'owners'
    hubspot_id_field = "hs_id"

    hs_id = fields.Char("HS ID")
    user_id = fields.Char("HS User ID")
    email = fields.Char("HS Email")
    first_name = fields.Char("HS First Name")
    last_name = fields.Char("HS Last Name")

    odoo_user = fields.Many2one("res.users", string="Odoo User", compute="_compute_odoo_user", store=True)

    @api.model
    def import_all(self):
        recs = self._api_client().crm.owners.get_all()
        properties = self.get_hs_properties_list()
        properties.remove('contents')
        for rec in recs:
            self.env[self._name].create(
                {field: getattr(rec, field.replace('hs_id', 'id')) for field in properties})

    def _extract_hs_fields(self):
        pass

    @api.depends("email")
    def _compute_odoo_user(self):
        for rec in self:
            if not rec.email:
                continue
            rec.odoo_user = self.env['res.users'].search('email', '=ilike', rec.email)
