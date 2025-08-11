# -*- coding: utf-8 -*-
{
    'name': 'Project Sharing Collaborator Fix',
    'version': '18.0.1.0.0',
    'category': 'Project',
    'summary': 'Fixes Odoo core bug preventing collaborator creation via project sharing UI',
    'description': """
Project Sharing Collaborator Creation Fix
=========================================

This module fixes a critical bug in Odoo's core project sharing functionality where
collaborators were not being created when using the project sharing UI wizard.

**Problem Fixed:**
- Project sharing UI appeared to work but silently failed to create collaborator records
- Portal users could see all project tasks instead of only shared ones
- Sharing security rules remained inactive due to missing collaborators

**Root Cause:**
- Collaborator creation logic was in wizard.create() method where collaborator_ids don't exist yet
- Should have been in wizard.action_send_mail() method where collaborators are actually processed

**Solution:**
- Moves collaborator creation logic to the correct method in the workflow
- Ensures collaborators are created when "Share Project" button is clicked
- Activates project sharing security rules properly

**Impact:**
- Portal users now only see project tasks for projects they are explicitly shared on
- Project sharing UI works as intended for all user types
- Sharing security is properly enforced

This fix is essential for proper project security and collaboration functionality.
    """,
    'author': 'Bemade Inc.',
    'website': 'https://bemade.org',
    'depends': ['project'],
    'data': [],
    'installable': True,
    'auto_install': False,
    'application': False,
    'license': 'LGPL-3',
}
