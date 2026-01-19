"""QuickBooks Online OAuth2 Callback Controller"""

import logging

from odoo import api, http, SUPERUSER_ID
from odoo.modules.registry import Registry

_logger = logging.getLogger(__name__)


class QBOController(http.Controller):
    """Controller for QBO OAuth2 callback handling."""

    def _success_response(self):
        """Return a simple HTML success page."""
        return """
        <html>
        <head><title>QuickBooks Online Connected</title>
        <style>
            body { font-family: sans-serif; display: flex; justify-content: center; 
                   align-items: center; min-height: 100vh; margin: 0; background: #f8f9fa; }
            .container { text-align: center; padding: 40px; background: white; 
                        border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
            .success { font-size: 64px; color: #28a745; }
            h1 { color: #333; }
            p { color: #666; }
        </style>
        </head>
        <body>
            <div class="container">
                <div class="success">✓</div>
                <h1>Connected Successfully!</h1>
                <p>QuickBooks Online has been connected to Odoo.</p>
                <p>You can close this tab and refresh the Odoo page.</p>
            </div>
        </body>
        </html>
        """

    def _error_response(self, error, description):
        """Return a simple HTML error page."""
        return f"""
        <html>
        <head><title>QuickBooks Online Error</title>
        <style>
            body {{ font-family: sans-serif; display: flex; justify-content: center; 
                   align-items: center; min-height: 100vh; margin: 0; background: #f8f9fa; }}
            .container {{ text-align: center; padding: 40px; background: white; 
                        border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
            .error {{ font-size: 64px; color: #dc3545; }}
            h1 {{ color: #333; }}
            .error-code {{ color: #dc3545; font-weight: bold; }}
            p {{ color: #666; }}
        </style>
        </head>
        <body>
            <div class="container">
                <div class="error">✗</div>
                <h1>Connection Failed</h1>
                <p class="error-code">{error}</p>
                <p>{description}</p>
                <p>Please close this tab and try again.</p>
            </div>
        </body>
        </html>
        """

    @http.route(
        ["/qbo/callback", "/get_auth_code"], type="http", auth="none", csrf=False
    )
    def qbo_callback(self, **kwargs):
        """Handle OAuth2 callback from QuickBooks Online."""
        error = kwargs.get("error")
        if error:
            error_desc = kwargs.get("error_description", "Unknown error")
            _logger.error(f"QBO OAuth error: {error} - {error_desc}")
            return self._error_response(error, error_desc)

        code = kwargs.get("code")
        realm_id = kwargs.get("realmId")
        state = kwargs.get("state", "")

        if not code:
            _logger.error("QBO OAuth callback missing authorization code")
            return self._error_response(
                "missing_code", "No authorization code received"
            )

        # Parse state to get database name and connection ID
        if ":" not in state:
            _logger.error(f"QBO OAuth callback invalid state: {state}")
            return self._error_response("invalid_state", "Invalid state parameter")

        db_name, connection_id = state.split(":", 1)
        _logger.info(
            f"QBO callback - db: {db_name}, connection_id: {connection_id}, realm_id: {realm_id}"
        )

        # Get registry and environment for the specific database
        try:
            registry = Registry(db_name)
            with registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})

                connection = env["qbo.connection"].browse(int(connection_id))
                if not connection.exists():
                    return self._error_response("no_connection", "Connection not found")

                # Update realm_id if provided
                if realm_id and not connection.realm_id:
                    connection.realm_id = realm_id

                # Build the full authorization response URL for token exchange
                redirect_uri = connection._get_redirect_uri()
                auth_response = f"{redirect_uri}?code={code}"
                if realm_id:
                    auth_response += f"&realmId={realm_id}"
                if state:
                    auth_response += f"&state={state}"

                connection.handle_oauth_callback(auth_response)
                _logger.info(f"QBO OAuth successful for connection {connection.id}")

                return self._success_response()
        except Exception as e:
            _logger.exception(f"QBO OAuth token exchange failed: {e}")
            return self._error_response("token_exchange_failed", str(e))
