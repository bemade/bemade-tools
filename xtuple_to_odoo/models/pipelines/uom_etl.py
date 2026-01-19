"""xTuple UoM Precision ETL Pipeline

This module contains the ETL pipeline for detecting and setting
decimal precision based on xTuple quantity data.
"""

import logging
from typing import Dict

from odoo import api, models

from odoo.addons.etl_framework.framework import ETL, ETLContext

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="decimal.precision",
    importer_name="xtuple.uom.precision.importer",
    depends_on=[],
)
class XtupleUomPrecisionImporter(models.AbstractModel):
    """ETL Pipeline for detecting and setting UoM decimal precision."""

    _name = "xtuple.uom.precision.importer"
    _description = "xTuple UoM Precision Importer"

    @ETL.extract("bomitem")
    def extract_precision(self, ctx: ETLContext) -> Dict:
        """Extract max decimal precision from xTuple quantity fields."""
        precision_query = """
            SELECT MAX(
                CASE 
                    WHEN qty::text LIKE '%.%' 
                    THEN LENGTH(SPLIT_PART(qty::text, '.', 2))
                    ELSE 0 
                END
            ) as max_precision
            FROM (
                SELECT bomitem_qtyper as qty FROM bomitem WHERE bomitem_qtyper IS NOT NULL
                UNION ALL
                SELECT wo_qtyord as qty FROM wo WHERE wo_qtyord IS NOT NULL
                UNION ALL
                SELECT poitem_qty_ordered as qty FROM poitem WHERE poitem_qty_ordered IS NOT NULL
            ) quantities
        """
        ctx.cr.execute(precision_query)
        result = ctx.cr.fetchone()
        max_precision = result[0] if result and result[0] else 2

        # Cap at reasonable range (2-6 digits)
        max_precision = min(max(max_precision, 2), 6)

        _logger.info(
            f"Detected max decimal precision from xTuple data: {max_precision}"
        )
        return {"max_precision": max_precision}

    @ETL.transform()
    def transform_precision(self, ctx: ETLContext, extracted: Dict) -> Dict:
        """Pass through the precision value."""
        max_precision = extracted.get("extract_precision", {}).get("max_precision", 4)
        return {"precision": max_precision}

    @ETL.load()
    def load_precision(self, ctx: ETLContext, transformed: Dict) -> None:
        """Update Odoo decimal precision for Product Unit of Measure."""
        max_precision = transformed.get("transform_precision", {}).get("precision", 4)

        # Update both 'Product Unit of Measure' and 'Product Unit' precisions
        # mrp.production.product_qty uses 'Product Unit'
        # uom.uom.rounding is computed as 10 ** -precision_get('Product Unit')
        for precision_name in ["Product Unit of Measure", "Product Unit"]:
            precision_record = ctx.env["decimal.precision"].search(
                [("name", "=", precision_name)], limit=1
            )
            if precision_record:
                if precision_record.digits != max_precision:
                    old_digits = precision_record.digits
                    precision_record.sudo().digits = max_precision
                    _logger.info(
                        f"Updated '{precision_name}' precision from {old_digits} to {max_precision}"
                    )
                else:
                    _logger.info(
                        f"'{precision_name}' precision already set to {max_precision}"
                    )
            else:
                ctx.env["decimal.precision"].sudo().create(
                    {
                        "name": precision_name,
                        "digits": max_precision,
                    }
                )
                _logger.info(
                    f"Created '{precision_name}' precision with {max_precision} digits"
                )
