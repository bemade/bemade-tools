"""ETL Mapping - Abstract model for ETL data mappings

This module provides the base functionality for ETL mappings between
external databases and Odoo models.
"""

from odoo import models, api, fields
from odoo.sql_db import db_connect
import logging

_logger = logging.getLogger(__name__)


class ETLMapping(models.AbstractModel):
    """Abstract model for ETL data mappings

    This model provides the base functionality for extracting data from
    external databases and loading it into Odoo models.

    Subclasses should define:
        _source_table: Name of the source table
        _source_schema: Schema name (optional, for PostgreSQL)
        _source_db: Database connection reference (optional)
        _target_model: Name of the target Odoo model

        And ETLField fields for field mappings
    """

    _name = "etl.mapping"
    _description = "ETL Mapping Base"

    # Configuration attributes (to be overridden by subclasses)
    _source_table = None
    _target_model = None
    _source_schema = None
    _source_db = None

    # External database configuration (required)
    external_database_id = fields.Many2one(
        "etl.external.database",
        string="External Database",
        required=True,
        help="External database to extract data from",
    )

    @api.model
    def _get_external_cursor(self):
        """Get a cursor to the external database

        Returns:
            Database cursor for external database
        """
        self.ensure_one()

        if not self.external_database_id:
            raise ValueError(
                f"No external database configured on {self._name}. "
                f"Set external_database_id field."
            )

        return self.external_database_id.get_cursor()

    @api.model
    def _get_etl_fields(self):
        """Get all ETLField fields defined on this model

        Returns:
            dict: {field_name: field_object} for all ETLFields
        """
        etl_fields = {}
        for field_name, field in self._fields.items():
            if field.type == "etl_field":
                etl_fields[field_name] = field
        return etl_fields

    @api.model
    def _build_select_query(self):
        """Build SELECT query from ETLField definitions

        Returns:
            str: SQL SELECT query
        """
        if not self._source_table:
            raise ValueError(f"No _source_table defined on {self._name}")

        etl_fields = self._get_etl_fields()
        if not etl_fields:
            raise ValueError(f"No ETLField fields defined on {self._name}")

        # Build column list from source attributes
        columns = []
        for field_name, field in etl_fields.items():
            if field.source:
                columns.append(field.source)

        if not columns:
            raise ValueError(f"No source columns defined in ETLFields on {self._name}")

        # Build query
        column_list = ", ".join(columns)

        # Add schema prefix if specified
        if self._source_schema:
            table_name = f"{self._source_schema}.{self._source_table}"
        else:
            table_name = self._source_table

        query = f"SELECT {column_list} FROM {table_name}"

        _logger.debug(f"Generated query: {query}")
        return query

    def extract(self):
        """Extract data from external database

        This method:
        1. Builds a SELECT query from ETLField definitions
        2. Executes the query on the external database
        3. Returns the results as a list of dictionaries

        Returns:
            list: List of dictionaries with extracted data
        """
        self.ensure_one()

        # Build the query
        query = self._build_select_query()

        # Execute on external database
        with self._get_external_cursor() as cr:
            _logger.info(f"Extracting data from {self._source_table}")
            cr.execute(query)
            results = cr.dictfetchall()
            _logger.info(f"Extracted {len(results)} records")

        return results
