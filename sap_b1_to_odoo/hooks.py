import logging
import os

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def post_init_hook(env):
    """Run SAP import after module installation completes.

    This hook runs after the module install transaction commits,
    ensuring all schema changes (new columns, etc.) are visible
    to any cron jobs or separate cursors that may run during import.
    """
    auto_import = os.getenv("SAP_AUTO_IMPORT", "").lower() in ("1", "true")
    if not auto_import:
        return

    _logger.info("SAP_AUTO_IMPORT is enabled, running import_all()")

    # Find the SAP database record created during install
    sap_db = env["sap.database"].search([], limit=1)
    if not sap_db:
        _logger.warning("No sap.database record found, skipping auto-import")
        return

    try:
        sap_db._import_all()
        _logger.info("Successfully completed SAP import")
    except Exception as e:
        _logger.error(f"Error during SAP import: {e}", exc_info=True)
