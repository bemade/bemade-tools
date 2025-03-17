import logging
import csv
import os
from datetime import datetime
from collections import defaultdict
from odoo import models, api, fields
from odoo.tools.sql import SQL

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
_logger = logging.getLogger(__name__)


class InventoryValuationReconstructor(models.TransientModel):
    """Class to handle the reconstruction of inventory valuation based on historical purchase orders."""

    _name = "inventory.valuation.reconstructor"
    _description = "Inventory Valuation Reconstruction"

    in_stock_products = fields.One2many(
        "product.product",
        compute="_compute_in_stock_products",
    )
    currency_id = fields.Many2one(
        "res.currency", default=lambda self: self.env.company.currency_id
    )
    db = fields.Many2one(
        "sap.database",
        default=lambda self: self.env["sap.database"].search([], limit=1),
    )

    def _compute_in_stock_products(self):
        """
        Get all products that have stock on hand.
        """
        for rec in self:
            rec.in_stock_products = self.env["product.product"].search(
                [
                    ("qty_available", ">", 0),
                ]
            )

    def _get_purchase_lines_by_product(self):
        """
        Calculate the weighted average cost for a product based on purchase history.

        Args:
            product: Product record

        Returns:
            dict: Product record to list of purchase lines dicts:
                {
                    product_id: [{
                        qty: float,
                        price: float,
                        date: datetime,
                        order_id: int,
                        currency_id: int,
                    }]
                }
        """
        sql = """
        SELECT 
            line.product_id as product_id,
            line.qty_received as qty,
            line.price_unit * po.currency_rate as price,
            po.date_order as date,
            po.id as order_id,
            po.currency_id as currency_id
        FROM
            purchase_order_line line
            INNER JOIN purchase_order po ON line.order_id = po.id
        WHERE
            line.product_id in %s
            AND line.qty_received > 0
        ORDER BY
            product_id, po.date_order DESC
        """

        self.env.cr.execute(SQL(sql, tuple(self.in_stock_products.ids)))
        purchase_lines = self.env.cr.dictfetchall()
        product_to_lines_dict = {}
        for line in purchase_lines:
            product_to_lines_dict.setdefault(line["product_id"], []).append(line)
        return product_to_lines_dict

    def _calculate_weighted_average_costs(self):
        """
        Calculate the weighted average cost for a product based on purchase history.

        Args:
            product: Product record

        Returns:
            dict: Product record to weighted average cost
            float: weighted average cost in company currency
        """
        sql = """
        SELECT 
            line.product_id as product_id,
            line.qty_received as qty,
            line.price_unit * po.currency_rate as price,
            po.date_order as date
        FROM
            purchase_order_line line
            INNER JOIN purchase_order po ON line.order_id = po.id
        WHERE
            line.product_id in %s
            AND line.qty_received > 0
        ORDER BY
            product_id, po.date_order DESC
        """

        self.env.cr.execute(SQL(sql, tuple(self.in_stock_products.ids)))
        purchase_lines = self.env.cr.dictfetchall()
        product_to_lines_dict = {}
        for line in purchase_lines:
            product_to_lines_dict.setdefault(line["product_id"], []).append(line)
        costs = {}
        for product in self.in_stock_products:
            lines = product_to_lines_dict.get(product.id, [])
            if not lines:
                costs[product] = 0.0
                continue
            total_cost = 0.0
            total_qty = 0.0
            quantity_to_cover = product.qty_available

            while quantity_to_cover > 0:
                quantity = min(line.get("qty"), quantity_to_cover)
                total_cost += line.get("price") * quantity
                total_qty += quantity
                quantity_to_cover -= quantity
                costs[product] = self.currency_id.round(total_cost / total_qty)

        return costs

    def _delete_valuation_layers(self):
        """
        Delete existing stock valuation layers.
        """
        self.env["stock.valuation.layer"].search([]).unlink()

    def _create_valuation_layer(self, product, cost, quantity=0):
        """
        Create a stock valuation layer for a product.
        """
        return self.env["stock.valuation.layer"].create(
            product._prepare_in_svl_vals(quantity, cost)
        )

    def _get_sap_cost_by_product(self):
        item_codes = str(
            tuple(
                self.in_stock_products.filtered("sap_item_code").mapped("sap_item_code")
            )
        )
        sql = f"""
        SELECT itemcode, avgprice
        FROM OITM
        WHERE itemcode in {item_codes}
        AND avgprice > 0
        """

        with self.db.get_cursor() as cr:
            cr.execute(sql)
            sap_items = cr.dictfetchall()
        products_by_item_code = {p.sap_item_code: p for p in self.in_stock_products}
        costs = {}
        for item in sap_items:
            costs[products_by_item_code[item["itemcode"]]] = item["avgprice"]
        return costs

    def _create_svl_from_sap(self, product, sap_cost_dict, qty=None):
        quantity = qty if qty is not None else product.qty_available
        cost = sap_cost_dict.get(product, 0.0)
        self._create_valuation_layer(product, cost, quantity)
        _logger.info(
            f"Created valuation layer for product"
            f" {product.id} ({product.display_name}) from SAP data."
            f" Cost: {cost}, Quantity: {quantity}"
        )

    def run(self):
        """
        Run the inventory valuation reconstruction process.

        The purchase history is used to calculate the valuation of each product.
        If no purchase history is available, the SAP valuation is used.

        Returns:
            dicte Summary of the process
        """
        _logger.info(f"Starting inventory valuation reconstruction.")
        _logger.info(f"Deleting existing valuation layers.")
        self._delete_valuation_layers()
        self._compute_in_stock_products()
        product_to_lines_dict = self._get_purchase_lines_by_product()
        product_to_sap_cost_dict = self._get_sap_cost_by_product()
        svl_dates = []
        for product in self.in_stock_products:
            lines = product_to_lines_dict.get(product.id, False)
            if not lines:
                self._create_svl_from_sap(product, product_to_sap_cost_dict)
                continue
            qty_remaining = product.qty_available
            while qty_remaining > 0:
                if not lines:
                    self._create_svl_from_sap(
                        product,
                        product_to_sap_cost_dict,
                        qty_remaining,
                    )
                    break
                line = lines.pop(0)
                qty = min(qty_remaining, line["qty"])
                rates = (
                    self.env["res.currency"]
                    .browse(line["currency_id"])
                    ._get_rates(product.company_id or self.env.company, line["date"])
                )
                rate = rates.get(1, 1)
                price = line["price"] / rate
                svl = self._create_valuation_layer(product, price, qty)
                svl_dates.append((svl.id, line["date"]))
                _logger.info(
                    f"Created valuation layer for product"
                    f" {product.id} ({product.display_name}) from purchase order"
                    f" {line['order_id']}, Cost: {price}, Quantity: {qty}"
                )
                qty_remaining -= qty
            values_list = []
            for row in svl_dates:
                svl_id, date = row
                formatted_date = date.strftime("%Y-%m-%d %H:%M:%S")
                values_list.append(f"({svl_id}, '{formatted_date}')")

        sql = f"""
        UPDATE product_product SET standard_price = jsonb_build_object('1', svl.value)
        FROM (
            SELECT
                product_id AS product_id,
                AVG(unit_cost * remaining_qty) AS value
            FROM stock_valuation_layer
            WHERE remaining_qty>0
            GROUP BY product_id 
        ) as svl
        WHERE svl.product_id = product_product.id
        """
        _logger.info(
            f"Updating average cost on products based on stock valuation layers."
        )
        self.env.cr.execute(sql)
        sql = f"""
        UPDATE stock_valuation_layer svl
        SET create_date = svl_dates.create_date::timestamp
        FROM
        (VALUES {', '.join(values_list)} ) as svl_dates (id, create_date)
        WHERE 
        svl.id = svl_dates.id
        """
        _logger.info(f"Updating create_date for stock valuation layers: {sql}")
        self.env.cr.execute(sql)
        _logger.info("Stock valuation update complete.")
