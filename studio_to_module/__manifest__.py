# -*- coding: utf-8 -*-
{
    'name': 'Studio to Module Converter',
    'version': '18.0.1.0.0',
    'category': 'Technical Settings',
    'summary': 'Convert Studio views to custom module code',
    'description': """
Studio to Module Converter
===========================

This module allows you to:

* List all Studio-created views
* Select a target custom module
* Convert Studio views to XML files in the module
* Automatically clean up Studio views after module update

Perfect for converting Studio customizations into version-controlled code.

**Note:** This module does NOT require web_studio to be installed. It detects
Studio views dynamically and will work whether web_studio is installed or not.
If web_studio is not installed, the module will simply show no Studio views.

Views Added
-----------

**Menu Structure:**

* Settings > Technical > Studio to Module (root menu)
    * Studio Views (list and manage Studio views)
    * Convert to Module (conversion wizard)

**Extended Views:**

* ir.ui.view list view: Added columns for Studio view tracking
    * is_studio_view (Boolean)
    * converted_to_module (Boolean)
    * target_module_id (Many2one to ir.module.module)
    * pending_cleanup (Boolean)
    * Decorations: Blue for Studio views, Green for converted views

* ir.ui.view search view: Added filters
    * Studio Views
    * Not Converted
    * Converted
    * Pending Cleanup
    * Group by: Studio / Target Module

**New Views:**

* studio.view.converter wizard form:
    * Studio views selection (many2many_tags)
    * Module author filter (selection)
    * Target module selection (filtered by author)
    * Views folder configuration
    * Auto cleanup toggle
    * Module path display
    * XML preview tab
    * Instructions tab

**Actions:**

* Studio Views Manager: Opens ir.ui.view with Studio filters
* Convert Studio Views: Opens conversion wizard
* Action binding on ir.ui.view list for quick access

**Scheduled Actions:**

* Automatic Studio View Cleanup (disabled by default)
    * Runs daily
    * Cleans up converted Studio views after module upgrade
    """,
    'author': 'Durpro',
    'website': 'https://www.durpro.com',
    'depends': ['base', 'studio_cleanup'],
    'data': [
        'security/ir.model.access.csv',
        'data/ir_cron_data.xml',
        'views/studio_view_manager_views.xml',
        'wizard/studio_view_converter_views.xml',
        'wizard/studio_view_converter_confirm_views.xml',
        'views/menu_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}
