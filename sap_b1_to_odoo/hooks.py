import logging
import os

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def _run_sap_import(env):
    """Run SAP import if SAP_AUTO_IMPORT is enabled."""
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


def post_init_hook(env):
    """Run SAP import after module installation completes."""
    _run_sap_import(env)


def post_load_hook():
    """Run SAP import after module update (post_load runs on every load).

    Note: This runs before the registry is fully loaded, so we need to
    defer the actual import using a post_init_hook pattern.
    """
    # post_load doesn't have env, so we can't run import here directly.
    # Instead, we'll use the uninstall_hook approach or rely on post_init.
    pass


def uninstall_hook(env):
    """Cleanup hook for module uninstall."""
    pass
