"""ETL Pipeline for importing attachments from SAP B1 into Odoo."""

import base64
import logging
import os
from typing import Dict, List

from odoo import models
from odoo.tools.sql import SQL

from odoo.addons.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)


@ETL.pipeline(
    target_model="ir.attachment",
    importer_name="ir.attachment.importer",
    sap_source="atc1",
    depends_on=[
        "account.payment.reconciliation",
    ],
    multiprocessing_threshold=100,
    chunk_size=500,
)
class IrAttachmentImporter(models.AbstractModel):
    _name = "ir.attachment.importer"
    _description = "SAP Attachment Importer (ATC1)"

    # Models to import attachments for
    ATTACHMENT_MODELS = [
        "res.partner",
        "product.template",
        "sale.order",
        "purchase.order",
        "account.move",
        "mrp.production",
        "mrp.bom",
    ]

    @ETL.extract("atc1")
    def extract_attachments(self, ctx: ETLContext) -> List[Dict]:
        """Extract attachment metadata from SAP ATC1 table.

        Args:
            ctx: ETL context with SAP cursor and Odoo environment.

        Returns:
            List of attachment records with all necessary metadata.
        """
        # Get filestore_path from source config
        filestore_path = ctx.get_config("filestore_path")
        if not filestore_path:
            _logger.warning(
                "No filestore_path configured on sap.database, skipping attachment import"
            )
            return []

        all_attachments = []

        for model_name in self.ATTACHMENT_MODELS:
            table_name = ctx.env[model_name]._table

            # Get atcentries that don't already have attachments imported
            absentries = self._get_missing_absentries(ctx.env, table_name)
            if not absentries:
                _logger.info(f"No new attachments to import for {model_name}")
                continue

            # Get record mapping: sap_atcentry -> odoo_id
            record_dict = self._get_record_dict(ctx.env, table_name)

            # Get SAP attachments
            ctx.cr.execute(SQL("SELECT * FROM atc1 WHERE absentry in %s", absentries))
            sap_attachments = ctx.cr.dictfetchall()

            # Enrich each attachment with model info and record mapping
            for att in sap_attachments:
                att["_model_name"] = model_name
                att["_res_id"] = record_dict.get(att["absentry"])
                att["_filestore_path"] = filestore_path

            all_attachments.extend(sap_attachments)
            _logger.info(
                f"Extracted {len(sap_attachments)} attachments for {model_name}"
            )

        _logger.info(f"Total attachments extracted: {len(all_attachments)}")
        return all_attachments

    def _get_missing_absentries(self, env, tablename):
        """Get atcentry values that don't have attachments imported yet."""
        env.cr.execute(
            SQL(
                """
                WITH existing AS (
                    SELECT DISTINCT sap_absentry 
                    FROM ir_attachment 
                    WHERE sap_absentry IS NOT NULL
                )
                SELECT DISTINCT sap_atcentry 
                FROM %s 
                WHERE sap_atcentry IS NOT NULL
                AND sap_atcentry NOT IN (SELECT sap_absentry FROM existing)
                """,
                SQL.identifier(tablename),
            )
        )
        return tuple(row[0] for row in env.cr.fetchall())

    def _get_record_dict(self, env, tablename):
        """Get mapping of sap_atcentry -> odoo record id."""
        env.cr.execute(
            SQL(
                "SELECT id, sap_atcentry FROM %s WHERE sap_atcentry IS NOT NULL",
                SQL.identifier(tablename),
            )
        )
        return {row[1]: row[0] for row in env.cr.fetchall()}

    @ETL.transform()
    def transform_attachments(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        """Transform SAP attachments into Odoo ir.attachment values.

        Reads file data and prepares values for creation.

        Args:
            ctx: ETL context.
            extracted: Dictionary containing extracted data.

        Returns:
            List of attachment value dictionaries ready for creation.
        """
        sap_attachments = extracted.get("extract_attachments") or []

        attachment_vals = []
        for att in sap_attachments:
            filestore_path = att["_filestore_path"]
            file_path = os.path.join(
                filestore_path, f"{att['filename']}.{att['fileext']}"
            )

            try:
                with open(file_path, "rb") as file:
                    file_data = file.read()

                vals = {
                    "name": f"{att['filename']}.{att['fileext']}",
                    "res_model": att["_model_name"],
                    "res_id": att["_res_id"],
                    "type": "binary",
                    "sap_absentry": att["absentry"],
                    "datas": base64.b64encode(file_data),
                }
                attachment_vals.append(vals)
            except FileNotFoundError:
                _logger.warning(f"File not found: {file_path}")
            except Exception as e:
                _logger.error(f"Error reading file {file_path}: {e}")

        _logger.info(f"Transformed {len(attachment_vals)} attachments")
        return attachment_vals

    @ETL.load()
    def load_attachments(self, ctx: ETLContext, transformed: Dict) -> None:
        """Load attachments into Odoo.

        Args:
            ctx: ETL context.
            transformed: Dictionary containing transformed data.
        """
        attachment_vals = transformed.get("transform_attachments") or []

        if not attachment_vals:
            _logger.info("No attachments to import")
            return

        attachments = ctx.env["ir.attachment"].create(attachment_vals)
        _logger.info(f"Created {len(attachments)} attachments")
