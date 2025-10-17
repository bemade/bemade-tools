# -*- coding: utf-8 -*-

import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class StudioViewConverterConfirm(models.TransientModel):
    _name = 'studio.view.converter.confirm'
    _description = 'Studio View Converter Confirmation'

    message = fields.Text(
        string='Message',
        readonly=True,
    )
    target_module_id = fields.Many2one(
        comodel_name='ir.module.module',
        string='Target Module',
        readonly=True,
    )
    converted_view_count = fields.Integer(
        string='Converted Views',
        readonly=True,
    )

    def action_upgrade_module(self):
        """Upgrade the target module immediately."""
        self.ensure_one()
        
        if not self.target_module_id:
            return {'type': 'ir.actions.act_window_close'}
        
        try:
            # Trigger module upgrade
            self.target_module_id.button_immediate_upgrade()
            
            # Redirect to Studio Views list instead of closing
            # (the original view might have been deleted after upgrade)
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'ir.ui.view',
                'view_mode': 'list',
                'domain': [('is_studio_view', '=', True)],
                'name': _('Studio Views'),
            }
        except Exception as e:
            _logger.error('Failed to upgrade module %s: %s', self.target_module_id.name, e)
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Error'),
                    'message': _('Failed to upgrade module: %s\n\nPlease restart Odoo and try again.') % str(e),
                    'type': 'danger',
                    'sticky': True,
                    'next': {'type': 'ir.actions.act_window_close'},
                }
            }

    def action_upgrade_and_cleanup(self):
        """Upgrade the module and manually run the cleanup hook (DEV mode)."""
        self.ensure_one()
        
        if not self.target_module_id:
            return {'type': 'ir.actions.act_window_close'}
        
        try:
            # First upgrade the module
            self.target_module_id.button_upgrade()
            
            # Then manually run the cleanup hook
            module_name = self.target_module_id.name
            
            try:
                # Try to import and run the hook
                hook_module = f'odoo.addons.{module_name}.hooks'
                import importlib
                hooks = importlib.import_module(hook_module)
                
                if hasattr(hooks, 'post_init_hook'):
                    _logger.info('Manually executing post_init_hook for %s', module_name)
                    hooks.post_init_hook(self.env)
                    
                    # Redirect to Studio Views list instead of closing
                    # (the original view might have been deleted)
                    return {
                        'type': 'ir.actions.client',
                        'tag': 'display_notification',
                        'params': {
                            'title': _('Success'),
                            'message': _('Module upgraded and Studio views cleaned up successfully!'),
                            'type': 'success',
                            'sticky': False,
                            'next': {
                                'type': 'ir.actions.act_window',
                                'res_model': 'ir.ui.view',
                                'view_mode': 'list',
                                'domain': [('is_studio_view', '=', True)],
                            },
                        }
                    }
                else:
                    _logger.warning('post_init_hook not found in hooks module for %s', module_name)
                    return {
                        'type': 'ir.actions.client',
                        'tag': 'display_notification',
                        'params': {
                            'title': _('Warning'),
                            'message': _('Module upgraded but no cleanup hook found.\n\nPlease restart Odoo and upgrade again to run the cleanup.'),
                            'type': 'warning',
                            'sticky': True,
                            'next': {'type': 'ir.actions.act_window_close'},
                        }
                    }
                    
            except ImportError as e:
                _logger.warning('Could not import hooks module for %s: %s', module_name, e)
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': _('Warning'),
                        'message': _('Module upgraded but hooks.py not found.\n\nPlease restart Odoo and upgrade again to run the cleanup.'),
                        'type': 'warning',
                        'sticky': True,
                        'next': {'type': 'ir.actions.act_window_close'},
                    }
                }
                
        except Exception as e:
            _logger.error('Failed to upgrade module %s: %s', self.target_module_id.name, e)
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Error'),
                    'message': _('Failed to upgrade module: %s') % str(e),
                    'type': 'danger',
                    'sticky': True,
                    'next': {'type': 'ir.actions.act_window_close'},
                }
            }

    def action_close(self):
        """Close the wizard without upgrading."""
        return {'type': 'ir.actions.act_window_close'}
