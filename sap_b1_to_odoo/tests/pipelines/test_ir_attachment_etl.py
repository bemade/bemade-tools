#
#    Bemade Inc.
#
#    Copyright (C) 2026-April Bemade Inc. (<https://www.bemade.org>).
#    Author: Marc Durepos (Contact : marc@bemade.org)
#
#    This program is under the terms of the GNU Lesser General Public License,
#    version 3.
#
#    For full license details, see https://www.gnu.org/licenses/lgpl-3.0.en.html.
#
"""Tests for the ATC1 attachment transform's missing-file diagnostics.

Acceptance criteria:

1. (test_readable_file_transforms) A readable file becomes an ir.attachment
   vals dict (name, res_model/res_id, sap_absentry, base64 datas) and no
   manifest is written.
2. (test_missing_file_reported) A missing file is skipped without aborting
   the batch, records an ETL-report warning carrying the expected path,
   model/res_id and an ``absentry`` source_ref, and lands in the
   missing-files manifest CSV with the SAP trgtpath/srcpath preserved —
   the data needed to fix the attachments folder contents.
3. (test_manifest_appends_within_run) A second transform batch in the same
   run appends to the manifest without duplicating the header (the
   multiprocessing chunk case; extract clears the file once per run).
"""

import base64
import csv
import os
import tempfile
from types import SimpleNamespace

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.etl_framework.framework import ETLContext
from odoo.addons.etl_framework.reporter import PipelineReport


@tagged("-at_install", "post_install", "ir_attachment_etl")
class TestIrAttachmentEtl(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.importer = cls.env["ir.attachment.importer"]

    def setUp(self):
        super().setUp()
        self.filestore = tempfile.mkdtemp(prefix="sap_att_test_")
        with open(os.path.join(self.filestore, "good.pdf"), "wb") as fh:
            fh.write(b"%PDF-fake")
        self.report = PipelineReport(pipeline_name="ir.attachment.importer")
        self.ctx = ETLContext(
            cr=None,
            env=self.env,
            _reporter=SimpleNamespace(current=self.report),
        )
        manifest = self.importer._missing_manifest_path(self.env)
        if os.path.exists(manifest):
            os.remove(manifest)

    def _att(self, name, absentry=1, **extra):
        vals = {
            "filename": name,
            "fileext": "pdf",
            "absentry": absentry,
            "_model_name": "res.partner",
            "_res_id": 42,
            "_filestore_path": self.filestore,
        }
        vals.update(extra)
        return vals

    def _transform(self, atts):
        return self.importer.transform_attachments(
            self.ctx, {"extract_attachments": atts}
        )

    def test_readable_file_transforms(self):
        vals = self._transform([self._att("good")])
        self.assertEqual(len(vals), 1)
        self.assertEqual(vals[0]["name"], "good.pdf")
        self.assertEqual(vals[0]["res_model"], "res.partner")
        self.assertEqual(vals[0]["res_id"], 42)
        self.assertEqual(vals[0]["sap_absentry"], 1)
        self.assertEqual(base64.b64decode(vals[0]["datas"]), b"%PDF-fake")
        self.assertFalse(self.report.details)
        self.assertFalse(
            os.path.exists(self.importer._missing_manifest_path(self.env))
        )

    def test_missing_file_reported(self):
        vals = self._transform(
            [
                self._att("good"),
                self._att(
                    "gone",
                    absentry=7,
                    trgtpath=r"\\SAPSRV\B1_SHR\Attachments",
                    srcpath=r"C:\temp",
                ),
            ]
        )
        # The good file still imports — one miss never aborts the batch.
        self.assertEqual(len(vals), 1)

        warnings = [d for d in self.report.details if d.level == "warning"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("gone.pdf", warnings[0].message)
        self.assertIn("res.partner", warnings[0].message)
        self.assertEqual(warnings[0].source_ref, "absentry 7")

        manifest = self.importer._missing_manifest_path(self.env)
        self.assertTrue(os.path.exists(manifest))
        with open(manifest, newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["absentry"], "7")
        self.assertEqual(row["model"], "res.partner")
        self.assertEqual(row["res_id"], "42")
        self.assertEqual(row["filename"], "gone.pdf")
        self.assertEqual(
            row["expected_path"], os.path.join(self.filestore, "gone.pdf")
        )
        self.assertEqual(row["sap_trgtpath"], r"\\SAPSRV\B1_SHR\Attachments")
        self.assertEqual(row["sap_srcpath"], r"C:\temp")
        self.assertEqual(row["reason"], "file not found")

    def test_manifest_appends_within_run(self):
        self._transform([self._att("gone1", absentry=11)])
        self._transform([self._att("gone2", absentry=12)])
        manifest = self.importer._missing_manifest_path(self.env)
        with open(manifest, newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual([r["absentry"] for r in rows], ["11", "12"])
