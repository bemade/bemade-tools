from odoo import models, fields


class StockWarehouse(models.Model):
    _inherit = "stock.warehouse"

    sap_whs_code = fields.Char(
        string="SAP Warehouse Code",
        index=True,
        help="Original warehouse code from SAP B1 (OWHS.WhsCode)",
    )
