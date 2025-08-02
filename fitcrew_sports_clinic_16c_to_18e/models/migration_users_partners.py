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
                
                for partner_data in partners:
                    # Create a dictionary mapping column names to values
                    partner_dict = dict(zip(query_columns, partner_data))
                    
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
                    
                    # Check if partner already exists
                    existing_partner = self.env['res.partner'].search([
                        ('name', '=', partner_dict.get('name')),
                        ('email', '=', partner_dict.get('email'))
                    ], limit=1)
                    
                    if not existing_partner:
                        try:
                            # Use savepoint to isolate this creation
                            with self.env.cr.savepoint():
                                self.env['res.partner'].sudo().create(partner_vals)
                                partner_count += 1
                        except Exception as e:
                            # Log the error but continue with other partners
                            _logger.warning(f"Failed to create partner {partner_dict.get('name')}: {str(e)}")
                            continue
                
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
                    
                    # Build user values dynamically based on available columns
                    user_vals = {
                        'login': user_dict.get('login'),
                        'partner_id': user_dict.get('partner_id'),
                        'active': user_dict.get('active', True),
                    }
                    
                    # Add optional fields if they exist, validating foreign key references
                    if 'password' in user_dict:
                        user_vals['password'] = user_dict['password']
                    
                    if 'company_id' in user_dict and user_dict['company_id']:
                        # Validate company exists
                        if self.env['res.company'].browse(user_dict['company_id']).exists():
                            user_vals['company_id'] = user_dict['company_id']
                    
                    if 'signature' in user_dict:
                        user_vals['signature'] = user_dict['signature']
                    if 'notification_type' in user_dict:
                        user_vals['notification_type'] = user_dict['notification_type']
                    if 'odoobot_state' in user_dict:
                        user_vals['odoobot_state'] = user_dict['odoobot_state']
                    if 'odoobot_failed' in user_dict:
                        user_vals['odoobot_failed'] = user_dict['odoobot_failed']
                    
                    # Check if user already exists or is a system user
                    login = user_dict.get('login')
                    if not login:
                        continue  # Skip users without login
                    
                    # Skip system users that shouldn't be migrated
                    system_logins = ['__system__', 'admin', 'public']
                    if login in system_logins:
                        continue
                    
                    existing_user = self.env['res.users'].search([
                        ('login', '=', login)
                    ], limit=1)
                    
                    if not existing_user:
                        try:
                            # Use savepoint to isolate this creation
                            with self.env.cr.savepoint():
                                self.env['res.users'].sudo().create(user_vals)
                                user_count += 1
                        except Exception as e:
                            # Log the error but continue with other users
                            _logger.warning(f"Failed to create user {login}: {str(e)}")
                            continue
            
            self._update_migration_status('completed', 
                f'Users and partners migration completed: {user_count} users, {partner_count} partners migrated')
            
            return self._success_notification(
                "Users & Partners Migration Successful",
                f"Successfully migrated {user_count} users and {partner_count} partners from Odoo 16."
            )
            
        except Exception as e:
            self._update_migration_status('failed', f'Users and partners migration failed: {str(e)}')
            _logger.error(f"Users and partners migration failed: {str(e)}")
            raise UserError(_("Users and partners migration failed: %s") % str(e))
