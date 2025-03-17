{
    "name": "Inventory Valuation Tools",
    "version": "18.0.1.0.0",
    "category": "Inventory/Inventory",
    "summary": "Tools for inventory valuation reconstruction and configuration",
    "description": """
Inventory Valuation Tools for Pneumac Automation
================================================

This module provides tools to help with inventory valuation tasks:

1. **Inventory Valuation Reconstruction**
   - Recalculate product costs based on historical purchase orders
   - Fix incorrect inventory valuation after migration from SAP Business One
   - Generate detailed reports on cost changes

2. **Product Category Configuration**
   - Batch configure inventory valuation settings across product categories
   - Set up accounting properties for proper inventory valuation
   - Ensure consistent configuration across the system

These tools are accessible through dedicated wizards in the Inventory app,
allowing administrators to:

- Run inventory valuation reconstruction in dry-run mode to preview changes
- Apply cost corrections based on historical purchase data
- Configure product categories with correct accounting settings
- Generate detailed reports for auditing and verification

Technical Features:
-------------------
- Weighted average cost calculation based on purchase history
- Configurable thresholds for cost difference and purchase history coverage
- CSV report generation for processed products, skipped products, and errors
- Support for date range filtering of purchase orders
""",
    "author": "Bemade Inc.",
    "website": "https://bemade.org",
    "license": "LGPL-3",
    "depends": [
        "stock",
        "stock_account",
        "purchase",
        "sap_b1_to_odoo",
    ],
    "data": ["security/ir.model.access.csv"],
    "demo": [],
    "installable": True,
    "application": False,
    "auto_install": False,
}
