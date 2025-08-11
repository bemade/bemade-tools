# -*- coding: utf-8 -*-

from odoo import Command, api, models, _


class ProjectShareWizard(models.TransientModel):
    _inherit = 'project.share.wizard'

    @api.model_create_multi
    def create(self, vals_list):
        """
        Override the create method to fix the collaborator creation bug.
        
        Original Issue:
        - The original create() method tried to process collaborators immediately
        - But wizard.collaborator_ids is empty at creation time (collaborators added after)
        - This caused silent failure where no collaborators were ever created
        
        Fix:
        - Remove collaborator creation logic from create() method
        - Logic moved to action_send_mail() method where collaborators actually exist
        """
        wizards = super().create(vals_list)
        # Collaborator creation moved to action_send_mail() method where collaborator_ids actually exist
        return wizards

    def action_send_mail(self):
        """
        Override action_send_mail to add collaborator creation logic.
        
        This method is called when user clicks "Share Project" button,
        at which point the collaborator wizard records actually exist.
        """
        # Create collaborator records first (moved from create() method)
        self.ensure_one()
        collaborator_ids_to_add = []
        collaborator_ids_to_add_with_limited_access = []
        collaborator_ids_vals_list = []
        project = self.resource_ref
        
        # Determine which existing collaborators should be removed
        project_collaborator_ids_to_remove = [
            c.id
            for c in project.collaborator_ids
            if c.partner_id not in self.collaborator_ids.partner_id
        ]
        
        # Handle project followers
        project_followers = project.message_partner_ids
        project_followers_to_add = []
        project_followers_to_remove = [
            partner.id
            for partner in project_followers
            if partner not in self.collaborator_ids.partner_id
        ]
        
        # Process each collaborator from the wizard
        project_collaborator_per_partner_id = {c.partner_id.id: c for c in project.collaborator_ids}
        for collaborator in self.collaborator_ids:
            partner_id = collaborator.partner_id.id
            project_collaborator = project_collaborator_per_partner_id.get(partner_id, self.env['project.collaborator'])
            
            if collaborator.access_mode in ("edit", "edit_limited"):
                limited_access = collaborator.access_mode == "edit_limited"
                if not project_collaborator:
                    # New collaborator to add
                    if limited_access:
                        collaborator_ids_to_add_with_limited_access.append(partner_id)
                    else:
                        collaborator_ids_to_add.append(partner_id)
                elif project_collaborator.limited_access != limited_access:
                    # Update existing collaborator's access level
                    collaborator_ids_vals_list.append(
                        Command.update(
                            project_collaborator.id,
                            {'limited_access': limited_access},
                        )
                    )
            elif project_collaborator:
                # Remove collaborator (changed from edit to read-only)
                project_collaborator_ids_to_remove.append(project_collaborator.id)
            
            # Add to followers if not already
            if partner_id not in project_followers.ids:
                project_followers_to_add.append(partner_id)
        
        # Create new collaborators with regular access
        if collaborator_ids_to_add:
            partners = project._get_new_collaborators(self.env['res.partner'].browse(collaborator_ids_to_add))
            collaborator_ids_vals_list.extend(Command.create({'partner_id': partner_id}) for partner_id in partners.ids)
        
        # Create new collaborators with limited access
        if collaborator_ids_to_add_with_limited_access:
            partners = project._get_new_collaborators(self.env['res.partner'].browse(collaborator_ids_to_add_with_limited_access))
            collaborator_ids_vals_list.extend(
                Command.create({'partner_id': partner_id, 'limited_access': True}) for partner_id in partners.ids
            )
        
        # Remove collaborators that are no longer shared
        if project_collaborator_ids_to_remove:
            collaborator_ids_vals_list.extend(Command.delete(collaborator_id) for collaborator_id in project_collaborator_ids_to_remove)
        
        # Apply collaborator changes to project
        project_vals = {}
        if collaborator_ids_vals_list:
            project_vals['collaborator_ids'] = collaborator_ids_vals_list
        if project_vals:
            project.write(project_vals)
        
        # Handle follower changes
        if project_followers_to_add:
            project._add_followers(self.env['res.partner'].browse(project_followers_to_add))
        if project_followers_to_remove:
            project.message_unsubscribe(project_followers_to_remove)
        
        # Now handle email sending and project privacy (original logic)
        return super().action_send_mail()
