from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = "product.template"

    sap_item_code = fields.Char(index="btree", copy=False)
    sap_atcentry = fields.Integer(copy=False)

    _sap_item_code_unique = models.Constraint(
        "unique (sap_item_code)",
        "A product with that SAP item code already exists.",
    )


class ProductCategory(models.Model):
    _inherit = "product.category"

    sap_itms_grp_cod = fields.Integer(index="btree", copy=False)

    _sap_itms_grp_cod_unique = models.Constraint(
        "unique (sap_itms_grp_cod)",
        "A product category with that SAP code already exists.",
    )
