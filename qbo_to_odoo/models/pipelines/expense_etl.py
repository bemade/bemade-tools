"""QuickBooks Online Expense (Purchase) ETL Pipeline

This module handles the migration of Purchases (expenses) from QBO to Odoo hr.expense
using the ETL framework.

In QBO, the Purchase entity represents:
- Check payments
- Credit card charges
- Cash purchases
These map to hr.expense in Odoo.
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext

from .utils import get_api_client

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="hr.expense",
    importer_name="qbo.expense.importer",
    sap_source="Purchase",
    depends_on=["qbo.employee.importer", "qbo.account.importer"],
)
class QboExpenseImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO Purchases as hr.expense."""

    _name = "qbo.expense.importer"
    _description = "QBO Expense Importer"

    @ETL.extract("Purchase")
    def extract_expenses(self, ctx: ETLContext) -> List[Dict]:
        """Extract purchases (expenses) from QBO API."""
        api_client = get_api_client(ctx)

        # Get existing QBO purchase IDs
        ctx.env.cr.execute(
            "SELECT qbo_purchase_id FROM hr_expense WHERE qbo_purchase_id IS NOT NULL"
        )
        existing_ids = {str(row[0]) for row in ctx.env.cr.fetchall()}
        _logger.info(f"Found {len(existing_ids)} existing expenses in Odoo")

        # Fetch all purchases from QBO
        purchases = api_client.query_all(entity="Purchase", order_by="Id")

        # Filter out already imported
        new_purchases = [p for p in purchases if str(p.get("Id")) not in existing_ids]

        _logger.info(
            f"Extracted {len(purchases)} purchases from QBO, "
            f"{len(new_purchases)} are new"
        )
        return new_purchases

    @ETL.transform()
    def transform_expenses(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO purchases into Odoo hr.expense values."""
        purchases = extracted.get("extract_expenses", [])

        # Build lookups
        ctx.env.cr.execute(
            "SELECT qbo_employee_id, id FROM hr_employee WHERE qbo_employee_id IS NOT NULL"
        )
        employee_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        ctx.env.cr.execute(
            "SELECT qbo_id, id FROM account_account WHERE qbo_id IS NOT NULL"
        )
        account_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        ctx.env.cr.execute(
            "SELECT qbo_vendor_id, id FROM res_partner WHERE qbo_vendor_id IS NOT NULL"
        )
        vendor_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        company = ctx.env.company

        # Get default expense product (or create one)
        expense_product = ctx.env["product.product"].search(
            [("can_be_expensed", "=", True)], limit=1
        )
        if not expense_product:
            expense_product = ctx.env["product.product"].create(
                {
                    "name": "QBO Expense",
                    "type": "service",
                    "can_be_expensed": True,
                }
            )

        expense_vals = []
        skipped = 0

        for purchase in purchases:
            try:
                # Get payment type (Check, CreditCard, Cash)
                payment_type = purchase.get("PaymentType", "")

                # Parse date
                txn_date = purchase.get("TxnDate")
                expense_date = None
                if txn_date:
                    try:
                        expense_date = datetime.strptime(txn_date, "%Y-%m-%d").date()
                    except ValueError:
                        expense_date = datetime.now().date()

                # Get vendor/payee
                entity_ref = purchase.get("EntityRef", {})
                vendor_id = None
                if entity_ref:
                    qbo_vendor_id = int(entity_ref.get("value", 0))
                    vendor_id = vendor_map.get(qbo_vendor_id)

                # Process each line as a separate expense
                for line in purchase.get("Line", []):
                    line_vals = self._transform_expense_line(
                        line,
                        purchase,
                        account_map,
                        expense_product,
                        expense_date,
                        payment_type,
                        vendor_id,
                        company,
                        ctx,
                    )
                    if line_vals:
                        expense_vals.append(line_vals)

            except Exception as e:
                _logger.error(f"Error transforming purchase {purchase.get('Id')}: {e}")
                skipped += 1

        _logger.info(
            f"Transformed {len(expense_vals)} expense lines, skipped {skipped} purchases"
        )
        return expense_vals

    def _transform_expense_line(
        self,
        line: Dict,
        purchase: Dict,
        account_map: Dict,
        expense_product,
        expense_date,
        payment_type: str,
        vendor_id: Optional[int],
        company,
        ctx: ETLContext,
    ) -> Optional[Dict]:
        """Transform a single purchase line into expense values."""
        detail_type = line.get("DetailType", "")

        # Handle account-based expense lines
        if detail_type == "AccountBasedExpenseLineDetail":
            detail = line.get("AccountBasedExpenseLineDetail", {})
            if not detail:
                return None

            # Get account
            account_ref = detail.get("AccountRef", {})
            qbo_account_id = int(account_ref.get("value", 0)) if account_ref else 0
            account_id = account_map.get(qbo_account_id)

            amount = float(line.get("Amount", 0) or 0)
            if amount <= 0:
                return None

            description = (
                line.get("Description", "")
                or account_ref.get("name", "")
                or "QBO Expense"
            )

            vals = {
                "name": description,
                "product_id": expense_product.id,
                "total_amount": amount,
                "quantity": 1,
                "date": expense_date,
                "qbo_purchase_id": int(purchase.get("Id", 0)),
                "company_id": company.id,
                "payment_mode": "company_account",  # Paid by company
            }

            if account_id:
                vals["account_id"] = account_id

            return vals

        # Handle item-based expense lines
        elif detail_type == "ItemBasedExpenseLineDetail":
            detail = line.get("ItemBasedExpenseLineDetail", {})
            if not detail:
                return None

            amount = float(line.get("Amount", 0) or 0)
            if amount <= 0:
                return None

            item_ref = detail.get("ItemRef", {})
            description = (
                line.get("Description", "") or item_ref.get("name", "") or "QBO Expense"
            )

            qty = float(detail.get("Qty", 1) or 1)
            unit_price = float(detail.get("UnitPrice", 0) or 0)
            if not unit_price and amount and qty:
                unit_price = amount / qty

            vals = {
                "name": description,
                "product_id": expense_product.id,
                "total_amount": amount,
                "quantity": qty,
                "date": expense_date,
                "qbo_purchase_id": int(purchase.get("Id", 0)),
                "company_id": company.id,
                "payment_mode": "company_account",
            }

            return vals

        return None

    @ETL.load()
    def load_expenses(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load expenses into Odoo."""
        expense_vals = transformed.get("transform_expenses", [])

        if not expense_vals:
            _logger.info("No new expenses to create")
            return

        created = 0
        errors = 0

        for vals in expense_vals:
            try:
                expense = ctx.env["hr.expense"].create(vals)
                created += 1
                _logger.debug(f"Created expense {expense.name}")

            except Exception as e:
                _logger.error(f"Failed to create expense {vals.get('name')}: {e}")
                errors += 1

        _logger.info(f"Created {created} expenses, {errors} errors")
