"""QuickBooks Online Payment Term ETL Pipeline

This module handles the migration of Terms from QBO to Odoo
using the ETL framework.
"""

import logging
from typing import Dict, List

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext

from .utils import get_api_client

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.payment.term",
    importer_name="qbo.term.importer",
    sap_source="Term",
    depends_on=[],
)
class QboTermImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO Payment Terms."""

    _name = "qbo.term.importer"
    _description = "QBO Term Importer"

    @ETL.extract("Term")
    def extract_terms(self, ctx: ETLContext) -> List[Dict]:
        """Extract terms from QBO API."""
        api_client = get_api_client(ctx)

        # Get existing QBO term IDs
        ctx.env.cr.execute(
            "SELECT qbo_term_id FROM account_payment_term WHERE qbo_term_id IS NOT NULL"
        )
        existing_ids = {str(row[0]) for row in ctx.env.cr.fetchall()}
        _logger.info(f"Found {len(existing_ids)} existing payment terms in Odoo")

        # Fetch all terms from QBO
        terms = api_client.query_all(entity="Term", order_by="Id")

        # Filter out already imported
        new_terms = [term for term in terms if str(term.get("Id")) not in existing_ids]

        _logger.info(f"Extracted {len(terms)} terms from QBO, {len(new_terms)} are new")
        return new_terms

    @ETL.transform()
    def transform_terms(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO terms into Odoo payment term values."""
        terms = extracted.get("extract_terms", [])

        term_vals = []

        for term in terms:
            name = term.get("Name", "")
            due_days = term.get("DueDays", 0) or 0

            # QBO Term fields:
            # - Name: term name
            # - DueDays: days until due
            # - DiscountPercent: early payment discount %
            # - DiscountDays: days to get discount
            # - Active: boolean

            vals = {
                "name": name,
                "qbo_term_id": int(term.get("Id")),
                "active": term.get("Active", True),
                # Create a single line for the payment term
                "line_ids": [
                    (
                        0,
                        0,
                        {
                            "value": "percent",
                            "value_amount": 100.0,
                            "nb_days": due_days,
                        },
                    )
                ],
            }

            # Add early payment discount if present
            discount_percent = term.get("DiscountPercent")
            discount_days = term.get("DiscountDays")
            if discount_percent and discount_days:
                vals["early_discount"] = True
                vals["discount_percentage"] = float(discount_percent)
                vals["discount_days"] = int(discount_days)

            term_vals.append(vals)

        _logger.info(f"Transformed {len(term_vals)} payment term records")
        return term_vals

    @ETL.load()
    def load_terms(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load terms into Odoo."""
        term_vals = transformed.get("transform_terms", [])

        if not term_vals:
            _logger.info("No new payment terms to create")
            return

        # Batch create payment terms
        terms = ctx.env["account.payment.term"].create(term_vals)
        _logger.info(f"Created {len(terms)} payment terms")
