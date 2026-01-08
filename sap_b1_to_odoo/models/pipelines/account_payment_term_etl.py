import logging
from typing import Dict, List

from odoo import Command, api, models

from odoo.addons.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="account.payment.term",
    importer_name="account.payment.term.importer",
    sap_source="octg",
    depends_on=[],
    allow_multiprocessing=False,  # Small dataset, always single-process
)
class AccountPaymentTermImporter(models.AbstractModel):
    _name = "account.payment.term.importer"
    _description = "SAP Payment Terms Importer"

    @ETL.extract("octg")
    def extract_payment_terms(self, ctx: ETLContext) -> List[Dict]:
        """Extract payment terms from SAP OCTG table.

        Args:
            ctx: ETL context with SAP cursor and Odoo environment.

        Returns:
            List of payment term dictionaries from SAP.
        """
        ctx.cr.execute("SELECT * FROM octg")
        sap_terms = ctx.cr.dictfetchall()
        _logger.info(f"Extracted {len(sap_terms)} payment terms from SAP.")
        return sap_terms

    @ETL.transform()
    def transform_payment_terms(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform SAP payment terms into Odoo payment term values.

        Args:
            ctx: ETL context.
            extracted: Dictionary containing extracted data.

        Returns:
            List of payment term value dictionaries ready for creation.
        """
        sap_terms = extracted["extract_payment_terms"]

        # Get existing payment terms to avoid duplicates
        existing_terms = ctx.env["account.payment.term"].search(
            [("sap_groupnum", "!=", False)]
        )
        existing_groupnums = set(existing_terms.mapped("sap_groupnum"))

        term_vals = []
        for term in sap_terms:
            # Skip if already exists
            if term["groupnum"] in existing_groupnums:
                continue

            term_vals.append(
                {
                    "name": term["pymntgroup"],
                    "sap_groupnum": term["groupnum"],
                    "line_ids": [
                        Command.create(
                            {
                                "value_amount": 100.0,
                                "value": "percent",
                                "nb_days": term["extradays"],
                                "delay_type": "days_after",
                            }
                        )
                    ],
                }
            )

        _logger.info(
            f"Skipped {len(sap_terms) - len(term_vals)} existing payment terms."
        )
        return term_vals

    @ETL.load()
    def load_payment_terms(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load payment terms into Odoo.

        Args:
            ctx: ETL context.
            transformed: Dictionary containing transformed data.
        """
        term_vals = transformed["transform_payment_terms"]

        # Create payment terms
        terms = ctx.env["account.payment.term"].create(term_vals)
        _logger.info(f"Created {len(terms)} payment terms.")
