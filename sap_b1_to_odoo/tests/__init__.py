from . import pipelines, test_sale_order_etl, test_itr_pipeline
# Re-export pipeline test modules at the top level so Odoo's test loader
# (which only picks up top-level members whose names start with 'test_') can
# discover all test classes without requiring an explicit --test-tags flag.
from .pipelines import test_jdt1_sale_link, test_account_move_jdt1_etl
