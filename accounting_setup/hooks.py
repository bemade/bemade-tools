# Copyright 2022 Benoît Vézina (it@bemade.org>)
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html).
import logging
from odoo import models, fields, api, SUPERUSER_ID

_logger = logging.getLogger(__name__)

ACCOUNT_GROUPS = [
    ("0", "0", "Outstanding Payment and Receipts"),
    ("110", "110", "Cash"),
    ("1111", "1111", "Trade Accounts Receivable"),
    ("1112", "1112", "Advance to an Employee"),
    ("1113", "1113", "Advance to a Shareholder"),
    ("1114", "1114", "Investment Tax Credit Receivable"),
    ("1115", "1115", "Forward contract receivable"),
    ("112", "112", "Income Tax Receivable"),
    ("113", "113", "Prepaid Expenses"),
    ("114", "114", "Inventory"),
    ("114003", "114004", "Equipment for Rent"),
    ("18", "18", "Long-Term Assets"),
    ("1801", "1807", "Property, Plant and Equipment"),
    ("1808", "1808", "Intangible Assets"),
    ("185", "185", "Future Income Tax Receivable"),
    ("211", "211", "Bank Loans"),
    ("2121", "2121", "Trade Accounts Payable"),
    ("2122", "2122", "Sales Taxes"),
    ("2123", "2123", "Accrued Liabilities"),
    ("214", "214", "Payroll Tax Payable"),
    ("215", "215", "Deferred Revenue"),
    ("216", "216", "Loan from a Shareholder"),
    ("217", "217", "Future Income Tax Payable"),
    ("218", "218", "Current Portion of Long-Term Debt"),
    ("219", "219", "Forward Contracts Payable"),
    ("25", "25", "Long-Term Debt"),
    ("31", "31", "Share Capital"),
    ("32", "32", "Premiums"),
    ("33", "33", "Retained Earnings"),
    ("34", "34", "Dividends"),
    ("36", "36", "Contributed Surplus"),
    ("411000", "411000", "Distribution"),
    ("411001", "411001", "Systems"),
    ("411002", "411002", "Service"),
    ("411003", "411003", "Delivery"),
    ("411005", "411005", "Transport"),
    ("411009", "411009", "Other"),
    ("421", "424", "Non-operating Income"),
    ("510000", "510000", "Distribution Costs"),
    ("510001", "510001", "Systems Costs"),
    ("510002", "510002", "Service Costs"),
    ("510003", "510009", "Other Cost of Goods Sold"),
    ("52", "52", "Variance Costs"),
    ("53", "53", "Miscellaneous Cost of Goods Sold"),
    ("612", "612", "Salaries and Employee Benefits"),
    ("613", "613", "Commissions to Agents"),
    ("614", "614", "Promotion, Representation and Travel Expenses"),
    ("615", "615", "Automotive Expenses and Transportation"),
    ("616", "616", "Insurance Expenses"),
    ("617", "617", "Maintenance and Repair"),
    ("618", "618", "Heating and Power"),
    ("619", "619", "Professional Fees"),
    ("620", "620", "Telecommunications"),
    ("621", "621", "Office Expenses"),
    ("622", "622", "Taxes and Permits"),
    ("623", "623", "Software Expense"),
    ("625", "625", "Warehouse Expense"),
    ("626", "626", "Advertising Expense"),
    ("627", "627", "Bank Charges"),
    ("628", "628", "Doubtful Accounts"),
    ("629", "629", "Amortization Expense"),
    ("630", "630", "Research and Development Expense"),
    ("679", "679", "Miscellaneous Expense"),
    ("68", "68", "Interest Expense"),
    ("9", "9", "Income Taxes"),
]

# Note: the order of mappings is important. Generally, shorter prefixes should appear first.
ACCOUNT_TYPE_MAPPINGS = [
    ("110000", "Bank"),
    ("113", "Prepaid Expenses"),
    ("114003", "Non-current Assets"),
    ("114004", "Non-current Assets"),
    ("18", "Non-current Assets"),
    ("1801", "Fixed Assets"),
    ("1802", "Fixed Assets"),
    ("212", "Payable"),
    ("212204", "Current Liability"),
    ("212205", "Current Liability"),
    ("216", "Non-current Liabilities"),
    ("217", "Non-current Liabilities"),
    ("250", "Non-current Liabilities"),
    ("253", "Non-current Liabilities"),
    ("254", "Non-current Liabilities"),
    ("411009", "Other Income"),
    ("42", "Other Income"),
    ("5", "Cost of Goods Sold"),
    ("629", "Depreciation"),
    ("911200", "Non-current Liabilities"),
]

