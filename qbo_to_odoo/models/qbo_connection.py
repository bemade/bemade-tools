"""QuickBooks Online Connection Model

This module provides the QBO API connection with OAuth2 authentication
and rate limiting support for the ETL framework.
"""

import base64
import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import requests
from requests_oauthlib import OAuth2Session

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from odoo.addons.etl_framework import (
    ETL,
    ETLContext,
    ETLExecutor,
    PipelineOrchestrator,
)

_logger = logging.getLogger(__name__)

# QBO API Constants
QBO_AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
QBO_TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
QBO_API_BASE_URL = "https://quickbooks.api.intuit.com/v3/company"
QBO_SANDBOX_API_BASE_URL = "https://sandbox-quickbooks.api.intuit.com/v3/company"

# Rate limiting: QBO allows 500 requests per minute
QBO_RATE_LIMIT_REQUESTS = 500
QBO_RATE_LIMIT_WINDOW = 60  # seconds


class QBORateLimiter:
    """Rate limiter for QBO API requests.

    QBO has a limit of 500 requests per minute. This class tracks
    request timestamps and enforces the limit by sleeping when needed.
    """

    def __init__(
        self,
        max_requests: int = QBO_RATE_LIMIT_REQUESTS,
        window_seconds: int = QBO_RATE_LIMIT_WINDOW,
    ):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.request_times: List[float] = []

    def wait_if_needed(self) -> None:
        """Wait if we've exceeded the rate limit."""
        now = time.time()

        # Remove timestamps outside the window
        self.request_times = [
            t for t in self.request_times if now - t < self.window_seconds
        ]

        if len(self.request_times) >= self.max_requests:
            # Calculate how long to wait
            oldest = min(self.request_times)
            wait_time = self.window_seconds - (now - oldest) + 0.1
            if wait_time > 0:
                _logger.info(f"Rate limit reached, waiting {wait_time:.1f}s")
                time.sleep(wait_time)

        # Record this request
        self.request_times.append(time.time())


class QBOApiClient:
    """QuickBooks Online API Client with rate limiting.

    This client handles all API communication with QBO, including:
    - OAuth2 token management
    - Rate limiting
    - Pagination for large result sets
    - Error handling and retries
    """

    def __init__(self, connection: "QboConnection"):
        self.connection = connection
        self.rate_limiter = QBORateLimiter()
        self._session: Optional[requests.Session] = None

    @property
    def base_url(self) -> str:
        """Get the appropriate API base URL."""
        if self.connection.sandbox_mode:
            return f"{QBO_SANDBOX_API_BASE_URL}/{self.connection.realm_id}"
        return f"{QBO_API_BASE_URL}/{self.connection.realm_id}"

    @property
    def headers(self) -> Dict[str, str]:
        """Get headers for API requests."""
        return {
            "Authorization": f"Bearer {self.connection.access_token}",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Content-Type": "application/json",
        }

    def _ensure_token_valid(self) -> None:
        """Ensure the access token is valid, refreshing if needed."""
        if self.connection.token_expiry:
            # Refresh if token expires in less than 5 minutes
            if self.connection.token_expiry <= datetime.now() + timedelta(minutes=5):
                self.connection.refresh_access_token()

    def query(
        self,
        entity: str,
        where: str = "",
        order_by: str = "",
        max_results: int = 1000,
        start_position: int = 1,
    ) -> List[Dict]:
        """Execute a QBO query and return results.

        Args:
            entity: QBO entity name (e.g., 'Account', 'Customer')
            where: Optional WHERE clause
            order_by: Optional ORDER BY clause
            max_results: Maximum results per page (max 1000)
            start_position: Starting position for pagination

        Returns:
            List of entity dictionaries
        """
        self._ensure_token_valid()
        self.rate_limiter.wait_if_needed()

        # Build query
        query = f"SELECT * FROM {entity}"
        if where:
            query += f" WHERE {where}"
        if order_by:
            query += f" ORDERBY {order_by}"
        query += f" STARTPOSITION {start_position} MAXRESULTS {max_results}"

        url = f"{self.base_url}/query"
        params = {"query": query}

        _logger.debug(f"QBO Query: {query}")

        response = requests.get(url, headers=self.headers, params=params)

        if response.status_code == 401:
            # Token expired, refresh and retry
            self.connection.refresh_access_token()
            response = requests.get(url, headers=self.headers, params=params)

        if response.status_code != 200:
            raise UserError(
                _(f"QBO API Error: {response.status_code} - {response.text}")
            )

        data = response.json()
        query_response = data.get("QueryResponse", {})

        return query_response.get(entity, [])

    def query_all(
        self, entity: str, where: str = "", order_by: str = "Id"
    ) -> List[Dict]:
        """Query all records of an entity with automatic pagination.

        Args:
            entity: QBO entity name
            where: Optional WHERE clause
            order_by: ORDER BY clause (default: Id)

        Returns:
            List of all entity dictionaries
        """
        all_results = []
        start_position = 1
        page_size = 1000

        while True:
            results = self.query(
                entity=entity,
                where=where,
                order_by=order_by,
                max_results=page_size,
                start_position=start_position,
            )

            if not results:
                break

            all_results.extend(results)

            if len(results) < page_size:
                break

            start_position += page_size
            _logger.info(f"Fetched {len(all_results)} {entity} records so far...")

        _logger.info(f"Total {entity} records fetched: {len(all_results)}")
        return all_results

    def get(self, entity: str, entity_id: str) -> Optional[Dict]:
        """Get a single entity by ID.

        Args:
            entity: QBO entity name (e.g., 'Account', 'Customer')
            entity_id: The entity's QBO ID

        Returns:
            Entity dictionary or None if not found
        """
        self._ensure_token_valid()
        self.rate_limiter.wait_if_needed()

        url = f"{self.base_url}/{entity.lower()}/{entity_id}"

        response = requests.get(url, headers=self.headers)

        if response.status_code == 401:
            self.connection.refresh_access_token()
            response = requests.get(url, headers=self.headers)

        if response.status_code == 404:
            return None

        if response.status_code != 200:
            raise UserError(
                _(f"QBO API Error: {response.status_code} - {response.text}")
            )

        data = response.json()
        return data.get(entity)


