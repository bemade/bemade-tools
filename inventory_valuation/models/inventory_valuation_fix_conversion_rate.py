from odoo import models, fields, api
import logging

_logger = logging.getLogger(__name__)


class InventoryValuationFixConversionRate(models.TransientModel):
    _name = "inventory.valuation.fix.conversion.rate"
    _description = "Inventory Valuation Fix Conversion Rate"

    @api.model
    def run(self):
        """
        Fix inventory valuations for products purchased in USD or other foreign currencies.
        This is applicable to stock valuation layers up to and including April 8th.
        The issue was that these were imported with improper currency conversion rates,
        generally resulting in 1:1 conversion.

        Steps:
        1. Retrieve the SVLs for products with purchase orders with conversion_rate != 1
           and order_date <= 2023-04-08.
        2. Match SVLs to their purchase orders by date, product and quantity. Quantity
           on the SVL may be lower than the quantity on the PO.
        3. Set the value of the SVL to the row (subtotal / quantity) / currency_rate.
        """

        svls = self._retrieve_foreign_purchase_svls()
        _logger.info(f"Found {len(svls)} SVLs to fix.")
        svl_to_po_line = self._match_svls_to_po_lines(svls)
        checked = 0
        for svl, po_line in svl_to_po_line.items():
            if not po_line:
                continue
            checked += 1
            unit_price = (
                po_line.price_subtotal / po_line.product_uom_qty
            ) / po_line.order_id.currency_rate
            svl.write(
                {
                    "unit_cost": unit_price,
                    "value": unit_price * svl.quantity,
                    "remaining_value": unit_price * svl.remaining_qty,
                }
            )
        _logger.info(f"Checked {checked} SVLs. Couldn't check {len(svls) - checked}.")

    def _retrieve_foreign_purchase_svls(self):
        foreign_orders = self.env["purchase.order"].search(
            [
                ("date_order", "<=", "2023-04-08"),
                ("currency_rate", "!=", 1),
            ]
        )
        product_ids = foreign_orders.order_line.mapped("product_id").ids
        return self.env["stock.valuation.layer"].search(
            [
                ("product_id", "in", product_ids),
                ("quantity", ">", 0),
            ]
        )

    def _match_svls_to_po_lines(self, svls):
        """
        Match SVLs to their purchase order date :

        1. By stock move -> picking -> purchase order if possible.
        2. By purchase order date if not possible.
        """

        svls_to_po_line_dict = {}
        svls_without_po = svls.filtered(
            lambda svl: not svl.stock_move_id or not svl.stock_move_id.purchase_line_id
        )

        # Match the easy ones with their related stock_move_id -> purchase_line_id
        svls_to_po_line_dict.update(
            {svl: svl.stock_move_id.purchase_line_id for svl in svls - svls_without_po}
        )

        # Try to match the remainder with PO dates
        po_lines = self.env["purchase.order.line"].search(
            [
                ("product_id", "in", svls_without_po.mapped("product_id").ids),
                ("order_id.date_order", "<=", "2023-04-08"),
                ("product_uom_qty", ">", 0),
            ]
        )
        po_lines_dict = {}
        for line in po_lines:
            po_lines_dict.setdefault(line.product_id, {})[
                line.order_id.date_order
            ] = line

        return svls_to_po_line_dict.update(
            {
                svl: po_lines_dict.get(svl.product_id, {}).get(svl.date, None)
                for svl in svls_without_po
            }
        )
