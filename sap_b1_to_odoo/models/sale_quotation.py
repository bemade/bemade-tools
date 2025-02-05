from odoo import models, api
from odoo.addons.sap_b1_to_odoo.tools import PagingIterator
import logging

_logger = logging.getLogger(__name__)


class SapSaleQuotationImporter(models.AbstractModel):
    _name = "sap.sale.quotation.importer"
    _description = "SAP Sales Quotation Importer"
    _inherit = ["sap.sale.order.importer"]

    # Configuration
    _sap_header_table = "oqut"
    _sap_lines_table = "qut1"
    _sap_text_lines_table = "qut10"

    @api.model
    def import_quotations(self, cr):
        return self._import_quotations(cr)

    @api.model
    def _import_quotations(self, cr):
        """Import quotations from SAP"""
        imported_docnums = tuple(self._get_imported_docnums())
        where = """
        where docentry not in (
        select baseentry from rdr1 where basetype = 23
        )
        """
        args = []
        if imported_docnums:
            where += " and docnum not in %s"
            args = [imported_docnums]

        quote_pager = PagingIterator(
            cr,
            fetch_query=f"select * from {self._sap_header_table} {where}",
            fetch_args=args,
            count_query=f"select count(*) from {self._sap_header_table} {where}",
            count_args=args,
            limit=500,
            orderby="docentry",
            logger=_logger,
        )

        _logger.info("Creating quotations.")
        self._create_orders(cr, quote_pager)
        _logger.info("Cancelling canceled quotations.")
        self._cancel_canceled_orders(cr)
        self._set_order_dates(cr)
        self.env[self._odoo_model].flush_model()
        self.env.cr.commit()
