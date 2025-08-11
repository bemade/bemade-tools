# -*- coding: utf-8 -*-
{
    'name': 'Project Sharing Collaborator Fix',
    'version': '18.0.1.0.0',
    'category': 'Project',
    'summary': 'Fixes Odoo core bugs in project sharing: collaborator creation and portal security',
    'description': """
Project Sharing Collaborator Creation & Security Fix
===================================================

This module fixes critical bugs in Odoo's core project sharing functionality affecting
both collaborator creation and portal user security.

**Problems Fixed:**

1. **Collaborator Creation Bug:**
   - Project sharing UI appeared to work but silently failed to create collaborator records
   - Sharing security rules remained inactive due to missing collaborators

2. **Portal Security Bug:**
   - Portal users could see ALL project tasks due to overly permissive ACL rule
   - Collaborator-based security restrictions were bypassed entirely

**Root Causes:**

1. **Wizard Logic Issue:**
   - Collaborator creation logic was in wizard.create() method where collaborator_ids don't exist yet
   - Should have been in wizard.action_send_mail() method where collaborators are actually processed

2. **ACL Override Issue:**
   - access_task_portal ACL granted portal users direct read access to all project tasks
   - This bypassed record rules that should enforce collaborator-based restrictions

**Solutions:**

1. **Wizard Fix:**
   - Moves collaborator creation logic to the correct method in the workflow
   - Ensures collaborators are created when "Share Project" button is clicked

2. **Security Fix:**
   - Removes overly permissive portal ACL rule for project tasks
   - Forces portal access to go through proper record rules that check collaborator status

**Impact:**
- Portal users now only see project tasks for projects they are explicitly shared on
- Project sharing UI works as intended for all user types
- Sharing security is properly enforced through collaborator-based record rules
- No unauthorized access to project tasks

This fix is essential for proper project security and collaboration functionality.
    """,
    'author': 'Bemade Inc.',
    'website': 'https://bemade.org',
    'depends': ['project'],
    'data': [
        'security/ir.model.access.csv',
    ],
    'installable': True,
    'auto_install': False,
    'application': False,
    'license': 'LGPL-3',
}
