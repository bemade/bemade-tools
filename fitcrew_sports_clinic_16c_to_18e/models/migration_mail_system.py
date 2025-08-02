from odoo import models, fields, api, _
from odoo.exceptions import UserError
from .odoo16_database_base import Odoo16DatabaseBase, PAGE_SIZE
import logging

_logger = logging.getLogger(__name__)


class MigrationMailSystem(models.Model):
    """Migration methods for mail system data (channels, notifications, tracking, followers)."""
    _name = 'migration.mail.system'
    _description = 'Mail System Migration'
    _inherits = {'odoo16.database.base': 'database_id'}
    
    database_id = fields.Many2one('odoo16.database.base', required=True, ondelete='cascade')
    
    def get_cursor(self):
        """Get database cursor - delegate to base class."""
        return self.database_id.get_cursor()
    
    def _update_migration_status(self, status, message):
        """Update migration status - delegate to base class."""
        return self.database_id._update_migration_status(status, message)
    
    def _success_notification(self, title, message):
        """Return success notification - delegate to base class."""
        return self.database_id._success_notification(title, message)
    
    def _error_notification(self, title, message):
        """Return error notification - delegate to base class."""
        return self.database_id._error_notification(title, message)
    
    def action_migrate_mail_system(self):
        """Migrate all mail system data from Odoo 16."""
        try:
            self._update_migration_status('in_progress', 'Starting mail system migration')
            
            # Migrate mail channels
            channel_count = self._migrate_mail_channels()
            
            # Migrate mail channel members
            member_count = self._migrate_mail_channel_members()
            
            # Migrate mail notifications
            notification_count = self._migrate_mail_notifications()
            
            # Migrate mail message reactions
            reaction_count = self._migrate_mail_reactions()
            
            # Migrate mail tracking values
            tracking_count = self._migrate_mail_tracking()
            
            # Migrate mail followers
            follower_count = self._migrate_mail_followers()
            
            self._update_migration_status('completed', 
                f'Mail system migration completed: {channel_count} channels, {member_count} members, '
                f'{notification_count} notifications, {reaction_count} reactions, '
                f'{tracking_count} tracking values, {follower_count} followers migrated')
            
            return self._success_notification(
                "Mail System Migration Successful",
                f"Successfully migrated mail system data: {channel_count} channels, {member_count} members, "
                f"{notification_count} notifications, {reaction_count} reactions, "
                f"{tracking_count} tracking values, {follower_count} followers."
            )
            
        except Exception as e:
            self._update_migration_status('failed', f'Mail system migration failed: {str(e)}')
            _logger.error(f"Mail system migration failed: {str(e)}")
            raise UserError(_("Mail system migration failed: %s") % str(e))
    
    def _migrate_mail_channels(self):
        """Migrate mail.channel records."""
        # Check if mail.channel model is available in the registry
        if 'mail.channel' not in self.env:
            _logger.warning("mail.channel model not available in registry, skipping channel migration")
            return 0
            
        count = 0
        with self.get_cursor() as cr:
            # Check which columns exist in mail_channel table
            cr.execute("""
                SELECT column_name FROM information_schema.columns 
                WHERE table_name = 'mail_channel' AND table_schema = 'public'
            """)
            available_columns = [row[0] for row in cr.fetchall()]
            
            # Build dynamic query based on available columns
            base_columns = ['id', 'name', 'channel_type', 'active']
            optional_columns = ['description', 'public', 'group_public_id', 'uuid', 'create_date', 'write_date', 'create_uid', 'write_uid']
            
            # Only include columns that exist
            query_columns = base_columns + [col for col in optional_columns if col in available_columns]
            columns_str = ', '.join(query_columns)
            
            cr.execute(f"""
                SELECT {columns_str}
                FROM mail_channel WHERE active = true ORDER BY id LIMIT %s
            """, (PAGE_SIZE,))
            
            channels = cr.fetchall()
            
            for channel_data in channels:
                # Create a dictionary mapping column names to values
                channel_dict = dict(zip(query_columns, channel_data))
                
                # Build channel values dynamically based on available columns
                channel_vals = {
                    'name': channel_dict.get('name'),
                    'channel_type': channel_dict.get('channel_type', 'channel'),
                    'active': channel_dict.get('active', True),
                }
                
                # Add optional fields if they exist
                if 'description' in channel_dict:
                    channel_vals['description'] = channel_dict['description']
                if 'public' in channel_dict:
                    channel_vals['public'] = channel_dict['public']
                if 'group_public_id' in channel_dict and channel_dict['group_public_id']:
                    # Validate group exists
                    if self.env['res.groups'].browse(channel_dict['group_public_id']).exists():
                        channel_vals['group_public_id'] = channel_dict['group_public_id']
                if 'uuid' in channel_dict:
                    channel_vals['uuid'] = channel_dict['uuid']
                
                # Check if channel already exists
                existing_channel = self.env['mail.channel'].search([
                    ('name', '=', channel_dict.get('name'))
                ], limit=1)
                
                # Also check by UUID if available
                if not existing_channel and 'uuid' in channel_dict and channel_dict['uuid']:
                    existing_channel = self.env['mail.channel'].search([
                        ('uuid', '=', channel_dict['uuid'])
                    ], limit=1)
                
                if not existing_channel:
                    self.env['mail.channel'].create(channel_vals)
                    count += 1
        
        return count
    
    def _migrate_mail_channel_members(self):
        """Migrate mail.channel.member records."""
        # Check if required models are available in the registry
        if 'mail.channel' not in self.env or 'mail.channel.member' not in self.env:
            _logger.warning("mail.channel or mail.channel.member models not available in registry, skipping channel member migration")
            return 0
            
        count = 0
        with self.get_cursor() as cr:
            cr.execute("""
                SELECT id, channel_id, partner_id, guest_id, custom_channel_name, fetched_message_id,
                       seen_message_id, fold_state, is_minimized, is_pinned, last_interest_dt,
                       last_seen_dt, create_date, write_date, create_uid, write_uid
                FROM mail_channel_member ORDER BY id LIMIT %s
            """, (PAGE_SIZE,))
            
            members = cr.fetchall()
            
            for member_data in members:
                member_vals = {
                    'channel_id': member_data[1],
                    'partner_id': member_data[2],
                    'guest_id': member_data[3],
                    'custom_channel_name': member_data[4],
                    'fetched_message_id': member_data[5],
                    'seen_message_id': member_data[6],
                    'fold_state': member_data[7],
                    'is_minimized': member_data[8],
                    'is_pinned': member_data[9],
                    'last_interest_dt': member_data[10],
                    'last_seen_dt': member_data[11],
                }
                
                # Check if member already exists
                existing_member = self.env['mail.channel.member'].search([
                    ('channel_id', '=', member_data[1]),
                    ('partner_id', '=', member_data[2])
                ], limit=1)
                
                if not existing_member:
                    self.env['mail.channel.member'].create(member_vals)
                    count += 1
        
        return count
    
    def _migrate_mail_notifications(self):
        """Migrate mail.notification records."""
        # Check if mail.notification model is available in the registry
        if 'mail.notification' not in self.env:
            _logger.warning("mail.notification model not available in registry, skipping notification migration")
            return 0
            
        count = 0
        with self.get_cursor() as cr:
            # Check which columns exist in mail_notification table
            cr.execute("""
                SELECT column_name FROM information_schema.columns 
                WHERE table_name = 'mail_notification' AND table_schema = 'public'
            """)
            available_columns = [row[0] for row in cr.fetchall()]
            
            # Build dynamic query based on available columns
            base_columns = ['id', 'mail_message_id', 'res_partner_id', 'notification_type', 'notification_status']
            optional_columns = ['is_read', 'read_date', 'failure_type', 'failure_reason']
            
            # Only include columns that exist
            select_columns = base_columns + [col for col in optional_columns if col in available_columns]
            
            query = f"SELECT {', '.join(select_columns)} FROM mail_notification ORDER BY id LIMIT %s"
            cr.execute(query, (PAGE_SIZE,))
            
            notifications = cr.fetchall()
            
            for notification_data in notifications:
                notification_vals = {
                    'mail_message_id': notification_data[1],
                    'res_partner_id': notification_data[2],
                    'notification_type': notification_data[3],
                    'notification_status': notification_data[4],
                }
                
                # Add optional fields if they exist in the source data
                col_index = 5  # Start after base columns
                if 'is_read' in available_columns:
                    notification_vals['is_read'] = notification_data[col_index]
                    col_index += 1
                if 'read_date' in available_columns:
                    notification_vals['read_date'] = notification_data[col_index]
                    col_index += 1
                if 'failure_type' in available_columns:
                    notification_vals['failure_type'] = notification_data[col_index]
                    col_index += 1
                if 'failure_reason' in available_columns:
                    notification_vals['failure_reason'] = notification_data[col_index]
                    col_index += 1
                
                # Check if the referenced mail.message exists in target database
                referenced_message = self.env['mail.message'].browse(notification_data[1])
                if not referenced_message.exists():
                    # Skip notifications for non-existent messages
                    continue
                
                # Check if notification already exists
                existing_notification = self.env['mail.notification'].search([
                    ('mail_message_id', '=', notification_data[1]),
                    ('res_partner_id', '=', notification_data[2])
                ], limit=1)
                
                if not existing_notification:
                    self.env['mail.notification'].create(notification_vals)
                    count += 1
        
        return count
    
    def _migrate_mail_reactions(self):
        """Migrate mail.message.reaction records."""
        # Check if mail.message.reaction model is available in the registry
        if 'mail.message.reaction' not in self.env:
            _logger.warning("mail.message.reaction model not available in registry, skipping reaction migration")
            return 0
            
        count = 0
        with self.get_cursor() as cr:
            # Check which columns exist in mail_message_reaction table
            cr.execute("""
                SELECT column_name FROM information_schema.columns 
                WHERE table_name = 'mail_message_reaction' AND table_schema = 'public'
            """)
            available_columns = [row[0] for row in cr.fetchall()]
            
            # Build dynamic query based on available columns
            base_columns = ['id', 'message_id', 'partner_id', 'guest_id', 'content']
            
            # Only include columns that exist
            select_columns = [col for col in base_columns if col in available_columns]
            
            if not select_columns:
                _logger.warning("No valid columns found in mail_message_reaction table, skipping reaction migration")
                return 0
            
            query = f"SELECT {', '.join(select_columns)} FROM mail_message_reaction ORDER BY id LIMIT %s"
            cr.execute(query, (PAGE_SIZE,))
            
            reactions = cr.fetchall()
            
            for reaction_data in reactions:
                # Build reaction_vals based on available columns
                reaction_vals = {}
                col_index = 1  # Skip id column
                
                if 'message_id' in available_columns:
                    reaction_vals['message_id'] = reaction_data[col_index]
                    col_index += 1
                if 'partner_id' in available_columns:
                    reaction_vals['partner_id'] = reaction_data[col_index]
                    col_index += 1
                if 'guest_id' in available_columns:
                    reaction_vals['guest_id'] = reaction_data[col_index]
                    col_index += 1
                if 'content' in available_columns:
                    reaction_vals['content'] = reaction_data[col_index]
                    col_index += 1
                
                # Check if the referenced mail.message exists in target database
                if 'message_id' in reaction_vals:
                    referenced_message = self.env['mail.message'].browse(reaction_vals['message_id'])
                    if not referenced_message.exists():
                        # Skip reactions for non-existent messages
                        continue
                
                # Check if reaction already exists (only if we have required fields)
                if 'message_id' in reaction_vals and 'partner_id' in reaction_vals and 'content' in reaction_vals:
                    existing_reaction = self.env['mail.message.reaction'].search([
                        ('message_id', '=', reaction_vals['message_id']),
                        ('partner_id', '=', reaction_vals['partner_id']),
                        ('content', '=', reaction_vals['content'])
                    ], limit=1)
                else:
                    existing_reaction = None
                
                if not existing_reaction:
                    self.env['mail.message.reaction'].create(reaction_vals)
                    count += 1
        
        return count
    
    def _migrate_mail_tracking(self):
        """Migrate mail.tracking.value records."""
        # Check if mail.tracking.value model is available in the registry
        if 'mail.tracking.value' not in self.env:
            _logger.warning("mail.tracking.value model not available in registry, skipping tracking migration")
            return 0
            
        count = 0
        with self.get_cursor() as cr:
            # Check which columns exist in mail_tracking_value table
            cr.execute("""
                SELECT column_name FROM information_schema.columns 
                WHERE table_name = 'mail_tracking_value' AND table_schema = 'public'
            """)
            available_columns = [row[0] for row in cr.fetchall()]
            
            # Build dynamic query based on available columns
            base_columns = ['id', 'field', 'field_desc', 'field_type', 'old_value_integer', 'old_value_float',
                           'old_value_monetary', 'old_value_char', 'old_value_text', 'old_value_datetime',
                           'new_value_integer', 'new_value_float', 'new_value_monetary', 'new_value_char',
                           'new_value_text', 'new_value_datetime', 'mail_message_id']
            
            # Only include columns that exist
            select_columns = [col for col in base_columns if col in available_columns]
            
            if not select_columns:
                _logger.warning("No valid columns found in mail_tracking_value table, skipping tracking migration")
                return 0
            
            query = f"SELECT {', '.join(select_columns)} FROM mail_tracking_value ORDER BY id LIMIT %s"
            cr.execute(query, (PAGE_SIZE,))
            
            tracking_values = cr.fetchall()
            
            for tracking_data in tracking_values:
                # Build tracking_vals based on available columns
                tracking_vals = {}
                col_index = 1  # Skip id column
                
                # Map columns to their values dynamically
                # Note: Odoo 18 changed mail.tracking.value structure:
                # - 'field' (string) -> 'field_id' (Many2one to ir.model.fields) + 'field_info' (JSON)
                # - Removed 'field_desc', 'field_type', 'old_value_monetary', 'new_value_monetary'
                column_mapping = {
                    'old_value_integer': 'old_value_integer',
                    'old_value_float': 'old_value_float',
                    'old_value_char': 'old_value_char',
                    'old_value_text': 'old_value_text',
                    'old_value_datetime': 'old_value_datetime',
                    'new_value_integer': 'new_value_integer',
                    'new_value_float': 'new_value_float',
                    'new_value_char': 'new_value_char',
                    'new_value_text': 'new_value_text',
                    'new_value_datetime': 'new_value_datetime',
                    'mail_message_id': 'mail_message_id'
                }
                
                for col_name in select_columns[1:]:  # Skip id column
                    if col_name in column_mapping and col_index < len(tracking_data):
                        tracking_vals[column_mapping[col_name]] = tracking_data[col_index]
                    col_index += 1
                
                # Check if the referenced mail.message exists in target database
                if 'mail_message_id' in tracking_vals:
                    referenced_message = self.env['mail.message'].browse(tracking_vals['mail_message_id'])
                    if not referenced_message.exists():
                        # Skip tracking values for non-existent messages
                        continue
                
                # Skip existence check for tracking values due to major structural changes between Odoo 16 and 18
                # Odoo 16 uses 'field' (string), Odoo 18 uses 'field_id' (Many2one) + 'field_info' (JSON)
                # This may result in some duplicate tracking values, but avoids migration errors
                existing_tracking = None
                
                if not existing_tracking:
                    self.env['mail.tracking.value'].create(tracking_vals)
                    count += 1
        
        return count
    
    def _migrate_mail_followers(self):
        """Migrate mail.followers records (limited to sports-related models)."""
        # Check if mail.followers model is available in the registry
        if 'mail.followers' not in self.env:
            _logger.warning("mail.followers model not available in registry, skipping followers migration")
            return 0
            
        count = 0
        sports_models = ['sports.team', 'sports.patient', 'sports.patient.injury']
        
        with self.get_cursor() as cr:
            # Get available columns in mail_followers table
            cr.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'mail_followers'")
            available_columns = [row[0] for row in cr.fetchall()]
            
            # Define base columns and optional columns
            base_columns = ['id', 'res_model', 'res_id', 'partner_id']
            optional_columns = ['subtype_ids', 'create_date', 'write_date']
            
            # Build select columns list based on what's available
            select_columns = base_columns + [col for col in optional_columns if col in available_columns]
            
            for model in sports_models:
                query = f"SELECT {', '.join(select_columns)} FROM mail_followers WHERE res_model = %s ORDER BY id LIMIT %s"
                cr.execute(query, (model, PAGE_SIZE))
                
                followers = cr.fetchall()
                
                for follower_data in followers:
                    # Build follower values dynamically based on available columns
                    col_index = 1  # Skip id column
                    follower_vals = {
                        'res_model': follower_data[col_index],
                        'res_id': follower_data[col_index + 1],
                        'partner_id': follower_data[col_index + 2],
                    }
                    col_index += 3
                    
                    # Add optional fields if they exist in the source data
                    if 'subtype_ids' in available_columns and col_index < len(follower_data):
                        subtype_data = follower_data[col_index]
                        if subtype_data:
                            follower_vals['subtype_ids'] = [(6, 0, subtype_data)]
                        col_index += 1
                    
                    # Check if the referenced partner exists in target database
                    referenced_partner = self.env['res.partner'].browse(follower_vals['partner_id'])
                    if not referenced_partner.exists():
                        # Skip followers for non-existent partners
                        continue
                    
                    # Check if follower already exists
                    existing_follower = self.env['mail.followers'].search([
                        ('res_model', '=', follower_vals['res_model']),
                        ('res_id', '=', follower_vals['res_id']),
                        ('partner_id', '=', follower_vals['partner_id'])
                    ], limit=1)
                    
                    if not existing_follower:
                        self.env['mail.followers'].create(follower_vals)
                        count += 1
        
        return count
