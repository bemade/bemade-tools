import logging
import re
from typing import Dict, Optional, Set

from fuzzywuzzy import process
from odoo import Command, api, models
from odoo.tools.sql import SQL

from odoo.addons.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)

# Threshold for fuzzy matching
FUZZY_THRESHOLD = 80


@ETL.pipeline(
    target_model="delivery.carrier",
    importer_name="delivery.carrier.importer",
    sap_source="ocrd,oshp",
    depends_on=[],
    allow_multiprocessing=False,  # Small dataset, always single-process
)
class DeliveryCarrierAccountImporter(models.AbstractModel):
    _name = "delivery.carrier.importer"
    _description = "Delivery Carrier Account Importer"

    # Class-level storage for unique carrier names during import
    _unique_carrier_names: Set[str] = set()

    ##################################################################
    # Extraction Methods
    ##################################################################

    @ETL.extract("ocrd,oshp")
    def extract_carriers_and_accounts(self, ctx: ETLContext) -> Dict[str, Set[int]]:
        """Extract delivery carriers from SAP OCRD and OSHP tables.

        Args:
            ctx: ETL context with SAP cursor and Odoo environment.

        Returns:
            Dictionary mapping carrier names to sets of SAP transport codes.
        """
        # Skip if carriers already exist
        if ctx.env["delivery.carrier"].search_count([]) != 1:
            _logger.info("More than 1 carrier already found, skipping carrier import.")
            return {}

        sql = """
        SELECT
            T0.CardCode,
            T0.ShipType,
            T1.TrnspName
        FROM
            OCRD T0
        LEFT JOIN
            OSHP T1
        ON
            T0.shiptype = T1.trnspcode
        WHERE
            T0.shiptype is not null
        """
        ctx.cr.execute(SQL(sql))
        data = ctx.cr.dictfetchall()
        delivery_carriers: Dict[str, Set[int]] = {}

        for row in data:
            trnspname = row.get("trnspname", "")
            shiptype = row["shiptype"]

            # Extract unique delivery carriers and link to SAP trnspcode (shiptype)
            carrier_name_raw = (
                re.split(r"[#(]", trnspname)[0].strip() if trnspname else None
            )
            carrier_name = self._get_or_add_carrier_name(carrier_name_raw)
            delivery_carriers.setdefault(carrier_name, set()).add(shiptype)

        return delivery_carriers

    ##################################################################
    # Transformation Methods
    ##################################################################

    @ETL.transform()
    def transform_carriers_and_accounts(
        self, ctx: ETLContext, extracted: Dict
    ) -> Dict[str, Set[int]]:
        """Pass through extracted data (no transformation needed).

        Args:
            ctx: ETL context.
            extracted: Dictionary containing extracted data.

        Returns:
            Dictionary mapping carrier names to sets of SAP transport codes
            (unchanged from extraction).
        """
        carriers = extracted["extract_carriers_and_accounts"]
        _logger.info(f"Found {len(carriers)} carriers.")
        return carriers

    @api.model
    def _get_or_add_carrier_name(self, carrier_name: Optional[str]) -> str:
        """Match or add a carrier name using fuzzy matching.

        Args:
            carrier_name: Raw carrier name from SAP.

        Returns:
            Normalized carrier name (matched or newly added).
        """
        cls = self.__class__
        if not carrier_name:
            carrier_name = "Unknown"

        # Try to match against existing carrier names using fuzzy matching
        match = process.extractOne(
            carrier_name, cls._unique_carrier_names, score_cutoff=FUZZY_THRESHOLD
        )
        if match:
            return match[0]

        # If no match, add the new carrier name
        cls._unique_carrier_names.add(carrier_name)
        return carrier_name

    ##################################################################
    # Loading Methods
    ##################################################################

    @ETL.load()
    def load_carriers_and_accounts(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load carriers into Odoo.

        Args:
            ctx: ETL context.
            transformed: Dictionary containing transformed data.
        """
        carriers = transformed["transform_carriers_and_accounts"]

        if not carriers:
            _logger.info("No carriers to import.")
            return

        self._load_carriers(ctx, carriers)

    @api.model
    def _load_carriers(self, ctx: ETLContext, carriers: Dict[str, Set[int]]) -> None:
        """Create delivery carrier records in Odoo.

        Args:
            ctx: ETL context.
            carriers: Dictionary mapping carrier names to sets of SAP transport codes.
        """
        # Get or create delivery product
        product = ctx.env["product.product"].search(
            [("name", "=", "Delivery")], limit=1
        )
        if not product:
            product = ctx.env["product.product"].create(
                {
                    "name": "Delivery",
                    "type": "service",
                    "service_tracking": "no",
                    "default_code": "DELIVERY",
                    "sale_ok": True,
                    "purchase_ok": True,
                }
            )

        # Create carrier records
        carrier_vals = []
        for name, trnspcodes in carriers.items():
            vals = {
                "name": name,
                "active": True,
                "sap_transporter_ids": [
                    Command.create({"sap_trnspcode": trnspcode})
                    for trnspcode in trnspcodes
                ],
                "product_id": product.id,
            }
            carrier_vals.append(vals)

        _logger.info(f"Creating {len(carrier_vals)} delivery carriers.")
        ctx.env["delivery.carrier"].create(carrier_vals)
