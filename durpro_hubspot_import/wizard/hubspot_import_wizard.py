from odoo import models, fields, api, _
import time



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

    def action_get_attachments(self):
        """Get attachments for any loaded HubSpotNotes and HubSpotEmails. There is no "get_all" method for files."""
        domain = [('hs_attachment_ids', '!=', False)]
        notes_count = self.env['durpro_hubspot_import.hubspot_note'].search_count(domain)
        emails_count = self.env['durpro_hubspot_import.hubspot_email'].search_count(domain)
        page_size = 100
        for offset in range(0, notes_count, page_size):
            notes = self.env['durpro_hubspot_import.hubspot_note'].search(domain, offset=offset, limit=page_size)
            for note in notes:
                for file_id in str.split(note.hs_attachment_ids):
                    f = self.env['durpro_hubspot_import.hubspot_attachment'].import_one(file_id)
                    raw = f.get_data()
                    filename = f.name or "" + f.extension or ""
                    attachment = self.env['ir.attachment'].create({
                        'name': filename,
                        'raw': raw,
                        'res_model': note._name,
                    })
        for offset in range(0, emails_count, page_size):
    def action_create_odoo_tickets(self):
        page_size = 1000
        no_tickets = self.env['durpro_hubspot_import.hubspot_ticket'].search_count([])
        for offset in range(0, no_tickets, page_size):
            # Only work on tickets that have a configured pipeline and stage to which to transfer
            tickets = self.env['durpro_hubspot_import.hubspot_ticket'].search([], offset=offset,
                                                                              limit=page_size).filtered(lambda r: (
                (r.pipeline and r.pipeline.helpdesk_team_id and r.pipeline_stage and r.pipeline_stage.helpdesk_stage)))
            for ticket in tickets:
                # Create a ticket in the right pipeline
                try:
                    create_date = time.strptime(ticket.create_date, "%Y-%m-%dT%H:%M:%S[.%f]Z")
                except:
                    try:
                        create_date = time.strptime(ticket.create_date, "%Y-%m-%dT%H:%M:%SZ")
                    except:
                        create_date = False
                hd_ticket = self.env['helpdesk.ticket'].create({
                    'name': ticket.subject or ticket.content or "No Subject",
                    'description': ticket.content,
                    'create_date': create_date,
                    'team_id': ticket.pipeline.helpdesk_team_id.id,
                    'stage_id': ticket.pipeline_stage.helpdesk_stage.id,
                    'user_id': ticket.user_id.id if ticket.user_id else False,
                    'partner_id': ticket.associated_contacts[
                        0].odoo_contact.id if ticket.associated_contacts else False,
                    'hubspot_ticket_id': ticket.id,
                })

                # For each associated mail message

                # mail.mail objects, subtype of mail.message.
                # fields: subject (char), date (datetime), body (html), attachment_ids(Many2many->ir.attachment via message_attachment_rel ),
                #   parent_id (Many2one -> mail.message), child_ids, model (char, related doc model), res_id,
                #   email_from, author_id (Many2one -> res.partner), partner_ids (Many2many, recipients),
                #   state -> selection("sent", "received")
                # for hs_email in ticket.associated_emails:
                #     self.env['mail.mail'].create({
                #
                #     })