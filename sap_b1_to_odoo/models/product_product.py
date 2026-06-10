from odoo import api, fields, models
from odoo.fields import Domain


class ProductTemplate(models.Model):
    _inherit = "product.template"

    sap_item_code = fields.Char(index="btree", copy=False)
    sap_atcentry = fields.Integer(copy=False)

    _rec_names_search = ["name", "default_code", "barcode", "sap_item_code"]

    _sap_item_code_unique = models.Constraint(
        "unique (sap_item_code)",
        "A product with that SAP item code already exists.",
    )


class ProductProduct(models.Model):
    _inherit = "product.product"

    @api.model
    def name_search(self, name='', domain=None, operator='ilike', limit=100):
        # Odoo 19 defines a fully custom name_search on product.product that
        # hardcodes searches on default_code / barcode / name and never reads
        # _rec_names_search.  We extend it here to also cover sap_item_code.
        results = super().name_search(name, domain, operator, limit)
        positive_operators = ['=', 'ilike', '=ilike', 'like', '=like']
        if name and operator in positive_operators:
            existing_ids = {r[0] for r in results}
            sap_domain = Domain(domain or Domain.TRUE) & Domain('sap_item_code', operator, name)
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
