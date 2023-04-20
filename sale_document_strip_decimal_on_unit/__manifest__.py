# -*- coding: utf-8 -*-
{
    'name': "Sale Document Strip Decimal on Unit",
    "license": "AGPL-3",
    'summary': """
        Simply hide decimal on sale document when the unit is of type unit""",

    'description': """
        Simply hide decimal on sale document when the unit is of type unit
    """,

    'author': "Bemade",
    'website': "https://bemade.org",

    'category': 'Sale',
    'version': '15.0.0.1',

    # any module necessary for this one to work correctly
    'depends': [
        'sale'
    ],

    # always loaded
    'data': [
        'views/report_saleorder_document.xml',
    ],
}
