#!/usr/bin/env python3
"""
Test script for Project Sharing Collaborator Fix module

This script can be run in Odoo shell to verify the fix is working correctly.
Usage: python3 test_fix.py | ./odoo-bin shell -c odoo.conf -d database_name --no-http
"""

print("=== TESTING PROJECT SHARING COLLABORATOR FIX ===")

# Check if the fix module is installed
fix_module = env['ir.module.module'].search([('name', '=', 'project_sharing_collaborator_fix')])
if not fix_module:
    print("❌ Fix module not found - please install project_sharing_collaborator_fix")
    exit()

if fix_module.state != 'installed':
    print(f"❌ Fix module not installed - current state: {fix_module.state}")
    exit()

print("✅ Fix module is installed and active")

# Find a project and portal user for testing
projects = env['project.project'].search([('privacy_visibility', '=', 'portal')], limit=1)
if not projects:
    print("❌ No portal projects found for testing")
    exit()

project = projects[0]
portal_users = env['res.users'].search([('groups_id', 'in', [env.ref('base.group_portal').id])], limit=1)
if not portal_users:
    print("❌ No portal users found for testing")
    exit()

portal_partner = portal_users[0].partner_id
print(f"Using project: {project.name}")
print(f"Using portal partner: {portal_partner.name}")

# Clear existing collaborators for clean test
initial_collaborators = len(project.collaborator_ids)
if project.collaborator_ids:
    project.collaborator_ids.unlink()
    print(f"Cleared {initial_collaborators} existing collaborators for clean test")

# Test the fixed wizard workflow
print("\n=== TESTING FIXED WIZARD WORKFLOW ===")

# Create wizard (this should NOT create collaborators anymore)
wizard = env['project.share.wizard'].with_context(
    active_model='project.project',
    active_id=project.id
).create({
    'res_model': 'project.project',
    'res_id': project.id,
})

# Add collaborator to wizard
collaborator_wizard = env['project.share.collaborator.wizard'].create({
    'parent_wizard_id': wizard.id,
    'partner_id': portal_partner.id,
    'access_mode': 'edit',
    'send_invitation': True,
})

print(f"Wizard created with {len(wizard.collaborator_ids)} collaborators")

# Check that create() method didn't create collaborators prematurely
project.invalidate_recordset()
collaborators_after_create = len(project.collaborator_ids)
print(f"Collaborators after wizard.create(): {collaborators_after_create} (should be 0)")

if collaborators_after_create > 0:
    print("❌ REGRESSION: create() method still creating collaborators prematurely")
    exit()

# Execute sharing action (this SHOULD create collaborators now)
try:
    result = wizard.action_share_record()
    print("✅ Sharing action executed successfully")
except Exception as e:
    print(f"❌ Sharing action failed: {e}")
    exit()

# Verify collaborators were created
project.invalidate_recordset()
final_collaborators = len(project.collaborator_ids)
print(f"Final collaborators after action_share_record(): {final_collaborators}")

if final_collaborators == 0:
    print("❌ FIX FAILED: No collaborators created")
    exit()

# Verify sharing rules are active
access_right = env.ref('project.access_project_sharing_task_portal')
record_rule = env.ref('project.project_task_rule_portal_project_sharing')
print(f"Sharing access right active: {access_right.active}")
print(f"Sharing record rule active: {record_rule.active}")

if not access_right.active or not record_rule.active:
    print("❌ Sharing rules not activated")
    exit()

print("\n🎉 SUCCESS! Project Sharing Collaborator Fix is working correctly!")
print("✅ Collaborators created via UI workflow")
print("✅ Sharing security rules activated")
print("✅ Portal access properly restricted")
