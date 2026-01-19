"""QuickBooks Online Expense ETL Pipeline

This module handles the migration of Expenses from QBO to Odoo hr.expense
using the ETL framework.

In QBO, the Expense entity represents employee expense reports,
which map directly to hr.expense in Odoo.
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
    sap_source="Expense",
    depends_on=["qbo.employee.importer", "qbo.account.importer"],
)
class QboExpenseImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO Expenses as hr.expense."""

    _name = "qbo.expense.importer"
    _description = "QBO Expense Importer"

    @ETL.extract("Expense")
    def extract_expenses(self, ctx: ETLContext) -> List[Dict]:
        """Extract expenses from QBO API."""
        api_client = get_api_client(ctx)

        # Get existing QBO expense IDs
        ctx.env.cr.execute(
            "SELECT qbo_expense_id FROM hr_expense WHERE qbo_expense_id IS NOT NULL"
        )
        existing_ids = {str(row[0]) for row in ctx.env.cr.fetchall()}
        _logger.info(f"Found {len(existing_ids)} existing expenses in Odoo")

        # Fetch all expenses from QBO
        expenses = api_client.query_all(entity="Expense", order_by="Id")

        # Filter out already imported
        new_expenses = [e for e in expenses if str(e.get("Id")) not in existing_ids]

        _logger.info(
            f"Extracted {len(expenses)} expenses from QBO, "
            f"{len(new_expenses)} are new"
        )
        return new_expenses

    @ETL.transform()
    def transform_expenses(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO expenses into Odoo hr.expense values."""
        expenses = extracted.get("extract_expenses", [])

        # Build lookups
        ctx.env.cr.execute(
            "SELECT qbo_employee_id, id FROM hr_employee WHERE qbo_employee_id IS NOT NULL"
        )
        employee_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        ctx.env.cr.execute(
            "SELECT qbo_id, id FROM account_account WHERE qbo_id IS NOT NULL"
        )
        account_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

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

        for expense in expenses:
            try:
                qbo_expense_id = int(expense.get("Id", 0))

                # Parse date
                txn_date = expense.get("TxnDate")
                expense_date = None
                if txn_date:
                    try:
                        expense_date = datetime.strptime(txn_date, "%Y-%m-%d").date()
                    except ValueError:
                        expense_date = datetime.now().date()

                # Get payment type
                payment_type = expense.get("PaymentType", "")

                # Get employee if linked
                employee_ref = expense.get("EmployeeRef", {})
                employee_id = None
                if employee_ref:
                    qbo_employee_id = int(employee_ref.get("value", 0))
                    employee_id = employee_map.get(qbo_employee_id)

                # Get account from AccountRef (main expense account)
                account_ref = expense.get("AccountRef", {})
                main_account_id = None
                if account_ref:
                    qbo_account_id = int(account_ref.get("value", 0))
                    main_account_id = account_map.get(qbo_account_id)

                # Process each line as a separate expense
                for line in expense.get("Line", []):
                    line_vals = self._transform_expense_line(
                        line,
                        expense,
                        account_map,
                        expense_product,
                        expense_date,
                        payment_type,
                        employee_id,
                        main_account_id,
                        company,
                        qbo_expense_id,
                    )
                    if line_vals:
                        expense_vals.append(line_vals)

            except Exception as e:
                _logger.error(f"Error transforming expense {expense.get('Id')}: {e}")
                skipped += 1

        _logger.info(
            f"Transformed {len(expense_vals)} expense lines, skipped {skipped} expenses"
        )
        return expense_vals

    def _transform_expense_line(
        self,
        line: Dict,
        expense: Dict,
        account_map: Dict,
        expense_product,
        expense_date,
        payment_type: str,
        employee_id: Optional[int],
        main_account_id: Optional[int],
        company,
        qbo_expense_id: int,
    ) -> Optional[Dict]:
        """Transform a single expense line into hr.expense values."""
        detail_type = line.get("DetailType", "")

        # Handle account-based expense lines
        if detail_type == "AccountBasedExpenseLineDetail":
            detail = line.get("AccountBasedExpenseLineDetail", {})
            if not detail:
                return None

            # Get account from line detail, fall back to main expense account
            account_ref = detail.get("AccountRef", {})
            qbo_account_id = int(account_ref.get("value", 0)) if account_ref else 0
            account_id = account_map.get(qbo_account_id) or main_account_id

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
                "qbo_expense_id": qbo_expense_id,
                "company_id": company.id,
                "payment_mode": "company_account",  # Paid by company
            }

            if account_id:
                vals["account_id"] = account_id
            if employee_id:
                vals["employee_id"] = employee_id

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

            vals = {
                "name": description,
                "product_id": expense_product.id,
                "total_amount": amount,
                "quantity": qty,
                "date": expense_date,
                "qbo_expense_id": qbo_expense_id,
                "company_id": company.id,
                "payment_mode": "company_account",
            }

            if employee_id:
                vals["employee_id"] = employee_id
            if main_account_id:
                vals["account_id"] = main_account_id

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
