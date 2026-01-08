import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class ProductPricelist(models.Model):
    _inherit = "product.pricelist"

    sap_abs_id = fields.Integer(
        index="btree",
    )
    sap_loginstanc = fields.Integer(index="btree")
    sap_listnum = fields.Integer(index="btree")  # ID in OPLN table

    _sap_abs_id_loginstanc_exclude = models.Constraint(
        "EXCLUDE USING btree (sap_abs_id WITH =, sap_loginstanc WITH =) WHERE (sap_abs_id != 0 AND sap_loginstanc != 0)",
        "sap_abs_id and sap_loginstance must be unique together when both are set",
    )

    @api.model_create_multi
    def create(self, vals_list):
        res = super().create(vals_list)
        _logger.info(f"Created {len(res)} pricelists.")
        return res
