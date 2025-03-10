# Inventory Valuation Tools

This module provides tools for inventory valuation tasks in Odoo 18.0.

## Features

### Inventory Valuation Reconstruction

Recalculate product costs based on historical purchase orders to fix incorrect inventory valuation.

- Identifies all products with current on-hand quantity
- Calculates weighted average cost based on purchase history
- Updates product cost prices directly
- Generates detailed logs of all changes

### Usage

1. Navigate to Inventory > Configuration > Reconstruct Inventory Valuation
2. Click "Reconstruct Valuation" to start the process
3. The system will update product costs based on purchase history
4. A notification will appear showing how many products were updated

## Technical Details

- Uses SQL queries for efficient processing of purchase history
- Directly updates product standard_price field
- Detailed logging for audit purposes

## Requirements

- Odoo 18.0
- Stock, Stock Account, and Purchase modules

## Author

Bemade Inc. (https://bemade.org)
