"""xTuple to Odoo Module Hooks

This module contains hooks for module installation and upgrade.
"""

import logging
import os

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def _run_xtuple_import(env):
    """Run xTuple import if XTUPLE_AUTO_IMPORT is enabled."""
    _logger.info("_run_xtuple_import called")
    auto_import = os.getenv("XTUPLE_AUTO_IMPORT", "").lower() in ("1", "true")
    _logger.info(
        f"XTUPLE_AUTO_IMPORT env var: {os.getenv('XTUPLE_AUTO_IMPORT', 'NOT SET')}"
    )
    if not auto_import:
        _logger.info("XTUPLE_AUTO_IMPORT not enabled, skipping import")
        return

    _logger.info("XTUPLE_AUTO_IMPORT is enabled, running import_all()")

    # Find the xTuple database record created during install
    xtuple_db = env["xtuple.database"].search([], limit=1)
    if not xtuple_db:
        _logger.warning("No xtuple.database record found, skipping auto-import")
        return

    try:
        xtuple_db._import_all()
        _logger.info("Successfully completed xTuple import")
    except Exception as e:
        _logger.error(f"Error during xTuple import: {e}", exc_info=True)


def post_init_hook(env):
    """Run xTuple import after module installation completes."""
    _run_xtuple_import(env)
