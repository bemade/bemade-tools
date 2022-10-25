# Copyright 2022 Benoît Vézina (it@bemade.org>)
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html).
import logging
from odoo import models, fields, api, SUPERUSER_ID
from odoo.modules.module import get_module_path

_logger = logging.getLogger(__name__)


def remove_self(cr):
    env = api.Environment(cr, SUPERUSER_ID, dict())
    module = env["ir.module.module"].search([('name', '=', 'modules_cleaner')])
    module.button_immediate_uninstall()


def fix_res_currency_rate_unique_name_per_day(cr):
    cr.execute(
        """
        DELETE FROM res_currency_rate R1
            USING res_currency_rate R2
        WHERE R1.id < R2.id
            AND R1.name = R2.name
            AND R1.currency_id = R2.currency_id
            AND R1.company_id = R2.company_id;
    """
    )


def fix_hr_work_location_not_null_address_id(cr):
    cr.execute(
        """
        UPDATE hr_work_location
        SET address_id = res_company.partner_id FROM res_company
        WHERE res_company.id = 1 AND hr_work_location.address_id is Null;
    """
    )


def fix_product_packaging_not_null_name(cr):
    cr.execute(
        """
        UPDATE product_packaging
        SET name = 'BOXES'
        WHERE product_packaging.name is Null;
    """
    )


def fix_hr_leave_allocation_duration_check(cr):
    cr.execute(
        """
        DELETE FROM public.hr_leave_allocation
        WHERE number_of_days = 0 AND allocation_type='regular';
    """
    )


def fix_account_payment_check_amount_not_negative(cr):
    cr.execute(
        """
        UPDATE account_payment
        SET (amount, payment_type) = (-amount, 'inbound') 
        WHERE amount < 0 AND payment_type = 'outbound';
    """
    )
    cr.execute(
        """
        UPDATE account_payment
        SET (amount, payment_type) = (-amount, 'outbound') 
        WHERE amount < 0 AND payment_type = 'inbound';
    """
    )


def remove_module_not_available(cr):
    env = api.Environment(cr, SUPERUSER_ID, dict())
    modules = env["ir.module.module"].search([])
    for module in modules:
        if not get_module_path(module.name, display_warning=False):
            if module.state == 'uninstalled':
                _logger.info(f"DELETING uninstalled module {module.name} : PATH NOT FOUND!")
                module.unlink()
            elif module.state == 'uninstallable':
                _logger.info(f"DELETING uninstallable module {module.name} : PATH NOT FOUND!")
                module.unlink()
            else:
                _logger.warning(f"WHAT STATE IS IT {module.name} ====> {module.state}")


def post_init_hook(cr, registry):  # pragma: no cover
    # Set all pre-existing pages history to approved
    remove_module_not_available(cr)

    fix_res_currency_rate_unique_name_per_day(cr)
    fix_hr_work_location_not_null_address_id(cr)
    fix_product_packaging_not_null_name(cr)
    fix_hr_leave_allocation_duration_check(cr)
    fix_account_payment_check_amount_not_negative(cr)

    remove_self(cr)

    # cr.execute(
    #     """
    #     UPDATE document_page_history
    #     SET state='approved',
    #         approved_uid=create_uid,
    #         approved_date=create_date
    #     WHERE state IS NULL OR state = 'draft'
    # """
    # )


