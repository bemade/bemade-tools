from odoo import models, fields, api, _
from odoo.exceptions import UserError
from .odoo16_database_base import Odoo16DatabaseBase, PAGE_SIZE
import logging

_logger = logging.getLogger(__name__)


class MigrationUsersPartners(models.Model):
    """Migration methods for res.users and res.partners."""
    _name = 'migration.users.partners'
    _description = 'Users and Partners Migration'
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
    
    def action_migrate_users_partners(self):
        """Migrate all res.users and res.partners from Odoo 16."""
        try:
            self._update_migration_status('in_progress', 'Starting users and partners migration')
            
            # Create a mapping to track Odoo 16 -> Odoo 18 partner ID relationships
            partner_id_mapping = {}
            
            with self.get_cursor() as cr:
                # Check which columns exist in res_partner table
                cr.execute("""
                    SELECT column_name FROM information_schema.columns 
                    WHERE table_name = 'res_partner' AND table_schema = 'public'
                """)
                available_columns = [row[0] for row in cr.fetchall()]
                
                # Build dynamic query based on available columns
                base_columns = ['id', 'name', 'email', 'phone', 'mobile', 'website', 'street', 'street2', 'city', 'zip']
                optional_columns = ['country_id', 'state_id', 'category_id', 'parent_id', 'is_company', 'supplier_rank', 'customer_rank', 'active', 'create_date', 'write_date', 'create_uid', 'write_uid']
                
                # Only include columns that exist
                query_columns = base_columns + [col for col in optional_columns if col in available_columns]
                columns_str = ', '.join(query_columns)
                
                # First migrate res.partner records
                cr.execute(f"""
                    SELECT {columns_str}
                    FROM res_partner ORDER BY id LIMIT %s
                """, (PAGE_SIZE,))
                
                partners = cr.fetchall()
                partner_count = 0
                total_partners_in_source = len(partners)
                skipped_partners = 0
                created_partners = 0
                
                for partner_data in partners:
                    # Create a dictionary mapping column names to values
                    partner_dict = dict(zip(query_columns, partner_data))
                    
                    # Skip system partners that shouldn't be migrated (check early to avoid warnings)
                    partner_name = partner_dict.get('name')
                    if not partner_name:
                        _logger.info(f"⚠️ SKIPPED partner ID {partner_dict.get('id')}: No name provided")
                        skipped_partners += 1
                        continue  # Skip partners without names
                    
                    system_partner_names = [
                        'OdooBot', 'Public user', 'Default User Template', 
                        'Portal User Template', 'Administrator'
                    ]
                    if partner_name in system_partner_names:
                        _logger.info(f"⚠️ SKIPPED system partner ID {partner_dict.get('id')}: '{partner_name}' (system partner)")
                        skipped_partners += 1
                        continue
                    
                    # Build partner values dynamically based on available columns
                    partner_vals = {
                        'name': partner_dict.get('name'),
                        'email': partner_dict.get('email'),
                        'phone': partner_dict.get('phone'),
                        'mobile': partner_dict.get('mobile'),
                        'website': partner_dict.get('website'),
                        'street': partner_dict.get('street'),
                        'street2': partner_dict.get('street2'),
                        'city': partner_dict.get('city'),
                        'zip': partner_dict.get('zip'),
                        'odoo16_partner_id': partner_dict.get('id'),  # Store original Odoo 16 partner ID for efficient lookups
                    }
                    
                    # Add optional fields if they exist, validating foreign key references
                    if 'country_id' in partner_dict and partner_dict['country_id']:
                        # Validate country exists
                        if self.env['res.country'].browse(partner_dict['country_id']).exists():
                            partner_vals['country_id'] = partner_dict['country_id']
                    
                    if 'state_id' in partner_dict and partner_dict['state_id']:
                        # Validate state exists
                        if self.env['res.country.state'].browse(partner_dict['state_id']).exists():
                            partner_vals['state_id'] = partner_dict['state_id']
                    
                    if 'category_id' in partner_dict and partner_dict['category_id']:
                        # Validate category exists
                        if self.env['res.partner.category'].browse(partner_dict['category_id']).exists():
                            partner_vals['category_id'] = [(6, 0, [partner_dict['category_id']])]
                    
                    if 'parent_id' in partner_dict and partner_dict['parent_id']:
                        # Validate parent partner exists
                        if self.env['res.partner'].browse(partner_dict['parent_id']).exists():
                            partner_vals['parent_id'] = partner_dict['parent_id']
                    
                    if 'is_company' in partner_dict:
                        partner_vals['is_company'] = partner_dict['is_company']
                    if 'supplier_rank' in partner_dict:
                        partner_vals['supplier_rank'] = partner_dict['supplier_rank']
                    if 'customer_rank' in partner_dict:
                        partner_vals['customer_rank'] = partner_dict['customer_rank']
                    if 'active' in partner_dict:
                        partner_vals['active'] = partner_dict['active']
                    
                    # Create partner directly without deduplication (preserve source data as-is)
                    try:
                        # Use savepoint to isolate this operation
                        with self.env.cr.savepoint():
                            # Skip partners without basic required information
                            if not partner_dict.get('name'):
                                _logger.warning(f"⚠️ SKIPPED partner ID {partner_dict.get('id')}: No name provided")
                                skipped_partners += 1
                                continue
                                
                            # Create partner directly (no deduplication)
                            partner = self.env['res.partner'].create(partner_vals)
                            
                            # Log successful creation
                            _logger.info(f"✅ CREATED partner ID {partner_dict.get('id')}: '{partner_dict.get('name')}' (email: {partner_dict.get('email')})")
                            created_partners += 1
                            partner_count += 1
                                
                            # Store the mapping between Odoo 16 and Odoo 18 partner IDs
                            odoo16_partner_id = partner_dict.get('id')
                            if odoo16_partner_id and partner:
                                partner_id_mapping[odoo16_partner_id] = partner.id
                                
                    except Exception as e:
                        # Log the error but continue with other partners
                        _logger.warning(f"⚠️ FAILED to process partner ID {partner_dict.get('id')} '{partner_dict.get('name')}' (email: {partner_dict.get('email')}): {str(e)}")
                        skipped_partners += 1
                        continue
                
                # Log partner migration summary
                _logger.info(f"📊 PARTNER MIGRATION SUMMARY: Total in source: {total_partners_in_source}, Created: {created_partners}, Skipped: {skipped_partners}")
                
                # Check which columns exist in res_users table
                cr.execute("""
                    SELECT column_name FROM information_schema.columns 
                    WHERE table_name = 'res_users' AND table_schema = 'public'
                """)
                user_available_columns = [row[0] for row in cr.fetchall()]
                
                # Build dynamic query for users based on available columns
                user_base_columns = ['id', 'login', 'partner_id', 'active']
                user_optional_columns = ['password', 'company_id', 'create_date', 'write_date', 'create_uid', 'write_uid', 'signature', 'notification_type', 'odoobot_state', 'odoobot_failed', 'sel_groups_1_9_10', 'groups_id']
                
                # Only include columns that exist
                user_query_columns = user_base_columns + [col for col in user_optional_columns if col in user_available_columns]
                user_columns_str = ', '.join(user_query_columns)
                
                # Now migrate res.users records
                cr.execute(f"""
                    SELECT {user_columns_str}
                    FROM res_users ORDER BY id LIMIT %s
                """, (PAGE_SIZE,))
                
                users = cr.fetchall()
                user_count = 0
                
                for user_data in users:
                    # Create a dictionary mapping column names to values
                    user_dict = dict(zip(user_query_columns, user_data))
                    
                    # Check if user should be migrated (do this first to avoid unnecessary processing)
                    login = user_dict.get('login')
                    if not login:
                        continue  # Skip users without login
                    
                    # Skip system users that shouldn't be migrated (check early to avoid warnings)
                    system_logins = [
                        '__system__', 'admin', 'public', 'default', 'portaltemplate',
                        'demo', 'base.user_demo', 'base.user_admin', 'base.default_user'
                    ]
                    if login in system_logins:
                        continue
                    
                    # Skip archived users (they shouldn't be migrated)
                    if not user_dict.get('active', True):
                        _logger.debug(f"Skipping archived user: {login}")
                        continue
                    
                    # Build user values dynamically based on available columns
                    # Handle partner_id mapping from Odoo 16 to Odoo 18
                    odoo16_partner_id = user_dict.get('partner_id')
                    mapped_partner_id = None
                    
                    if odoo16_partner_id:
                        # Try to find the mapped partner ID
                        mapped_partner_id = partner_id_mapping.get(odoo16_partner_id)
                        
                        if not mapped_partner_id:
                            # If no mapping found, try to find partner by other means or create a basic one
                            _logger.warning(f"No partner mapping found for Odoo 16 partner_id {odoo16_partner_id} for user {user_dict.get('login')}")
                            # Skip this user for now - we could create a basic partner here if needed
                            continue
                    
                    user_vals = {
                        'login': user_dict.get('login'),
                        'partner_id': mapped_partner_id,
                        'active': True,  # Always create as active initially to avoid constraint issues
                        'odoo16_user_id': user_dict.get('id'),  # Store original Odoo 16 user ID
                    }
                    
                    # Add optional fields if they exist, validating foreign key references and data types
                    if 'password' in user_dict and user_dict['password']:
                        password_value = user_dict['password']
                        # Ensure password is a string, not boolean or other type
                        if isinstance(password_value, (str, bytes)):
                            user_vals['password'] = password_value
                        else:
                            _logger.warning(f"Invalid password type for user {login}: {type(password_value)}, skipping password field")
                    
                    if 'company_id' in user_dict and user_dict['company_id']:
                        # Validate company exists
                        if self.env['res.company'].browse(user_dict['company_id']).exists():
                            user_vals['company_id'] = user_dict['company_id']
                    
                    if 'signature' in user_dict and user_dict['signature']:
                        signature_value = user_dict['signature']
                        # Ensure signature is a string
                        if isinstance(signature_value, str):
                            user_vals['signature'] = signature_value
                        else:
                            _logger.warning(f"Invalid signature type for user {login}: {type(signature_value)}, skipping signature field")
                    
                    if 'notification_type' in user_dict and user_dict['notification_type']:
                        notification_value = user_dict['notification_type']
                        # Ensure notification_type is a string
                        if isinstance(notification_value, str):
                            user_vals['notification_type'] = notification_value
                        else:
                            _logger.warning(f"Invalid notification_type for user {login}: {type(notification_value)}, skipping notification_type field")
                    
                    if 'odoobot_state' in user_dict and user_dict['odoobot_state'] is not None:
                        user_vals['odoobot_state'] = user_dict['odoobot_state']
                    
                    if 'odoobot_failed' in user_dict and isinstance(user_dict['odoobot_failed'], bool):
                        user_vals['odoobot_failed'] = user_dict['odoobot_failed']
                    
                    # Store original active state for later processing (login already validated above)
                    original_active_state = user_dict.get('active', True)
                    
                    # Create or update user using merge functionality
                    try:
                        # Use savepoint to isolate this operation
                        with self.env.cr.savepoint():
                            # Search for existing user (including archived ones)
                            search_domain = [('login', '=', login)]
                            record_identifier = f"user '{login}'"
                            
                            # Debug: Check if user already exists
                            existing_user = self.env['res.users'].with_context(active_test=False).search(search_domain, limit=1)
                            if existing_user:
                                _logger.info(f"Found existing user '{login}' (ID: {existing_user.id}, active: {existing_user.active})")
                            
                            user, action = self.database_id._create_or_update_record(
                                'res.users',
                                search_domain,
                                user_vals,
                                record_identifier
                            )
                            
                            if action in ('created', 'updated'):
                                user_count += 1
                                
                                # If the user was originally inactive, mark them as inactive now
                                # (after successful creation to avoid constraint issues)
                                if not original_active_state:
                                    try:
                                        user.write({'active': False})
                                        _logger.info(f"Marked user '{login}' as inactive (preserving original state)")
                                    except Exception as deactivate_error:
                                        _logger.warning(f"Failed to deactivate user '{login}': {deactivate_error}")
                                
                    except Exception as e:
                        # Log the error but continue with other users
                        _logger.warning(f"Failed to process user {login}: {str(e)}")
                        continue
            
            self._update_migration_status('completed', 
                f'Users and partners migration completed: {user_count} users, {partner_count} partners migrated')
            
            # Commit the transaction to ensure partners are persisted before dependent migrations
            self.env.cr.commit()
            _logger.info(f"✅ Partner migration transaction committed - {partner_count} partners and {user_count} users persisted")
            
            return self._success_notification(
                "Users & Partners Migration Successful",
                f"Successfully migrated {user_count} users and {partner_count} partners from Odoo 16."
            )
            
        except Exception as e:
            self._update_migration_status('failed', f'Users and partners migration failed: {str(e)}')
            _logger.error(f"Users and partners migration failed: {str(e)}")
            raise UserError(_("Users and partners migration failed: %s") % str(e))
