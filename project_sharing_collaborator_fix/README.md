# Project Sharing Collaborator & Security Fix

## Overview

This module fixes critical bugs in Odoo's core project sharing functionality affecting both collaborator creation and portal user security.

## Problem Description

### Symptoms
- Portal users could see **ALL project tasks** regardless of sharing status
- Project sharing UI **appeared to work** but silently failed to create collaborator records
- Sharing security rules remained **inactive** due to missing collaborators
- Even with collaborators created, portal users still had unauthorized access
- Issue affected **all user types**, including administrators

### Root Causes

#### 1. Collaborator Creation Bug
The original `project.share.wizard` had a fundamental architectural flaw:
- **Collaborator creation logic was in `create()` method** where `wizard.collaborator_ids` is empty
- **Should have been in `action_send_mail()` method** where collaborators are actually processed
- **Timing issue**: Logic ran before collaborator wizard records were created

#### 2. Portal Security Bug
The project module had an overly permissive Access Control List (ACL) rule:
- **ACL Rule**: `access_task_portal` granted portal users direct read access to ALL project tasks
- **Security Bypass**: This ACL overrode record rules that should enforce collaborator-based restrictions
- **Critical Flaw**: Portal access was not properly controlled by sharing settings

## Solution

### What This Module Does

#### 1. Wizard Fix
1. **Overrides `ProjectShareWizard.create()`** - Removes premature collaborator creation logic
2. **Overrides `ProjectShareWizard.action_send_mail()`** - Adds collaborator creation at correct timing
3. **Preserves all original functionality** - Email sending, follower management, etc.

#### 2. Security Fix
1. **Removes overly permissive ACL rule** - Disables `access_task_portal` that granted blanket access
2. **Forces proper record rule enforcement** - Portal access now goes through collaborator-based restrictions
3. **Maintains intended security model** - Only shared project tasks are accessible to portal users

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
