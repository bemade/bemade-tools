from odoo import api, fields, models


class ProductTemplate(models.Model):
    _name = "product.template"
    _inherit = ["product.template", "sap.search.mixin"]

    sap_item_code = fields.Char(index="btree", copy=False)
    sap_atcentry = fields.Integer(copy=False)

    _rec_names_search = ["name", "default_code", "barcode", "sap_item_code"]

    _SAP_SEARCH_FIELDS = ["sap_item_code"]

    _sap_item_code_unique = models.Constraint(
        "unique (sap_item_code)",
        "A product with that SAP item code already exists.",
    )

    @api.model
    def name_search(self, name='', domain=None, operator='ilike', limit=100):
        # Odoo 19's core product.template.name_search delegates to
        # Model.name_search (via _rec_names_search), so this thin stub is
        # only needed because this class's own def sits ABOVE the mixin in
        # the MRO (see sap_search_mixin.py docstring for the mode-2
        # rationale) — it must stay a def-level override to remain
        # outermost and robust against other modules overriding
        # _rec_names_search / _search_display_name on this model.
        result = super().name_search(name, domain, operator, limit)
        return self._sap_augment_name_search(result, name, domain, operator, limit)


class ProductProduct(models.Model):
    _name = "product.product"
    _inherit = ["product.product", "sap.search.mixin"]

    _SAP_SEARCH_FIELDS = ["sap_item_code"]

    @api.model
    def name_search(self, name='', domain=None, operator='ilike', limit=100):
        # Odoo 19 defines a fully custom name_search on product.product that
        # hardcodes searches on default_code / barcode / name and never
        # calls super() nor reads _rec_names_search, so it shadows the
        # mixin's own name_search in the MRO. This thin stub restores the
        # outermost seam: call core name_search, then delegate to the
        # shared mixin helper. sap_item_code lives on product.template; the
        # search traverses the delegation join automatically
        # (product.product -> product.template).
        result = super().name_search(name, domain, operator, limit)
        return self._sap_augment_name_search(result, name, domain, operator, limit)


class ProductCategory(models.Model):
    _inherit = "product.category"

    sap_itms_grp_cod = fields.Integer(index="btree", copy=False)

    _sap_itms_grp_cod_unique = models.Constraint(
        "unique (sap_itms_grp_cod)",
        "A product category with that SAP code already exists.",
    )
