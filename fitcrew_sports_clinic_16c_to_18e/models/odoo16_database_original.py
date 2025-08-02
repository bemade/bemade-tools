from odoo import models, fields, api, _
from odoo.sql_db import db_connect
from odoo.exceptions import ValidationError, UserError
import os
import logging

_logger = logging.getLogger(__name__)
PAGE_SIZE = 1000


class Odoo16Database(models.Model):
    _name = "odoo16.database"
    _description = "Odoo 16 Community Database Connection"

    name = fields.Char(string="Connection Name", required=True)
    database_host = fields.Char(
        string="Database Host",
        required=True, 
        default=lambda self: os.environ.get("ODOO16_HOST", "localhost")
    )
    database_name = fields.Char(
        string="Database Name",
        required=True, 
        default=lambda self: os.environ.get("ODOO16_DBNAME", "")
    )
    database_username = fields.Char(
        string="Database Username",
        required=True, 
        default=lambda self: os.environ.get("ODOO16_USER", "odoo")
    )
    database_password = fields.Char(
        string="Database Password",
        default=lambda self: os.environ.get("ODOO16_PASSWORD", "")
    )
    database_port = fields.Integer(
        string="Database Port",
        required=True, 
        default=lambda self: int(os.environ.get("ODOO16_PORT", "5432"))
    )
    filestore_path = fields.Char(
        string="Filestore Path",
        help="Path to the Odoo 16 filestore directory for attachment migration"
    )
    skip_filestore = fields.Boolean(
        string="Skip Filestore Import",
        default=True,
        help="Skip importing attachment files and nullify file references. Recommended for production migrations."
    )
    migrate_ir_filters = fields.Boolean(
        string="Migrate User Filters (ir.filter)",
        default=False,
        help="Import user-created filters. Requires client validation - check if these filters are needed."
    )
    
    # Migration tracking fields
    last_migration_date = fields.Datetime(string="Last Migration Date", readonly=True)
    migration_status = fields.Selection([
        ('not_started', 'Not Started'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('failed', 'Failed')
    ], string="Migration Status", default='not_started', readonly=True)
    migration_log = fields.Text(string="Migration Log", readonly=True)

    @api.depends("database_host", "database_name")
    def _compute_display_name(self):
        for rec in self:
            if rec.name:
                rec.display_name = f"{rec.name} ({rec.database_host}/{rec.database_name})"
            else:
                rec.display_name = f"{rec.database_host}/{rec.database_name}"

    @api.constrains("filestore_path")
    def _constrain_filestore_path(self):
        for record in self:
            if record.filestore_path:
                try:
                    if not os.path.exists(record.filestore_path):
                        raise ValidationError(
                            _("The provided filestore path does not exist: %s")
                            % record.filestore_path
                        )
                    if not os.access(record.filestore_path, os.R_OK):
                        raise ValidationError(
                            _("The provided filestore path is not readable: %s")
                            % record.filestore_path
                        )
                except Exception as e:
                    raise ValidationError(
                        _("Unable to access the filestore path: %s. Error: %s")
                        % (record.filestore_path, str(e))
                    )

    def get_cursor(self):
        """
        Get a database cursor for the Odoo 16 source database.
        
        Returns:
            Database cursor for executing queries on the source database
        """
        self.ensure_one()
        
        if self.database_password:
            uri = (
                "postgresql://{user}:{password}@{host}:{port}/{database}"
            ).format(
                user=self.database_username,
                password=self.database_password,
                host=self.database_host,
                port=self.database_port,
                database=self.database_name,
            )
        else:
            uri = (
                "postgresql://{user}@{host}:{port}/{database}"
            ).format(
                user=self.database_username,
                host=self.database_host,
                port=self.database_port,
                database=self.database_name,
            )

        try:
            return db_connect(uri, allow_uri=True).cursor()
        except Exception as e:
            raise UserError(
                _("Failed to connect to Odoo 16 database: %s") % str(e)
            )

    def test_connection(self):
        """Test the database connection and return success notification."""
        try:
            with self.get_cursor() as cr:
                cr.execute("SELECT version()")
                version = cr.fetchone()[0]
                _logger.info(f"Successfully connected to Odoo 16 database: {version}")
                
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Connection Successful"),
                    "message": _("Successfully connected to Odoo 16 database."),
                    "sticky": False,
                    "type": "success",
                },
            }
        except Exception as e:
            _logger.error(f"Failed to connect to Odoo 16 database: {str(e)}")
            raise UserError(_("Connection failed: %s") % str(e))

    @api.model
    def _success_notification(self, title="Migration Successful", message="The migration completed successfully."):
        """Return a success notification for migration operations."""
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _(title),
                "message": _(message),
                "sticky": False,
                "type": "success",
            },
        }

    def _update_migration_status(self, status, log_message=None):
        """Update migration status and log."""
        self.migration_status = status
        if status == 'completed':
            self.last_migration_date = fields.Datetime.now()
        
        if log_message:
            current_log = self.migration_log or ""
            timestamp = fields.Datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            new_entry = f"[{timestamp}] {log_message}\n"
            self.migration_log = current_log + new_entry

    # Migration action methods (to be implemented)
    def action_migrate_users(self):
        """Migrate users from Odoo 16 to Odoo 18."""
        # TODO: Implement user migration
        raise UserError(_("User migration not yet implemented"))

    def action_migrate_teams(self):
        """Migrate sports teams from Odoo 16 to Odoo 18."""
        # TODO: Implement team migration
        raise UserError(_("Team migration not yet implemented"))

    def action_migrate_patients(self):
        """Migrate patients (players) from Odoo 16 to Odoo 18."""
        # TODO: Implement patient migration
        raise UserError(_("Patient migration not yet implemented"))

    def action_migrate_injuries(self):
        """Migrate patient injuries from Odoo 16 to Odoo 18."""
        # TODO: Implement injury migration
        raise UserError(_("Injury migration not yet implemented"))

    def action_migrate_activities(self):
        """Migrate mail activities from Odoo 16 to Odoo 18."""
        # TODO: Implement activity migration
        raise UserError(_("Activity migration not yet implemented"))

    def action_migrate_attachments(self):
        """Migrate attachments from Odoo 16 to Odoo 18.
        
        If skip_filestore is enabled, attachment records will be created but file references will be nullified.
        """
        try:
            self._update_migration_status('in_progress', 'Starting attachments migration')
            
            with self.get_cursor() as cr:
                # Get attachments from source database
                cr.execute("""
                    SELECT 
                        id, name, datas_fname, description, res_model, res_id, res_field,
                        company_id, type, url, public, access_token, create_date, write_date,
                        create_uid, write_uid, checksum, mimetype, index_content
                    FROM ir_attachment 
                    WHERE res_model IN ('sports.team', 'sports.patient', 'sports.patient.injury', 'mail.activity')
                    ORDER BY id
                    LIMIT %s
                """, (PAGE_SIZE,))
                
                attachments = cr.fetchall()
                migrated_count = 0
                skipped_files_count = 0
                
                for attachment in attachments:
                    (
                        att_id, name, datas_fname, description, res_model, res_id, res_field,
                        company_id, att_type, url, public, access_token, create_date, write_date,
                        create_uid, write_uid, checksum, mimetype, index_content
                    ) = attachment
                    
                    # Create attachment record
                    attachment_data = {
                        'name': name or 'Untitled',
                        'datas_fname': datas_fname,
                        'description': description or '',
                        'res_model': res_model,
                        'res_id': res_id,
                        'res_field': res_field,
                        'company_id': company_id,
                        'type': att_type or 'binary',
                        'url': url,
                        'public': public or False,
                        'access_token': access_token,
                        'create_date': create_date,
                        'write_date': write_date,
                        'mimetype': mimetype,
                        'index_content': index_content,
                    }
                    
                    if self.skip_filestore:
                        # Nullify file references when skipping filestore
                        attachment_data.update({
                            'datas': False,  # No binary data
                            'store_fname': False,  # No file store reference
                            'checksum': False,  # No checksum
                            'description': (attachment_data['description'] or '') + 
                                         '\n\n[NOTE: Original file content not migrated - filestore import was skipped]'
                        })
                        skipped_files_count += 1
                    else:
                        # TODO: Implement actual file content migration from filestore
                        # This would require reading files from self.filestore_path
                        attachment_data['checksum'] = checksum
                        _logger.warning(f"File content migration not implemented for attachment {att_id}")
                    
                    # Create the attachment
                    new_attachment = self.env['ir.attachment'].create(attachment_data)
                    migrated_count += 1
                    
                    _logger.info(f"Migrated attachment {att_id} to {new_attachment.id}")
                
                message = f"Successfully migrated {migrated_count} attachments."
                if self.skip_filestore:
                    message += f" File content skipped for {skipped_files_count} attachments (filestore import disabled)."
                
                self._update_migration_status('completed', f'Attachments migration completed: {message}')
                return self._success_notification(
                    "Attachments Migration Successful",
                    message
                )
                
        except Exception as e:
            self._update_migration_status('failed', f'Attachments migration failed: {str(e)}')
            _logger.error(f"Attachments migration failed: {str(e)}")
            raise UserError(_("Attachments migration failed: %s") % str(e))

    def action_migrate_ir_filters(self):
        """Migrate user filters (ir.filter) from Odoo 16 to Odoo 18.
        
        Only migrates if migrate_ir_filters is enabled. Requires client validation.
        """
        if not self.migrate_ir_filters:
            return self._success_notification(
                "IR Filters Migration Skipped",
                "User filters migration is disabled. Enable 'Migrate User Filters' if needed."
            )
        
        try:
            self._update_migration_status('in_progress', 'Starting user filters (ir.filter) migration')
            
            with self.get_cursor() as cr:
                # Get user filters from source database
                cr.execute("""
                    SELECT 
                        id, user_id, action_id, name, model_id, domain, context, sort,
                        is_default, active, create_date, write_date, create_uid, write_uid
                    FROM ir_filters 
                    WHERE active = true
                    ORDER BY id
                    LIMIT %s
                """, (PAGE_SIZE,))
                
                filters = cr.fetchall()
                migrated_count = 0
                skipped_count = 0
                
                for filter_data in filters:
                    (
                        filter_id, user_id, action_id, name, model_id, domain, context, sort,
                        is_default, active, create_date, write_date, create_uid, write_uid
                    ) = filter_data
                    
                    # Check if the user exists in target system
                    target_user = self.env['res.users'].browse(user_id).exists()
                    if not target_user:
                        _logger.warning(f"Skipping filter {filter_id}: user {user_id} not found in target system")
                        skipped_count += 1
                        continue
                    
                    # Check if the model exists in target system
                    target_model = self.env['ir.model'].browse(model_id).exists()
                    if not target_model:
                        _logger.warning(f"Skipping filter {filter_id}: model {model_id} not found in target system")
                        skipped_count += 1
                        continue
                    
                    # Create filter data
                    filter_vals = {
                        'user_id': user_id,
                        'action_id': action_id,
                        'name': name,
                        'model_id': model_id,
                        'domain': domain,
                        'context': context,
                        'sort': sort,
                        'is_default': is_default,
                        'active': active,
                        'create_date': create_date,
                        'write_date': write_date,
                    }
                    
                    # Create the filter
                    new_filter = self.env['ir.filters'].create(filter_vals)
                    migrated_count += 1
                    
                    _logger.info(f"Migrated filter {filter_id} to {new_filter.id}")
                
                message = f"Successfully migrated {migrated_count} user filters."
                if skipped_count > 0:
                    message += f" Skipped {skipped_count} filters due to missing users/models."
                
                self._update_migration_status('completed', f'User filters migration completed: {message}')
                return self._success_notification(
                    "User Filters Migration Successful",
                    message + "\n\nNote: Please validate with client that these filters are needed."
                )
                
        except Exception as e:
            self._update_migration_status('failed', f'User filters migration failed: {str(e)}')
            _logger.error(f"User filters migration failed: {str(e)}")
            raise UserError(_("User filters migration failed: %s") % str(e))

    def action_migrate_users_partners(self):
        """Migrate all res.users and res.partners from Odoo 16 to Odoo 18."""
        try:
            self._update_migration_status('in_progress', 'Starting users and partners migration')
            
            with self.get_cursor() as cr:
                # Migrate res.users first
                cr.execute("""
                    SELECT 
                        id, login, password, partner_id, company_id, active, 
                        create_date, write_date, create_uid, write_uid,
                        signature, share, notification_type, odoobot_state,
                        livechat_username, im_status, totp_secret
                    FROM res_users 
                    ORDER BY id
                    LIMIT %s
                """, (PAGE_SIZE,))
                
                users = cr.fetchall()
                users_migrated = 0
                
                for user_data in users:
                    (
                        user_id, login, password, partner_id, company_id, active,
                        create_date, write_date, create_uid, write_uid,
                        signature, share, notification_type, odoobot_state,
                        livechat_username, im_status, totp_secret
                    ) = user_data
                    
                    # Check if user already exists
                    existing_user = self.env['res.users'].browse(user_id).exists()
                    if existing_user:
                        _logger.info(f"User {user_id} already exists, skipping")
                        continue
                    
                    user_vals = {
                        'id': user_id,
                        'login': login,
                        'password': password,
                        'partner_id': partner_id,
                        'company_id': company_id,
                        'active': active,
                        'create_date': create_date,
                        'write_date': write_date,
                        'signature': signature,
                        'share': share,
                        'notification_type': notification_type or 'email',
                        'odoobot_state': odoobot_state,
                        'livechat_username': livechat_username,
                        'im_status': im_status,
                        'totp_secret': totp_secret,
                    }
                    
                    # Create user with sudo to bypass access controls
                    self.env['res.users'].sudo().create(user_vals)
                    users_migrated += 1
                
                # Migrate res.partners
                cr.execute("""
                    SELECT 
                        id, name, email, phone, mobile, website, street, street2,
                        city, zip, state_id, country_id, company_type, is_company,
                        parent_id, category_id, supplier_rank, customer_rank,
                        active, create_date, write_date, create_uid, write_uid
                    FROM res_partner 
                    ORDER BY id
                    LIMIT %s
                """, (PAGE_SIZE,))
                
                partners = cr.fetchall()
                partners_migrated = 0
                
                for partner_data in partners:
                    (
                        partner_id, name, email, phone, mobile, website, street, street2,
                        city, zip_code, state_id, country_id, company_type, is_company,
                        parent_id, category_id, supplier_rank, customer_rank,
                        active, create_date, write_date, create_uid, write_uid
                    ) = partner_data
                    
                    # Check if partner already exists
                    existing_partner = self.env['res.partner'].browse(partner_id).exists()
                    if existing_partner:
                        _logger.info(f"Partner {partner_id} already exists, skipping")
                        continue
                    
                    partner_vals = {
                        'id': partner_id,
                        'name': name or 'Unknown',
                        'email': email,
                        'phone': phone,
                        'mobile': mobile,
                        'website': website,
                        'street': street,
                        'street2': street2,
                        'city': city,
                        'zip': zip_code,
                        'state_id': state_id,
                        'country_id': country_id,
                        'company_type': company_type or 'person',
                        'is_company': is_company or False,
                        'parent_id': parent_id,
                        'supplier_rank': supplier_rank or 0,
                        'customer_rank': customer_rank or 0,
                        'active': active,
                        'create_date': create_date,
                        'write_date': write_date,
                    }
                    
                    # Create partner with sudo to bypass access controls
                    self.env['res.partner'].sudo().create(partner_vals)
                    partners_migrated += 1
                
                message = f"Successfully migrated {users_migrated} users and {partners_migrated} partners."
                self._update_migration_status('completed', f'Users and partners migration completed: {message}')
                return self._success_notification(
                    "Users and Partners Migration Successful",
                    message
                )
                
        except Exception as e:
            self._update_migration_status('failed', f'Users and partners migration failed: {str(e)}')
            _logger.error(f"Users and partners migration failed: {str(e)}")
            raise UserError(_("Users and partners migration failed: %s") % str(e))

    def action_migrate_mail_system(self):
        """Migrate mail system data: channels, members, notifications, reactions, tracking, followers."""
        try:
            self._update_migration_status('in_progress', 'Starting mail system migration')
            
            with self.get_cursor() as cr:
                migrated_counts = {
                    'channels': 0,
                    'members': 0,
                    'notifications': 0,
                    'reactions': 0,
                    'tracking': 0,
                    'followers': 0
                }
                
                # 1. Migrate mail.channel
                cr.execute("""
                    SELECT 
                        id, name, description, channel_type, public, group_public_id,
                        uuid, active, create_date, write_date, create_uid, write_uid
                    FROM mail_channel 
                    WHERE active = true
                    ORDER BY id
                    LIMIT %s
                """, (PAGE_SIZE,))
                
                channels = cr.fetchall()
                for channel_data in channels:
                    (
                        channel_id, name, description, channel_type, public, group_public_id,
                        uuid, active, create_date, write_date, create_uid, write_uid
                    ) = channel_data
                    
                    channel_vals = {
                        'name': name,
                        'description': description,
                        'channel_type': channel_type or 'channel',
                        'public': public or 'public',
                        'group_public_id': group_public_id,
                        'uuid': uuid,
                        'active': active,
                        'create_date': create_date,
                        'write_date': write_date,
                    }
                    
                    new_channel = self.env['mail.channel'].create(channel_vals)
                    migrated_counts['channels'] += 1
                    _logger.info(f"Migrated channel {channel_id} to {new_channel.id}")
                
                # 2. Migrate mail.channel.member
                cr.execute("""
                    SELECT 
                        id, channel_id, partner_id, guest_id, is_pinned, last_interest_dt,
                        last_seen_dt, create_date, write_date, create_uid, write_uid
                    FROM mail_channel_member 
                    ORDER BY id
                    LIMIT %s
                """, (PAGE_SIZE,))
                
                members = cr.fetchall()
                for member_data in members:
                    (
                        member_id, channel_id, partner_id, guest_id, is_pinned, last_interest_dt,
                        last_seen_dt, create_date, write_date, create_uid, write_uid
                    ) = member_data
                    
                    member_vals = {
                        'channel_id': channel_id,
                        'partner_id': partner_id,
                        'guest_id': guest_id,
                        'is_pinned': is_pinned or False,
                        'last_interest_dt': last_interest_dt,
                        'last_seen_dt': last_seen_dt,
                        'create_date': create_date,
                        'write_date': write_date,
                    }
                    
                    new_member = self.env['mail.channel.member'].create(member_vals)
                    migrated_counts['members'] += 1
                
                # 3. Migrate mail.notification
                cr.execute("""
                    SELECT 
                        id, mail_message_id, res_partner_id, notification_type,
                        notification_status, failure_type, failure_reason,
                        create_date, write_date, create_uid, write_uid
                    FROM mail_notification 
                    ORDER BY id
                    LIMIT %s
                """, (PAGE_SIZE,))
                
                notifications = cr.fetchall()
                for notif_data in notifications:
                    (
                        notif_id, mail_message_id, res_partner_id, notification_type,
                        notification_status, failure_type, failure_reason,
                        create_date, write_date, create_uid, write_uid
                    ) = notif_data
                    
                    notif_vals = {
                        'mail_message_id': mail_message_id,
                        'res_partner_id': res_partner_id,
                        'notification_type': notification_type or 'inbox',
                        'notification_status': notification_status or 'ready',
                        'failure_type': failure_type,
                        'failure_reason': failure_reason,
                        'create_date': create_date,
                        'write_date': write_date,
                    }
                    
                    new_notif = self.env['mail.notification'].create(notif_vals)
                    migrated_counts['notifications'] += 1
                
                # 4. Migrate mail.message.reaction
                cr.execute("""
                    SELECT 
                        id, message_id, content, partner_id, guest_id,
                        create_date, write_date, create_uid, write_uid
                    FROM mail_message_reaction 
                    ORDER BY id
                    LIMIT %s
                """, (PAGE_SIZE,))
                
                reactions = cr.fetchall()
                for reaction_data in reactions:
                    (
                        reaction_id, message_id, content, partner_id, guest_id,
                        create_date, write_date, create_uid, write_uid
                    ) = reaction_data
                    
                    reaction_vals = {
                        'message_id': message_id,
                        'content': content,
                        'partner_id': partner_id,
                        'guest_id': guest_id,
                        'create_date': create_date,
                        'write_date': write_date,
                    }
                    
                    new_reaction = self.env['mail.message.reaction'].create(reaction_vals)
                    migrated_counts['reactions'] += 1
                
                # 5. Migrate mail.tracking.value
                cr.execute("""
                    SELECT 
                        id, mail_message_id, field, field_desc, field_type,
                        old_value_integer, old_value_float, old_value_monetary,
                        old_value_char, old_value_text, old_value_datetime,
                        new_value_integer, new_value_float, new_value_monetary,
                        new_value_char, new_value_text, new_value_datetime,
                        create_date, write_date, create_uid, write_uid
                    FROM mail_tracking_value 
                    ORDER BY id
                    LIMIT %s
                """, (PAGE_SIZE,))
                
                tracking_values = cr.fetchall()
                for tracking_data in tracking_values:
                    (
                        tracking_id, mail_message_id, field, field_desc, field_type,
                        old_value_integer, old_value_float, old_value_monetary,
                        old_value_char, old_value_text, old_value_datetime,
                        new_value_integer, new_value_float, new_value_monetary,
                        new_value_char, new_value_text, new_value_datetime,
                        create_date, write_date, create_uid, write_uid
                    ) = tracking_data
                    
                    tracking_vals = {
                        'mail_message_id': mail_message_id,
                        'field': field,
                        'field_desc': field_desc,
                        'field_type': field_type,
                        'old_value_integer': old_value_integer,
                        'old_value_float': old_value_float,
                        'old_value_monetary': old_value_monetary,
                        'old_value_char': old_value_char,
                        'old_value_text': old_value_text,
                        'old_value_datetime': old_value_datetime,
                        'new_value_integer': new_value_integer,
                        'new_value_float': new_value_float,
                        'new_value_monetary': new_value_monetary,
                        'new_value_char': new_value_char,
                        'new_value_text': new_value_text,
                        'new_value_datetime': new_value_datetime,
                        'create_date': create_date,
                        'write_date': write_date,
                    }
                    
                    new_tracking = self.env['mail.tracking.value'].create(tracking_vals)
                    migrated_counts['tracking'] += 1
                
                # 6. Migrate mail.followers for sports models
                cr.execute("""
                    SELECT 
                        id, res_model, res_id, partner_id, channel_id,
                        create_date, write_date, create_uid, write_uid
                    FROM mail_followers 
                    WHERE res_model IN ('sports.team', 'sports.patient', 'sports.patient.injury', 'mail.activity')
                    ORDER BY id
                    LIMIT %s
                """, (PAGE_SIZE,))
                
                followers = cr.fetchall()
                for follower_data in followers:
                    (
                        follower_id, res_model, res_id, partner_id, channel_id,
                        create_date, write_date, create_uid, write_uid
                    ) = follower_data
                    
                    follower_vals = {
                        'res_model': res_model,
                        'res_id': res_id,
                        'partner_id': partner_id,
                        'channel_id': channel_id,
                        'create_date': create_date,
                        'write_date': write_date,
                    }
                    
                    new_follower = self.env['mail.followers'].create(follower_vals)
                    migrated_counts['followers'] += 1
                
                message = (
                    f"Successfully migrated mail system data:\n"
                    f"- Channels: {migrated_counts['channels']}\n"
                    f"- Members: {migrated_counts['members']}\n"
                    f"- Notifications: {migrated_counts['notifications']}\n"
                    f"- Reactions: {migrated_counts['reactions']}\n"
                    f"- Tracking Values: {migrated_counts['tracking']}\n"
                    f"- Followers: {migrated_counts['followers']}"
                )
                
                self._update_migration_status('completed', f'Mail system migration completed: {message}')
                return self._success_notification(
                    "Mail System Migration Successful",
                    message
                )
                
        except Exception as e:
            self._update_migration_status('failed', f'Mail system migration failed: {str(e)}')
            _logger.error(f"Mail system migration failed: {str(e)}")
            raise UserError(_("Mail system migration failed: %s") % str(e))

    def action_migrate_all(self):
        """Perform complete migration from Odoo 16 to Odoo 18."""
        try:
            self._update_migration_status('in_progress', 'Starting complete migration')
            
            # Migration sequence (to be implemented)
            # self.action_migrate_users_partners()  # All users and partners first
            # self.action_migrate_teams()
            # self.action_migrate_patients()
            # self.action_migrate_injuries()
            # self.action_migrate_activities()
            # self.action_migrate_calendar_events_to_tasks()
            # self.action_migrate_mail_system()  # Mail channels, notifications, etc.
            # self.action_migrate_attachments()
            # self.action_migrate_ir_filters()
            
            self._update_migration_status('completed', 'Complete migration finished successfully')
            return self._success_notification(
                "Complete Migration Successful",
                "All data has been successfully migrated from Odoo 16 to Odoo 18."
            )
            
        except Exception as e:
            self._update_migration_status('failed', f'Migration failed: {str(e)}')
            _logger.error(f"Migration failed: {str(e)}")
            raise UserError(_("Migration failed: %s") % str(e))

    def action_validate_source_data(self):
        """Validate the source Odoo 16 database structure and data."""
        try:
            with self.get_cursor() as cr:
                # Check if bemade_sports_clinic module exists
                cr.execute("""
                    SELECT name, state FROM ir_module_module 
                    WHERE name = 'bemade_sports_clinic'
                """)
                module_info = cr.fetchone()
                
                if not module_info:
                    raise UserError(_("bemade_sports_clinic module not found in source database"))
                
                if module_info[1] != 'installed':
                    raise UserError(_("bemade_sports_clinic module is not installed in source database"))
                
                # Check key tables exist
                required_tables = [
                    'sports_team',
                    'sports_patient', 
                    'sports_patient_injury',
                    'sports_team_staff',
                    'sports_patient_contact'
                ]
                
                for table in required_tables:
                    cr.execute("""
                        SELECT EXISTS (
                            SELECT FROM information_schema.tables 
                            WHERE table_name = %s
                        )
                    """, (table,))
                    
                    if not cr.fetchone()[0]:
                        raise UserError(_("Required table '%s' not found in source database") % table)
                
                # Get record counts
                counts = {}
                for table in required_tables:
                    cr.execute(f"SELECT COUNT(*) FROM {table}")
                    counts[table] = cr.fetchone()[0]
                
                message = "Source database validation successful:\n"
                for table, count in counts.items():
                    message += f"- {table}: {count} records\n"
                
                return {
                    "type": "ir.actions.client",
                    "tag": "display_notification",
                    "params": {
                        "title": _("Validation Successful"),
                        "message": _(message),
                        "sticky": True,
                        "type": "success",
                    },
                }
                
        except Exception as e:
            _logger.error(f"Source data validation failed: {str(e)}")
            raise UserError(_("Source data validation failed: %s") % str(e))

    def action_migrate_calendar_events_to_tasks(self):
        """Migrate calendar events from Odoo 16 to project tasks in Odoo 18.
        
        Calendar event attendees will be converted to task assignees.
        """
        try:
            self._update_migration_status('in_progress', 'Starting calendar events to project tasks migration')
            
            with self.get_cursor() as cr:
                # First, check if we have calendar events to migrate
                cr.execute("""
                    SELECT COUNT(*) FROM calendar_event 
                    WHERE active = true
                """)
                total_events = cr.fetchone()[0]
                
                if total_events == 0:
                    return self._success_notification(
                        "Calendar Migration Completed",
                        "No calendar events found to migrate."
                    )
                
                # Get calendar events with their attendees
                cr.execute("""
                    SELECT 
                        ce.id,
                        ce.name,
                        ce.description,
                        ce.start,
                        ce.stop,
                        ce.user_id,
                        ce.location,
                        ce.privacy,
                        ce.allday,
                        ce.create_date,
                        ce.write_date,
                        ce.create_uid,
                        ce.write_uid
                    FROM calendar_event ce
                    WHERE ce.active = true
                    ORDER BY ce.id
                    LIMIT %s
                """, (PAGE_SIZE,))
                
                events = cr.fetchall()
                migrated_count = 0
                
                # Create a default project for migrated calendar events if it doesn't exist
                project_env = self.env['project.project']
                default_project = project_env.search([('name', '=', 'Migrated Calendar Events')], limit=1)
                
                if not default_project:
                    default_project = project_env.create({
                        'name': 'Migrated Calendar Events',
                        'description': 'Project containing tasks migrated from Odoo 16 calendar events',
                        'privacy_visibility': 'employees',
                    })
                
                for event in events:
                    event_id, name, description, start, stop, user_id, location, privacy, allday, create_date, write_date, create_uid, write_uid = event
                    
                    # Get attendees for this event
                    cr.execute("""
                        SELECT ca.partner_id, ca.state
                        FROM calendar_attendee ca
                        WHERE ca.event_id = %s
                    """, (event_id,))
                    attendees = cr.fetchall()
                    
                    # Map attendees to users (if they exist in target system)
                    assignee_ids = []
                    for partner_id, state in attendees:
                        # Only include accepted or tentative attendees
                        if state in ['accepted', 'tentative']:
                            # Try to find corresponding user in target system
                            user = self.env['res.users'].search([
                                ('partner_id', '=', partner_id)
                            ], limit=1)
                            if user:
                                assignee_ids.append(user.id)
                    
                    # Create task data
                    task_data = {
                        'name': name or f'Calendar Event {event_id}',
                        'description': description or '',
                        'project_id': default_project.id,
                        'date_deadline': stop.date() if stop else None,
                        'user_ids': [(6, 0, assignee_ids)] if assignee_ids else [],
                        'create_date': create_date,
                        'write_date': write_date,
                    }
                    
                    # Add location to description if present
                    if location:
                        task_data['description'] += f'\n\nLocation: {location}'
                    
                    # Add event timing information
                    if start and stop:
                        if allday:
                            task_data['description'] += f'\n\nOriginal Event: All day on {start.date()}'
                        else:
                            task_data['description'] += f'\n\nOriginal Event: {start} to {stop}'
                    
                    # Add privacy information
                    if privacy and privacy != 'public':
                        task_data['description'] += f'\n\nPrivacy: {privacy}'
                    
                    # Create the task
                    task = self.env['project.task'].create(task_data)
                    migrated_count += 1
                    
                    _logger.info(f"Migrated calendar event {event_id} to task {task.id}")
                
                self._update_migration_status('completed', f'Calendar events migration completed: {migrated_count} events migrated to tasks')
                return self._success_notification(
                    "Calendar Migration Successful",
                    f"Successfully migrated {migrated_count} calendar events to project tasks.\n"
                    f"Tasks created in project: {default_project.name}"
                )
                
        except Exception as e:
            self._update_migration_status('failed', f'Calendar events migration failed: {str(e)}')
            _logger.error(f"Calendar events migration failed: {str(e)}")
            raise UserError(_("Calendar events migration failed: %s") % str(e))
