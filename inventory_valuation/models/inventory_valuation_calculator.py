import logging
from odoo import models, fields, Command, api
from odoo.exceptions import ValidationError
from odoo.tools.sql import SQL
from datetime import timedelta

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
                if not lines:
                    costs[product] = 0.0
                    break
                line = lines.pop(0)
                qty = min(line.get("qty"), quantity_to_cover)
                total_cost += line.get("price") * qty
                total_qty += qty
                quantity_to_cover -= qty
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
        FROM OITW
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
            dict: Summary of the process
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

    def repair_after_manual_to_auto_valuation(self):
        label = "Valuation method change"
        _logger.info(
            "Starting repair of valuation and journal entries created by going to automatic valuation."
        )
        broken_products = self.env["product.product"].search(
            [
                (
                    "stock_valuation_layer_ids.account_move_id.invoice_line_ids.display_name",
                    "ilike",
                    label,
                )
            ]
        )
        correct_credit_account = self.env["account.account"].search(
            [
                (
                    "code",
                    "=",
                    "3400",
                )
            ]
        )
        self._fix_broken_journal_entries(label, correct_credit_account)

        _logger.info("Creating stock valuation layer corrections.")
        for product in broken_products:
            # Iterate until we find the broken one.
            # The broken one's predecessor will have the value that we need to use to
            # adjust the remaining value on the broken one.
            svls = product.stock_valuation_layer_ids
            last_svl = self.env["stock.valuation.layer"]
            for svl in svls:
                if svl.account_move_id.invoice_line_ids.filtered(
                    lambda n: "Valuation method change" in n.display_name
                ):
                    broken_svl = svl
                    predecessor_svl = last_svl
                    self._revaluate_broken_svl(broken_svl, predecessor_svl)
                last_svl = svl

    def _fix_broken_journal_entries(self, label, correct_credit_account):
        _logger.info("Fixing broken journal entries (wrong accounts).")
        broken_journal_entries = self.env["account.move"].search(
            [
                (
                    "line_ids.name",
                    "ilike",
                    label,
                )
            ]
        )
        wrong_debit_accounts = self.env["account.account"].search(
            [("code", "in", ["1511"])]
        )
        wrong_credit_accounts = self.env["account.account"].search(
            [("code", "in", ["1700", "1701", "2028"])]
        )
        correct_debit_account = self.env["account.account"].search(
            [
                (
                    "code",
                    "=",
                    "1300",
                )
            ]
        )
        broken_journal_entries.button_draft()
        _logger.info("Entries are set to draft... fixing accounts.")
        debit_lines = broken_journal_entries.line_ids.filtered(
            lambda aml: aml.account_id in wrong_debit_accounts
        )
        credit_lines = broken_journal_entries.line_ids.filtered(
            lambda aml: aml.account_id in wrong_credit_accounts
        )
        debit_lines.account_id = correct_debit_account
        credit_lines.account_id = correct_credit_account
        _logger.info("Entries are corrected, posting.")
        broken_journal_entries.action_post()

    def _revaluate_broken_svl(self, broken_svl, predecessor_svl):
        """
        Directly revalue a broken stock valuation layer and adjust its account move.
        """
        if not broken_svl or not predecessor_svl:
            _logger.info("Missing broken_svl or predecessor_svl, skipping revaluation")
            return

        product = broken_svl.product_id
        _logger.info(
            f"Revaluing SVL {broken_svl.id} for product {product.name} (id: {product.id})"
        )

        # Calculate the target value we want to achieve
        target_value = abs(predecessor_svl.value)
        current_value = broken_svl.value
        adjustment = target_value - current_value

        if abs(adjustment) < 0.01:
            _logger.info(f"Adjustment is negligible (${adjustment}), skipping")
            return

        _logger.info(
            f"Current value: {current_value}, Target value: {target_value}, Adjustment: {adjustment}"
        )

        # Collect all account moves that need to be updated
        moves_to_update = []

        # Get the associated account move
        account_move = broken_svl.account_move_id
        if account_move:
            moves_to_update.append(account_move)

        # Update the SVL value directly
        try:
            old_value = broken_svl.value
            new_unit_cost = (
                target_value / broken_svl.quantity if broken_svl.quantity else 0
            )

            broken_svl.write(
                {
                    "value": target_value,
                    "remaining_value": (
                        target_value if broken_svl.remaining_qty > 0 else 0
                    ),
                    "unit_cost": new_unit_cost,
                }
            )
            _logger.info(
                f"Updated SVL {broken_svl.id} value from {old_value} to {target_value}"
            )
        except Exception as e:
            _logger.error(f"Error updating SVL value: {str(e)}", exc_info=True)
            return

        # Now revalue the entire chain of subsequent SVLs
        self._revalue_subsequent_layers(broken_svl, moves_to_update)

        # Update all the collected account moves
        if moves_to_update:
            self._update_account_moves(moves_to_update)

        # Update related invoices to recalculate COGS
        self._update_related_invoices(broken_svl)

    def _revalue_subsequent_layers(self, broken_svl, moves_to_update):
        """
        Revalue the entire chain of subsequent SVLs using FIFO principles.

        This method:
        1. Finds all subsequent SVLs for the product (both incoming and outgoing)
        2. Simulates FIFO consumption to recalculate values
        3. Updates each layer with the new value
        """
        product = broken_svl.product_id

        # Get all subsequent layers, both incoming and outgoing, in chronological order
        subsequent_layers = self.env["stock.valuation.layer"].search(
            [
                ("product_id", "=", product.id),
                ("id", ">", broken_svl.id),  # Exclude the broken SVL itself
            ],
            order="create_date asc",
        )

        if not subsequent_layers:
            _logger.info(f"No subsequent layers found for product {product.id}")
            return

        _logger.info(f"Found {len(subsequent_layers)} subsequent layers to revalue")

        # Initialize our available layers queue with the revalued layer
        # We start with the FULL quantity of the broken SVL, not just the remaining quantity
        available_fifo_layers = [
            {
                "svl_id": broken_svl.id,
                "remaining_qty": broken_svl.quantity,  # Use full quantity, not remaining_qty
                "unit_cost": broken_svl.unit_cost,
            }
        ]

        _logger.info(
            f"Starting with {broken_svl.quantity} units from broken SVL at unit cost {broken_svl.unit_cost}"
        )

        # Process each subsequent layer
        for layer in subsequent_layers:
            _logger.info(
                f"Processing layer {layer.id}: qty={layer.quantity}, value={layer.value}"
            )

            if layer.quantity > 0:  # Incoming layer
                # Add this layer to the available layers
                available_fifo_layers.append(
                    {
                        "svl_id": layer.id,
                        "remaining_qty": layer.quantity,
                        "unit_cost": layer.unit_cost,
                    }
                )
                _logger.info(f"Added incoming layer {layer.id} to available layers")
                continue

            # For outgoing layers, we need to consume from available layers
            if layer.quantity >= 0:
                continue  # Skip zero or positive quantity layers

            qty_to_consume = abs(layer.quantity)
            new_value = 0
            layer_costs = []

            # Consume from available layers using FIFO
            remaining_to_consume = qty_to_consume
            i = 0

            while remaining_to_consume > 0 and i < len(available_fifo_layers):
                avail_layer = available_fifo_layers[i]

                if avail_layer["remaining_qty"] <= 0:
                    i += 1
                    continue

                # Calculate how much we can take from this layer
                qty_taken = min(avail_layer["remaining_qty"], remaining_to_consume)
                value_taken = qty_taken * avail_layer["unit_cost"]

                # Record the cost for this portion
                layer_costs.append(
                    {
                        "qty": qty_taken,
                        "unit_cost": avail_layer["unit_cost"],
                        "value": value_taken,
                    }
                )

                # Update remaining quantities
                available_fifo_layers[i]["remaining_qty"] -= qty_taken
                remaining_to_consume -= qty_taken

                if available_fifo_layers[i]["remaining_qty"] <= 0:
                    i += 1

                _logger.info(
                    f"Consumed {qty_taken} units at {avail_layer['unit_cost']} per unit from layer {avail_layer['svl_id']}"
                )

            # If we couldn't consume everything, use the last known cost
            if remaining_to_consume > 0 and layer_costs:
                last_cost = layer_costs[-1]["unit_cost"]
                value_taken = remaining_to_consume * last_cost

                layer_costs.append(
                    {
                        "qty": remaining_to_consume,
                        "unit_cost": last_cost,
                        "value": value_taken,
                    }
                )

                _logger.warning(
                    f"Insufficient stock available, using last known cost {last_cost} for remaining {remaining_to_consume} units"
                )
            elif remaining_to_consume > 0 and not layer_costs:
                # If we have nothing to consume from, use the broken SVL's unit cost
                value_taken = remaining_to_consume * broken_svl.unit_cost

                layer_costs.append(
                    {
                        "qty": remaining_to_consume,
                        "unit_cost": broken_svl.unit_cost,
                        "value": value_taken,
                    }
                )

                _logger.warning(
                    f"No available layers to consume from, using broken SVL cost {broken_svl.unit_cost} for {remaining_to_consume} units"
                )

            # Calculate the new total value for this outgoing layer
            total_value = sum(cost["value"] for cost in layer_costs)
            new_value = -total_value  # Negative because it's an outgoing layer

            # Update the layer with the new value
            old_value = layer.value
            new_unit_cost = total_value / qty_to_consume if qty_to_consume else 0

            layer.write(
                {
                    "value": new_value,
                    "unit_cost": new_unit_cost,
                }
            )

            _logger.info(
                f"Updated layer {layer.id} value from {old_value} to {new_value}"
            )

            # Add the layer's account move to the list to update
            if layer.account_move_id:
                moves_to_update.append(layer.account_move_id)

        _logger.info(
            f"Completed revaluation of subsequent layers for product {product.id}"
        )

    def _update_related_invoices(self, svl):
        """
        Find and update invoices related to the revalued stock valuation layer.

        This method:
        1. Finds invoices with lines for the same product after the SVL date
        2. Sets them to draft while preserving payment reconciliations
        3. Posts them again to recalculate COGS with the new valuation
        4. Re-applies the payment reconciliations
        """
        product = svl.product_id
        svl_date = svl.create_date

        _logger.info(
            f"Finding invoices for product {product.id} ({product.name}) after {svl_date}"
        )

        # Find all posted customer invoices with this product after the SVL date
        domain = [
            ("move_type", "=", "out_invoice"),
            ("state", "=", "posted"),
            ("invoice_date", ">=", svl_date.date()),
            ("invoice_line_ids.product_id", "=", product.id),
        ]

        invoices = self.env["account.move"].search(domain)

        if not invoices:
            _logger.info(f"No invoices found for product {product.id} after {svl_date}")
            return

        _logger.info(
            f"Found {len(invoices)} invoices to update for product {product.id}"
        )

        for invoice in invoices:
            # Find all reconciled payment lines for this invoice
            reconciled_lines = self.env["account.move.line"]
            for line in invoice.line_ids.filtered(
                lambda l: l.account_id.account_type
                in ("asset_receivable", "liability_payable")
            ):
                reconciled_lines |= line.matched_debit_ids.debit_move_id.filtered(
                    lambda l: l.id != line.id
                )
                reconciled_lines |= line.matched_credit_ids.credit_move_id.filtered(
                    lambda l: l.id != line.id
                )

            # Store the IDs of reconciled lines
            reconciled_line_ids = reconciled_lines.ids

            if reconciled_line_ids:
                _logger.info(
                    f"Found {len(reconciled_line_ids)} reconciled payment lines for invoice {invoice.id}"
                )

            # Remember the original name/sequence
            original_name = invoice.name

            _logger.info(
                f"Setting invoice {invoice.id} ({original_name}) to draft for recalculation"
            )
            invoice.button_draft()

            # Ensure the name is preserved (might be reset when set to draft)
            if invoice.name != original_name:
                invoice.name = original_name

            _logger.info(f"Posting invoice {invoice.id} to recalculate COGS")
            invoice.action_post()

            # Re-apply reconciled lines using js_assign_outstanding_line, which is
            # the method called when applying a payment to an invoice from the UI
            if reconciled_line_ids:
                _logger.info(
                    f"Re-applying {len(reconciled_line_ids)} payment lines to invoice {invoice.id}"
                )
                for line_id in reconciled_line_ids:
                    invoice.js_assign_outstanding_line(line_id)
                    _logger.info(
                        f"Re-applied payment line {line_id} to invoice {invoice.id}"
                    )

    def _update_account_moves(self, moves):
        """Update account moves by setting to draft, updating values, and re-posting."""
        if not moves:
            return

        # Deduplicate moves
        unique_moves = list(set(moves))
        _logger.info(f"Updating {len(unique_moves)} account moves")

        # Process each move
        for move in unique_moves:
            # Set to draft if posted
            if move.state == "posted":
                move.button_draft()
                _logger.info(f"Set account move {move.id} to draft")

            svl = move.stock_valuation_layer_ids
            if not svl or len(svl) > 1:
                _logger.warning(
                    f"Unexpected number of SVLs ({len(svl)}) for journal entry {move.id}"
                )
                continue

            # Find the debit and credit lines
            move_lines = move.line_ids
            debit_line = move_lines.filtered(lambda l: l.debit > 0)
            credit_line = move_lines.filtered(lambda l: l.credit > 0)

            if not debit_line or not credit_line:
                _logger.warning(
                    f"Could not find both debit and credit lines in move {move.id}"
                )
                continue

            # Update the debit and credit lines with the new values
            new_value = abs(svl.value)

            _logger.info(f"Updating move {move.id} lines - New value: {new_value}")

            # Important: Update both lines in a single write operation to maintain balance
            # This is how Odoo handles updating move lines while maintaining the balance constraint
            move.write(
                {
                    "line_ids": [
                        Command.update(
                            debit_line.id,
                            {
                                "debit": new_value,
                                "credit": 0.0,
                                "balance": new_value,
                            },
                        ),
                        Command.update(
                            credit_line.id,
                            {
                                "debit": 0.0,
                                "credit": new_value,
                                "balance": -new_value,
                            },
                        ),
                    ]
                }
            )
            _logger.info(f"Updated move lines for account move {move.id}")

            # Re-post the move
            move.action_post()
            _logger.info(f"Re-posted account move {move.id}")


"""
Notes on things that are still broken:
    1. The stock valuation layers that are in the "broken svl" should have their credit
    account changed to the correct one, something in equity.
    2. The vendor bills and customer invoices prior to the valuation method change need
    to debit and credit the stock account 1300 instead of the stock received/delivered
    but not paid accounts ().
    3. The product cost field needs to be updated to reflect the average unit price
    of the remaining stock or of the last SVL if there is no stock remaining.
     
"""
