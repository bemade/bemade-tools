"""Utility functions for QBO ETL pipelines."""

from odoo.addons.etl_framework import ETLContext


def get_api_client(ctx: ETLContext):
    """Get the QBO API client from the ETL context.

    Reconstructs the client from the QBO connection record identified
    by ``source_id`` / ``source_model`` in the source config.

    Args:
        ctx: The ETL context.

    Returns:
        The QBO API client.

    Raises:
        ValueError: If the connection record cannot be found.
    """
    source_id = ctx.get_config("source_id")
    source_model = ctx.get_config("source_model")
    if not source_id or not source_model:
        raise ValueError(
            "source_id and source_model must be set in source_config"
        )
    connection = ctx.env[source_model].browse(source_id)
    if not connection.exists():
        raise ValueError(
            f"{source_model} record {source_id} not found"
        )
    return connection.get_api_client()
