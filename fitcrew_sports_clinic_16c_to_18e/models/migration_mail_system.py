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
            
            # Migrate mail channels FIRST (required by channel members)
            channel_count = self._migrate_mail_channels()
            
            # Migrate mail channel members
            member_count = self._migrate_mail_channel_members()
            
            # Migrate mail messages (required by notifications, reactions, tracking)
            message_count = self._migrate_mail_messages()
            
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
                f'{message_count} messages, {notification_count} notifications, {reaction_count} reactions, '
                f'{tracking_count} tracking values, {follower_count} followers migrated')
            
            return self._success_notification(
                "Mail System Migration Successful",
                f"Successfully migrated mail system data: {channel_count} channels, {member_count} members, "
                f"{message_count} messages, {notification_count} notifications, {reaction_count} reactions, "
                f"{tracking_count} tracking values, {follower_count} followers."
            )
            
        except Exception as e:
            error_msg = f'Mail system migration failed: {str(e)}'
            _logger.error(error_msg, exc_info=True)
            
            # Ensure clean transaction state before attempting status update
            try:
                self.env.cr.rollback()
                self._update_migration_status('failed', error_msg)
                self.env.cr.commit()
            except Exception as status_error:
                _logger.error(f"Failed to update migration status after error: {status_error}")
                # Continue with the original error even if status update fails
            
            raise UserError(_(error_msg))
    
    def _migrate_mail_channels(self):
        """Migrate mail.channel records to discuss.channel with robust group-first approach."""
        count = 0
        skipped_channels = 0
        skipped_channel_reasons = {}
        total_members_added = 0
        total_members_skipped = 0
        
        # Check if discuss.channel model is available in the registry (Odoo 18 rename)
        if 'discuss.channel' not in self.env:
            _logger.warning("discuss.channel model not available in registry, skipping channel migration")
            return 0
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
            
            # Get total count for progress tracking
            cr.execute("SELECT COUNT(*) FROM mail_channel WHERE active = true")
            total_count = cr.fetchone()[0]
            _logger.info(f"Found {total_count} mail channels to migrate")
            
            # Migrate all channels using pagination
            offset = 0
            batch_size = PAGE_SIZE
            
            while True:
                cr.execute(f"""
                    SELECT {columns_str}
                    FROM mail_channel WHERE active = true ORDER BY id LIMIT %s OFFSET %s
                """, (batch_size, offset))
                
                channels = cr.fetchall()
                if not channels:
                    break
                    
                _logger.info(f"Processing mail channels batch {offset}-{offset + len(channels)} of {total_count}")
                
                for channel_data in channels:
                    try:
                        # Create a dictionary mapping column names to values
                        channel_dict = dict(zip(query_columns, channel_data))
                        source_channel_id = channel_dict.get('id')
                        original_channel_type = channel_dict.get('channel_type', 'channel')
                        
                        # Check if channel already exists
                        existing_channel = self.env['discuss.channel'].with_context(active_test=False).search([
                            ('odoo16_channel_id', '=', source_channel_id)
                        ], limit=1)
                        
                        if existing_channel:
                            # Channel already exists, skip to avoid conflicts
                            _logger.debug(f"Channel {channel_dict.get('name')} already exists, skipping")
                            count += 1
                            continue
                        
                        # Fetch all members for this channel from source
                        cr.execute("""
                            SELECT id, partner_id, guest_id, custom_channel_name, fetched_message_id,
                                   seen_message_id, fold_state, is_minimized, is_pinned, last_interest_dt,
                                   last_seen_dt, create_date, write_date, create_uid, write_uid
                            FROM mail_channel_member 
                            WHERE channel_id = %s
                            ORDER BY id
                        """, (source_channel_id,))
                        
                        source_members = cr.fetchall()
                        
                        # Step 1: Create channel - use group type only for chat channels to avoid auto-member issues
                        # Other channel types should be created normally to avoid constraint violations
                        temp_channel_type = 'group' if original_channel_type == 'chat' else original_channel_type
                        
                        channel_vals = {
                            'name': channel_dict.get('name'),
                            'channel_type': temp_channel_type,
                            'active': channel_dict.get('active', True),
                            'odoo16_channel_id': source_channel_id,  # Store original ID for mapping
                        }
                        
                        # Add optional fields if they exist
                        if 'description' in channel_dict:
                            channel_vals['description'] = channel_dict['description']
                        if 'public' in channel_dict:
                            channel_vals['public'] = channel_dict['public']
                        # Handle group_public_id based on constraint: only 'channel' type can have group_public_id
                        if temp_channel_type == 'channel' and 'group_public_id' in channel_dict and channel_dict['group_public_id']:
                            # Only channel type can have group_public_id - validate group exists
                            if self.env['res.groups'].browse(channel_dict['group_public_id']).exists():
                                channel_vals['group_public_id'] = channel_dict['group_public_id']
                        # For all other types (chat, group, etc.), group_public_id must be NULL (constraint requirement)
                        if 'uuid' in channel_dict:
                            channel_vals['uuid'] = channel_dict['uuid']
                        
                        # Create channel
                        channel = self.env['discuss.channel'].create(channel_vals)
                        _logger.debug(f"Created channel '{channel.name}' as {temp_channel_type} type")
                        
                        # Step 2: Add all members explicitly
                        members_added = 0
                        members_skipped = 0
                        skipped_reasons = {}
                        
                        _logger.info(f"🔍 DEBUG: Channel '{channel.name}' (ID: {channel.id}) - Processing {len(source_members)} members from source")
                        
                        for member_data in source_members:
                            source_partner_id = member_data[1]
                            
                            # Map partner ID using odoo16_partner_id
                            target_partner = self.env['res.partner'].with_context(active_test=False).search([
                                ('odoo16_partner_id', '=', source_partner_id)
                            ], limit=1)
                            
                            # Debug logging for Marie-Claude's partner (source partner_id 3) - log to skip files
                            if source_partner_id == 3:
                                if target_partner:
                                    self.database_id._log_skipped_item(
                                        'DEBUG_PARTNER_MAPPING',
                                        3,
                                        f"SUCCESS: Found partner for source_partner_id=3: '{target_partner.name}' (ID: {target_partner.id}, odoo16_partner_id: {target_partner.odoo16_partner_id})",
                                        {'channel_id': source_channel_id, 'target_partner_name': target_partner.name}
                                    )
                                else:
                                    # Check if there are any partners with odoo16_partner_id=3
                                    all_partners_with_id_3 = self.env['res.partner'].with_context(active_test=False).search([
                                        ('odoo16_partner_id', '=', 3)
                                    ])
                                    self.database_id._log_skipped_item(
                                        'DEBUG_PARTNER_MAPPING',
                                        3,
                                        f"FAILURE: No partner found for source_partner_id=3. Found {len(all_partners_with_id_3)} partners with odoo16_partner_id=3: {[p.name for p in all_partners_with_id_3]}",
                                        {'channel_id': source_channel_id, 'all_partners_count': len(all_partners_with_id_3)}
                                    )
                            
                            if target_partner:
                                # Check if member already exists (shouldn't happen but safety check)
                                existing_member = self.env['discuss.channel.member'].search([
                                    ('channel_id', '=', channel.id),
                                    ('partner_id', '=', target_partner.id)
                                ], limit=1)
                                
                                if not existing_member:
                                    member_vals = {
                                        'channel_id': channel.id,
                                        'partner_id': target_partner.id,
                                        'fold_state': member_data[6] or 'open',
                                        'is_pinned': member_data[8] or False,
                                    }
                                    
                                    # Add optional datetime fields if they exist
                                    if member_data[9]:  # last_interest_dt
                                        member_vals['last_interest_dt'] = member_data[9]
                                    if member_data[10]:  # last_seen_dt
                                        member_vals['last_seen_dt'] = member_data[10]
                                    
                                    try:
                                        member = self.env['discuss.channel.member'].create(member_vals)
                                        
                                        # Set custom_channel_name if it exists
                                        if member_data[3]:  # custom_channel_name
                                            try:
                                                member.write({'custom_channel_name': member_data[3]})
                                            except Exception as e:
                                                _logger.warning(f"Could not set custom_channel_name for member {member.id}: {str(e)}")
                                        
                                        members_added += 1
                                        _logger.debug(f"Added member {target_partner.name} to channel {channel.name}")
                                    except Exception as e:
                                        reason = f"Failed to create member: {str(e)}"
                                        skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
                                        members_skipped += 1
                                        _logger.warning(f"Failed to add member {target_partner.name} to channel {channel.name}: {str(e)}")
                                        # Log to skip file for visibility
                                        self.database_id._log_skipped_item(
                                            'discuss.channel.member',
                                            member_data[0],
                                            f"Failed to create member: {str(e)}",
                                            {'channel_name': channel.name, 'partner_name': target_partner.name}
                                        )
                                else:
                                    _logger.debug(f"Member {target_partner.name} already exists in channel {channel.name}")
                                    members_added += 1  # Count as successful
                            else:
                                reason = f"Partner not found for odoo16_partner_id {source_partner_id}"
                                skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
                                members_skipped += 1
                                _logger.warning(f"Could not find partner for odoo16_partner_id {source_partner_id}")
                                # Log to skip file for visibility
                                self.database_id._log_skipped_item(
                                    'discuss.channel.member',
                                    member_data[0],
                                    f"Partner not found for odoo16_partner_id {source_partner_id}",
                                    {'channel_name': channel.name, 'source_partner_id': source_partner_id}
                                )
                        
                        # Step 3: Remove OdooBot if it was auto-added (shouldn't happen with group type, but safety check)
                        odoobot_partner = self.env.ref('base.partner_root', raise_if_not_found=False)
                        if odoobot_partner:
                            odoobot_member = self.env['discuss.channel.member'].search([
                                ('channel_id', '=', channel.id),
                                ('partner_id', '=', odoobot_partner.id)
                            ], limit=1)
                            if odoobot_member:
                                odoobot_member.unlink()
                                _logger.debug(f"Removed auto-added OdooBot from channel {channel.name}")
                        
                        # Step 4: Change channel type back to original if it was temporarily changed
                        # Only chat channels that were actually created as group type need restoration
                        if original_channel_type == 'chat' and temp_channel_type == 'group' and channel.channel_type == 'group':
                            try:
                                # Convert back to chat type (group_public_id is already NULL for group types)
                                channel.write({'channel_type': 'chat'})
                                _logger.debug(f"Changed channel {channel.name} back to chat type")
                            except Exception as e:
                                _logger.warning(f"Could not change channel {channel.name} back to chat type: {str(e)}")
                        elif original_channel_type == 'chat' and temp_channel_type == original_channel_type:
                            _logger.debug(f"Channel {channel.name} was created as original chat type (no group conversion used)")
                        
                        # Log skipped members for this channel
                        if members_skipped > 0:
                            for reason, count_reason in skipped_reasons.items():
                                self.database_id._log_skipped_item(
                                    'discuss.channel.member',
                                    f"channel_{source_channel_id}",
                                    reason,
                                    {'channel_name': channel.name, 'source_channel_id': source_channel_id, 'count': count_reason}
                                )
                        
                        _logger.info(f"Successfully migrated channel '{channel.name}' (type: {original_channel_type}) with {members_added} members, {members_skipped} skipped")
                        count += 1
                        total_members_added += members_added
                        total_members_skipped += members_skipped
                            
                    except Exception as e:
                        reason = f"Channel creation failed: {str(e)}"
                        self.database_id._log_skipped_item(
                            'discuss.channel',
                            source_channel_id,
                            reason,
                            {'channel_name': channel_dict.get('name', 'Unknown'), 'channel_type': channel_dict.get('channel_type')}
                        )
                        skipped_channels += 1
                        skipped_channel_reasons[reason] = skipped_channel_reasons.get(reason, 0) + 1
                        _logger.error(f"Failed to migrate mail channel ID {source_channel_id}: {str(e)}")
                        continue
                
                # Move to next batch
                offset += len(channels)
        
        # Log comprehensive migration summary
        total_processed = count + skipped_channels
        self.database_id._log_migration_summary(
            'Mail Channels (discuss.channel)', 
            total_processed, 
            skipped_channels, 
            skipped_channel_reasons
        )
        
        # Log member migration summary
        total_members_processed = total_members_added + total_members_skipped
        if total_members_processed > 0:
            member_skipped_reasons = {'Various member creation failures': total_members_skipped} if total_members_skipped > 0 else {}
            self.database_id._log_migration_summary(
                'Mail Channel Members (discuss.channel.member)', 
                total_members_processed, 
                total_members_skipped, 
                member_skipped_reasons
            )
        
        _logger.info(f"Mail channel migration completed: {count} channels migrated, {skipped_channels} skipped")
        _logger.info(f"Mail channel members: {total_members_added} members added, {total_members_skipped} skipped")
        
        return count
    
    def _migrate_mail_channel_members(self):
        """Channel members are now created atomically with channels.
        
        This method is kept for compatibility but now only handles
        updating existing members that might need additional field updates.
        """
        _logger.info("Checking for mail channel member updates (members created atomically with channels)...")
        
        updated_count = 0
        skipped_count = 0
        skipped_reasons = {}
        
        # Get all migrated channels from target database first
        migrated_channels = self.env['discuss.channel'].with_context(active_test=False).search([
            ('odoo16_channel_id', '!=', False)
        ])
        migrated_channel_ids = [ch.odoo16_channel_id for ch in migrated_channels]
        
        if not migrated_channel_ids:
            _logger.info("No migrated channels found - nothing to update")
            return 0
        
        with self.get_cursor() as cr:
            # Only check for members from migrated channels
            cr.execute("""
                SELECT COUNT(*) FROM mail_channel_member mcm
                WHERE mcm.channel_id = ANY(%s)
            """, (migrated_channel_ids,))
            total_count = cr.fetchone()[0]
            _logger.info(f"Found {total_count} potential channel member updates to check")
            
            if total_count == 0:
                _logger.info("No channel member updates needed - all members created atomically with channels")
                return 0
            
            # Check for any members that might need updates
            cr.execute("""
                SELECT mcm.id, mcm.channel_id, mcm.partner_id, mcm.custom_channel_name,
                       mcm.fold_state, mcm.is_minimized, mcm.is_pinned, mcm.last_interest_dt,
                       mcm.last_seen_dt
                FROM mail_channel_member mcm
                WHERE mcm.channel_id = ANY(%s)
                ORDER BY mcm.id
            """, (migrated_channel_ids,))
            
            members_to_check = cr.fetchall()
            
            for member_data in members_to_check:
                try:
                    source_channel_id = member_data[1]
                    source_partner_id = member_data[2]
                    
                    # Find target channel and partner
                    target_channel = self.env['discuss.channel'].with_context(active_test=False).search([
                        ('odoo16_channel_id', '=', source_channel_id)
                    ], limit=1)
                    
                    target_partner = self.env['res.partner'].with_context(active_test=False).search([
                        ('odoo16_partner_id', '=', source_partner_id)
                    ], limit=1)
                    
                    if not target_channel or not target_partner:
                        reason = "Channel or partner not found for update check"
                        self.database_id._log_skipped_item(
                            'discuss.channel.member', 
                            member_data[0], 
                            reason,
                            {'source_channel_id': source_channel_id, 'source_partner_id': source_partner_id}
                        )
                        skipped_count += 1
                        skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
                        continue
                    
                    # Check if member exists and needs updates
                    existing_member = self.env['discuss.channel.member'].with_context(active_test=False).search([
                        ('channel_id', '=', target_channel.id),
                        ('partner_id', '=', target_partner.id)
                    ], limit=1)
                    
                    if existing_member:
                        # Check if any fields need updating
                        member_vals = {}
                        if member_data[3] and not existing_member.custom_channel_name:  # custom_channel_name
                            member_vals['custom_channel_name'] = member_data[3]
                        if member_data[7] and not existing_member.last_interest_dt:  # last_interest_dt
                            member_vals['last_interest_dt'] = member_data[7]
                        if member_data[8] and not existing_member.last_seen_dt:  # last_seen_dt
                            member_vals['last_seen_dt'] = member_data[8]
                        
                        if member_vals:
                            existing_member.write(member_vals)
                            updated_fields = ', '.join(member_vals.keys())
                            _logger.debug(f"Updated discuss channel member (channel: {target_channel.name}, partner: {target_partner.name}) - Fields: {updated_fields}")
                            updated_count += 1
                    
                except Exception as e:
                    reason = f"Member update check failed: {str(e)}"
                    self.database_id._log_skipped_item(
                        'discuss.channel.member', 
                        member_data[0], 
                        reason,
                        {
                            'source_channel_id': source_channel_id if 'source_channel_id' in locals() else None,
                            'source_partner_id': source_partner_id if 'source_partner_id' in locals() else None,
                            'error': str(e)
                        }
                    )
                    skipped_count += 1
                    skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
                    continue
        
        # Log comprehensive mail channel members migration summary
        total_processed = updated_count + skipped_count
        self.database_id._log_migration_summary(
            'Mail Channel Member Updates', 
            total_processed, 
            skipped_count, 
            skipped_reasons
        )
        
        _logger.info(f"Mail channel member updates completed: {updated_count} members updated, {skipped_count} skipped")
        return updated_count
    
    def _migrate_mail_messages(self):
        """Migrate mail.message records using two-pass approach.
        
        Pass 1: Create all messages without parent_id to avoid transaction issues
        Pass 2: Update parent_id relationships after all messages are committed
        """
        _logger.info("Starting two-pass mail message migration...")
        
        # Pass 1: Create all messages without parent_id
        count = self._migrate_mail_messages_pass1()
        
        # Pass 2: Update parent_id relationships
        parent_updates = self._migrate_mail_messages_pass2()
        
        _logger.info(f"Mail messages migration completed: {count} messages created, {parent_updates} parent relationships updated")
        return count
    
    def _migrate_mail_messages_pass1(self):
        """Pass 1: Create all mail messages without parent_id relationships."""
        _logger.info("Pass 1: Creating mail messages without parent_id...")
        count = 0
        skipped_count = 0
        skipped_reasons = {}
        
        # Check if mail.message model is available in the registry
        if 'mail.message' not in self.env:
            _logger.warning("mail.message model not available in registry, skipping message migration")
            return 0
        with self.get_cursor() as cr:
            # Check which columns exist in mail_message table
            cr.execute("""
                SELECT column_name FROM information_schema.columns 
                WHERE table_name = 'mail_message' AND table_schema = 'public'
            """)
            available_columns = [row[0] for row in cr.fetchall()]
            
            # Build dynamic query based on available columns
            base_columns = ['id', 'subject', 'body', 'message_type', 'subtype_id', 'date', 
                           'res_id', 'model', 'author_id', 'parent_id', 'reply_to']
            optional_columns = ['email_from', 'message_id', 'reply_to_message_id', 'is_internal',
                               'create_date', 'create_uid', 'write_date', 'write_uid']
            
            # Only include columns that exist
            select_columns = base_columns + [col for col in optional_columns if col in available_columns]
            
            # Get total count for progress tracking
            cr.execute("SELECT COUNT(*) FROM mail_message")
            total_count = cr.fetchone()[0]
            _logger.info(f"Found {total_count} mail messages to migrate")
            
            # Migrate all messages using pagination
            batch_size = PAGE_SIZE
            offset = 0
            
            while True:
                query = f"SELECT {', '.join(select_columns)} FROM mail_message ORDER BY id LIMIT %s OFFSET %s"
                cr.execute(query, (batch_size, offset))
                messages = cr.fetchall()
                
                if not messages:
                    break
                    
                _logger.info(f"Processing mail messages batch {offset}-{offset + len(messages)} of {total_count}")
                
                for message_data in messages:
                    try:
                        # Build message values from available columns
                        message_vals = {}
                        for i, col in enumerate(select_columns):
                            if col == 'author_id' and message_data[i]:
                                # Map author_id from Odoo 16 to migrated partner ID
                                source_author_id = message_data[i]
                                migrated_author = self.env['res.partner'].with_context(active_test=False).search([
                                    ('odoo16_partner_id', '=', source_author_id)
                                ], limit=1)
                                
                                if migrated_author:
                                    message_vals['author_id'] = migrated_author.id
                                else:
                                    _logger.warning(f"Could not find migrated partner for author_id {source_author_id} in mail message {message_data[0]} - using admin")
                                    message_vals['author_id'] = self.env.ref('base.partner_admin').id
                            elif col == 'create_uid' and message_data[i]:
                                # Map create_uid from Odoo 16 to migrated user ID
                                source_user_id = message_data[i]
                                migrated_user = self.env['res.users'].with_context(active_test=False).search([
                                    ('odoo16_user_id', '=', source_user_id)
                                ], limit=1)
                                
                                if migrated_user:
                                    # Debug logging for Marie-Claude Leblanc (source user_id 2)
                                    if source_user_id == 2:
                                        _logger.info(f"🔍 DEBUG: Found user for source_user_id=2: '{migrated_user.login}' (ID: {migrated_user.id}, odoo16_user_id: {migrated_user.odoo16_user_id})")
                                    message_vals['create_uid'] = migrated_user.id
                                else:
                                    # Debug logging when no user found
                                    if source_user_id == 2:
                                        _logger.warning(f"🔍 DEBUG: No user found for source_user_id=2")
                                # If no migrated user found, let Odoo use current user as default
                            # Skip parent_id in Pass 1 - will be handled in Pass 2
                            elif col == 'res_id' and message_data[i]:
                                # Handle res_id mapping based on model type
                                res_id = message_data[i]
                                model_col_index = select_columns.index('model') if 'model' in select_columns else None
                                
                                if model_col_index is not None:
                                    model_name = message_data[model_col_index]
                                    if model_name:  # Only process if model is not None/empty
                                        # Handle model name mapping for Odoo 18 renames
                                        target_model_name = self._map_model_name(model_name)
                                        
                                        # Debug logging for troubleshooting
                                        _logger.debug(f"Processing mail.message {message_data[0]}: mapping res_id {res_id} for model {model_name} -> {target_model_name}")
                                        
                                        mapped_res_id = self._map_record_id(model_name, res_id)
                                        if mapped_res_id:
                                            message_vals['res_id'] = mapped_res_id
                                            message_vals['model'] = target_model_name  # Use mapped model name
                                            _logger.debug(f"Successfully mapped: {model_name}#{res_id} -> {target_model_name}#{mapped_res_id}")
                                        else:
                                            # Skip message if we can't map the referenced record
                                            reason = f"Could not map res_id {res_id} for model {model_name}"
                                            _logger.debug(f"Failed to map: {model_name}#{res_id} - record not found in target DB")
                                            self.database_id._log_skipped_item(
                                                'mail.message', 
                                                message_data[0], 
                                                reason,
                                                {'model': model_name, 'res_id': res_id}
                                            )
                                            skipped_count += 1
                                            skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
                                            continue
                                    else:
                                        # No model specified, use res_id as-is
                                        message_vals[col] = message_data[i]
                                else:
                                    # No model column available, use res_id as-is
                                    message_vals[col] = message_data[i]
                            elif col not in ['model', 'parent_id']:  # Skip model and parent_id - they have special handling
                                message_vals[col] = message_data[i]
                        
                        # CRITICAL: Set odoo16_message_id for proper mapping in parent update pass
                        message_vals['odoo16_message_id'] = message_data[0]
                        
                        # Use merge functionality to create or update message
                        search_domain = [('odoo16_message_id', '=', message_data[0])]  # Use odoo16_message_id for proper mapping
                        
                        record_identifier = f"mail message {message_data[0]}"
                        message, action = self.database_id._create_or_update_record(
                            'mail.message',
                            search_domain,
                            message_vals,
                            record_identifier
                        )
                        
                        if action in ['created', 'updated']:
                            count += 1
                            # Debug: Verify odoo16_message_id was set correctly
                            if message_data[0] in [867, 13937, 13938, 13939]:  # Sample problematic IDs
                                _logger.info(f"🔍 DEBUG: Message {message_data[0]} {action} - Target ID: {message.id}, odoo16_message_id: {message.odoo16_message_id}")
                                # Log to skip file for visibility
                                self.database_id._log_skipped_item(
                                    'DEBUG_MESSAGE_CREATION',
                                    message_data[0],
                                    f"Message {action} - Target ID: {message.id}, odoo16_message_id: {message.odoo16_message_id}",
                                    {'action': action, 'target_id': message.id}
                                )
                            
                    except Exception as e:
                        # Log detailed error information
                        reason = f"Message creation failed: {str(e)}"
                        _logger.error(f"Error migrating mail message {message_data[0]}: {e}")
                        
                        # Extract context information for better debugging
                        context_info = {
                            'subject': message_vals.get('subject', 'No subject'),
                            'model': message_vals.get('model', 'No model'),
                            'res_id': message_vals.get('res_id', 'No res_id'),
                            'parent_id': message_vals.get('parent_id', 'No parent_id'),
                            'author_id': message_vals.get('author_id', 'No author_id'),
                            'error': str(e)
                        }
                        
                        # Log to skipped items file
                        self.database_id._log_skipped_item(
                            'mail.message',
                            message_data[0],
                            reason,
                            context_info
                        )
                        
                        # Update skipped count and reasons
                        skipped_count += 1
                        skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
                        
                        # Rollback transaction to clear aborted state
                        try:
                            self.env.cr.rollback()
                        except Exception as rollback_error:
                            _logger.error(f"Failed to rollback transaction after mail message error: {rollback_error}")
                        continue
                
                offset += len(messages)
                
        # Log migration summary
        total_processed = count + skipped_count
        self.database_id._log_migration_summary(
            'Mail Messages', 
            total_processed, 
            skipped_count, 
            skipped_reasons
        )
        
        # Commit Pass 1 changes before starting Pass 2
        self.env.cr.commit()
        _logger.info(f"Pass 1 completed: {count} messages created, {skipped_count} skipped. Starting Pass 2...")
        return count
    
    def _migrate_mail_messages_pass2(self):
        """Pass 2: Update parent_id relationships after all messages are committed."""
        _logger.info("Pass 2: Updating parent_id relationships...")
        updated_count = 0
        failed_count = 0
        
        with self.get_cursor() as cr:
            # Get all messages from source that have parent_id
            cr.execute("""
                SELECT id, parent_id 
                FROM mail_message 
                WHERE parent_id IS NOT NULL 
                ORDER BY id
            """)
            parent_relationships = cr.fetchall()
            
            _logger.info(f"Found {len(parent_relationships)} messages with parent relationships to update")
            
            for source_message_id, source_parent_id in parent_relationships:
                try:
                    # Validate parent_id is a positive integer
                    if not (isinstance(source_parent_id, (int, float)) and source_parent_id > 0):
                        _logger.warning(f"Invalid parent_id value '{source_parent_id}' for message {source_message_id} - skipping")
                        failed_count += 1
                        continue
                    
                    # Find the migrated child message
                    child_message = self.env['mail.message'].with_context(active_test=False).search([
                        ('odoo16_message_id', '=', source_message_id)
                    ], limit=1)
                    
                    if not child_message:
                        _logger.warning(f"Could not find migrated child message for source ID {source_message_id} - skipping parent update")
                        failed_count += 1
                        continue
                    
                    # Find the migrated parent message
                    parent_message = self.env['mail.message'].with_context(active_test=False).search([
                        ('odoo16_message_id', '=', int(source_parent_id))
                    ], limit=1)
                    
                    if parent_message:
                        # Update the parent_id relationship
                        child_message.write({'parent_id': parent_message.id})
                        updated_count += 1
                        _logger.debug(f"Updated parent_id: message {child_message.id} -> parent {parent_message.id} (source: {source_message_id} -> {source_parent_id})")
                    else:
                        _logger.warning(f"Could not find migrated parent message for parent_id {source_parent_id} (child: {source_message_id}) - parent may have been skipped")
                        failed_count += 1
                        
                except Exception as e:
                    _logger.error(f"Error updating parent_id for message {source_message_id}: {e}")
                    failed_count += 1
                    continue
        
        # Commit Pass 2 changes
        self.env.cr.commit()
        _logger.info(f"Pass 2 completed: {updated_count} parent relationships updated, {failed_count} failed")
        return updated_count
    
    def _map_model_name(self, source_model_name):
        """Map model names from Odoo 16 to Odoo 18 for renamed models."""
        model_mapping = {
            'mail.channel': 'discuss.channel',
            'mail.channel.member': 'discuss.channel.member',
            'calendar.event': 'project.task',  # Calendar events migrated to project tasks
            # Add other model renames here as needed
        }
        return model_mapping.get(source_model_name, source_model_name)
    
    def _get_model_tracking_field(self, model_name):
        """Get the odoo16_*_id tracking field name for a given model."""
        # Comprehensive mapping of models to their tracking field names
        model_field_mapping = {
            # Core models
            'res.partner': 'odoo16_partner_id',
            'res.users': 'odoo16_user_id',
            
            # Sports models
            'sports.patient': 'odoo16_patient_id',
            'sports.patient.injury': 'odoo16_injury_id',
            'sports.patient.contact': 'odoo16_contact_id',
            'sports.team': 'odoo16_team_id',  # Note: teams might use direct ID mapping
            
            # Mail system models (renamed in Odoo 18)
            'mail.channel': 'odoo16_channel_id',  # → discuss.channel
            'mail.channel.member': 'odoo16_member_id',  # → discuss.channel.member
            'mail.message': 'odoo16_message_id',
            'mail.notification': 'odoo16_notification_id',
            'mail.activity': 'odoo16_activity_id',
            
            # Project models
            'project.project': 'odoo16_project_id',
            'project.task': 'odoo16_task_id',
            
            # Calendar models
            'calendar.event': 'odoo16_event_id',
            
            # IR models
            'ir.attachment': 'odoo16_attachment_id',
            'ir.filters': 'odoo16_filter_id',
        }
        
        return model_field_mapping.get(model_name)
    
    def _map_record_id(self, model_name, source_res_id):
        """Map a record ID from source to target database based on model type."""
        try:
            _logger.debug(f"_map_record_id: {model_name}#{source_res_id}")
            
            # Explicit mapping for each migrated model
            if model_name == 'res.partner':
                target_record = self.env['res.partner'].with_context(active_test=False).search([
                    ('odoo16_partner_id', '=', source_res_id)
                ], limit=1)
                result = target_record.id if target_record else None
                
                # Enhanced debugging for Marie-Claude's partner (source partner_id 3) - log to skip files
                if source_res_id == 3:
                    if target_record:
                        self.database_id._log_skipped_item(
                            'DEBUG_PARTNER_MAP_RECORD_ID',
                            3,
                            f"SUCCESS: _map_record_id found partner for source_res_id=3: '{target_record.name}' (ID: {target_record.id}, odoo16_partner_id: {target_record.odoo16_partner_id})",
                            {'model_name': model_name, 'target_record_id': target_record.id}
                        )
                    else:
                        # Check all partners with odoo16_partner_id=3
                        all_partners_3 = self.env['res.partner'].with_context(active_test=False).search([
                            ('odoo16_partner_id', '=', 3)
                        ])
                        self.database_id._log_skipped_item(
                            'DEBUG_PARTNER_MAP_RECORD_ID',
                            3,
                            f"FAILURE: _map_record_id found NO partner for source_res_id=3. All partners with odoo16_partner_id=3: {len(all_partners_3)} found - {[f'{p.name}(ID:{p.id})' for p in all_partners_3]}",
                            {'model_name': model_name, 'all_partners_count': len(all_partners_3)}
                        )
                
                _logger.debug(f"Partner mapping: {source_res_id} -> {result}")
                return result
                
            elif model_name == 'res.users':
                target_record = self.env['res.users'].with_context(active_test=False).search([
                    ('odoo16_user_id', '=', source_res_id)
                ], limit=1)
                result = target_record.id if target_record else None
                _logger.debug(f"User mapping: {source_res_id} -> {result}")
                return result
                
            elif model_name == 'sports.patient':
                target_record = self.env['sports.patient'].with_context(active_test=False).search([
                    ('odoo16_patient_id', '=', source_res_id)
                ], limit=1)
                result = target_record.id if target_record else None
                _logger.debug(f"Patient mapping: {source_res_id} -> {result}")
                return result
                
            elif model_name == 'sports.patient.injury':
                target_record = self.env['sports.patient.injury'].with_context(active_test=False).search([
                    ('odoo16_injury_id', '=', source_res_id)
                ], limit=1)
                result = target_record.id if target_record else None
                _logger.debug(f"Injury mapping: {source_res_id} -> {result}")
                return result
                
            elif model_name == 'sports.patient.contact':
                target_record = self.env['sports.patient.contact'].with_context(active_test=False).search([
                    ('odoo16_contact_id', '=', source_res_id)
                ], limit=1)
                result = target_record.id if target_record else None
                _logger.debug(f"Patient contact mapping: {source_res_id} -> {result}")
                return result
                
            elif model_name == 'sports.team':
                # Sports teams use direct ID mapping (no tracking field)
                target_record = self.env['sports.team'].browse(source_res_id)
                result = target_record.id if target_record.exists() else None
                _logger.debug(f"Team direct mapping: {source_res_id} -> {result}")
                return result
                
            elif model_name == 'mail.channel':
                # mail.channel renamed to discuss.channel in Odoo 18
                target_record = self.env['discuss.channel'].with_context(active_test=False).search([
                    ('odoo16_channel_id', '=', source_res_id)
                ], limit=1)
                result = target_record.id if target_record else None
                _logger.debug(f"Channel mapping: {source_res_id} -> {result}")
                return result
                
            elif model_name == 'mail.message':
                target_record = self.env['mail.message'].with_context(active_test=False).search([
                    ('odoo16_message_id', '=', source_res_id)
                ], limit=1)
                result = target_record.id if target_record else None
                _logger.debug(f"Message mapping: {source_res_id} -> {result}")
                return result
                
            elif model_name == 'mail.activity':
                target_record = self.env['mail.activity'].with_context(active_test=False).search([
                    ('odoo16_activity_id', '=', source_res_id)
                ], limit=1)
                result = target_record.id if target_record else None
                _logger.debug(f"Activity mapping: {source_res_id} -> {result}")
                return result
                
            elif model_name == 'project.task':
                # Calendar events are migrated to project tasks
                target_record = self.env['project.task'].with_context(active_test=False).search([
                    ('odoo16_task_id', '=', source_res_id)
                ], limit=1)
                result = target_record.id if target_record else None
                _logger.debug(f"Task mapping: {source_res_id} -> {result}")
                return result
                
            elif model_name == 'calendar.event':
                # Calendar events are migrated to project tasks
                target_record = self.env['project.task'].with_context(active_test=False).search([
                    ('odoo16_event_id', '=', source_res_id)
                ], limit=1)
                result = target_record.id if target_record else None
                _logger.debug(f"Calendar event -> task mapping: {source_res_id} -> {result}")
                return result
                
            elif model_name == 'ir.attachment':
                target_record = self.env['ir.attachment'].with_context(active_test=False).search([
                    ('odoo16_attachment_id', '=', source_res_id)
                ], limit=1)
                result = target_record.id if target_record else None
                _logger.debug(f"Attachment mapping: {source_res_id} -> {result}")
                return result
                
            else:
                # Unknown model - try direct ID lookup as fallback
                _logger.warning(f"Unknown model '{model_name}' - trying direct ID lookup")
                if model_name in self.env:
                    target_record = self.env[model_name].browse(source_res_id)
                    result = target_record.id if target_record.exists() else None
                    _logger.debug(f"Direct ID fallback for {model_name}: {source_res_id} -> {result}")
                    return result
                else:
                    _logger.warning(f"Model '{model_name}' not available in environment")
                    return None
                
        except Exception as e:
            _logger.warning(f"Error mapping record ID {source_res_id} for model {model_name}: {e}")
            return None
    
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
            
            # Get total count for progress tracking
            cr.execute("SELECT COUNT(*) FROM mail_notification")
            total_count = cr.fetchone()[0]
            _logger.info(f"Found {total_count} mail notifications to migrate")
            
            # Migrate all notifications using pagination
            offset = 0
            batch_size = PAGE_SIZE
            
            while True:
                query = f"SELECT {', '.join(select_columns)} FROM mail_notification ORDER BY id LIMIT %s OFFSET %s"
                cr.execute(query, (batch_size, offset))
                
                notifications = cr.fetchall()
                if not notifications:
                    break
                    
                _logger.info(f"Processing mail notifications batch {offset}-{offset + len(notifications)} of {total_count}")
                
                for notification_data in notifications:
                    # Map res_partner_id from Odoo 16 to migrated partner ID
                    source_partner_id = notification_data[2]
                    migrated_partner = self.env['res.partner'].with_context(active_test=False).search([
                        ('odoo16_partner_id', '=', source_partner_id)
                    ], limit=1)
                    
                    if not migrated_partner:
                        _logger.warning(f"Could not find migrated partner for partner_id {source_partner_id} in mail notification - skipping")
                        continue
                    
                    # Find the migrated mail.message using odoo16_message_id FIRST
                    source_message_id = notification_data[1]
                    referenced_message = self.env['mail.message'].search([
                        ('odoo16_message_id', '=', source_message_id)
                    ], limit=1)
                    if not referenced_message:
                        # Skip notifications for non-migrated messages (legitimately skipped due to missing parent objects)
                        self.database_id._log_skipped_item(
                            'mail.notification',
                            notification_data[0],
                            f"Referenced message {source_message_id} not found in migrated messages",
                            {'source_message_id': source_message_id, 'source_partner_id': source_partner_id}
                        )
                        continue
                    
                    notification_vals = {
                        'mail_message_id': referenced_message.id,  # ✅ CORRECT: Use migrated message ID
                        'res_partner_id': migrated_partner.id,     # ✅ CORRECT: Use migrated partner ID
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
                    
                    # Use merge functionality to create or update notification
                    search_domain = [
                        ('mail_message_id', '=', referenced_message.id),  # Use migrated message ID
                        ('res_partner_id', '=', migrated_partner.id)  # Use migrated partner ID
                    ]
                    
                    record_identifier = f"mail notification (message: {notification_data[1]}, partner: {migrated_partner.id} [orig: {source_partner_id}])"
                    notification, action = self.database_id._create_or_update_record(
                        'mail.notification',
                        search_domain,
                        notification_vals,
                        record_identifier
                    )
                    
                    if action in ['created', 'updated']:
                        count += 1
                
                # Move to next batch
                offset += len(notifications)
        
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
            
            # Get total count for progress tracking
            cr.execute("SELECT COUNT(*) FROM mail_message_reaction")
            total_count = cr.fetchone()[0]
            _logger.info(f"Found {total_count} mail message reactions to migrate")
            
            # Migrate all reactions using pagination
            offset = 0
            batch_size = PAGE_SIZE
            
            while True:
                query = f"SELECT {', '.join(select_columns)} FROM mail_message_reaction ORDER BY id LIMIT %s OFFSET %s"
                cr.execute(query, (batch_size, offset))
                
                reactions = cr.fetchall()
                if not reactions:
                    break
                    
                _logger.info(f"Processing mail message reactions batch {offset}-{offset + len(reactions)} of {total_count}")
                
                for reaction_data in reactions:
                    # Build reaction_vals based on available columns
                    reaction_vals = {}
                    col_index = 1  # Skip id column
                    
                    if 'message_id' in available_columns:
                        reaction_vals['message_id'] = reaction_data[col_index]
                        col_index += 1
                    if 'partner_id' in available_columns:
                        source_partner_id = reaction_data[col_index]
                        # Map partner_id from Odoo 16 to migrated partner ID
                        migrated_partner = self.env['res.partner'].with_context(active_test=False).search([
                            ('odoo16_partner_id', '=', source_partner_id)
                        ], limit=1)
                        
                        if migrated_partner:
                            reaction_vals['partner_id'] = migrated_partner.id
                        else:
                            _logger.warning(f"Could not find migrated partner for partner_id {source_partner_id} in mail reaction - skipping")
                            continue
                        col_index += 1
                    if 'guest_id' in available_columns:
                        reaction_vals['guest_id'] = reaction_data[col_index]
                        col_index += 1
                    if 'content' in available_columns:
                        reaction_vals['content'] = reaction_data[col_index]
                        col_index += 1
                    
                    # Find the migrated mail.message using odoo16_message_id
                    if 'message_id' in reaction_vals:
                        source_message_id = reaction_vals['message_id']
                        referenced_message = self.env['mail.message'].search([
                            ('odoo16_message_id', '=', source_message_id)
                        ], limit=1)
                        if not referenced_message:
                            # Skip reactions for non-migrated messages
                            continue
                        # Update reaction_vals to use migrated message ID
                        reaction_vals['message_id'] = referenced_message.id
                    
                    # Use merge functionality to create or update reaction (only if we have required fields)
                    if 'message_id' in reaction_vals and 'partner_id' in reaction_vals and 'content' in reaction_vals:
                        search_domain = [
                            ('message_id', '=', reaction_vals['message_id']),
                            ('partner_id', '=', reaction_vals['partner_id']),
                            ('content', '=', reaction_vals['content'])
                        ]
                        
                        record_identifier = f"mail reaction (message: {reaction_vals['message_id']}, partner: {reaction_vals['partner_id']}, content: {reaction_vals['content']})"
                        reaction, action = self.database_id._create_or_update_record(
                            'mail.message.reaction',
                            search_domain,
                            reaction_vals,
                            record_identifier
                        )
                        
                        if action in ['created', 'updated']:
                            count += 1
                
                # Move to next batch
                offset += len(reactions)
        
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
            
            # Get total count for progress tracking
            cr.execute("SELECT COUNT(*) FROM mail_tracking_value")
            total_count = cr.fetchone()[0]
            _logger.info(f"Starting migration of {total_count} mail tracking values")
            
            batch_size = PAGE_SIZE
            offset = 0
            
            while True:
                query = f"SELECT {', '.join(select_columns)} FROM mail_tracking_value ORDER BY id LIMIT %s OFFSET %s"
                cr.execute(query, (batch_size, offset))
                
                tracking_values = cr.fetchall()
                if not tracking_values:
                    break
                    
                _logger.info(f"Processing mail tracking values batch {offset}-{offset + len(tracking_values)} of {total_count}")
                
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
                    
                    # Find the migrated mail.message using odoo16_message_id
                    if 'mail_message_id' in tracking_vals:
                        source_message_id = tracking_vals['mail_message_id']
                        referenced_message = self.env['mail.message'].search([
                            ('odoo16_message_id', '=', source_message_id)
                        ], limit=1)
                        if not referenced_message:
                            # Skip tracking values for non-migrated messages
                            continue
                        # Update tracking_vals to use migrated message ID
                        tracking_vals['mail_message_id'] = referenced_message.id
                    
                    # Skip existence check for tracking values due to major structural changes between Odoo 16 and 18
                    # Odoo 16 uses 'field' (string), Odoo 18 uses 'field_id' (Many2one) + 'field_info' (JSON)
                    # This may result in some duplicate tracking values, but avoids migration errors
                    existing_tracking = None
                    
                    if not existing_tracking:
                        self.env['mail.tracking.value'].create(tracking_vals)
                        count += 1
                
                # Move to next batch
                offset += len(tracking_values)
        
        return count
    
    def _migrate_mail_followers(self):
        """Migrate all mail.followers records from source database."""
        # Check if mail.followers model is available in the registry
        if 'mail.followers' not in self.env:
            _logger.warning("mail.followers model not available in registry, skipping followers migration")
            return 0
            
        count = 0
        skipped_count = 0
        
        with self.get_cursor() as cr:
            # Get available columns in mail_followers table
            cr.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'mail_followers'")
            available_columns = [row[0] for row in cr.fetchall()]
            
            # Define base columns and optional columns
            base_columns = ['id', 'res_model', 'res_id', 'partner_id']
            optional_columns = ['subtype_ids', 'create_date', 'write_date']
            
            # Build select columns list based on what's available
            select_columns = base_columns + [col for col in optional_columns if col in available_columns]
            
            # Get total count for progress tracking
            cr.execute("SELECT COUNT(*) FROM mail_followers")
            total_count = cr.fetchone()[0]
            _logger.info(f"Found {total_count} mail.followers records to migrate")
            
            # Migrate all followers without model filtering, using pagination
            offset = 0
            batch_size = PAGE_SIZE
            
            while True:
                query = f"SELECT {', '.join(select_columns)} FROM mail_followers ORDER BY id LIMIT %s OFFSET %s"
                cr.execute(query, (batch_size, offset))
                
                followers = cr.fetchall()
                if not followers:
                    break
                    
                _logger.info(f"Processing followers batch {offset}-{offset + len(followers)} of {total_count}")
                
                for follower_data in followers:
                    try:
                        # Build follower values dynamically based on available columns
                        col_index = 1  # Skip id column
                        source_res_model = follower_data[col_index]
                        source_res_id = follower_data[col_index + 1]
                        source_partner_id = follower_data[col_index + 2]
                        col_index += 3
                        
                        # Map partner ID using odoo16_partner_id
                        referenced_partner = self.env['res.partner'].with_context(active_test=False).search([
                            ('odoo16_partner_id', '=', source_partner_id)
                        ], limit=1)
                        if not referenced_partner:
                            skipped_count += 1
                            continue
                        
                        # Map target record ID using appropriate odoo16_*_id field
                        target_res_id = None
                        if source_res_model == 'sports.patient':
                            target_record = self.env['sports.patient'].with_context(active_test=False).search([
                                ('odoo16_patient_id', '=', source_res_id)
                            ], limit=1)
                            if target_record:
                                target_res_id = target_record.id
                        elif source_res_model == 'sports.patient.injury':
                            target_record = self.env['sports.patient.injury'].with_context(active_test=False).search([
                                ('odoo16_injury_id', '=', source_res_id)
                            ], limit=1)
                            if target_record:
                                target_res_id = target_record.id
                        elif source_res_model == 'res.partner':
                            target_record = self.env['res.partner'].with_context(active_test=False).search([
                                ('odoo16_partner_id', '=', source_res_id)
                            ], limit=1)
                            if target_record:
                                target_res_id = target_record.id
                        elif source_res_model == 'res.users':
                            target_record = self.env['res.users'].with_context(active_test=False).search([
                                ('odoo16_user_id', '=', source_res_id)
                            ], limit=1)
                            if target_record:
                                target_res_id = target_record.id
                        elif source_res_model == 'sports.team':
                            # Sports teams use direct ID mapping
                            target_record = self.env['sports.team'].browse(source_res_id)
                            if target_record.exists():
                                target_res_id = target_record.id
                        elif source_res_model == 'mail.channel':
                            # mail.channel renamed to discuss.channel in Odoo 18
                            target_record = self.env['discuss.channel'].with_context(active_test=False).search([
                                ('odoo16_channel_id', '=', source_res_id)
                            ], limit=1)
                            if target_record:
                                target_res_id = target_record.id
                                # Update res_model to the new model name
                                source_res_model = 'discuss.channel'
                        else:
                            # For other models, check if they exist in target environment
                            if source_res_model in self.env:
                                target_record = self.env[source_res_model].browse(source_res_id)
                                if target_record.exists():
                                    target_res_id = target_record.id
                        
                        if not target_res_id:
                            skipped_count += 1
                            continue
                        
                        follower_vals = {
                            'res_model': source_res_model,
                            'res_id': target_res_id,
                            'partner_id': referenced_partner.id,
                        }
                        
                        # Add optional fields if they exist in the source data
                        if 'subtype_ids' in available_columns and col_index < len(follower_data):
                            subtype_data = follower_data[col_index]
                            if subtype_data:
                                follower_vals['subtype_ids'] = [(6, 0, subtype_data)]
                            col_index += 1
                        
                        # Use merge functionality to create or update follower
                        search_domain = [
                            ('res_model', '=', source_res_model),
                            ('res_id', '=', target_res_id),
                            ('partner_id', '=', referenced_partner.id)
                        ]
                        
                        record_identifier = f"mail.followers on {source_res_model}({target_res_id}) for partner {referenced_partner.id}"
                        
                        follower, action = self.database_id._create_or_update_record(
                            'mail.followers',
                            search_domain,
                            follower_vals,
                            record_identifier
                        )
                        
                        if action in ['created', 'updated']:
                            count += 1
                            
                    except Exception as e:
                        _logger.error(f"Failed to migrate follower: {str(e)}")
                        skipped_count += 1
                        continue
                
                offset += batch_size
            
            _logger.info(f"Mail followers migration completed: {count} migrated, {skipped_count} skipped")
        
        return count
