# -*- coding: utf-8 -*-
{
    'name': 'Studio Cleanup Helpers',
    'version': '18.0.1.0.0',
    'category': 'Technical',
    'summary': 'Helper functions for cleaning up migrated Studio views',
    'description': """
Studio Cleanup Helpers
======================

This module provides reusable helper functions for cleaning up Studio views
that have been migrated to module code.

Key Features:
-------------
* Lightweight module with no UI
* Reusable cleanup functions
* Safe deletion by external ID
* Detailed logging
* Production-ready

Usage:
------
Add this module as a dependency in modules that have migrated Studio views::

    'depends': ['base', 'studio_cleanup'],

Then in your module's hooks.py::

    from odoo.addons.studio_cleanup.tools import cleanup_studio_views_by_xmlid
    
    def post_init_hook(env):
        studio_view_ids = [
            'studio_customization.odoo_studio_xxx',
        ]
        cleanup_studio_views_by_xmlid(env, studio_view_ids, 'my_module')

This module is designed to be installed in production environments.
    """,
    'author': 'Durpro',
    'website': 'https://www.durpro.com',
    'depends': ['base'],
    'data': [],
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}
