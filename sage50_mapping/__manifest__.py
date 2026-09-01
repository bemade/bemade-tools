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
    "name": "Sage 50 Mapping",
    "version": "19.0.1.2.0",
    "summary": "Where each Odoo record came from in a Sage 50 company file",
    "description": "Carries the Sage 50 source identifiers on the Odoo "
                   "records a Sage take-on creates, and nothing else. This is "
                   "the half of the Sage tooling that stays installed in "
                   "production. See README.md in the module directory.",
    "category": "Technical",
    "author": "Bemade Inc.",
    "website": "https://www.bemade.org",
    "license": "LGPL-3",
    "depends": [
        "account",
        "product",
    ],
    "data": [
        "data/ir_filters_data.xml",
    ],
    "installable": True,
    "auto_install": False,
}
