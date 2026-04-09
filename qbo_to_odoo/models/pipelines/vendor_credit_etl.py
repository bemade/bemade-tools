"""QuickBooks Online VendorCredit ETL Pipeline

This module handles the migration of VendorCredits from QBO to Odoo
using the ETL framework. VendorCredits become in_refund account.move records.
"""

import logging
from typing import Dict, List

from odoo import models

from odoo.addons.etl_framework import ETL, ETLContext, ChunkableData

from .extractor import QBOExtractor
from .move_builder import QBOMoveBuilder
from .move_posting_helpers import load_and_post_invoice_moves
from .utils import get_api_client

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.move",
    importer_name="qbo.vendor.credit.importer",
    sap_source="VendorCredit",
    depends_on=["qbo.vendor.importer", "qbo.item.importer", "qbo.tax.importer", "qbo.category.account.fixer"],
)
class QboVendorCreditImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO VendorCredits."""

    _name = "qbo.vendor.credit.importer"
    _description = "QBO Vendor Credit Importer"

    @ETL.extract("VendorCredit")
    def extract_vendor_credits(self, ctx: ETLContext) -> ChunkableData:
        """Extract vendor credits from QBO API and preload lookup maps."""
        api_client = get_api_client(ctx)
        extractor = QBOExtractor(ctx)

        # Get existing QBO vendor credit IDs
        existing_ids = extractor.existing_qbo_ids(
            "account_move", "qbo_vendor_credit_id"
        )
        _logger.info(f"Found {len(existing_ids)} existing vendor credits in Odoo")

        # Fetch all vendor credits from QBO
        vendor_credits = api_client.query_all(entity="VendorCredit", order_by="Id")

        # Filter out already imported
        new_vendor_credits = [
            vc for vc in vendor_credits if str(vc.get("Id")) not in existing_ids
        ]

        _logger.info(
            f"Extracted {len(vendor_credits)} vendor credits from QBO, "
            f"{len(new_vendor_credits)} are new"
        )

        # Preload maps for transform
        extractor.preload(
            "account", "vendor", "product", "product_expense",
            "purchase_tax", "purchase_tax_rate", "currency",
        )
        extractor.preload_journals("purchase")

        return ChunkableData(
            records=new_vendor_credits,
            context={"extractor": extractor.export()},
        )

    @ETL.transform()
    def transform_vendor_credits(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO vendor credits into Odoo account.move values."""
        data = extracted.get("extract_vendor_credits")
        if not data:
            return []
        vendor_credits = data.records if hasattr(data, "records") else data
        context = data.context if hasattr(data, "context") else {}

        builder = QBOMoveBuilder(context["extractor"])

        move_vals = []
        skipped = 0

        for vc in vendor_credits:
            vals = builder.build_invoice_move_vals(
                vc,
                move_type="in_refund",
                journal_type="purchase",
                partner_type="vendor",
                qbo_id_field="qbo_vendor_credit_id",
                line_detail_types=(
                    "ItemBasedExpenseLineDetail",
                    "AccountBasedExpenseLineDetail",
                ),
                tax_use="purchase",
                direction="expense",
                memo_field="Memo",
                memo_key=None,
            )
            if vals:
                move_vals.append(vals)
            else:
                skipped += 1

        _logger.info(f"Transformed {len(move_vals)} vendor credits, skipped {skipped}")
        return move_vals

    @ETL.load()
    def load_vendor_credits(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load vendor credits into Odoo with GL-accuracy fixes."""
        move_vals = transformed.get("transform_vendor_credits", [])
        load_and_post_invoice_moves(ctx, move_vals)
