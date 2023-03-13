from odoo import models, fields, api, _
import requests
import json


class HubSpotAttachment(models.Model):
    _inherit = "durpro_hubspot_import.hubspot_model"
    _name = "durpro_hubspot_import.hubspot_attachment"
    _description = "A hybrid of an attachment record and utilities to retrieve and store the actual attached file."

    hubspot_model_name = "files"
    hubspot_id_field = "hs_id"

    hs_id = fields.Char("HS ID", compute="_extract_hs_fields", store=True)
    name = fields.Char("HS File Name", compute="_extract_hs_fields", store=True)
    extension = fields.Char("HS File Extension", compute="_extract_hs_fields", store=True)
    created_at = fields.Char("Creation Time", compute="_extract_hs_fields", store=True)
    type = fields.Char("File Type", compute="_extract_hs_fields", store=True)

    @api.model
    def import_all(self):
        """Not implemented for files objects"""
        pass

    @api.model
    def import_one(self, file_id):
        """Imports the file metadata from HubSpot and returns the created HubSpotAttachment record."""
        file_metadata = self._api_client().files.files.files_api.get_by_id(file_id=file_id).to_dict()
        contents = json.dumps(file_metadata, default=str)
        r = self.env[self._name].create({'contents': contents})
        return r

    @api.depends('name', 'extension')
    def get_data(self):
        """Retrieves the file data from HubSpot servers using a signed_url."""
        signed_url = self._api_client().files.files.files_api.get_signed_url(file_id=self.hs_id,
                                                                             expiration_seconds=60).to_dict()
        r = requests.get(signed_url['url'], allow_redirects=True)
        return r.content
    @api.depends('name', 'extension')
    def get_filename(self):
        self.ensure_one()
        return self.name or "" + self.extension or ""
