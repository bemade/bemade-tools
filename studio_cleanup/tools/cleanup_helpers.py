# -*- coding: utf-8 -*-
"""
Helper functions for cleaning up Studio views after migration.

This module provides reusable functions that can be called from any module's hooks.py
to clean up Studio views that have been migrated to module code.
"""

import logging

_logger = logging.getLogger(__name__)


def cleanup_studio_views_by_xmlid(env, studio_view_xmlids, module_name):
    """Delete specific Studio views by their external IDs.
    
    This is a reusable function that can be called from any module's post_init_hook
    to clean up Studio views that have been migrated to module code.
    
    :param env: Odoo environment
    :param list studio_view_xmlids: List of Studio view external IDs to delete
                                     Format: 'module.xml_id' (e.g., 'studio_customization.odoo_studio_xxx')
    :param str module_name: Name of the calling module (for logging)
    :return: Number of views deleted
    :rtype: int
    
    Example usage in a module's hooks.py:
        from odoo.addons.studio_cleanup.tools import cleanup_studio_views_by_xmlid
        
        def post_init_hook(env):
            studio_view_ids = [
                'studio_customization.odoo_studio_stock_lot_tree_customization',
                'studio_customization.odoo_studio_stock_picking_form_customization',
            ]
            cleanup_studio_views_by_xmlid(env, studio_view_ids, 'my_module')
    """
    if not studio_view_xmlids:
        _logger.info("[%s] No Studio views to clean up", module_name)
        return 0
    
    deleted_count = 0
    skipped_count = 0
    failed_count = 0
    
    for xml_id in studio_view_xmlids:
        try:
            # Try to get the view by external ID
            view = env.ref(xml_id, raise_if_not_found=False)
            
            if view:
                view_name = view.name
                view_id = view.id
                view.sudo().unlink()
                deleted_count += 1
                _logger.info("[%s] ✓ Deleted Studio view: %s (ID: %s, XML ID: %s)", 
                           module_name, view_name, view_id, xml_id)
            else:
                skipped_count += 1
                _logger.debug("[%s] ○ Studio view not found (already deleted?): %s", 
                            module_name, xml_id)
                
        except Exception as e:
            failed_count += 1
            _logger.warning("[%s] ✗ Failed to delete Studio view %s: %s", 
                          module_name, xml_id, e)
    
    # Summary log
    if deleted_count > 0:
        _logger.info("[%s] Studio views cleanup completed: %d deleted, %d skipped, %d failed", 
                    module_name, deleted_count, skipped_count, failed_count)
    elif skipped_count > 0:
        _logger.info("[%s] No Studio views deleted (all %d already cleaned up)", 
                    module_name, skipped_count)
    else:
        _logger.info("[%s] No Studio views to clean up", module_name)
    
    return deleted_count
