from odoo import models, fields, api, _
import time
from hubspot.crm.associations.models.batch_input_public_object_id import BatchInputPublicObjectId


class HubSpotTicket(models.Model):
    _name = "durpro_hubspot_import.hubspot_ticket"
    _inherit = "durpro_hubspot_import.hubspot_model"
    _description = 'Carries information imported from Hubspot Tickets'

    hubspot_model_name = "tickets"
    hubspot_id_field = "hs_ticket_id"

    # Basic fields
    subject = fields.Char(string="Subject", compute="_extract_hs_fields", store=True)
    content = fields.Char(string="Content", compute="_extract_hs_fields", store=True)
    hubspot_owner_id = fields.Char(string="Owner", compute="_extract_hs_fields", store=True)
    createdate = fields.Char(string="Date created", compute="_extract_hs_fields", store=True)
    hs_pipeline = fields.Char(string="HS Pipeline", compute="_extract_hs_fields", store=True)
    hs_pipeline_stage = fields.Char(string="HS Pipeline Stage", compute="_extract_hs_fields", store=True)
    hs_ticket_id = fields.Char(string="HS Ticket ID", compute="_extract_hs_fields", store=True)

    # Sales fields
    so_number = fields.Char(string="SO/PO Number", compute="_extract_hs_fields", store=True)
    quote_value____ = fields.Char(string="Quote Value", compute="_extract_hs_fields", store=True)

    # Service fields
    technician = fields.Char(string="Technician", compute="_extract_hs_fields", store=True)
    other_techs = fields.Char(string="Other Techs", compute="_extract_hs_fields", store=True)
    under_contract = fields.Char(string="Under Contract", compute="_extract_hs_fields", store=True)
    recently_serviced = fields.Char(string="Recently Serviced", compute="_extract_hs_fields", store=True)
    planned_hours = fields.Char(string="Planned Hours", compute="_extract_hs_fields", store=True)
    planned_service_date = fields.Char(string="Planned Service Date", compute="_extract_hs_fields", store=True)
    operational_impact = fields.Char(string="Operational Impact", compute="_extract_hs_fields", store=True)

    # Associations
    associated_contacts = fields.Many2many("durpro_hubspot_import.hubspot_contact",
                                           "durpro_hubspot_import_ticket_contact_rel", "hs_ticket_id", "hs_object_id",
                                           string="Associated Contacts")
    associated_companies = fields.Many2many("durpro_hubspot_import.hubspot_company",
                                            "durpro_hubspot_import_ticket_company_rel", "hs_ticket_id", "hs_object_id",
                                            string="Associated Companies")
    associated_emails = fields.Many2many("durpro_hubspot_import.hubspot_email",
                                         "durpro_hubspot_import_ticket_email_rel", "hs_ticket_id", "hs_object_id",
                                         string="Associated Emails")
    associated_notes = fields.Many2many("durpro_hubspot_import.hubspot_note",
                                        "durpro_hubspot_import_ticket_note_rel", "hs_ticket_id", "hs_object_id",
                                        string="Associated Notes")
    associated_owner = fields.Many2one("durpro_hubspot_import.hubspot_owner", compute="_compute_owner", store=True)
    pipeline = fields.Many2one("durpro_hubspot_import.hubspot_pipeline", compute="_compute_pipeline", store=True)
    pipeline_stage = fields.Many2one("durpro_hubspot_import.hubspot_pipeline_stage", compute="_compute_pipeline",
                                     store=True)

    user_id = fields.Many2one("res.users", compute="_compute_owner", store=True)

    @api.model
    def import_associated_contacts(self):
        self.import_associations('ticket', 'contact', 'associated_contacts')

    @api.model
    def import_associated_companies(self):
        self.import_associations('ticket', 'company', 'associated_companies')

    @api.model
    def import_associated_emails(self):
        self.import_associations('ticket', 'email', 'associated_emails')

    @api.model
    def import_associated_notes(self):
        self.import_associations('ticket', 'note', 'associated_notes')

    @api.depends("hs_pipeline", "hs_pipeline_stage")
    def _compute_pipeline(self):
        for rec in self:
            if rec.hs_pipeline:
                pl = self.env['durpro_hubspot_import.hubspot_pipeline'].search(
                    [('hs_pipeline_id', '=', rec.hs_pipeline)], limit=1)
                if pl:
                    rec.pipeline = pl[0]
                else:
                    print(f"Failed to link pipeline to ticket {rec.hs_ticket_id}")
            if rec.hs_pipeline_stage:
                pls = self.env['durpro_hubspot_import.hubspot_pipeline_stage'].search(
                    [('hs_stage_id', '=', rec.hs_pipeline_stage)])
                if pls:
                    rec.pipeline_stage = pls[0]
                else:
                    print(f"Failed to link pipeline stage to ticket {rec.hs_ticket_id}")

    @api.depends('hubspot_owner_id')
    def _compute_owner(self):
        owners = self.env['durpro_hubspot_import.hubspot_owner'].search(
            [('hs_id', 'in', self.mapped("hubspot_owner_id"))])
        owners_dict = {o.hs_id: o for o in owners}
        users_dict = {u.email: u for u in self.env['res.users'].search([('email', 'in', owners.mapped("email"))])}
        for rec in self:
            rec.associated_owner = owners_dict[rec.hubspot_owner_id] if rec.hubspot_owner_id in owners_dict else False
            if rec.associated_owner and rec.associated_owner.email:
                rec.user_id = users_dict[
                    rec.associated_owner.email] if rec.associated_owner.email in users_dict else False


class HelpdeskTicket(models.Model):
    _inherit = "helpdesk.ticket"

    hubspot_ticket_id = fields.Many2one("durpro_hubspot_import.hubspot_ticket", string="Original Hubspot Ticket",
                                        help="The HubSpot ticket that this ticket was created from.",)
