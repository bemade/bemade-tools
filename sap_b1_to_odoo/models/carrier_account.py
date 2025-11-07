import logging
import re
from typing import Dict, List, Set, Tuple, Optional, Any

from fuzzywuzzy import process
from odoo import api, Command, fields, models
from odoo.tools.sql import SQL

_logger = logging.getLogger(__name__)

# Threshold for fuzzy matching
FUZZY_THRESHOLD = 80


class DeliveryCarrier(models.Model):
    _inherit = "delivery.carrier"

    sap_transporter_ids = fields.One2many(
        comodel_name="sap.transporter",
        inverse_name="delivery_carrier_id",
    )


class SapTransporter(models.Model):
    _name = "sap.transporter"
    _description = "SAP Transporter"

    sap_trnspcode = fields.Integer()
    delivery_carrier_id = fields.Many2one("delivery.carrier")


class DeliveryCarrierAccountImporter(models.AbstractModel):
    _name = "delivery.carrier.account.importer"
    _description = "Delivery Carrier Account Importer"

    # Class-level storage for unique carrier names during import
    _unique_carrier_names: Set[str] = set()

    ##################################################################
    # Public Interface and Main Entry Point Methods
    ##################################################################

    @api.model
    def import_all(self, cr) -> None:
        """Import all delivery carriers and carrier accounts from SAP.
        
        Args:
            cr: Database cursor for the SAP database.
        """
        if self.env["delivery.carrier"].search_count([]) != 1:
            _logger.info("More than 1 carrier already found, skipping carrier import.")
            return
        
        carriers, accounts = self._extract_all(cr)
        self._load_carriers(carriers)
        self._load_carrier_accounts(accounts)

    ##################################################################
    # Extraction Methods
    ##################################################################

    @api.model
    def _extract_all(self, cr: Any) -> Tuple[Dict[str, Set[int]], List[Dict[str, Any]]]:
        """Extract delivery carriers and carrier accounts from SAP OCRD and OSHP tables.
        
        Args:
            cr: Database cursor for the SAP database.
            
        Returns:
            Tuple containing:
                - Dictionary mapping carrier names to sets of SAP transport codes
                - List of carrier account dictionaries with cardcode, carrier_name, and account_number
        """
        cls = self.__class__
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
        cr.execute(SQL(sql))
        data = cr.dictfetchall()
        delivery_carriers: Dict[str, Set[int]] = {}
        carrier_accounts: List[Dict[str, Any]] = []

        for row in data:
            cardcode = row["cardcode"]
            trnspname = row.get("trnspname", "")
            shiptype = row["shiptype"]

            # Extract unique delivery carriers and link to SAP trnspcode (shiptype)
            carrier_name_raw = (
                re.split(r"[#(]", trnspname)[0].strip() if trnspname else None
            )
            carrier_name = self._get_or_add_carrier_name(carrier_name_raw)
            delivery_carriers.setdefault(carrier_name, set()).add(shiptype)

            # Extract account numbers from trnspname
            account = self._extract_account(trnspname)
            if account and account not in cls._unique_carrier_names:
                carrier_accounts.append(
                    {
                        "cardcode": cardcode,
                        "carrier_name": carrier_name,
                        "account_number": account,
                    }
                )
        
        return delivery_carriers, carrier_accounts

    ##################################################################
    # Transformation Methods
    ##################################################################

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

    @api.model
    def _extract_account(self, account_str: Optional[str]) -> Optional[str]:
        """Extract account number from SAP transporter name string.
        
        Args:
            account_str: Raw account string from SAP (e.g., "Carrier#12345" or "Carrier 12345").
            
        Returns:
            Extracted account number or None if not found.
        """
        if not account_str:
            return None
        
        cls = self.__class__
        
        # Handle "Carrier#Account" format
        if "#" in account_str:
            return account_str.split("#")[1]
        
        # Handle "Carrier Account" format
        split = account_str.split()
        if split and split[0] in cls._unique_carrier_names:
            return " ".join(split[1:])
        
        return account_str

    ##################################################################
    # Loading Methods
    ##################################################################

    @api.model
    def _load_carriers(self, carriers: Dict[str, Set[int]]) -> None:
        """Create delivery carrier records in Odoo.
        
        Args:
            carriers: Dictionary mapping carrier names to sets of SAP transport codes.
        """
        # Get or create delivery product
        product = self.env["product.product"].search([("name", "=", "Delivery")], limit=1)
        if not product:
            product = self.env["product.product"].create(
                {
                    "name": "Delivery",
                    "type": "service",
                    "service_tracking": "no",
                    "default_code": "DELIVERY",
                    "sale_ok": True,
                    "purchase_ok": True,
                    "company_id": self.env.company.id,
                }
            )
        
        # Create carrier records
        carrier_vals = []
        for name, trnspcodes in carriers.items():
            vals = {
                "name": name,
                "active": True,
                "company_id": self.env.company.id,
                "sap_transporter_ids": [
                    Command.create({"sap_trnspcode": trnspcode})
                    for trnspcode in trnspcodes
                ],
                "product_id": product.id,
            }
            carrier_vals.append(vals)
        
        _logger.info(f"Creating {len(carrier_vals)} delivery carriers.")
        self.env["delivery.carrier"].create(carrier_vals)

    @api.model
    def _load_carrier_accounts(self, accounts: List[Dict[str, Any]]) -> None:
        """Create delivery carrier account records in Odoo.
        
        Args:
            accounts: List of account dictionaries with cardcode, carrier_name, and account_number.
        """
        if not accounts:
            return
        
        # Build lookup dictionaries
        carriers = self.env["delivery.carrier"].search([])
        carriers_dict = {carrier.name: carrier for carrier in carriers}
        
        partners = self.env["res.partner"].search(
            [("sap_card_code", "in", [account["cardcode"] for account in accounts])]
        )
        partners_dict = {partner.sap_card_code: partner for partner in partners}
        company_partner = self.env.company.partner_id
        
        # Create account records
        account_vals = []
        for account in accounts:
            partner = partners_dict.get(account["cardcode"])
            
            # Use company partner for suppliers, otherwise use the partner itself
            partner_to_use = (
                partner
                if partner and partner.sap_partner_type != "S"
                else company_partner
            )
            
            vals = {
                "account_number": account["account_number"],
                "partner_id": partner_to_use.id if partner_to_use else company_partner.id,
                "delivery_carrier_id": carriers_dict[account["carrier_name"]].id,
            }
            
            # Link supplier if applicable
            if partner and partner.sap_partner_type == "S":
                vals["supplier_ids"] = [Command.link(partner.id)]
            
            account_vals.append(vals)
        
        _logger.info(f"Creating {len(account_vals)} carrier accounts.")
        self.env["delivery.carrier.account"].create(account_vals)
