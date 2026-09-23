"""ETL Pipeline for importing attachments from SAP B1 into Odoo."""

import base64
import csv
import logging
import os
import tempfile
from collections import Counter
from typing import Dict, List

from odoo import models
from odoo.tools import config as odoo_config
from odoo.tools.sql import SQL

from odoo.addons.etl_framework import ETL, ETLContext

_logger = logging.getLogger(__name__)

# Columns of the missing-files manifest CSV (see _write_missing_manifest).
_MANIFEST_FIELDS = [
    "absentry",
    "model",
    "res_id",
    "filename",
    "expected_path",
    "sap_trgtpath",
    "sap_srcpath",
    "reason",
]


@ETL.pipeline(
    target_model="ir.attachment",
    importer_name="ir.attachment.importer",
    sap_source="atc1",
    depends_on=[
        "account.move.jdt1.importer",
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

        # Fresh run, fresh manifest: extract runs once per pipeline (before any
        # transform chunk), so this is the place to drop the previous run's
        # missing-files manifest.
        manifest = self._missing_manifest_path(ctx.env)
        if os.path.exists(manifest):
            os.remove(manifest)

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
        missing = []
        found_by_model = Counter()
        missing_by_model = Counter()
        for att in sap_attachments:
            filestore_path = att["_filestore_path"]
            filename = f"{att['filename']}.{att['fileext']}"
            file_path = os.path.join(filestore_path, filename)

            try:
                with open(file_path, "rb") as file:
                    file_data = file.read()

                vals = {
                    "name": filename,
                    "res_model": att["_model_name"],
                    "res_id": att["_res_id"],
                    "type": "binary",
                    "sap_absentry": att["absentry"],
                    "datas": base64.b64encode(file_data),
                }
                attachment_vals.append(vals)
                found_by_model[att["_model_name"]] += 1
            except FileNotFoundError:
                _logger.warning(f"File not found: {file_path}")
                self._record_missing(ctx, missing, missing_by_model, att,
                                     file_path, "file not found")
            except Exception as e:
                _logger.error(f"Error reading file {file_path}: {e}")
                self._record_missing(ctx, missing, missing_by_model, att,
                                     file_path, str(e))

        if missing:
            manifest = self._write_missing_manifest(ctx.env, missing)
            _logger.warning(
                "attachments: %d of %d files missing/unreadable (%s) — "
                "manifest for fixing the folder: %s",
                len(missing),
                len(sap_attachments),
                ", ".join(f"{m}: {n}" for m, n in missing_by_model.most_common()),
                manifest,
            )
        _logger.info(
            "Transformed %d attachments (%s)",
            len(attachment_vals),
            ", ".join(f"{m}: {n}" for m, n in found_by_model.most_common())
            or "none",
        )
        return attachment_vals

    def _record_missing(self, ctx, missing, missing_by_model, att, file_path,
                        reason):
        """Track one unreadable attachment: ETL report + manifest row."""
        model_name = att["_model_name"]
        missing_by_model[model_name] += 1
        ctx.report.warning(
            "Attachment file missing/unreadable (%s): %s [%s res_id=%s]"
            % (reason, file_path, model_name, att["_res_id"]),
            source_ref=f"absentry {att['absentry']}",
        )
        missing.append(
            {
                "absentry": att["absentry"],
                "model": model_name,
                "res_id": att["_res_id"],
                "filename": f"{att['filename']}.{att['fileext']}",
                "expected_path": file_path,
                "sap_trgtpath": att.get("trgtpath") or "",
                "sap_srcpath": att.get("srcpath") or "",
                "reason": reason,
            }
        )

    def _missing_manifest_path(self, env):
        """Deterministic per-database manifest location (next to the log file,
        or the system temp dir when no logfile is configured)."""
        logfile = odoo_config.get("logfile")
        base = os.path.dirname(logfile) if logfile else tempfile.gettempdir()
        return os.path.join(
            base, f"sap_missing_attachments_{env.cr.dbname}.csv"
        )

    def _write_missing_manifest(self, env, rows):
        """Append missing-file rows to the manifest CSV, creating it with a
        header when new. Append (not overwrite) because in multiprocessing
        mode each transform chunk writes its own batch; extract clears the
        file once at the start of every run."""
        path = self._missing_manifest_path(env)
        write_header = not os.path.exists(path)
        with open(path, "a", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=_MANIFEST_FIELDS)
            if write_header:
                writer.writeheader()
            writer.writerows(rows)
        return path

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
