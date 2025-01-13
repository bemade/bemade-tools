""" Import attachemnts from SAP B1 into Odoo objects """

from num2words.lang_HE import chunk2word

from odoo import models, fields, api
from odoo.tools.sql import SQL
from odoo.modules.registry import Registry
from concurrent.futures import ProcessPoolExecutor
import multiprocessing
import os
import logging
import base64
import psycopg2

_logger = logging.getLogger(__name__)
workers = os.cpu_count() - 1


class IrAttachment(models.Model):
    _inherit = "ir.attachment"

    sap_absentry = fields.Integer(index="btree")
    sap_line = fields.Integer(index="btree")

    _sql_constraints = [
        (
            "sap_absentry_line_unique",
            "UNIQUE(sap_absentry, sap_line)",
            "SAP AbsEntry must be unique",
        )
    ]


class SapIrAttachmentImporter(models.AbstractModel):
    _name = "sap.ir.attachment.importer"
    _description = "SAP Attachment Importer"

    @api.model
    def import_attachments(self, cr, filestore_path):
        """
        Imports attachments from SAP for a specific model.

        :param cr: Database cursor to execute SQL queries.
        :param filestore_path: Path to the filestore where attachment files are located.
        :returns: None
        """
        self._import_attachments_for_model(cr, "res.partner", filestore_path)
        self._import_attachments_for_model(cr, "product.product", filestore_path)
        self._import_attachments_for_model(cr, "sale.order", filestore_path)
        self._import_attachments_for_model(cr, "purchase.order", filestore_path)

    def _import_attachments_for_model(self, cr, model_name, filestore_path):
        """
        Handles the import of attachments related to a specific model.

        :param cr: Database cursor to execute SQL queries.
        :param model_name: Name of the model for which attachments are being imported.
        :param filestore_path: Path to the filestore where attachment files are located.
        :returns: None
        """
        _logger.info(f"Importing attachments for {model_name}...")
        table_name = self.env[model_name]._table
        absentries = self._get_absentries(cr, table_name)
        if not absentries:
            return
        record_dict = self._get_record_dict(table_name)
        sap_attachments = self._get_sap_attachments(cr, absentries)
        self._import_sap_attachments(
            sap_attachments,
            model_name,
            record_dict,
            filestore_path,
        )

    def _get_record_dict(self, tablename):
        """
        Retrieves a dictionary mapping `atcentry` to record `id` for a given table.

        :param tablename: Name of the table to query.
        :returns: Dictionary mapping `atcentry` to `id`.
        """
        self.env.cr.execute(
            SQL(
                "SELECT id, sap_atcentry FROM %s WHERE sap_atcentry is not null",
                SQL.identifier(tablename),
            )
        )
        return {row[1]: row[0] for row in self.env.cr.fetchall()}

    def _get_absentries(self, cr, tablename):
        """
        Retrieves a list of `atcentry` values that do not yet exist in attachments.

        :param cr: Database cursor to execute SQL queries.
        :param tablename: Name of the table to query for `atcentry`.
        :returns: Tuple of `atcentry` values.
        """
        self.env.cr.execute(
            SQL(
                """
                WITH existing AS (SELECT DISTINCT sap_absentry FROM ir_attachment WHERE sap_absentry is not null)
                SELECT DISTINCT sap_atcentry 
                FROM %s 
                WHERE sap_atcentry is not null
                AND sap_atcentry NOT IN (SELECT sap_absentry FROM existing)
                """,
                SQL.identifier(tablename),
            )
        )
        return tuple(row[0] for row in self.env.cr.fetchall())

    def _get_sap_attachments(self, cr, absentries):
        """
        Fetches SAP attachments corresponding to the provided `absentries`.

        :param cr: Database cursor to execute SQL queries.
        :param absentries: Tuple of `absentry` IDs to fetch SAP attachments.
        :returns: List of SAP attachments as dictionaries.
        """
        cr.execute(
            SQL(
                "SELECT * FROM atc1 WHERE absentry in %s",
                absentries,
            )
        )
        attachments = cr.dictfetchall()
        return attachments

    def _import_sap_attachments(
        self, sap_attachments, model_name, record_dict, filestore_path
    ):
        """
        Imports the SAP attachments by creating Odoo attachment records.

        :param sap_attachments: List of SAP attachments to import.
        :param model_name: Name of the Odoo model to link attachments to.
        :param record_dict: Dictionary mapping SAP `atcentry` to Odoo `res_id`.
        :param filestore_path: Path to the filestore where attachment files are located.
        :returns: The model for which the attachments were imported.
        """
        if len(sap_attachments) == 0:
            return self.env[model_name]
        processed_chunks = 0
        chunk_size = 500
        chunks = [
            sap_attachments[i : i + chunk_size]
            for i in range(0, len(sap_attachments), chunk_size)
        ]
        if False:  # len(sap_attachments) > 100:
            start_method = multiprocessing.get_start_method()
            multiprocessing.set_start_method("fork", force=True)
            try:
                with ProcessPoolExecutor(max_workers=workers) as executor:
                    futures = [
                        executor.submit(
                            self._process_sap_attachments_concurrent,
                            self.env.cr.dbname,
                            self.env.uid,
                            dict(self._context),
                            chunk,
                            model_name,
                            record_dict,
                            filestore_path,
                        )
                        for chunk in chunks
                    ]
                    for future in futures:
                        future.result()
            finally:
                multiprocessing.set_start_method(start_method, force=True)
        else:
            for chunk in chunks:
                self._process_sap_attachments(
                    chunk,
                    model_name,
                    record_dict,
                    filestore_path,
                )
                self.env.cr.commit()
                processed_chunks += 1
                _logger.info(
                    f"Processed {processed_chunks * chunk_size} attachments, {max(len(sap_attachments) - processed_chunks * chunk_size, 0)} remaining."
                )

    @staticmethod
    def _process_sap_attachments_concurrent(
        dbname, uid, context, chunk, model_name, record_dict, filestore_path
    ):
        """
        Processes chunks of SAP attachments concurrently in subprocesses.

        :param dbname: Name of the Odoo database.
        :param uid: User ID to create environment context.
        :param context: Context dictionary passed to the subprocess.
        :param chunk: List of SAP attachments broken into chunks.
        :param model_name: Name of the Odoo model to link attachments to.
        :param record_dict: Dictionary mapping SAP `atcentry` to Odoo `res_id`.
        :param filestore_path: Path to the filestore where attachment files are located.
        :returns: None
        """
        tries = 1
        maxtries = 3
        while tries < maxtries:
            with Registry(dbname).cursor() as cr:
                try:
                    env = api.Environment(cr, uid, context)
                    self = env["sap.ir.attachment.importer"]
                    self._process_sap_attachments(
                        chunk,
                        model_name,
                        record_dict,
                        filestore_path,
                    )
                except psycopg2.errors.SerializationFailure:
                    if tries < maxtries:
                        tries += 1
                        continue
                    else:
                        raise
                except Exception as e:
                    _logger.error(
                        "An exception occurred in a subprocess.", exc_info=True
                    )
                    raise e

    @api.model
    def _process_sap_attachments(
        self, sap_attachments, model_name, record_dict, filestore_path
    ):
        """
        Processes SAP attachments and creates corresponding Odoo attachment records.

        :param sap_attachments: List of SAP attachments to process.
        :param model_name: Name of the Odoo model to link attachments to.
        :param record_dict: Dictionary mapping SAP `atcentry` to Odoo `res_id`.
        :param filestore_path: Path to the filestore where attachment files are located.
        :returns: None
        """
        vals_list = []
        for attachment in sap_attachments:
            file_path = os.path.join(
                filestore_path, f"{attachment['filename']}.{attachment['fileext']}"
            )
            with open(file_path, "rb") as file:
                file_data = file.read()
            vals = {
                "name": f"{attachment["filename"]}.{attachment["fileext"]}",
                "res_model": model_name,
                "res_id": record_dict.get(attachment["absentry"]),
                "type": "binary",
                "sap_absentry": attachment["absentry"],
                "datas": base64.b64encode(file_data),
            }
            vals_list.append(vals)
        self.env["ir.attachment"].create(vals_list)
        self.env.cr.commit()
