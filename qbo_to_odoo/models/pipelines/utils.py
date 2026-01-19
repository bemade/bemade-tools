"""Utility functions for QBO ETL pipelines."""

from odoo.addons.etl_framework import ETLContext


def get_api_client(ctx: ETLContext):
    """Get the QBO API client from the ETL context.

    Args:
        ctx: The ETL context.

    Returns:
        The QBO API client.

    Raises:
        ValueError: If the API client is not found in the context.
    """
    api_client = ctx.get_config("api_client")
    if not api_client:
        raise ValueError("API client not found in ETL context")
    return api_client
