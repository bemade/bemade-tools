"""Sale/Purchase Order ETL Mixin

Shared helper methods and base logic for sales and purchase order ETL pipelines.
"""
import logging
from typing import Dict, Any, List
from fuzzywuzzy import process

from odoo import models
from odoo.tools.sql import SQL
from odoo.addons.sap_b1_to_odoo.tools import fix_tz

_logger = logging.getLogger(__name__)


class SalePurchaseOrderETLMixin(models.AbstractModel):
    """Mixin with shared methods for sale and purchase order ETL pipelines.
    
    Subclasses should define:
    - _sap_header_table: SAP table name (e.g., 'ordr', 'opor')
    - _sap_lines_table: SAP lines table (e.g., 'rdr1', 'por1')
    - _sap_text_lines_table: SAP text lines table (e.g., 'rdr10', 'por10')
    - _odoo_model: Odoo model name (e.g., 'sale.order', 'purchase.order')
    - _odoo_line_model: Odoo line model (e.g., 'sale.order.line', 'purchase.order.line')
    - _quantity_field: Quantity field name (e.g., 'product_uom_qty', 'product_qty')
    - _qty_received_delivered_field: Field for received/delivered qty
    - _status_field: Status field name (e.g., 'delivery_status', 'receipt_status')
    """
    
    _name = "sale.purchase.order.etl.mixin"
    _description = "Sale/Purchase Order ETL Mixin"
    
    # Subclasses must define these
    _sap_header_table = None
    _sap_lines_table = None
    _sap_text_lines_table = None
    _odoo_model = None
    _odoo_line_model = None
    _quantity_field = None
    _qty_received_delivered_field = None
    _status_field = None

    @staticmethod
    def extract_address_string(partner_record) -> str:
        """Extract address string from partner record."""
        parts = ["street", "street2", "city", "state", "zip", "country"]
        address = " ".join(
            [
                str(getattr(partner_record, part, "") or "").strip()
                for part in parts
                if getattr(partner_record, part, False)
            ]
        )
        return address

    @staticmethod
    def get_partner_id(header: Dict, cache: Dict) -> int:
        """Get partner ID from header, preferring contact's parent over company."""
        # If there's a contact set, use its parent (the company)
        if header.get("cntctcode"):
            contact_parent_id = cache["contacts_map"].get(header["cntctcode"])
            if contact_parent_id:
                return contact_parent_id

        # Otherwise use company directly
        cardcode = header["cardcode"]
        return (
            cache["partners_map"].get(cardcode)
            or cache["partners_map"].get(cardcode.upper())
            or cache["partners_map"].get(cardcode.lower())
        )

    @staticmethod
    def find_partner_address_id(
        header: Dict, partner_id: int, address_type: str, cache: Dict
    ) -> int:
        """Find partner address ID by type using fuzzy matching on pre-computed data."""
        # Get SAP address
        sap_address = (
            header["address2"] if address_type == "delivery" else header["address"]
        )
        if sap_address:
            sap_address = sap_address.replace("\r\n", " ")

        # Get pre-computed addresses for this partner
        partner_addresses_data = cache["partner_addresses_map"].get(partner_id, {})
        potential_addresses = partner_addresses_data.get(address_type, [])
        commercial_id = partner_addresses_data.get("commercial_id", partner_id)

        # Use fuzzy matching if multiple addresses and SAP address provided
        if len(potential_addresses) > 1 and sap_address:
            # Build dict of address_string -> partner_id
            address_to_id = {addr_str: pid for pid, addr_str in potential_addresses}
            match_result = process.extractOne(sap_address, address_to_id.keys())
            if match_result:
                matched_address = match_result[0]
                return address_to_id[matched_address]

        # Return first address of this type if available
        if len(potential_addresses) >= 1:
            return potential_addresses[0][
                0
            ]  # Return the ID from (id, address_string) tuple

        # Fallback to commercial partner
        return commercial_id
