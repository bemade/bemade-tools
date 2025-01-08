from odoo import fields, models, api, Command
from odoo.tools.sql import SQL
import re
from fuzzywuzzy import process
import logging

_logger = logging.getLogger(__name__)

# Threshold for fuzzy matching
fuzzy_threshold = 80


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

    # Dictionary to store carriers and extracted accounts
    _unique_carrier_names = set()

    @api.model
    def _extract_account(self, account_str):
        if not account_str:
            return None
        cls = self.__class__
        if "#" in account_str:
            return account_str.split("#")[1]
        else:
            split = account_str.split()
            first_part = split[0]
            if first_part in cls._unique_carrier_names:
                return " ".join(split[1:])
            else:
                return account_str

    # Function to match or add a transporter name dynamically
    @api.model
    def _get_or_add_carrier_name(self, carrier_name):
        cls = self.__class__
        if not carrier_name:
            carrier_name = "Unknown"
        # Try to match against existing carrier names using fuzzy matching
        match = process.extractOne(
            carrier_name, cls._unique_carrier_names, score_cutoff=fuzzy_threshold
        )
        if match:
            return match[0]  # Return the matched name
        # If no match, add the new carrier name
        cls._unique_carrier_names.add(carrier_name)
        return carrier_name

    @api.model
    def extract_all(self, cr):
        cls = self.__class__
        sql = """
        SELECT
            T0.CardCode,
            T0.ShipType,
            T0.u_fcsdk_shipviaacct,
            T1.TrnspName
        FROM
            OCRD T0
        LEFT JOIN
            OSHP T1
        ON
            T0.shiptype = T1.trnspcode
        WHERE
            T0.shiptype is not null
            OR T0.u_fcsdk_shipviaacct is not null
        """
        cr.execute(SQL(sql))
        data = cr.dictfetchall()
        delivery_carriers = {}  # unique name to set of trnspcode
        carrier_accounts = []

        for row in data:
            cardcode = row["cardcode"]
            trnspname = row.get("trnspname", "")
            u_fcsdk_shipviaacct = row.get("u_fcsdk_shipviaacct", "")
            shiptype = row["shiptype"]

            # Extract unique delivery carriers and link the unique name to its matching
            # SAP trnspcode (shiptype)

            carrier_name_raw = (
                re.split(r"[#(]", trnspname)[0].strip() if trnspname else None
            )
            carrier_name = self._get_or_add_carrier_name(carrier_name_raw)

            delivery_carriers.setdefault(carrier_name, set()).add(shiptype)

            # Extract account numbers
            accounts = set()
            for account_string in [u_fcsdk_shipviaacct, trnspname]:
                account = self._extract_account(account_string)
                if account:
                    accounts.add(account)
            # Take carrier names out of the accounts
            accounts = set(
                [
                    account
                    for account in accounts
                    if account not in cls._unique_carrier_names
                ]
            )

            # Add accounts to carrier_accounts
            for account in accounts:
                carrier_accounts.append(
                    {
                        "cardcode": cardcode,
                        "carrier_name": carrier_name,
                        "account_number": account,
                    }
                )
        return delivery_carriers, carrier_accounts

    @api.model
    def import_all(self, cr):
        carriers, accounts = self.extract_all(cr)

        carrier_vals = []
        product = self.env["product.product"].create(
            {
                "name": "Delivery",
                "type": "service",
                "service_policy": "delivered_manual",
                "service_tracking": "no",
                "default_code": "LIVRAISON",
                "sale_ok": True,
                "purchase_ok": True,
                "company_id": self.env.company.id,
            }
        )
        for name, trnspcodes in carriers.items():
            carrier_vals.append(
                {
                    "name": name,
                    "active": True,
                    "company_id": self.env.company.id,
                    "sap_transporter_ids": [
                        Command.create({"sap_trnspcode": trnspcode})
                        for trnspcode in trnspcodes
                    ],
                    "product_id": product.id,
                }
            )
        _logger.info(f"Creating delivery carriers: {carrier_vals}")
        carriers = self.env["delivery.carrier"].create(carrier_vals)
        carriers_dict = {carrier.name: carrier for carrier in carriers}
        account_vals = []
        partners = self.env["res.partner"].search(
            [("sap_card_code", "in", [account["cardcode"] for account in accounts])]
        )
        partners_dict = {partner.sap_card_code: partner for partner in partners}
        for account in accounts:
            account_vals.append(
                {
                    "account_number": account["account_number"],
                    "partner_id": partners_dict[account["cardcode"]].id,
                    "delivery_carrier_id": carriers_dict[account["carrier_name"]].id,
                }
            )

        self.env["delivery.carrier.account"].create(account_vals)
