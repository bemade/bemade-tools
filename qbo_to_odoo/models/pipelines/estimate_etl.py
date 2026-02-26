"""QuickBooks Online Estimate ETL Pipeline

This module handles the migration of Estimates from QBO to Odoo sale.order
using the ETL framework.
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext

from .utils import get_api_client

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="sale.order",
    importer_name="qbo.estimate.importer",
    sap_source="Estimate",
    depends_on=["qbo.customer.importer", "qbo.item.importer", "qbo.tax.importer", "qbo.category.account.fixer"],
)
class QboEstimateImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO Estimates as sale.order."""

    _name = "qbo.estimate.importer"
    _description = "QBO Estimate Importer"

    @ETL.extract("Estimate")
    def extract_estimates(self, ctx: ETLContext) -> List[Dict]:
        """Extract estimates from QBO API."""
        api_client = get_api_client(ctx)

        # Get existing QBO estimate IDs
        ctx.env.cr.execute(
            "SELECT qbo_estimate_id FROM sale_order WHERE qbo_estimate_id IS NOT NULL"
        )
        existing_ids = {str(row[0]) for row in ctx.env.cr.fetchall()}
        _logger.info(f"Found {len(existing_ids)} existing estimates in Odoo")

        # Fetch all estimates from QBO
        estimates = api_client.query_all(entity="Estimate", order_by="Id")

        # Filter out already imported
        new_estimates = [
            est for est in estimates if str(est.get("Id")) not in existing_ids
        ]

        _logger.info(
            f"Extracted {len(estimates)} estimates from QBO, "
            f"{len(new_estimates)} are new"
        )
        return new_estimates

    @ETL.transform()
    def transform_estimates(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO estimates into Odoo sale.order values."""
        estimates = extracted.get("extract_estimates", [])

        # Build lookups
        ctx.env.cr.execute(
            "SELECT qbo_customer_id, id FROM res_partner WHERE qbo_customer_id IS NOT NULL"
        )
        customer_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        ctx.env.cr.execute(
            "SELECT qbo_item_id, id FROM product_product WHERE qbo_item_id IS NOT NULL"
        )
        product_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        ctx.env.cr.execute(
            "SELECT qbo_tax_id, id FROM account_tax WHERE qbo_tax_id IS NOT NULL AND type_tax_use = 'sale'"
        )
        tax_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        ctx.env.cr.execute(
            "SELECT qbo_tax_rate_id, id FROM account_tax WHERE qbo_tax_rate_id IS NOT NULL AND type_tax_use = 'sale'"
        )
        tax_rate_map = {row[0]: row[1] for row in ctx.env.cr.fetchall()}

        company = ctx.env.company

        order_vals = []
        skipped = 0

        for est in estimates:
            # Get customer
            customer_ref = est.get("CustomerRef", {})
            qbo_customer_id = int(customer_ref.get("value", 0))
            partner_id = customer_map.get(qbo_customer_id)

            if not partner_id:
                _logger.warning(
                    f"Customer not found for QBO ID {qbo_customer_id} "
                    f"in estimate {est.get('Id')}"
                )
                skipped += 1
                continue

            # Parse date
            txn_date = est.get("TxnDate")
            order_date = None
            if txn_date:
                try:
                    order_date = datetime.strptime(txn_date, "%Y-%m-%d").date()
                except ValueError:
                    order_date = datetime.now().date()

            # Parse expiration date
            expiry_date = est.get("ExpirationDate")
            validity_date = None
            if expiry_date:
                try:
                    validity_date = datetime.strptime(expiry_date, "%Y-%m-%d").date()
                except ValueError:
                    pass

            # Get currency
            currency_id = company.currency_id.id
            currency_ref = est.get("CurrencyRef", {})
            if currency_ref:
                currency_code = currency_ref.get("value")
                if currency_code:
                    currency = ctx.env["res.currency"].search(
                        [("name", "=", currency_code)], limit=1
                    )
                    if currency:
                        currency_id = currency.id

            # Build order lines
            line_vals = []
            for line in est.get("Line", []):
                line_data = self._transform_estimate_line(
                    line, product_map, tax_map, tax_rate_map, est, ctx
                )
                if line_data:
                    line_vals.append((0, 0, line_data))

            if not line_vals:
                _logger.warning(f"No valid lines for estimate {est.get('Id')}")
                skipped += 1
                continue

            vals = {
                "partner_id": partner_id,
                "date_order": order_date,
                "validity_date": validity_date,
                "currency_id": currency_id,
                "client_order_ref": est.get("DocNumber", ""),
                "note": est.get("CustomerMemo", {}).get("value", ""),
                "order_line": line_vals,
                "qbo_estimate_id": int(est.get("Id", 0)),
                "company_id": company.id,
            }

            order_vals.append(vals)

        _logger.info(f"Transformed {len(order_vals)} estimates, skipped {skipped}")
        return order_vals

    def _transform_estimate_line(
        self,
        line: Dict,
        product_map: Dict,
        tax_map: Dict,
        tax_rate_map: Dict,
        estimate: Dict,
        ctx: ETLContext,
    ) -> Optional[Dict]:
        """Transform a single estimate line."""
        detail_type = line.get("DetailType", "")

        # Skip non-product lines
        if detail_type not in ("SalesItemLineDetail",):
            return None

        detail = line.get("SalesItemLineDetail", {})
        if not detail:
            return None

        # Get product
        item_ref = detail.get("ItemRef", {})
        item_value = item_ref.get("value", "0") if item_ref else "0"
        try:
            qbo_item_id = int(item_value)
        except ValueError:
            qbo_item_id = 0
        product_id = product_map.get(qbo_item_id)

        # Get quantity and price
        qty = float(detail.get("Qty", 1) or 1)
        unit_price = float(detail.get("UnitPrice", 0) or 0)
        amount = float(line.get("Amount", 0) or 0)

        # If no unit price but has amount and qty, calculate
        if not unit_price and amount and qty:
            unit_price = amount / qty

        # Get tax
        tax_ids = []
        tax_code_ref = detail.get("TaxCodeRef", {})
        if tax_code_ref:
            tax_code_value = tax_code_ref.get("value")
            if tax_code_value and tax_code_value not in ("NON", ""):
                tax_id = tax_map.get(tax_code_value)
                if tax_id:
                    tax_ids.append(tax_id)
                elif tax_code_value == "TAX":
                    txn_tax = estimate.get("TxnTaxDetail", {})
                    for tax_line in txn_tax.get("TaxLine", []):
                        tax_detail = tax_line.get("TaxLineDetail", {})
                        tax_rate_ref = tax_detail.get("TaxRateRef", {}).get("value")
                        if tax_rate_ref:
                            rate_tax_id = tax_rate_map.get(tax_rate_ref)
                            if rate_tax_id and rate_tax_id not in tax_ids:
                                tax_ids.append(rate_tax_id)

        line_vals = {
            "name": line.get("Description", "") or item_ref.get("name", "") or "/",
            "product_uom_qty": qty,
            "price_unit": unit_price,
        }

        if product_id:
            line_vals["product_id"] = product_id

        if tax_ids:
            line_vals["tax_ids"] = [(6, 0, tax_ids)]

        return line_vals

    @ETL.load()
    def load_estimates(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load estimates into Odoo as sale.order."""
        order_vals = transformed.get("transform_estimates", [])

        if not order_vals:
            _logger.info("No new estimates to create")
            return

        # Batch create sale orders
        orders = ctx.env["sale.order"].create(order_vals)
        _logger.info(f"Created {len(orders)} sale orders from estimates")
