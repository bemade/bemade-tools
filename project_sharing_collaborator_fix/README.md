# Project Sharing Collaborator Fix

## Overview

This module fixes a critical bug in Odoo's core project sharing functionality where collaborators were not being created when using the project sharing UI wizard.

## Problem Description

### Symptoms
- Portal users could see **all project tasks** on public projects instead of only shared ones
- Project sharing UI **appeared to work** but silently failed to create collaborator records
- Sharing security rules remained **inactive** due to missing collaborators
- Issue affected **all user types**, including administrators

### Root Cause
The original `project.share.wizard` had a fundamental architectural flaw:
- **Collaborator creation logic was in `create()` method** where `wizard.collaborator_ids` is empty
- **Should have been in `action_send_mail()` method** where collaborators are actually processed
- **Timing issue**: Logic ran before collaborator wizard records were created

## Solution

### What This Module Does
1. **Overrides `ProjectShareWizard.create()`** - Removes premature collaborator creation logic
2. **Overrides `ProjectShareWizard.action_send_mail()`** - Adds collaborator creation at correct timing
3. **Preserves all original functionality** - Email sending, follower management, etc.

### Technical Details
- **Method**: Uses Odoo inheritance (`_inherit`) to override core wizard methods
- **Timing**: Collaborator creation now happens when "Share Project" button is clicked
- **Compatibility**: Works with all Odoo 18.0 installations
- **Safety**: Non-destructive override that maintains all existing functionality

## Installation

1. Copy this module to your Odoo addons path
2. Update the app list: `Settings > Apps > Update Apps List`
3. Install the module: `Apps > Search "Project Sharing Collaborator Fix" > Install`

## Verification

After installation, test the project sharing functionality:

1. Go to `Project > Projects > [Any Project] > Share`
2. Add a portal user as a collaborator
3. Click "Share Project"
4. Verify collaborator was created: `Project > Configuration > Collaborators`
5. Verify sharing rules are active: Portal users should only see shared project tasks

## Impact

### Before Fix ❌
- Portal users: Unrestricted access to all public project tasks
- UI workflow: Silent failure, no collaborators created
- Security: Sharing rules inactive, no access control

### After Fix ✅
- Portal users: Restricted access to only shared project tasks
- UI workflow: Collaborators created successfully
- Security: Sharing rules active, proper access control enforced

## Dependencies

- `project` (Odoo core project module)

## Compatibility

- **Odoo Version**: 18.0
- **Edition**: Community and Enterprise
- **Database**: All databases with project sharing functionality

## Support

This module addresses a core Odoo bug and should be safe for all installations. If you encounter any issues, please check:

1. Module is properly installed and active
2. User has appropriate permissions for project sharing
3. Portal users have `partner_share = True` flag set

## License

LGPL-3 (same as Odoo core)
