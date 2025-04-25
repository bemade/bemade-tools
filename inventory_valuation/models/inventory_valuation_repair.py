import logging
from odoo import fields, models, _, Command
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class InventoryValuationRepair(models.TransientModel):
    """
    Specialized tool for repairing inventory valuation after switching from manual to automatic.

    This class implements a structured process to:
    1. Fix broken stock valuation layers
    2. Revalue subsequent layers using FIFO principles
    3. Update product costs to reflect correct valuation
    4. Recalculate COGS on invoices
    5. Fix account entries to use the correct accounts

    Usage from Odoo shell:
    ```
    env['inventory.valuation.repair'].create_repair().repair_inventory_valuation()
    ```
    """

    _name = "inventory.valuation.repair"
    _description = "Inventory Valuation Repair Tool"

    # Configuration fields
    broken_svl_label = fields.Char(
        string="Broken SVL Label",
        default="Valuation method change",
        help="Label used to identify broken stock valuation layers",
    )
    valuation_change_date = fields.Date(
        string="Valuation Change Date",
        default="2025-04-08",
        help="Date when valuation method was changed from manual to automatic",
    )
    migration_date = fields.Date(
        string="Migration Date",
        default="2025-02-08",
        help="Date when the migration was completed",
    )
    stock_account_code = fields.Char(
        string="Stock Account Code",
        default="1300",
        help="Code of the stock account",
    )
    equity_account_code = fields.Char(
        string="Equity Account Code",
        default="3400",
        help="Code of the equity account used for broken SVL corrections",
    )
    interim_received_account_code = fields.Char(
        string="Interim Received Account Code",
        default="2028",
        help="Code of the interim account for goods received but not billed",
    )
    interim_delivered_account_code = fields.Char(
        string="Interim Delivered Account Code",
        default="1128",
        help="Code of the interim account for goods delivered but not billed",
    )
    broken_interim_received_code = fields.Char(
        string="Broken Interim Received Code",
        default="5060",
        help="Code of the broken interim account for goods received but not billed",
    )

    def repair_inventory_valuation(self):
        """
        Execute the complete repair process in the correct order.

        This method follows the process:
        1. Fix broken SVLs
        2. Revalue subsequent layers using FIFO
        3. Update product costs
        4. Recalculate invoice COGS
        5. Fix account entries
        """
        _logger.info("Starting inventory valuation repair process...")

        # Step 1: Fix broken SVLs
        moves_to_update = self._fix_broken_svls()
        _logger.info("Step 1 completed: Broken SVLs fixed")

        # Step 2: Revalue subsequent layers using FIFO
        moves_to_update |= self._revalue_subsequent_layers()
        _logger.info("Step 2 completed: Subsequent layers revalued")

        # Step 3: Update the account moves linked to modified layers
        self._update_account_moves(moves_to_update)

        # Step 3: Update product costs
        self._update_product_costs()
        _logger.info("Step 3 completed: Product costs updated")

        # Step 4: Recalculate invoice COGS
        self._recalculate_invoice_cogs()
        _logger.info("Step 4 completed: Invoice COGS recalculated")

        # Step 5: Fix account entries
        self._fix_account_entries()
        _logger.info("Step 5 completed: Account entries fixed")

        # Step 6: Globally reverse entries affecting Stock prior to the valuation change date
        self._reverse_predating_stock_entries()

        _logger.info("Inventory valuation repair process completed successfully")
        return True

    def _log(self, message):
        """Add a log message to the logger."""
        _logger.info(message)

    def _fix_broken_svls(self):
        """
        Step 1: Find and fix broken stock valuation layers.

        This method:
        1. Identifies products with broken SVLs
        2. Fixes the journal entries related to broken SVLs
        3. Revalues each broken SVL based on its predecessor
        """
        _logger.info("Finding products with broken SVLs...")

        # Find products with broken SVLs
        broken_products = self.env["product.product"].search(
            [
                (
                    "stock_valuation_layer_ids.account_move_id.line_ids.name",
                    "ilike",
                    self.broken_svl_label,
                )
            ]
        )

        if not broken_products:
            _logger.info("No products with broken SVLs found")
            return

        _logger.info(f"Found {len(broken_products)} products with broken SVLs")

        # Step 1.1: Fix journal entries related to broken SVLs
        self._fix_broken_journal_entries()

        moves_to_update = self.env["account.move"]
        # Step 1.2: Revalue broken SVLs
        for product in broken_products:
            _logger.info(f"Processing product: {product.name} (ID: {product.id})")

            # Get all SVLs for this product, ordered by ID (chronological)
            svls = product.stock_valuation_layer_ids.sorted(key=lambda l: l.id)

            # Find and revalue broken SVLs
            predecessor_svl = None
            for svl in svls:
                if svl.account_move_id.line_ids.filtered(
                    lambda l: l.name and self.broken_svl_label in l.name
                ):
                    if predecessor_svl:
                        moves_to_update |= self._revalue_broken_svl(
                            svl, predecessor_svl
                        )
                predecessor_svl = svl
        return moves_to_update

    def _revalue_subsequent_layers(self):
        """
        Step 2: Revalue subsequent layers using FIFO principles.

        This method:
        1. Orders layers by ID (chronological order), grouped by product
        2. Finds the broken SVL for each product
        3. Processes subsequent layers, updating outgoing layers based on FIFO
        4. Updates the account moves linked to modified layers
        """
        _logger.info("Revaluing subsequent layers using FIFO principles...")

        # Get all the SVLs, grouped by product and ordered by ID (chronological)
        svls_dict = self.env["stock.valuation.layer"].search([]).grouped("product_id")
        moves_to_update = self.env["account.move"]
        for product, svls in svls_dict.items():
            svls = svls.sorted("id")
            for index, svl in enumerate(svls):
                if svl.account_move_id and svl.account_move_id.line_ids.filtered(
                    lambda l: l.name and self.broken_svl_label in l.name
                ):
                    moves_to_update |= self._revalue_subsequent_layers_for_product(
                        product,
                        svl,
                        svls[index + 1 :],
                    )
        return moves_to_update

    def _revalue_subsequent_layers_for_product(
        self,
        product,
        broken_svl,
        subsequent_layers,
    ):
        """
        Revalue subsequent layers for a product using FIFO principles.

        Args:
            product: The product to revalue
            broken_svl: The broken SVL that serves as the starting point
            subsequent_layers: Layers that come after the broken SVL
        """
        moves_to_update = self.env["account.move"]
        if not subsequent_layers:
            _logger.info(
                f"No subsequent layers found for product {product.name}, skipping"
            )
            return moves_to_update

        _logger.info(f"Found {len(subsequent_layers)} subsequent layers to revalue")

        # Initialize the FIFO queue with the broken SVL
        fifo_queue = [
            {"qty_remaining": broken_svl.quantity, "unit_cost": broken_svl.unit_cost}
        ]

        # Process each subsequent layer
        for layer in subsequent_layers:
            if layer.quantity > 0:  # Incoming layer
                # Add to the queue with its full quantity
                fifo_queue.append(
                    {"qty_remaining": layer.quantity, "unit_cost": layer.unit_cost}
                )
            elif layer.quantity < 0:  # Outgoing layer
                # Process using FIFO
                remaining_qty_to_consume = abs(layer.quantity)
                consumed_value = 0

                # Consume from the queue
                i = 0
                while i < len(fifo_queue) and remaining_qty_to_consume > 0:
                    queue_item = fifo_queue[i]

                    # How much can we consume from this queue item
                    qty_to_consume = min(
                        remaining_qty_to_consume, queue_item["qty_remaining"]
                    )

                    if qty_to_consume <= 0:
                        i += 1
                        continue

                    # Calculate the value to consume
                    value_to_consume = qty_to_consume * queue_item["unit_cost"]
                    consumed_value += value_to_consume

                    # Update remaining quantities
                    remaining_qty_to_consume -= qty_to_consume
                    queue_item["qty_remaining"] -= qty_to_consume

                    # If this item is exhausted, remove it from the queue
                    if queue_item["qty_remaining"] <= 0:
                        fifo_queue.pop(i)
                    else:
                        i += 1

                # If we've consumed everything needed and the value is different, update the layer
                new_value = -consumed_value
                if abs(new_value - layer.value) > 0.01:
                    old_value = layer.value
                    new_unit_cost = new_value / layer.quantity

                    layer.write(
                        {
                            "value": new_value,
                            "unit_cost": new_unit_cost,
                            "remaining_value": new_unit_cost * layer.remaining_qty,
                        }
                    )
                    _logger.info(
                        f"Updated layer {layer.id} value from {old_value} to {new_value}"
                    )

                    moves_to_update |= layer.account_move_id
        return moves_to_update

    def _fix_broken_journal_entries(self):
        """
        Fix journal entries related to broken SVLs.

        This updates the accounts used in journal entries related to broken SVLs,
        ensuring they use the correct stock and equity accounts.
        """
        _logger.info("Fixing broken SVL journal entries...")

        # Get the correct accounts
        stock_account = self.env["account.account"].search(
            [("code", "=", self.stock_account_code)], limit=1
        )
        equity_account = self.env["account.account"].search(
            [("code", "=", self.equity_account_code)], limit=1
        )

        if not stock_account or not equity_account:
            raise UserError(
                f"Stock account {self.stock_account_code} "
                f"or equity account {self.equity_account_code} not found."
            )

        # Find journal items related to broken SVLs
        broken_journal_items = self.env["account.move.line"].search(
            [("move_id.line_ids.name", "ilike", self.broken_svl_label)]
        )

        if not broken_journal_items:
            _logger.info("No broken journal entries found")
            return

        _logger.info(f"Found {len(broken_journal_items)} journal items to fix")

        # Get the wrong accounts that need to be replaced
        wrong_debit_accounts = self.env["account.account"].search(
            [("code", "in", ["1028", "2028"])]  # Interim accounts
        )

        wrong_credit_accounts = self.env["account.account"].search(
            [("code", "in", ["1300", "1028", "2028"])]  # Stock and interim accounts
        )

        # Update the accounts
        debit_lines = broken_journal_items.filtered(
            lambda aml: aml.account_id in wrong_debit_accounts
        )
        credit_lines = broken_journal_items.filtered(
            lambda aml: aml.account_id in wrong_credit_accounts
        )

        if debit_lines:
            debit_lines.write({"account_id": stock_account.id})
            _logger.info(f"Updated {len(debit_lines)} debit lines to use stock account")

        if credit_lines:
            credit_lines.write({"account_id": equity_account.id})
            _logger.info(
                f"Updated {len(credit_lines)} credit lines to use equity account"
            )

    def _revalue_broken_svl(self, broken_svl, predecessor_svl):
        """
        Revalue a broken SVL based on its predecessor.

        Args:
            broken_svl: The broken stock valuation layer to fix
            predecessor_svl: The SVL that comes before the broken one
        """
        if not broken_svl or not predecessor_svl:
            _logger.info(
                f"Missing broken_svl or predecessor_svl for product {broken_svl.product_id.name}, skipping"
            )
            return

        product = broken_svl.product_id
        _logger.info(
            f"Revaluing SVL {broken_svl.id} for product {product.name} (ID: {product.id})"
        )

        # Calculate the target value we want to achieve
        target_value = abs(predecessor_svl.value)
        current_value = broken_svl.value
        adjustment = target_value - current_value

        if abs(adjustment) < 0.01:
            _logger.info(f"Adjustment is negligible (${adjustment}), skipping")
            return self.env["account.move"]

        _logger.info(
            f"Current value: {current_value}, Target value: {target_value}, Adjustment: {adjustment}"
        )

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
            _logger.info(f"Error updating SVL value: {str(e)}")
            return

        return broken_svl.account_move_id

    def _update_account_moves(self, moves):
        """
        Update account moves to reflect the new valuation.

        Args:
            moves: List of account.move records to update
        """
        _logger.info(f"Updating {len(moves)} account moves")

        for move in moves:
            svl = move.stock_valuation_layer_ids
            if len(svl) != 1:
                raise ValidationError(
                    "Account move {move.name} should have exactly one SVL."
                )

            # Set to draft
            move.button_draft()

            debit_line = move.line_ids.filtered(lambda aml: aml.debit > 0)
            credit_line = move.line_ids.filtered(lambda aml: aml.credit > 0)

            if len(debit_line) != 1 or len(credit_line) != 1:
                raise ValidationError(
                    "Account move {move.name} should have exactly one debit and credit line."
                )

            move.write(
                {
                    "line_ids": [
                        Command.update(debit_line.id, {"debit": svl.value}),
                        Command.update(credit_line.id, {"credit": svl.value}),
                    ]
                }
            )
            # Post again
            move.action_post()

            _logger.info(f"Updated account move {move.id}")

    def _update_product_costs(self):
        """
        Step 3: Update product costs to reflect the correct valuation.

        This method updates the standard_price field on products based on:
        1. The remaining value and quantity for products with stock
        2. The last purchase price for products without stock
        """

        _logger.info("Updating product costs...")

        # Get all the SVLs grouped by product
        svls_dict = (
            self.env["stock.valuation.layer"]
            .search(
                [
                    ("remaining_qty", ">", 0),
                    ("product_id.cost_method", "=", "fifo"),
                ]
            )
            .grouped("product_id")
        )
        for product, svls in svls_dict.items():
            product.standard_price = sum(svl.remaining_value for svl in svls) / sum(
                svl.remaining_qty for svl in svls
            )
        # Update the standard price for products without stock valuation layers
        # based on purchase history
        self._fix_product_value_for_products_without_svl()
        _logger.info(f"Updated costs for {len(svls_dict)} products")

    def _recalculate_invoice_cogs(self):
        """
        Step 4: Recalculate COGS for all invoices after the migration.

        This method:
        1. Finds all posted customer invoices after the valuation change date
        2. Sets them to draft
        3. Posts them again to recalculate COGS with updated product costs
        4. Re-applies payment reconciliations
        """
        _logger.info("Recalculating COGS for invoices...")

        # Find all posted customer invoices after the valuation change date
        invoices = self.env["account.move"].search(
            [
                ("move_type", "=", "out_invoice"),
                ("state", "=", "posted"),
                ("date", ">", self.migration_date),
            ]
        )

        if not invoices:
            _logger.info("No invoices found to recalculate")
            return

        _logger.info(f"Found {len(invoices)} invoices to recalculate")

        # Process in batches to avoid memory issues
        batch_size = 50
        total_processed = 0

        for i in range(0, len(invoices), batch_size):
            batch = invoices[i : i + batch_size]
            _logger.info(
                f"Processing batch {i//batch_size + 1} with {len(batch)} invoices"
            )

            for invoice in batch:
                try:
                    # Find all reconciled payment lines for this invoice
                    reconciled_lines = self.env["account.move.line"]
                    for line in invoice.line_ids.filtered(
                        lambda l: l.account_id.account_type
                        in ("asset_receivable", "liability_payable")
                    ):
                        reconciled_lines |= (
                            line.matched_debit_ids.debit_move_id.filtered(
                                lambda l: l.id != line.id
                            )
                        )
                        reconciled_lines |= (
                            line.matched_credit_ids.credit_move_id.filtered(
                                lambda l: l.id != line.id
                            )
                        )

                    # Store the IDs of reconciled lines
                    reconciled_line_ids = reconciled_lines.ids

                    # Remember the original name/sequence
                    original_name = invoice.name

                    _logger.info(
                        f"Setting invoice {invoice.id} ({original_name}) to draft for COGS recalculation"
                    )
                    invoice.button_draft()

                    # Ensure the name is preserved (might be reset when set to draft)
                    if invoice.name != original_name:
                        invoice.name = original_name

                    _logger.info(f"Posting invoice {invoice.id} to recalculate COGS")
                    invoice.action_post()

                    # Re-apply reconciled lines
                    if reconciled_line_ids:
                        _logger.info(
                            f"Re-applying {len(reconciled_line_ids)} payment lines to invoice {invoice.id}"
                        )

                        # Get the new receivable/payable lines from the reposted invoice
                        new_receivable_lines = invoice.line_ids.filtered(
                            lambda l: l.account_id.account_type
                            in ("asset_receivable", "liability_payable")
                            and not l.reconciled
                        )

                        if not new_receivable_lines:
                            _logger.info(
                                f"No receivable/payable lines found for invoice {invoice.id} after reposting"
                            )
                            continue

                        # For each payment line, try to reconcile with the new invoice lines
                        for line_id in reconciled_line_ids:
                            try:
                                payment_line = self.env["account.move.line"].browse(
                                    line_id
                                )

                                # Skip if the payment line is already reconciled
                                if payment_line.reconciled:
                                    _logger.info(
                                        f"Payment line {line_id} is already reconciled, skipping"
                                    )
                                    continue

                                # Try to reconcile
                                (new_receivable_lines | payment_line).reconcile()
                                _logger.info(
                                    f"Successfully reconciled payment line {line_id} with invoice {invoice.id}"
                                )
                            except Exception as e:
                                _logger.info(
                                    f"Failed to reconcile payment line {line_id}: {str(e)}"
                                )
                                continue

                    total_processed += 1
                except Exception as e:
                    _logger.info(f"Error processing invoice {invoice.id}: {str(e)}")
                    continue

            _logger.info(
                f"Completed batch {i//batch_size + 1}, total processed: {total_processed}"
            )

        _logger.info(f"Completed recalculating COGS for {total_processed} invoices")

    def _fix_account_entries(self):
        """
        Step 5: Fix account entries to use the correct accounts.

        This method:
        1. Fixes SVL journal entries to use the correct stock and interim accounts
        2. Fixes invoice journal entries created before the valuation change date
           to use the stock account instead of interim accounts
        """
        _logger.info("Fixing account entries...")

        # Step 5.1: Fix SVL journal entries
        self._fix_svl_account_entries()

        # Step 5.2: Fix invoice account entries
        self._fix_invoice_account_entries()

    def _fix_svl_account_entries(self):
        """
        Fix account entries in stock valuation layers.

        This method updates the accounts used in journal entries related to SVLs,
        ensuring they use the correct accounts based on the direction of the move.
        """

        def _fix_debit_credit(lines, debit_account, credit_account):
            lines.filtered(lambda l: l.debit > 0).account_id = debit_account
            lines.filtered(lambda l: l.credit > 0).account_id = credit_account

        _logger.info("Fixing SVL account entries...")

        # Get the accounts
        stock_account = self.env["account.account"].search(
            [("code", "=", self.stock_account_code)], limit=1
        )
        interim_received_account = self.env["account.account"].search(
            [("code", "=", self.interim_received_account_code)], limit=1
        )
        interim_delivered_account = self.env["account.account"].search(
            [("code", "=", self.interim_delivered_account_code)], limit=1
        )
        equity_account = self.env["account.account"].search(
            [("code", "=", self.equity_account_code)], limit=1
        )

        if (
            not stock_account
            or not interim_received_account
            or not interim_delivered_account
            or not equity_account
        ):
            raise UserError(_("Required accounts not found"))

        # Find incoming moves (excluding broken SVLs)
        incoming_move_lines = self.env["account.move.line"].search(
            [
                ("move_id.stock_valuation_layer_ids", "!=", False),
                ("move_id.stock_valuation_layer_ids.quantity", ">", 0),
                ("name", "not ilike", self.broken_svl_label),
            ]
        )
        _fix_debit_credit(incoming_move_lines, stock_account, interim_received_account)

        # Find outgoing moves
        outgoing_move_lines = self.env["account.move.line"].search(
            [
                ("move_id.stock_valuation_layer_ids", "!=", False),
                ("move_id.stock_valuation_layer_ids.quantity", "<", 0),
            ]
        )

        _fix_debit_credit(outgoing_move_lines, interim_delivered_account, stock_account)

        # Handle broken SVL entries separately
        broken_svl_lines = self.env["account.move.line"].search(
            [
                ("move_id.stock_valuation_layer_ids", "!=", False),
                ("move_id.stock_valuation_layer_ids.quantity", ">", 0),
                ("name", "ilike", self.broken_svl_label),
            ]
        )

        _fix_debit_credit(broken_svl_lines, stock_account, equity_account)

        _logger.info(
            f"Fixed {len(incoming_move_lines) + len(outgoing_move_lines) + len(broken_svl_lines)} account entries."
        )

    def _fix_invoice_account_entries(self):
        """
        Fix account entries in invoices created before the valuation change date.

        This ensures that invoices created before the valuation change date use
        the stock account (1300) instead of the interim accounts (1028, 2028).
        """
        _logger.info("Fixing invoice account entries...")

        # Get the accounts
        stock_account = self.env["account.account"].search(
            [("code", "=", self.stock_account_code)], limit=1
        )
        interim_received_account = self.env["account.account"].search(
            [("code", "=", self.interim_received_account_code)], limit=1
        )
        interim_delivered_account = self.env["account.account"].search(
            [("code", "=", self.interim_delivered_account_code)], limit=1
        )
        broken_interim_receipt_account = self.env["account.account"].search(
            [("code", "=", self.broken_interim_received_code)], limit=1
        )

        if (
            not stock_account
            or not interim_received_account
            or not interim_delivered_account
        ):
            raise UserError(_("Required accounts not found"))

        # Find invoice lines using interim accounts before the valuation change date
        invoice_lines = self.env["account.move.line"].search(
            [
                ("move_id.date", "<", self.valuation_change_date),
                (
                    "account_id",
                    "in",
                    [
                        interim_received_account.id,
                        interim_delivered_account.id,
                        broken_interim_receipt_account.id,
                    ],
                ),
                (
                    "move_id.move_type",
                    "in",
                    ["out_invoice", "out_refund", "in_invoice", "in_refund"],
                ),
            ]
        )

        if not invoice_lines:
            _logger.info("No invoice lines found to fix account entries")
            return

        _logger.info(f"Found {len(invoice_lines)} invoice lines to fix account entries")

        # Update the account to stock account
        invoice_lines.write({"account_id": stock_account.id})

        _logger.info(f"Updated {len(invoice_lines)} invoice lines to use stock account")

    def _reverse_predating_stock_entries(self):
        _logger.info(
            f"Reversing stock entries for invoices and bills predating {self.valuation_change_date}"
        )
        lines = self.env["account.move.line"].search(
            [
                (
                    "move_id.move_type",
                    "in",
                    ["out_invoice", "out_refund", "in_invoice", "in_refund"],
                ),
                ("move_id.date", "<", self.valuation_change_date),
                ("account_id.code", "=", self.stock_account_code),
                ("move_id.state", "=", "posted"),
            ]
        )
        balance = sum(lines.mapped("balance"))
        equity_account = self.env["account.account"].search(
            [("code", "=", self.equity_account_code)], limit=1
        )
        stock_account = self.env["account.account"].search(
            [("code", "=", self.stock_account_code)], limit=1
        )
        if balance > 0:
            debit_account = equity_account
            credit_account = stock_account
        else:
            debit_account = stock_account
            credit_account = equity_account
        new_move = self.env["account.move"].create(
            {
                "date": fields.Date.today(),
                "journal_id": 8,
                "move_type": "entry",
                "ref": "Inventory Valuation Repair - reverse old entries",
                "line_ids": [
                    Command.create(
                        {
                            "account_id": debit_account.id,
                            "name": "Inventory Valuation Repair - reverse old entries",
                            "debit": abs(balance),
                            "credit": 0,
                        }
                    ),
                    Command.create(
                        {
                            "account_id": credit_account.id,
                            "name": "Inventory Valuation Repair - reverse old entries",
                            "debit": 0,
                            "credit": abs(balance),
                        }
                    ),
                ],
            }
        )
        new_move.action_post()

    def _fix_product_value_for_products_without_svl(self):
        invoices = self.env["account.move"].search(
            [
                ("date", "<", self.valuation_change_date),
                ("date", ">", "2025-02-08"),
                ("move_type", "in", ["out_invoice", "out_refund"]),
                ("state", "=", "posted"),
            ]
        )
        products_without_svl = invoices.line_ids.mapped("product_id").filtered(
            lambda prod: not prod.stock_valuation_layer_ids
        )
        for product in products_without_svl:
            last_purchase = self.env["purchase.order.line"].search(
                [("product_id", "=", product.id)],
                order="date_order desc",
                limit=1,
            )
            if last_purchase and last_purchase.product_uom_qty != 0:
                price_raw = last_purchase.price_subtotal / last_purchase.product_uom_qty
                price = price_raw / last_purchase.order_id.currency_rate
                product.write({"standard_price": price})
