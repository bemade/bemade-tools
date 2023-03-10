{
    "name": "Durpro HubSpot Import",
    "version": "1.0",
    "license": "Other proprietary",
    "author": "Durpro Ltd",
    "category": "Generic Modules/Others",
    "depends": [
        "helpdesk",
    ],
    "external_dependencies": ["hubspot-api-client", ],
    "external_dependencies": {
        "python": ["hubspot"],
    },
    "description": """
    This module allows for importing records from HubSpot into Odoo.
    """,
    "demo": [],
    'data': [
        "views/res_config_settings_views.xml",
        "views/hubspot_import_views.xml",
        "views/pipeline_views.xml",
        "security/ir.model.access.csv",
        "wizard/hubspot_import_wizard_views.xml",
    ],
    'test': [],
    'installable': True,
    'active': False
}
