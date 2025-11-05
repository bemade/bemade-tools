"""ETL External Database - Configuration for external database connections"""

from odoo import models, fields, api, _
from odoo.sql_db import db_connect
from odoo.exceptions import ValidationError
import logging

_logger = logging.getLogger(__name__)


class ETLExternalDatabase(models.Model):
    """External Database Connection Configuration

    This model stores connection information for external databases
    that will be used as sources for ETL operations.
    """

    _name = "etl.external.database"
    _description = "External Database Connection"

    name = fields.Char(string="Database Name", required=True)
    active = fields.Boolean(default=True)

    # Connection details (PostgreSQL only)
    host = fields.Char(string="Host", required=True, default="localhost")
    port = fields.Integer(string="Port", required=True, default=5432)
    database = fields.Char(string="Database", required=True)
    username = fields.Char(string="Username", required=True)
    password = fields.Char(string="Password")
    schema = fields.Char(string="Schema", help="Default schema for queries")

    # Additional options
    connection_options = fields.Text(
        string="Connection Options",
        help="Additional connection parameters (key=value format, one per line)",
    )

    # Mappings registry
    mapping_ids = fields.One2many(
        "etl.mapping",
        "external_database_id",
        string="Mappings",
    )

    @api.depends("host", "database")
    def _compute_display_name(self):
        """Compute display name from host and database"""
        for record in self:
            if record.host and record.database:
                record.display_name = f"{record.host}/{record.database}"
            else:
                record.display_name = record.name or "New Database"

    def get_cursor(self):
        """Get a database cursor for this external database

        Returns:
            Database cursor connected to the external database
        """
        self.ensure_one()

        # Build PostgreSQL connection URI
        if self.password:
            uri = ("postgresql://{user}:{password}@{host}:{port}/{database}").format(
                user=self.username,
                password=self.password,
                host=self.host,
                port=self.port,
                database=self.database,
            )
        else:
            uri = ("postgresql://{user}@{host}:{port}/{database}").format(
                user=self.username,
                host=self.host,
                port=self.port,
                database=self.database,
            )

        # Add schema to search path if specified
        if self.schema:
            uri += f"?options=-c%20search_path%3D{self.schema}"

        _logger.debug(f"Connecting to external database: {self.host}/{self.database}")
        return db_connect(uri, allow_uri=True).cursor()

    def action_test_connection(self):
        """Test the database connection

        Returns:
            dict: Notification action
        """
        self.ensure_one()

        try:
            with self.get_cursor() as cr:
                cr.execute("SELECT 1")
                result = cr.fetchone()
                if result and result[0] == 1:
                    return {
                        "type": "ir.actions.client",
                        "tag": "display_notification",
                        "params": {
                            "title": _("Connection Successful"),
                            "message": _("Successfully connected to %s")
                            % self.display_name,
                            "type": "success",
                            "sticky": False,
                        },
                    }
        except Exception as e:
            raise ValidationError(_("Connection failed: %s") % str(e))
