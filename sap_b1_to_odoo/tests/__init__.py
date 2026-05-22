from . import pipelines, test_sale_order_etl, test_itr_pipeline, test_res_company_etl
# Re-export pipeline test modules with test_ names so Odoo's test loader
# (which only picks up top-level members whose names start with 'test_') can
# discover them without requiring an explicit --test-tags flag.
from .pipelines import (
    test_account_move_jdt1_etl,
    test_jdt1_sale_link,
    test_product_pricelist_etl,
    test_product_product_etl,
    test_product_product_etl_description_ecommerce,
    test_res_partner_payment_term,
    test_res_partner_pricelist,
)
