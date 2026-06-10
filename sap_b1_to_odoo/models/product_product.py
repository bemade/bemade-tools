from odoo import api, fields, models
from odoo.fields import Domain

_POSITIVE_OPERATORS = ['=', 'ilike', '=ilike', 'like', '=like']


class ProductTemplate(models.Model):
    _inherit = "product.template"

    sap_item_code = fields.Char(index="btree", copy=False)
    sap_atcentry = fields.Integer(copy=False)

    _rec_names_search = ["name", "default_code", "barcode", "sap_item_code"]

    _sap_item_code_unique = models.Constraint(
        "unique (sap_item_code)",
        "A product with that SAP item code already exists.",
    )

    @api.model
    def name_search(self, name='', args=None, operator='ilike', limit=100):
        # Odoo 19's product.template.name_search delegates to Model.name_search
        # which uses _rec_names_search (so sap_item_code is already covered via
        # _search_display_name).  We add an explicit fallback that mirrors the
        # product.product override to be robust against other modules overriding
        # _rec_names_search or _search_display_name on this model.
        results = super().name_search(name, args, operator, limit)
        if name and operator in _POSITIVE_OPERATORS:
            existing_ids = {r[0] for r in results}
            sap_domain = Domain(args or Domain.TRUE) & Domain('sap_item_code', operator, name)
            sap_templates = self.search_fetch(sap_domain, ['display_name'], limit=limit)
            for tmpl in sap_templates.sudo():
                if tmpl.id not in existing_ids:
                    results.append((tmpl.id, tmpl.display_name))
                    existing_ids.add(tmpl.id)
            if limit:
                results = results[:limit]
        return results


class ProductProduct(models.Model):
    _inherit = "product.product"

    @api.model
    def name_search(self, name='', args=None, operator='ilike', limit=100):
        # Odoo 19 defines a fully custom name_search on product.product that
        # hardcodes searches on default_code / barcode / name and never reads
        # _rec_names_search.  We extend it here to also cover sap_item_code.
        # sap_item_code lives on product.template; the search traverses the
        # delegation join automatically (product.product → product.template).
        results = super().name_search(name, args, operator, limit)
        if name and operator in _POSITIVE_OPERATORS:
            existing_ids = {r[0] for r in results}
            sap_domain = Domain(args or Domain.TRUE) & Domain('sap_item_code', operator, name)
            sap_products = self.search_fetch(sap_domain, ['display_name'], limit=limit)
            for product in sap_products.sudo():
                if product.id not in existing_ids:
                    results.append((product.id, product.display_name))
                    existing_ids.add(product.id)
            if limit:
                results = results[:limit]
        return results


class ProductCategory(models.Model):
    _inherit = "product.category"

    sap_itms_grp_cod = fields.Integer(index="btree", copy=False)

    _sap_itms_grp_cod_unique = models.Constraint(
        "unique (sap_itms_grp_cod)",
        "A product category with that SAP code already exists.",
    )
