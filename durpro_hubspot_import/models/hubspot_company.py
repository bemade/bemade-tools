from odoo import models, fields, api, _


class HubSpotCompany(models.Model):
    _name = 'durpro_hubspot_import.hubspot_company'
    _inherit = 'durpro_hubspot_import.hubspot_model'
    _description = 'Carries information imported from Hubspot Companies'

    hubspot_model_name = "companies"
    hubspot_id_field = "hs_object_id"

    hs_object_id = fields.Char(string="HS Object ID - Unique ID for this object", compute="_extract_hs_fields",
                               store=True)
    sdi_client = fields.Char(string="SDI Client (Yes/No)", compute="_extract_hs_fields", store=True)
    total_revenue = fields.Char(string="Total revenue.", compute="_extract_hs_fields", store=True)
    name = fields.Char(string="HS Company Name", compute="_extract_hs_fields", store=True)
    phone = fields.Char(string="HS Phone", compute="_extract_hs_fields", store=True)
    address = fields.Char(string="HS Address", compute="_extract_hs_fields", store=True)
    address2 = fields.Char(string="HS Address 2nd line", compute="_extract_hs_fields", store=True)
    city = fields.Char(string="HS City", compute="_extract_hs_fields", store=True)
    state = fields.Char(string="HS State or Province", compute="_extract_hs_fields", store=True)
    zip = fields.Char(string="HS Zip/Postal Code", compute="_extract_hs_fields", store=True)
    country = fields.Char(string="HS Country", compute="_extract_hs_fields", store=True)
    website = fields.Char(string="HS Website", compute="_extract_hs_fields", store=True)
    domain = fields.Char(string="HS Domain", compute="_extract_hs_fields", store=True)
    industry = fields.Char(string="HS Industry", compute="_extract_hs_fields", store=True)
    description = fields.Char(string="HS Description", compute="_extract_hs_fields", store=True)

    odoo_partner = fields.Many2one("res.partner", string="Matching Odoo Partner (company)",
                                   compute='_match_company', store=True)

    def _match_company(self):
        """Matches the HubSpot companies in this RecordSet by name only."""
        for rec in self:
            partner = self.env['res.partner'].search([('is_company', '=', True), ('name', '=ilike', rec.name)],
                                                     limit=1)
            rec.odoo_partner = partner or False
