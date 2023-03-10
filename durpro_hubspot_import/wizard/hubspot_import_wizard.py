import time

from odoo import models, fields, api, _
from hubspot import HubSpot
from collections import deque
from .. import constants

import json


class HubSpotImportWizard(models.TransientModel):
    _name = "durpro_hubspot_import.hubspot_import_wizard"
    _description = 'Allows for the importation of HubSpot data into Odoo Helpdesk Tickets (and associations)'

    def action_get_hubspot_tickets(self):
        self.env['durpro_hubspot_import.hubspot_ticket'].import_all()

    def action_get_hubspot_contacts(self):
        self.env['durpro_hubspot_import.hubspot_contact'].import_all()

    def action_get_hubspot_companies(self):
        self.env['durpro_hubspot_import.hubspot_company'].import_all()

    def action_get_hubspot_pipelines(self):
        self.env['durpro_hubspot_import.hubspot_pipeline'].import_all()

    def action_get_hubspot_emails(self):
        self.env['durpro_hubspot_import.hubspot_email'].import_all()

    def action_get_hubspot_notes(self):
        self.env['durpro_hubspot_import.hubspot_note'].import_all()

    def action_get_hubspot_owners(self):
        self.env['durpro_hubspot_import.hubspot_owner'].import_all()

    def action_associate_tickets_with_contacts(self):
        self.env['durpro_hubspot_import.hubspot_ticket'].import_associated_contacts()

    def action_associate_tickets_with_companies(self):
        self.env['durpro_hubspot_import.hubspot_ticket'].import_associated_companies()

    def action_associate_tickets_with_emails(self):
        self.env['durpro_hubspot_import.hubspot_ticket'].import_associated_emails()

    def action_associate_tickets_with_notes(self):
        self.env['durpro_hubspot_import.hubspot_ticket'].import_associated_notes()

    def action_create_odoo_tickets(self):
        page_size = 1000
        no_tickets = self.env['durpro_hubspot_import.hubspot_ticket'].search_count([])
        for offset in range(0, no_tickets, page_size):
            tickets = self.env['durpro_hubspot_import.hubspot_ticket'].search([], offset=offset, limit=page_size)
            for ticket in tickets:
                # Create a ticket in the right pipeline
                # For each associated mail message

                # mail.mail objects, subtype of mail.message.
                # fields: subject (char), date (datetime), body (html), attachment_ids(Many2many->ir.attachment via message_attachment_rel ),
                #   parent_id (Many2one -> mail.message), child_ids, model (char, related doc model), res_id,
                #   email_from, author_id (Many2one -> res.partner), partner_ids (Many2many, recipients),
                #   state -> selection("sent", "received")
                for hs_email in ticket.associated_emails:
                    self.env['mail.mail'].create({

                    })


    # def action_get_ticket_associations(self):
    #     tickets = self.env['durpro_hubspot_import.hubspot_ticket'].search()
    #     associations = {}
    #     for ticket in tickets:
    #         # Get contact associations (15)
    #         contact_associations = self._api_client().crm.tickets.associations_api.get_all(
    #             ticket_id=ticket.hs_ticket_id,
    #             to_object_type=15).to_dict()
    #         hs_contact_ids = contact_associations.results
