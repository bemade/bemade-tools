# -*- coding: utf-8 -*-
{
    'name': "Durpro Accounting Setup",
    "license": "AGPL-3",
    'summary': """
        Sets up the chart of account, account groups and taxes to meet Durpro's accounting needs.""",

    'description': """
        
    """,

    'author': "Marc Durepos",
    'website': "https://bemade.org",

    # Categories can be used to filter modules in modules listing
    # Check https://github.com/odoo/odoo/blob/15.0/odoo/addons/base/data/ir_module_category_data.xml
    # for the full list
    'category': 'Uncategorized',
    'version': '0.1',

    # any module necessary for this one to work correctly
    'depends': [
        'base',
        'account',
    ],
    'post_init_hook': 'post_init_hook',

}
