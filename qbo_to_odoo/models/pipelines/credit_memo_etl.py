"""QuickBooks Online CreditMemo ETL Pipeline

This module handles the migration of CreditMemos from QBO to Odoo
using the ETL framework. CreditMemos become out_refund account.move records.
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
    importer_name="qbo.credit.memo.importer",
    sap_source="CreditMemo",
    depends_on=["qbo.account.importer", "qbo.customer.importer", "qbo.item.importer", "qbo.tax.importer", "qbo.category.account.fixer"],
)
class QboCreditMemoImporter(models.AbstractModel):
    """ETL Pipeline for importing QBO CreditMemos."""

    _name = "qbo.credit.memo.importer"
    _description = "QBO Credit Memo Importer"

    @ETL.extract("CreditMemo")
    def extract_credit_memos(self, ctx: ETLContext) -> ChunkableData:
        """Extract credit memos from QBO API and preload lookup maps."""
        api_client = get_api_client(ctx)
        extractor = QBOExtractor(ctx)

        # Get existing QBO credit memo IDs
        existing_ids = extractor.existing_qbo_ids(
            "account_move", "qbo_credit_memo_id"
        )
        _logger.info(f"Found {len(existing_ids)} existing credit memos in Odoo")

        # Fetch all credit memos from QBO
        credit_memos = api_client.query_all(entity="CreditMemo", order_by="Id")

        # Filter out already imported
        new_credit_memos = [
            cm for cm in credit_memos if str(cm.get("Id")) not in existing_ids
        ]

        _logger.info(
            f"Extracted {len(credit_memos)} credit memos from QBO, "
            f"{len(new_credit_memos)} are new"
        )

        # Preload maps for transform
        extractor.preload(
            "account", "customer", "product", "product_income",
            "sale_tax", "sale_tax_rate", "currency",
        )
        extractor.preload_journals("sale")

        # Resolve shipping account from QBO Preferences
        api_client = get_api_client(ctx)
        prefs = api_client.query("Preferences", max_results=1)
        if prefs:
            ship_qbo_id = (prefs[0] if isinstance(prefs, list) else prefs
                           ).get("SalesFormsPrefs", {}).get("DefaultShippingAccount")
            if ship_qbo_id:
                extractor.extra["shipping_account_id"] = extractor.account_map.get(
                    int(ship_qbo_id)
                )

        return ChunkableData(
            records=new_credit_memos,
            context={"extractor": extractor.export()},
        )

    @ETL.transform()
    def transform_credit_memos(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform QBO credit memos into Odoo account.move values."""
        data = extracted.get("extract_credit_memos")
        if not data:
            return []
        credit_memos = data.records if hasattr(data, "records") else data
        context = data.context if hasattr(data, "context") else {}

        builder = QBOMoveBuilder(context["extractor"])

        move_vals = []
        skipped = 0

        for cm in credit_memos:
            vals = builder.build_invoice_move_vals(
                cm,
                move_type="out_refund",
                journal_type="sale",
                partner_type="customer",
                qbo_id_field="qbo_credit_memo_id",
                line_detail_types=("SalesItemLineDetail", "DiscountLineDetail"),
                tax_use="sale",
                direction="income",
                memo_field="CustomerMemo",
                memo_key="value",
            )
            if vals:
                move_vals.append(vals)
            else:
                skipped += 1

        _logger.info(f"Transformed {len(move_vals)} credit memos, skipped {skipped}")
        return move_vals

    @ETL.load()
    def load_credit_memos(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load credit memos into Odoo with GL-accuracy fixes."""
        move_vals = transformed.get("transform_credit_memos", [])
        load_and_post_invoice_moves(ctx, move_vals)
