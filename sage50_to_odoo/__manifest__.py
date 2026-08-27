#
#    Bemade Inc.
#
#    Copyright (C) 2026 Bemade Inc. (<https://www.bemade.org>).
#    Author: Marc Durepos (Contact : marc@bemade.org)
#
#    This program is under the terms of the GNU Lesser General Public License,
#    version 3.
#
#    For full license details, see https://www.gnu.org/licenses/lgpl-3.0.en.html.
#
{
    "name": "Sage 50 to Odoo",
    "version": "19.0.1.0.0",
    "summary": "Migrate an offline Sage 50 Canadian Edition company file",
    "description": "ETL pipelines reading a Sage 50 company file served "
                   "read-only from a userland mysqld: chart of accounts, "
                   "partners, product categories, products, pricelists, the "
                   "open receivables and payables, and the opening trial "
                   "balance. Install on the machine running the migration "
                   "only. See README.md in the module directory.",
    "category": "Technical",
    "author": "Bemade Inc.",
    "website": "https://www.bemade.org",
    "license": "LGPL-3",
    "depends": [
        "sage50_mapping",
        "etl_framework",
        "account",
        "product",
    ],
    "data": [
        "security/ir.model.access.csv",
        "views/sage_database_views.xml",
    ],
    "external_dependencies": {"python": ["PyMySQL"]},
    "installable": True,
    "auto_install": False,
}
