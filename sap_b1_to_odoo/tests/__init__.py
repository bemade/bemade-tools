from . import pipelines, test_sale_order_etl, test_itr_pipeline
# Re-export test_jdt1_sale_link with a test_ name so Odoo's test loader
# (which only picks up top-level members whose names start with 'test_') can
# discover it without requiring an explicit --test-tags flag.
from .pipelines import test_jdt1_sale_link