TAX_GROUPS = [
    ("GST/HST", "TPS/TVH"),
    ("QST", "TVQ"),
    ("Taxes", "Taxes"),
    ("Included GST/HST", "TPS/TVH incluse"),
    ("Included QST", "TVQ incluse"),
]


def add_account_groups(cr):
    env = api.Environment(cr, SUPERUSER_ID, dict())
    existing_groups = env['account.group'].search([])
    for group in ACCOUNT_GROUPS:
        if not update_existing_account_group(existing_groups, group):
            env['account.group'].create({
                'code_prefix_start': group[0],
                'code_prefix_end': group[1],
                'name': group[2],
            })


def update_existing_account_group(existing_groups, group):
    for existing in existing_groups:
        if existing.code_prefix_start == group[0]:
            existing.code_prefix_end = group[1]
            existing.name = group[2]
            return True
    return False


def set_tax_groups(cr):
    env = api.Environment(cr, SUPERUSER_ID, dict())
    existing_groups = env['account.tax.group'].search([])
    taxes = env['account.tax'].search([])
    new_groups_dict = dict()
    # Create and translate the new groups
    for group in TAX_GROUPS:
        tax_group = env['account.tax.group'].create({"name": group[0]})
        new_groups_dict[group[0]] = tax_group
        translation = env['ir.translation'].search([('module', '=', 'account'),
                                                    ('name', '=', 'account.tax,name'),
                                                    ('src', '=', group[0])])
        if not translation:
            env['ir.translation'].create({
                'module': 'account',
                'name': 'account.tax,name',
                'src': group[0],
                'value': group[1],
                'type': 'model',
                'state': 'translated',
                })
            continue
        translation.value = group[1]
        translation.state = 'translated'
    # Associate the existing taxes to the appropriate new group
    for tax in taxes:
        if "INCLUDED GST" in tax.name or "INCLUDED HST" in tax.name:
            tax.tax_group_id = new_groups_dict['Included GST/HST']
        elif "INCLUDED PST" in tax.name:
            tax.tax_group_id = new_groups_dict['Included QST']
        elif "GST" in tax.name or "HST" in tax.name:
            tax.tax_group_id = new_groups_dict['GST/HST']
        elif "PST" in tax.name or "QST" in tax.name:
            tax.tax_group_id = new_groups_dict['QST']
        else:
            tax.tax_group_id = new_groups_dict['Taxes']
    # Delete the old groups
    existing_groups.unlink()


def update_accounts(cr):
    env = api.Environment(cr, SUPERUSER_ID, dict())
    # delete the old "view" accounts and the corresponding account type
    env['account.account'].search([('user_type_id.name', '=', 'View')]).unlink()
    env['account.account.type'].search([('name', '=', 'View')]).unlink()

    # Clean up the old types and their organization
    env['account.account.type'].search([('name', '=', 'Asset')]).name = 'Current Asset'
    env['account.account.type'].search([('name', '=', 'Liability')]).name = 'Current Liability'
    env['account.account.type'].search([('name', '=', 'Cost of Revenue')]).name = 'Cost of Goods Sold'
    env['account.account.type'].search([('name', '=', 'Capital')]).internal_group = 'equity'
    prepayment = env['account.account.type'].search([('name', '=', 'Prepayments')])
    prepayment.name = 'Prepaid Expenses'

    # set the outstanding accounts as a bank & cash type
    bank_type = env['account.account.type'].search([('name', '=', 'Bank')])
    outstanding_accounts = env['account.account'].search([('code', '=like', '00%')])
    for a in outstanding_accounts:
        a.user_type_id = bank_type

    # map the accounts to their appropriate types
    account_types_rs = env['account.account.type'].search([])
    account_types = dict(zip(account_types_rs.mapped('name'), account_types_rs))
    for prefix, account_type in ACCOUNT_TYPE_MAPPINGS:
        accounts = env['account.account'].search([('code', '=like', prefix+'%')])
        for account in accounts:
            account.user_type_id = account_types[account_type]
            if account.user_type_id.type in ('payable', 'receivable'):
                account.reconcile = True

    # clean up unused account types
    used_types = env['account.account'].search([]).mapped('user_type_id')
    unused_types = env['account.account.type'].search([]).filtered(lambda r: r not in used_types)
    unused_types.unlink()


def remove_self(cr):
    env = api.Environment(cr, SUPERUSER_ID, dict())
    module = env["ir.module.module"].search([('name', '=', 'accounting_setup')])
    module.button_immediate_uninstall()


def post_init_hook(cr, registry):  # pragma: no cover
    add_account_groups(cr)
    set_tax_groups(cr)
    update_accounts(cr)
    cr.commit()
    remove_self(cr)
