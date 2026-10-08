"""Which Sage documents the document importer takes.

Acceptance criteria
-------------------
- Balances-only and history take-ons without the closed-documents option take
  only the documents with something outstanding (unchanged behaviour).
- With `import_closed_documents`, every document dated on or after the
  history start is taken, settled or not.
- A settled document dated before the history start is left out: its balance
  and its settlement are both inside the opening entry. An *open* one is still
  taken, so the loader reports it as it always has instead of losing it.
- Customer and vendor documents are numbered from separate counters, so
  "already imported" is decided on (side, Sage id) — a vendor bill must not be
  skipped because a customer invoice shares its id.
"""

from odoo.tests import TransactionCase, tagged

from odoo.addons.etl_framework import ETLContext


@tagged("post_install", "-at_install")
class TestDocumentScope(TransactionCase):

    def _importer(self, **config):
        ctx = ETLContext(cr=None, env=self.env, source_config=config)
        return self.env["sage.open.item.importer"], ctx

    def test_open_only_without_the_option(self):
        importer, ctx = self._importer(history_start_date="2021-09-01")
        in_scope = importer._in_scope
        self.assertTrue(in_scope(ctx, residual=12.5, date="2022-01-10",
                                 start="2021-09-01"))
        self.assertFalse(in_scope(ctx, residual=0.0, date="2022-01-10",
                                  start="2021-09-01"))

    def test_closed_documents_from_the_history_start(self):
        importer, ctx = self._importer(
            history_start_date="2021-09-01", import_closed_documents=True,
        )
        in_scope = importer._in_scope
        self.assertTrue(in_scope(ctx, residual=0.0, date="2021-09-01",
                                 start="2021-09-01"))
        self.assertTrue(in_scope(ctx, residual=0.004, date="2023-06-30",
                                 start="2021-09-01"))

    def test_settled_document_before_the_start_is_left_out(self):
        importer, ctx = self._importer(
            history_start_date="2021-09-01", import_closed_documents=True,
        )
        self.assertFalse(importer._in_scope(
            ctx, residual=0.0, date="2021-08-31", start="2021-09-01",
        ))

    def test_open_document_before_the_start_is_still_taken(self):
        importer, ctx = self._importer(
            history_start_date="2021-09-01", import_closed_documents=True,
        )
        self.assertTrue(importer._in_scope(
            ctx, residual=503.57, date="2021-02-12", start="2021-09-01",
        ))

    def test_already_imported_is_keyed_by_side(self):
        partner = self.env["res.partner"].create({"name": "Scope partner"})
        invoice = self.env["account.move"].create({
            "move_type": "out_invoice",
            "partner_id": partner.id,
            "sage_doc_id": 42,
        })
        importer, ctx = self._importer()
        ctx.source_config["company_id"] = invoice.company_id.id
        already = importer._already_imported(ctx)
        self.assertIn(("customer", 42), already)
        self.assertNotIn(("vendor", 42), already)
