# -*- coding: utf-8 -*-
"""
Custom partner merge wizard to handle odoo16_partner_id conflicts during migration.
"""

from odoo import models, api
import logging

_logger = logging.getLogger(__name__)


class MergePartnerAutomatic(models.TransientModel):
    _inherit = 'base.partner.merge.automatic.wizard'

    def _merge(self, partner_ids, dst_partner):
        """Override the main merge method to handle odoo16_partner_id conflicts."""
        
        # Get all partners involved in the merge
        src_partners = self.env['res.partner'].browse(partner_ids)
        
        # Handle odoo16_partner_id conflicts before any merge operations
        self._handle_odoo16_partner_id_conflicts(src_partners, dst_partner)
        
        # Call the original merge method
        return super()._merge(partner_ids, dst_partner)
    
    def _handle_odoo16_partner_id_conflicts(self, src_partners, dst_partner):
        """Handle odoo16_partner_id conflicts before merging partners."""
        
        if not hasattr(dst_partner, 'odoo16_partner_id'):
            return
            
        # Collect all odoo16_partner_id values involved in this merge
        all_odoo16_ids = []
        partner_odoo16_map = {}
        
        # Map destination partner
        if dst_partner.odoo16_partner_id:
            all_odoo16_ids.append(dst_partner.odoo16_partner_id)
            partner_odoo16_map[dst_partner.id] = dst_partner.odoo16_partner_id
        
        # Map source partners
        for src_partner in src_partners:
            if hasattr(src_partner, 'odoo16_partner_id') and src_partner.odoo16_partner_id:
                all_odoo16_ids.append(src_partner.odoo16_partner_id)
                partner_odoo16_map[src_partner.id] = src_partner.odoo16_partner_id
        
        if not all_odoo16_ids:
            return
            
        _logger.info(f"Partner merge: Handling odoo16_partner_id conflicts for partners {[p.id for p in src_partners]} -> {dst_partner.id}")
        _logger.info(f"Partner merge: odoo16_partner_id values involved: {all_odoo16_ids}")
        
        # Strategy: Clear odoo16_partner_id from all source partners to avoid conflicts
        # The destination partner keeps its odoo16_partner_id if it has one
        for src_partner in src_partners:
            if hasattr(src_partner, 'odoo16_partner_id') and src_partner.odoo16_partner_id:
                _logger.info(f"Partner merge: Clearing odoo16_partner_id {src_partner.odoo16_partner_id} "
                           f"from source partner {src_partner.id} to avoid conflicts")
                
                # Clear the odoo16_partner_id to prevent constraint violations
                src_partner.sudo().write({'odoo16_partner_id': False})
        
        # If destination doesn't have odoo16_partner_id, preserve the first available one
        if not dst_partner.odoo16_partner_id and all_odoo16_ids:
            # Use the first odoo16_partner_id we found
            preserve_id = all_odoo16_ids[0]
            
            # Double-check it's not used by another partner outside this merge
            existing = self.env['res.partner'].search([
                ('odoo16_partner_id', '=', preserve_id),
                ('id', '!=', dst_partner.id),
                ('id', 'not in', src_partners.ids)
            ], limit=1)
            
            if not existing:
                _logger.info(f"Partner merge: Preserving odoo16_partner_id {preserve_id} "
                           f"on destination partner {dst_partner.id}")
                dst_partner.sudo().write({'odoo16_partner_id': preserve_id})
