import logging
import multiprocessing
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from typing import Dict, List, Tuple, Any

from odoo import api, fields, models
from odoo.addons.sap_b1_to_odoo.tools import fix_quotes
from odoo.modules.registry import Registry
from odoo.sql_db import SQL

_logger = logging.getLogger(__name__)

MAX_WORKERS = 8


class ProductTemplate(models.Model):
    _inherit = "product.template"

    sap_item_code = fields.Char(index="btree", copy=False)
    sap_atcentry = fields.Integer(copy=False)
    _sql_constraints = [
        (
            "sap_item_code_unique",
            "unique (sap_item_code)",
            "A product with that SAP item code already exists.",
        )
    ]


class ProductCategory(models.Model):
    _inherit = "product.category"

    sap_itms_grp_cod = fields.Integer(index="btree", copy=False)
    _sql_constraints = [
        (
            "sap_itms_grp_cod_unique",
            "unique (sap_itms_grp_cod)",
            "A product category with that SAP code already exists.",
        )
    ]