class QboConnection(models.Model):
    """QuickBooks Online Connection Configuration.

    This model stores the OAuth2 credentials and connection settings
    for communicating with the QBO API.
    """

    _name = "qbo.connection"
    _description = "QuickBooks Online Connection"

    name = fields.Char(
        string="Connection Name", required=True, default="QuickBooks Online"
    )

    # OAuth2 Credentials
    client_id = fields.Char(
        string="Client ID",
        required=True,
        default=lambda self: os.environ.get("QBO_CLIENT_ID", ""),
        help="OAuth2 Client ID from Intuit Developer Portal",
    )
    client_secret = fields.Char(
        string="Client Secret",
        required=True,
        default=lambda self: os.environ.get("QBO_CLIENT_SECRET", ""),
        help="OAuth2 Client Secret from Intuit Developer Portal",
    )
    realm_id = fields.Char(
        string="Realm ID (Company ID)",
        default=lambda self: os.environ.get("QBO_REALM_ID", ""),
        help="QuickBooks Company ID",
    )

    # Tokens
    access_token = fields.Char(string="Access Token")
    refresh_token = fields.Char(string="Refresh Token")
    token_expiry = fields.Datetime(string="Token Expiry")

    # Settings
    redirect_uri = fields.Char(
        string="Redirect URI",
        default=lambda self: os.environ.get("QBO_REDIRECT_URI", ""),
        help="OAuth2 Redirect URI (e.g., https://your-odoo.com/qbo/callback)",
    )
    sandbox_mode = fields.Boolean(
        string="Sandbox Mode",
        default=lambda self: os.environ.get("QBO_SANDBOX", "").lower() in ("1", "true"),
        help="Use QBO Sandbox environment for testing",
    )

    # Default accounts for partners (auto-detected or manually set)
    default_receivable_account_id = fields.Many2one(
        "account.account",
        string="Default Receivable Account",
        domain="[('account_type', '=', 'asset_receivable')]",
        help="Default Account Receivable for imported partners. Auto-detected from QBO accounts if not set.",
    )
    default_payable_account_id = fields.Many2one(
        "account.account",
        string="Default Payable Account",
        domain="[('account_type', '=', 'liability_payable')]",
        help="Default Account Payable for imported partners. Auto-detected from QBO accounts if not set.",
    )

    # Import tracking
    last_account_sync = fields.Datetime(string="Last Account Sync")
    last_customer_sync = fields.Datetime(string="Last Customer Sync")
    last_vendor_sync = fields.Datetime(string="Last Vendor Sync")
    last_product_sync = fields.Datetime(string="Last Product Sync")
    last_invoice_sync = fields.Datetime(string="Last Invoice Sync")
    last_bill_sync = fields.Datetime(string="Last Bill Sync")
    last_journal_entry_sync = fields.Datetime(string="Last Journal Entry Sync")

    # Connection state
    state = fields.Selection(
        [
            ("draft", "Not Connected"),
            ("connected", "Connected"),
            ("error", "Error"),
        ],
        default="draft",
        string="Status",
    )

    @api.depends("name", "realm_id")
    def _compute_display_name(self):
        for rec in self:
            if rec.realm_id:
                rec.display_name = f"{rec.name} ({rec.realm_id})"
            else:
                rec.display_name = rec.name

    def get_oauth_url(self) -> str:
        """Generate OAuth2 authorization URL.

        Returns:
            URL to redirect user for OAuth2 authorization
        """
        self.ensure_one()

        # Include database name and connection ID in state for callback
        db_name = self.env.cr.dbname
        state_data = f"{db_name}:{self.id}"

        oauth = OAuth2Session(
            self.client_id,
            redirect_uri=self._get_redirect_uri(),
            scope=["com.intuit.quickbooks.accounting"],
        )

        authorization_url, state = oauth.authorization_url(
            QBO_AUTH_URL, state=state_data
        )

        return authorization_url

    def _get_redirect_uri(self) -> str:
        """Get the OAuth2 redirect URI."""
        self.ensure_one()
        if self.redirect_uri:
            return self.redirect_uri
        base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url")
        return f"{base_url}/qbo/callback"

    def handle_oauth_callback(self, authorization_response: str) -> None:
        """Handle OAuth2 callback and exchange code for tokens.

        Args:
            authorization_response: The full callback URL with auth code
        """
        self.ensure_one()

        oauth = OAuth2Session(
            self.client_id,
            redirect_uri=self._get_redirect_uri(),
        )

        token = oauth.fetch_token(
            QBO_TOKEN_URL,
            authorization_response=authorization_response,
            client_secret=self.client_secret,
        )

        self._save_token(token)
        self.state = "connected"

    def refresh_access_token(self) -> None:
        """Refresh the OAuth2 access token using the refresh token."""
        self.ensure_one()

        if not self.refresh_token:
            raise UserError(_("No refresh token available. Please re-authorize."))

        auth_header = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()

        response = requests.post(
            QBO_TOKEN_URL,
            headers={
                "Authorization": f"Basic {auth_header}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            },
        )

        if response.status_code != 200:
            self.state = "error"
            raise UserError(
                _(f"Failed to refresh token: {response.status_code} - {response.text}")
            )

        token = response.json()
        self._save_token(token)

    def _save_token(self, token: Dict) -> None:
        """Save OAuth2 token data to the record."""
        self.write(
            {
                "access_token": token.get("access_token"),
                "refresh_token": token.get("refresh_token"),
                "token_expiry": datetime.now()
                + timedelta(seconds=token.get("expires_in", 3600)),
                "state": "connected",
            }
        )

    def get_api_client(self) -> QBOApiClient:
        """Get an API client instance for this connection.

        Returns:
            QBOApiClient configured for this connection
        """
        self.ensure_one()

        if not self.access_token:
            raise UserError(_("Not connected to QuickBooks. Please authorize first."))

        return QBOApiClient(self)

    def query_qbo(self, entity, where="", order_by="Id", max_results=0):
        """Run a QBO API query and return results as a list of dicts.

        Callable via MCP / xmlrpc for ad-hoc data exploration.

        Args:
            entity: QBO entity name (e.g. "Invoice", "Account", "Bill").
            where: Optional WHERE clause (e.g. "Active = true").
            order_by: ORDER BY clause, default "Id".
            max_results: If >0, use single-page query with this limit.
                         If 0 (default), fetch all pages.

        Returns:
            List of QBO entity dicts.
        """
        self.ensure_one()
        client = self.get_api_client()
        if max_results:
            return client.query(
                entity, where=where, order_by=order_by, max_results=max_results
            )
        return client.query_all(entity, where=where, order_by=order_by)

    def count_qbo(self, entity, where=""):
        """Return the record count for a QBO entity.

        Uses SELECT COUNT(*) for efficiency — no record data transferred.
        """
        self.ensure_one()
        client = self.get_api_client()
        client._ensure_token_valid()
        client.rate_limiter.wait_if_needed()
        query = f"SELECT COUNT(*) FROM {entity}"
        if where:
            query += f" WHERE {where}"
        url = f"{client.base_url}/query"
        response = requests.get(
            url, headers=client.headers, params={"query": query}
        )
        if response.status_code == 401:
            client.connection.refresh_access_token()
            response = requests.get(
                url, headers=client.headers, params={"query": query}
            )
        data = response.json()
        return data.get("QueryResponse", {}).get("totalCount", 0)

    def _get_source_config(self) -> dict:
        """Build source configuration dictionary for ETL framework.

        Returns:
            Dictionary with source-specific configuration values.
        """
        self.ensure_one()
        return {
            "source_id": self.id,
            "source_model": "qbo.connection",
        }

    @api.model
    def setup_from_env(self):
        """Setup QBO connection from environment variables.

        Called from XML data file on module install/upgrade.

        Environment variables:
        - QBO_CLIENT_ID: OAuth2 Client ID
        - QBO_CLIENT_SECRET: OAuth2 Client Secret
        - QBO_REALM_ID: Company/Realm ID
        - QBO_SANDBOX: Use sandbox mode (1/true)
        - QBO_ACCESS_TOKEN: Pre-configured access token (optional)
        - QBO_REFRESH_TOKEN: Pre-configured refresh token (optional)
        """
        if not os.getenv("QBO_CLIENT_ID"):
            _logger.info("QBO_CLIENT_ID not set, skipping QBO connection setup")
            return

        client_id = os.getenv("QBO_CLIENT_ID")
        client_secret = os.getenv("QBO_CLIENT_SECRET")
        realm_id = os.getenv("QBO_REALM_ID", "")
        sandbox = os.getenv("QBO_SANDBOX", "").lower() in ("1", "true")
        access_token = os.getenv("QBO_ACCESS_TOKEN", "")
        refresh_token = os.getenv("QBO_REFRESH_TOKEN", "")

        if not all([client_id, client_secret]):
            _logger.warning(
                "Missing required QBO environment variables. "
                "Required: QBO_CLIENT_ID, QBO_CLIENT_SECRET"
            )
            return

        _logger.info(f"Creating qbo.connection record")

        existing = self.search(
            [
                ("client_id", "=", client_id),
                ("realm_id", "=", realm_id),
            ],
            limit=1,
        )

        vals = {
            "name": "QuickBooks Online",
            "client_id": client_id,
            "client_secret": client_secret,
            "realm_id": realm_id,
            "sandbox_mode": sandbox,
        }

        if access_token:
            vals["access_token"] = access_token
            vals["state"] = "connected"
        if refresh_token:
            vals["refresh_token"] = refresh_token

        if existing:
            _logger.info(f"Updating existing qbo.connection record (ID: {existing.id})")
            existing.write(vals)
        else:
            _logger.info("Creating new qbo.connection record")
            self.create(vals)

    @api.model
    def _success_notification(self) -> dict:
        """Return a success notification action."""
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Import Successful"),
                "message": _(
                    "The QuickBooks Online records were successfully imported."
                ),
                "sticky": False,
                "type": "success",
            },
        }

    ##################################################################
    # ETL Pipeline Execution Helpers
    ##################################################################

    _PIPELINE_SYNC_FIELDS = {
        "qbo.account.importer": "last_account_sync",
        "qbo.customer.importer": "last_customer_sync",
        "qbo.vendor.importer": "last_vendor_sync",
        "qbo.item.importer": "last_product_sync",
        "qbo.invoice.importer": "last_invoice_sync",
        "qbo.bill.importer": "last_bill_sync",
        "qbo.journal.entry.importer": "last_journal_entry_sync",
    }

    def _execute_pipeline(self, pipeline_name: str) -> dict:
        """Execute a single ETL pipeline by name."""
        self.ensure_one()

        pipeline = ETL.get_pipeline(pipeline_name)
        if not pipeline:
            raise UserError(_(f"{pipeline_name} ETL pipeline not found"))

        # For QBO, we pass None as cr since we use API, not database cursor
        importer = self.env[pipeline_name].with_company(self.env.company)
        ctx = ETLContext(
            cr=None,  # No source database cursor for API-based ETL
            env=self.env,
            source_config=self._get_source_config(),
        )
        executor = ETLExecutor(pipeline, ctx, importer)
        executor.execute()

        # Update sync timestamp once (on orchestrator only, not per chunk)
        sync_field = self._PIPELINE_SYNC_FIELDS.get(pipeline_name)
        if sync_field:
            self[sync_field] = fields.Datetime.now()

        self.env.cr.commit()

        return self._success_notification()

    def _execute_pipelines(self, pipeline_names: list) -> dict:
        """Execute multiple ETL pipelines."""
        self.ensure_one()

        for pipeline_name in pipeline_names:
            self._execute_pipeline(pipeline_name)

        return self._success_notification()

    def _execute_all_pipelines(self) -> dict:
        """Execute all QBO ETL pipelines using the orchestrator."""
        self.ensure_one()

        orchestrator = PipelineOrchestrator(
            self.env,
            source_config=self._get_source_config(),
            module_filter="qbo_to_odoo",
        )
        # For QBO, we pass None as cr since we use API, not database cursor
        orchestrator.execute_all(cr=None)

        return self._success_notification()

    ##################################################################
    # Public Action Methods (Called from UI)
    ##################################################################

    def action_authorize(self) -> dict:
        """Start OAuth2 authorization flow."""
        self.ensure_one()

        auth_url = self.get_oauth_url()

        return {
            "type": "ir.actions.act_url",
            "url": auth_url,
            "target": "new",
        }

    def action_test_connection(self) -> dict:
        """Test the QBO connection."""
        self.ensure_one()

        try:
            client = self.get_api_client()
            # Try to fetch company info
            accounts = client.query("Account", max_results=1)

            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Connection Successful"),
                    "message": _("Successfully connected to QuickBooks Online."),
                    "sticky": False,
                    "type": "success",
                },
            }
        except Exception as e:
            self.state = "error"
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Connection Failed"),
                    "message": str(e),
                    "sticky": True,
                    "type": "danger",
                },
            }

    def action_import_accounts(self) -> dict:
        """Import QBO Chart of Accounts."""
        result = self._execute_pipeline("qbo.account.importer")
        # Auto-detect AR/AP accounts after import
        self._auto_detect_default_accounts()
        return result

    def _auto_detect_default_accounts(self) -> None:
        """Auto-detect default AR/AP accounts by finding most-used accounts in QBO.

        Queries QBO customers for ARAccountRef and vendors for APAccountRef
        to find the most commonly used accounts, then sets them as Odoo system
        defaults via ir.property.
        """
        self.ensure_one()

        try:
            client = self.get_api_client()
        except Exception:
            _logger.warning("Cannot auto-detect accounts - not connected to QBO")
            return

        IrDefault = self.env["ir.default"].sudo()

        # Find most-used AR account from Invoices
        ar_account_id = self._find_most_used_account(client, "Invoice", "ARAccountRef")
        # Find most-used AP account from Bills
        ap_account_id = self._find_most_used_account(client, "Bill", "APAccountRef")

        # If not found on transactions, fall back to account type search
        if ar_account_id:
            receivable = self.env["account.account"].search(
                [("qbo_id", "=", ar_account_id)], limit=1
            )
        else:
            receivable = self.env["account.account"].search(
                [("account_type", "=", "asset_receivable"), ("qbo_id", "!=", False)],
                limit=1,
            )

        if ap_account_id:
            payable = self.env["account.account"].search(
                [("qbo_id", "=", ap_account_id)], limit=1
            )
        else:
            payable = self.env["account.account"].search(
                [("account_type", "=", "liability_payable"), ("qbo_id", "!=", False)],
                limit=1,
            )

        # Set defaults - delete all existing defaults first to avoid duplicates
        if receivable:
            self.default_receivable_account_id = receivable.id
            # Delete all existing defaults for this field
            ar_field = self.env["ir.model.fields"]._get(
                "res.partner", "property_account_receivable_id"
            )
            IrDefault.search([("field_id", "=", ar_field.id)]).unlink()
            IrDefault.set(
                "res.partner", "property_account_receivable_id", receivable.id
            )
            _logger.info(
                f"Set default receivable account: {receivable.code} - {receivable.name}"
            )

        if payable:
            self.default_payable_account_id = payable.id
            # Delete all existing defaults for this field
            ap_field = self.env["ir.model.fields"]._get(
                "res.partner", "property_account_payable_id"
            )
            IrDefault.search([("field_id", "=", ap_field.id)]).unlink()
            IrDefault.set("res.partner", "property_account_payable_id", payable.id)
            _logger.info(
                f"Set default payable account: {payable.code} - {payable.name}"
            )

    def _find_most_used_account(self, client, entity_type: str, account_ref_field: str):
        """Find the most commonly used account from QBO entities.

        Args:
            client: QBO API client
            entity_type: 'Customer' or 'Vendor'
            account_ref_field: 'ARAccountRef' or 'APAccountRef'

        Returns:
            QBO account ID of the most used account, or None
        """
        from collections import Counter

        try:
            # Query all entities to count account usage
            entities = client.query(entity_type, max_results=1000)

            account_counts = Counter()
            for entity in entities:
                account_ref = entity.get(account_ref_field, {})
                if account_ref and account_ref.get("value"):
                    account_counts[int(account_ref["value"])] += 1

            if account_counts:
                most_common = account_counts.most_common(1)[0]
                _logger.info(
                    f"Most used {account_ref_field}: QBO ID {most_common[0]} "
                    f"(used by {most_common[1]} {entity_type.lower()}s)"
                )
                return most_common[0]

            _logger.info(f"No {account_ref_field} found on {entity_type}s")
            return None
        except Exception as e:
            _logger.warning(f"Error finding most used {account_ref_field}: {e}")
            return None

    def action_import_customers(self) -> dict:
        """Import QBO Customers."""
        return self._execute_pipeline("qbo.customer.importer")

    def action_import_vendors(self) -> dict:
        """Import QBO Vendors."""
        return self._execute_pipeline("qbo.vendor.importer")

    def action_import_categories(self) -> dict:
        """Import QBO Product Categories."""
        return self._execute_pipeline("qbo.category.importer")

    def action_import_products(self) -> dict:
        """Import QBO Items (Products/Services)."""
        return self._execute_pipeline("qbo.item.importer")

    def action_import_journal_entries(self) -> dict:
        """Import QBO Journal Entries."""
        return self._execute_pipeline("qbo.journal.entry.importer")

    def action_import_payment_terms(self) -> dict:
        """Import QBO Payment Terms."""
        return self._execute_pipeline("qbo.term.importer")

    def action_import_taxes(self) -> dict:
        """Import QBO Tax Codes and Rates."""
        return self._execute_pipeline("qbo.tax.importer")

    def action_import_invoices(self) -> dict:
        """Import QBO Invoices."""
        return self._execute_pipeline("qbo.invoice.importer")

    def action_import_bills(self) -> dict:
        """Import QBO Bills (Vendor Bills)."""
        return self._execute_pipeline("qbo.bill.importer")

    def action_import_payments(self) -> dict:
        """Import QBO Payments (Customer and Vendor)."""
        return self._execute_pipeline("qbo.payment.importer")

    def action_create_bank_journals(self) -> dict:
        """Create bank journals for existing bank accounts that don't have one."""
        company = self.env.company
        bank_accounts = self.env["account.account"].search(
            [
                ("account_type", "=", "asset_cash"),
                ("company_ids", "in", [company.id]),
            ]
        )

        created = 0
        for account in bank_accounts:
            existing = self.env["account.journal"].search(
                [
                    ("type", "=", "bank"),
                    ("default_account_id", "=", account.id),
                    ("company_id", "=", company.id),
                ],
                limit=1,
            )

            if not existing:
                self.env["account.journal"].create(
                    {
                        "name": account.name,
                        "type": "bank",
                        "code": account.code[:5],
                        "default_account_id": account.id,
                        "company_id": company.id,
                    }
                )
                created += 1

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Bank Journals",
                "message": f"Created {created} bank journals",
                "type": "success",
            },
        }

    def action_reconcile_payments(self) -> dict:
        """Reconcile imported payments with invoices/bills."""
        return self._execute_pipeline("qbo.payment.reconciler")

    def action_import_all(self) -> dict:
        """Import all QBO data in the correct order."""
        return self._execute_all_pipelines()
