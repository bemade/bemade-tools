#
#    Bemade Inc.
#
#    Copyright (C) 2024-June Bemade Inc. (<https://www.bemade.org>).
#    Author: Marc Durepos (Contact : marc@bemade.org)
#
#    This program is under the terms of the GNU Lesser General Public License,
#    version 3.
#
#    For full license details, see https://www.gnu.org/licenses/lgpl-3.0.en.html.
#
from odoo import models, fields


class StockLocation(models.Model):
    _inherit = "stock.location"

    sap_sww_code = fields.Char(
        string="SAP SWW Code",
        index=True,
        help="SAP default-warehouse code from SAP B1 (OITM.sww)",
    )
