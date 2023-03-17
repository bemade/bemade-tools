from odoo import models, fields, api, _
import time
from odoo.tools.mail import plaintext2html
from lxml import etree
from .. import constants
from odoo.tools import config
import threading


class HubSpotImportWizard(models.TransientModel):
    _name = "durpro_hubspot_import.hubspot_import_wizard"
    _description = 'Allows for the importation of HubSpot data into Odoo Helpdesk Tickets (and associations)'

    ticket_page_size = fields.Integer(string="Ticket Page Size", compute="_compute_page_size")

    def _compute_page_size(self):
        self.ticket_page_size = self.env['ir.config_parameter'].sudo().get_param(constants.PAGE_SIZE_PARAM)

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
        """Get attachments for any loaded HubSpotNotes and HubSpotEmails. There is no "get_all" method for files.
        Note that this function will not re-fetch attachments for notes and emails that already have ir_attachments
        related to them."""
        self._get_attachments('durpro_hubspot_import.hubspot_note')
        self._get_attachments('durpro_hubspot_import.hubspot_email')

    def _get_attachments(self, res_model: str):
        """
        Loads the attachments for all the records of type res_model. Records with existing ir_attachments are
        ignored as this is meant to be run as a one-time import.

        :param res_model: The addressable model name in form module.model_name for which to fetch attachments.
            The model passed is expected to have a field hs_attachment_ids representing the file IDs of the associated
            attachments, semicolon separated.
        :return: None
        """
        time_limit = config['limit_time_real']
        thread = threading.current_thread()

        page_size = 100
        already_loaded_recs = self.env['ir.attachment'].search([('res_model', '=', res_model)])
        res_ids = already_loaded_recs.mapped('res_id')
        domain = [('hs_attachment_ids', '!=', False), ('id', 'not in', res_ids)]
        record_count = self.env[res_model].search_count(domain)
        call_count = 0
        start_time = time.time()
        warn = ""

        for offset in range(0, record_count, page_size):
            recs = self.env[res_model].search(domain, offset=offset, limit=page_size)
            thread_execution_time = time.time() - thread.start_time
            if thread_execution_time + 20 > time_limit:
                warn = f"Stopping attachment import for server thread time limit. Processed {offset} " \
                       f"attachments. {record_count - offset} remaining."
                break
            for index, rec in enumerate(recs):
                thread_execution_time = time.time() - thread.start_time
                if thread_execution_time + 20 > time_limit:
                    warn = f"Stopping attachment import for server thread time limit. Processed {offset + index} " \
                           f"attachments. {record_count - (offset + index)} remaining."
                    break
                for file_id in str.split(rec.hs_attachment_ids):
                    f = self.env['durpro_hubspot_import.hubspot_attachment'].import_one(file_id)  # one API call
                    # f is False if the file is not found on HubSpot servers
                    if not f:
                        continue
                    raw = f.get_data()  # one API call
                    filename = f.name or "" + f.extension or ""
                    self.env['ir.attachment'].create({
                        'name': filename,
                        'raw': raw,
                        'res_model': res_model,
                        'res_id': rec.id,
                    })
                    if call_count == 4:
                        time.sleep(time.time() - start_time)
                        start_time = time.time()
                    call_count = (call_count + 1) % 5
            self.env['ir.attachment'].flush()
            self.env.cr.commit()
            if warn:
                raise Warning(_(warn))

    @api.depends('ticket_page_size')
    def action_create_odoo_tickets(self):
        """Converts as many HubSpot Tickets to Odoo tickets as possible in the threading time limit imposed in the
        server config (limit_time_real). Configured page size (see module settings) determines how often we commit to
        the database. We allow 5 seconds for a final database commit after processing the last batch in the given time
        limit.
        """
        # Handle time limits, turn off notifications
        time_limit = config['limit_time_real']
        thread = threading.current_thread()
        already_loaded_ids = self.env['helpdesk.ticket'].search([('hubspot_ticket_id', '!=', False)]).mapped(
            'hubspot_ticket_id').ids
        # temporarily deactivate notifications
        subtype = self.env['mail.message.subtype'].search(
            [('res_model', '=', 'helpdesk.team'), ('relation_field', '=', 'team_id'), ('name', '=', 'Ticket Created')])
        if subtype:
            subtype_default_initial = subtype.default
            subtype.default = False
        notify_stages = self.env['helpdesk.stage'].search([('template_id', '!=', False)])
        stage_template_dict = {s: s.template_id for s in notify_stages}
        notify_stages.write({'template_id': False})
        page_size = int(self.ticket_page_size)
        domain = [('id', 'not in', already_loaded_ids)]
        no_tickets = self.env['durpro_hubspot_import.hubspot_ticket'].search_count(domain)
        warn = ""
        for offset in range(0, no_tickets, page_size):
            thread_execution_time = time.time() - thread.start_time
            if thread_execution_time + 5 > time_limit:
                warn = f"Stopping Odoo Ticket Creation for server thread time limit. Processed {offset} tickets. " \
                       f"{no_tickets - offset} remain to be processed."
                break
            # Only work on tickets that have a configured pipeline and stage to which to transfer
            tickets = self.env['durpro_hubspot_import.hubspot_ticket'].search(domain,
                                                                              offset=offset, limit=page_size).filtered(
                lambda
                    r: r.pipeline and r.pipeline.helpdesk_team_id and r.pipeline_stage and r.pipeline_stage.helpdesk_stage)
            for index, ticket in enumerate(tickets):
                thread_execution_time = time.time() - thread.start_time
                if thread_execution_time + 5 > time_limit:
                    warn = f"Stopping Odoo Ticket Creation for server thread time limit. Processed {offset+index} " \
                           f"tickets. {no_tickets - (offset + index)} remain to be processed."
                    break
                # Create a ticket in the right pipeline
                hs_time = ticket.hs_time_to_time(ticket.createdate) if ticket.createdate else False
                create_date = time.strftime('%Y-%m-%d %H:%M:%S', hs_time) if hs_time else False
                hd_ticket = self.env['helpdesk.ticket'].create({
                    'name': ticket.subject or ticket.content or "No Subject",
                    'description': plaintext2html(ticket.content),
                    'create_date': create_date,
                    'team_id': ticket.pipeline.helpdesk_team_id.id,
                    'stage_id': ticket.pipeline_stage.helpdesk_stage.id,
                    'user_id': ticket.user_id.id if ticket.user_id else False,
                    'partner_id': ticket.associated_contacts[
                        0].odoo_contact.id if ticket.associated_contacts else False,
                    'hubspot_ticket_id': ticket.id,
                })

                # Add the notes and emails to the chatter with their attachments
                for note in ticket.associated_notes:
                    # Start by creating the attachments, then we'll link them up appropriately later
                    # We let ir.attachment guess the mimetype since HubSpot's file type field is non-MIME
                    attachments = self.env['ir.attachment'].search(
                        [('res_model', '=', 'durpro_hubspot_import.hubspot_note'),
                         ('res_id', 'in', [n.id for n in ticket.associated_notes])])
                    hs_time = note.hs_time_to_time(note.hs_created_date) if note.hs_created_date else False
                    create_date = time.strftime('%Y-%m-%d %H:%M:%S', hs_time) if hs_time else False
                    message = hd_ticket.sudo().message_post(body=note.hs_note_body,
                                                            message_type='comment',
                                                            author_id=note.author.id if note.author else False,
                                                            attachment_ids=attachments.ids,
                                                            date=create_date, )
                    attachments.write({
                        'res_model': message._name,
                        'res_id': message.id,
                        'create_date': create_date})

                for email in ticket.associated_emails:
                    attachments = self.env['ir.attachment'].search(
                        [('res_model', '=', 'durpro_hubspot_import.hubspot_email'),
                         ('res_id', 'in', [e.id for e in ticket.associated_emails])])
                    hs_time = email.hs_time_to_time(email.hs_createdate) if email.hs_createdate else False
                    create_date = time.strftime('%Y-%m-%d %H:%M:%S', hs_time) if hs_time else False
                    body = email.hs_email_html or plaintext2html(email.hs_email_text) or plaintext2html("")
                    tree = etree.fromstring(body, parser=etree.HTMLParser())
                    if tree is None:
                        body = plaintext2html(email.hs_email_text) or plaintext2html("")
                        tree = etree.fromstring(body, parser=etree.HTMLParser())
                        if tree is None:
                            body = ""
                    message = hd_ticket.sudo().message_post(subject=email.hs_email_subject or "",
                                                            body=body,
                                                            message_type='email',
                                                            author_id=email.author.id if email.author else False,
                                                            email_from=email.hs_email_from_email if not email.author else False,
                                                            partner_ids=email.recipients.ids,
                                                            attachment_ids=attachments.ids,
                                                            date=create_date,
                                                            )
                    attachments.write({
                        'res_model': message._name,
                        'res_id': message.id,
                        'create_date': create_date})
            self.env['ir.attachment'].flush()
            self.env['mail.message'].flush()
            self.env.cr.commit()

        # Last commit if we got interrupted by time running out
        if subtype:
            subtype.default = subtype_default_initial
        for s in notify_stages:
            s.write({'template_id': stage_template_dict[s].id})
        self.env.cr.commit()
        if warn:
            raise Warning(_(warn))
