from odoo import models, fields, api, _
from .. import constants
import json
from hubspot import HubSpot
from hubspot.crm.associations.models.batch_input_public_object_id import BatchInputPublicObjectId
import time
from typing import Union
import threading
import time
from odoo.tools import config
import logging

_logger = logging.getLogger(__name__)


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
            if 'properties' in hs_model:
                properties = hs_model['properties']
            else:
                properties = hs_model
            to_write = {}
            for field in fields:
                if field == 'contents':
                    continue
                to_read = 'id' if field == 'hs_id' else field
                if to_read in properties:
                    to_write[field] = properties[to_read]
                if to_read in hs_model:
                    to_write[field] = hs_model[to_read]
            rec.write(to_write)

    @api.model
    def get_hs_properties_list(self):
        return list(set(list(self.fields_get())) - constants.BASE_FIELDS)

    @api.model
    def _check_time(self, delay: int) -> bool:
        time_limit = config['limit_time_real']
        if time_limit < 30:
            _logger.warning(f"Thread time limit: { time_limit } too low to run HubSpot Auto Import")
        thread = threading.current_thread()
        thread_execution_time = time.time() - thread.start_time
        if thread_execution_time + delay < time_limit:
            return True
        else:
            return False

    @api.model
    def import_all(self, after=None) -> str:
        """ We copy some code from the HubSpot API fetch_all method instead of using get_all so that we can commit to
        the database and free up memory as we go.

        :param after: The "after" token for fetching the next page of results.
        :return: The str representing the "after" token to pass for the next page of results, if any, otherwise None.
        """
        properties = self.get_hs_properties_list()
        PAGE_MAX_SIZE = 100
        call_count = 0
        start_time = time.time()
        while True and self._check_time(10):
            page = self._api_client().crm.objects.basic_api.get_page(self.hubspot_model_name, after=after,
                                                                     limit=PAGE_MAX_SIZE, properties=properties)
            objects_fetched = page.results
            already_loaded = self.env[self._name].search(
                [(self.hubspot_id_field, 'in', [o.id for o in objects_fetched])]).mapped(self.hubspot_id_field)
            for obj in objects_fetched:
                if obj.id in already_loaded:
                    continue
                object_as_dict = obj.to_dict()
                contents = json.dumps(object_as_dict, default=str)
                self.env[self._name].create({'contents': contents})
            self.env[self._name].flush()
            self.env.cr.commit()
            if page.paging is None:
                return None
            after = page.paging.next.after
            if call_count == 9:
                time.sleep(time.time() - start_time)
                start_time = time.time()
            call_count = (call_count + 1) % 10
        return after

    @api.model
    def import_associations(self, model_from_suffix, model_to_suffix, association_field, start: int = 0) -> int:
        rs_from_count = self.env[f'durpro_hubspot_import.hubspot_{model_from_suffix}'].search_count([])
        i = start
        start_time = time.time()
        while i < rs_from_count - 1 and self._check_time(10):
            associations = {}
            rs_from = self.env[f'durpro_hubspot_import.hubspot_{model_from_suffix}'].search([], offset=i, limit=100)
            rs_from_dict = {getattr(r, rs_from.hubspot_id_field): r for r in rs_from}
            i += 100
            if i % 1000 == 0:
                time.sleep(time.time() - start_time)
                start_time = time.time()
            ids = BatchInputPublicObjectId(inputs=[{'id': getattr(r, r.hubspot_id_field)} for r in rs_from])
            rs_to = self.env[f'durpro_hubspot_import.hubspot_{model_to_suffix}']
            recs = self._api_client().crm.associations.batch_api.read(self.hubspot_model_name, rs_to.hubspot_model_name,
                                                                      batch_input_public_object_id=ids).results
            for rec in recs:
                rec = rec.to_dict()
                from_rec = rs_from_dict[rec['_from']['id']]
                rs_to = self.env[f'durpro_hubspot_import.hubspot_{model_to_suffix}'].search(
                    [(rs_to.hubspot_id_field, 'in', [r['id'] for r in rec['to']])])
                if not from_rec or not rs_to:
                    continue
                associations.setdefault(from_rec, []).extend([r for r in rs_to])
            for from_rec, to_recs in associations.items():
                from_rec.write({association_field: [(6, 0, [r.id for r in to_recs])]})
            self.env.cr.commit()
        return i if i < rs_from_count - 1 else 0

    @api.model
    def hs_time_to_time(self, hs_timestamp: str) -> Union[time.struct_time, bool]:
        """
        Converts a HubSpot formatted timestamp to a usable format for Odoo fields.

        :param hs_timestamp: HubSpot timestamp string

        :return: A python struct_time representation of the HubSpot timestamp
        """
        try:
            return time.strptime(hs_timestamp, "%Y-%m-%dT%H:%M:%S[.%f]Z")
        except ValueError:
            try:
                return time.strptime(hs_timestamp, "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                return False
