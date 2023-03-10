from odoo import models, fields, api, _
from .. import constants
import json
from hubspot import HubSpot
from hubspot.crm.associations.models.batch_input_public_object_id import BatchInputPublicObjectId
import time


class HubSpotModel(models.AbstractModel):
    """Base class usable for most HubSpot API objects.

    Subclasses must define:
      1. The hubspot_model_name (string) that matches the endpoint URL for the API.
      2. The exact field names from hubspot as model fields (If an object returns the property "id",
         name the field "hs_id" to have it extracted into the appropriate model field).
      3. The member hubspot_id_field (str) that gives the name of the hubspot unique identifier field for the model."""

    _name = "durpro_hubspot_import.hubspot_model"
    _description = 'Abstract model common to (almost) all Hubspot Import models'

    contents = fields.Char(string="JSON Contents")

    def _api_client(self) -> HubSpot:
        return HubSpot(access_token=self.env['ir.config_parameter'].sudo().get_param(constants.APPKEY_PARAM))

    @api.depends('contents')
    def _extract_hs_fields(self):
        fields = self.get_hs_properties_list()
        for rec in self:
            hs_model = json.loads(rec.contents)
            properties = hs_model['properties']
            to_write = {}
            for field in fields:
                if field == 'contents':
                    continue
                to_read = 'id' if field == 'hs_id' else field
                if to_read in properties:
                    to_write |= {field: properties[to_read]}
                if to_read in hs_model:
                    to_write |= {field: hs_model[to_read]}
            rec.write(to_write)

    @api.model
    def get_hs_properties_list(self):
        return list(set(list(self.fields_get())) - constants.BASE_FIELDS)

    @api.model
    def import_all(self):
        """ We copy some code from the HubSpot API fetch_all method instead of using get_all so that we can commit to
        the database and free up memory as we go."""
        properties = self.get_hs_properties_list()
        after = None
        PAGE_MAX_SIZE = 100
        call_count = 0
        start_time = time.time()
        while True:
            page = self._api_client().crm.objects.basic_api.get_page(self.hubspot_model_name, after=after,
                                                                     limit=PAGE_MAX_SIZE, properties=properties)
            objects_fetched = page.results
            for obj in objects_fetched:
                object_as_dict = obj.to_dict()
                contents = json.dumps(object_as_dict, default=str)
                self.env[self._name].create({'contents': contents})
            self.env[self._name].flush()
            self.env.cr.commit()
            if page.paging is None:
                break
            after = page.paging.next.after
            if call_count == 9:
                time.sleep(time.time() - start_time)
                start_time = time.time()
            call_count = (call_count + 1) % 10

    @api.model
    def import_associations(self, model_from_suffix, model_to_suffix, association_field):
        rs_from = self.env[f'durpro_hubspot_import.hubspot_{model_from_suffix}'].search([])
        rs_from_dict = {getattr(r, rs_from.hubspot_id_field): r for r in rs_from}
        i = 0
        start_time = time.time()
        associations = {}
        while i < len(rs_from) - 1:
            sublist = rs_from[i:min(i + 100, len(rs_from) - 1)]
            i += 100
            if i % 1000 == 0:
                time.sleep(time.time() - start_time)
                start_time = time.time()
            ids = BatchInputPublicObjectId(inputs=[{'id': getattr(r, r.hubspot_id_field)} for r in sublist])
            rs_to = self.env[f'durpro_hubspot_import.hubspot_{model_to_suffix}']
            recs = self._api_client().crm.associations.batch_api.read(self.hubspot_model_name, rs_to.hubspot_model_name,
                                                                      batch_input_public_object_id=ids).results
            print(f"{recs}")
            for rec in recs:
                rec = rec.to_dict()
                from_rec = rs_from_dict[rec['_from']['id']]
                rs_to = self.env[f'durpro_hubspot_import.hubspot_{model_to_suffix}'].search(
                    [(rs_to.hubspot_id_field, 'in', [r['id'] for r in rec['to']])])
                if not from_rec or not rs_to:
                    continue
                if from_rec not in associations:
                    associations |= {from_rec: [r for r in rs_to]}
                else:
                    associations[from_rec].add(rs_to)
        for from_rec, to_recs in associations.items():
            from_rec.write({association_field: [(6, 0, [r.id for r in to_recs])]})
