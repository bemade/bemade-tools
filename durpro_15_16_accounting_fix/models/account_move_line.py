from odoo import models, fields, api, _, Command
from odoo.exceptions import ValidationError
from odoo.tools import mute_logger
import logging

_logger = logging.getLogger(__name__)


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    @api.model
    def fix_problem_account_payments(self):
        """
        The following account.payment entries are problematic for migration:
            * Have more than 1 currency in the lines
            * Have more than 1 liquidity line having:
                line.journal_id.default_account_id |
                line.payment_method_line_id.payment_account_id |
                line.journal_id.company_id.account_journal_payment_debit_account_id |
                line.journal_id.company_id.account_journal_payment_credit_account_id |
                line.journal_id.inbound_payment_method_line_ids.payment_account_id |
                line.journal_id.outbound_payment_method_line_ids.payment_account_id
            * Have more than 1 counterpart line having:
                line.account_id.account_type in ('receivable', 'payable') |
                line.account_id == company_id.transfer_account_id
        :return:
        """
        _logger.info("Getting Journal Entries with more than one currency")
        self.env.cr.execute("""
            select am.id as move_id, am.currency_id as move_currency_id
            from account_move_line aml
                inner join account_move am on am.id=aml.move_id
                inner join account_account a on aml.account_id=a.id
            where am.state not in ('draft','cancel')
            group by
                am.id,
                am.currency_id
            having count(distinct aml.currency_id) > 1
            """)
        lines = self.env.cr.dictfetchall()
        if lines:
            _logger.info(f"Found {self.env.cr.rowcount} problematic entries")
            self._fix_double_currency(lines)
        else:
            _logger.info(f"No entries with two or more currencies found.")

        _logger.info("Getting Counterpart Lines")
        # Get the move lines grouped by account to see which ones we can match up
        self.env.cr.execute("""
            select am.id, aml.account_id, am.currency_id as currency_id, string_agg(distinct(aml.id)::text, ',') as aml_ids, 
                sum(aml.debit) as debit, sum(aml.amount_currency) as amount_currency, sum(aml.credit) as credit
            from account_move am 
                inner join account_move_line aml on am.id = aml.move_id
                inner join account_account a on aml.account_id = a.id
                inner join res_company company on am.company_id = company.id
                inner join account_journal j on am.journal_id = j.id
            where j.type in ('bank', 'cash') 
                and (internal_type in ('receivable', 'payable')
                    or aml.account_id = company.transfer_account_id)
            group by aml.account_id, am.id, am.currency_id
            having count(*) > 1
        """)
        counterpart_lines = self.env.cr.dictfetchall()
        # Merge each group of counterpart lines into a single line
        _logger.info(f"Updating {len(counterpart_lines)} Counterpart Lines")
        self._run_updates(counterpart_lines)

        # Get the account.payment entries that have more than 1 liquidity line
        _logger.info("Getting Liquidity Lines.")
        self.env.cr.execute("""
            select am.id, aml.account_id, am.currency_id as currency_id, string_agg(distinct(aml.id)::text, ',') as aml_ids, 
                sum(aml.debit) as debit, sum(aml.amount_currency) as amount_currency, sum(aml.credit) as credit
            from account_move am 
                inner join account_move_line aml on am.id = aml.move_id
                inner join account_payment ap on ap.move_id = am.id
                inner join account_payment_method_line mapml on ap.payment_method_line_id = mapml.id
                inner join account_account a on aml.account_id = a.id
                inner join account_journal j on am.journal_id = j.id
                inner join account_payment_method_line japml on japml.journal_id = j.id
                inner join res_company company on j.company_id = company.id
            where  j.type in ('bank', 'cash')
                and (aml.account_id = j.default_account_id
                    or mapml.payment_account_id = aml.account_id
                    or company.account_journal_payment_debit_account_id = aml.account_id
                    or company.account_journal_payment_credit_account_id = aml.account_id
                    or japml.payment_account_id = aml.account_id)
            group by aml.account_id, am.id, am.currency_id
            having count(*) > 1
        """)
        liquidity_lines = self.env.cr.dictfetchall()
        _logger.info(f"Updating {len(liquidity_lines)} Liquidity Lines")
        self._run_updates(liquidity_lines)

        # Check that all entries are now ok

        sql = """
            select am.id, am.name, count(aml.id), a.internal_type, string_agg(a.name, ', ')
            from account_move_line aml
                inner join account_move am on am.id=aml.move_id
                inner join account_account a on a.id=aml.account_id
            group by
                am.id,
                am.name,
                a.internal_type
            having
                count(aml.id) > 1 and a.internal_type in ('payable', 'receivable') and am.name ilike '%2023%';
        """
        self.env.cr.execute(sql)
        lines = self.env.cr.dictfetchall()
        if lines:
            _logger.warning(f"There are still {len(lines)} problematic entries")
        else:
            _logger.info("All journal entries have been fixed.")
        self.env.cr.commit()

    def _run_updates(self, lines):
        updates = [
            {
                'move_line_id': int(line['aml_ids'].split(',')[0]),
                'line_ids_to_merge': set(int(line_id) for line_id in line['aml_ids'].split(',')[1:]),
                'debit': max(line['debit'] - line['credit'], 0),
                'credit': max(line['credit'] - line['debit'], 0),
                #  Make sure that te sign on amount_currency matches the new debit/credit balance
                'amount_currency': self._get_amount_currency(line),
            }
            for line in lines]
        _logger.info("Updating values for lines to keep ...")
        self._update_keeper_lines(updates)
        _logger.info("Merging lines to delete ...")
        self._merge_entries(updates)

    def _update_keeper_lines(self, updates) -> None:
        """ Update the keeper lines with the new debit/credit/amount_currency

        :param updates: A list of dictionaries with the following keys:
            move_line_id: The id of the keeper line
            debit: The new debit balance
            credit: The new credit balance
            amount_currency: The new amount_currency balance
            line_ids_to_merge: A list of ids of the lines to merge into the keeper line
        """
        # Insert the keepers into a temporary table
        self.env.cr.execute("DROP TABLE IF EXISTS durpro_fix_aml_keepers")
        self.env.cr.execute("""CREATE TABLE durpro_fix_aml_keepers (move_line_id int, debit numeric, credit numeric, 
                                    amount_currency numeric)""")
        tups = ((update['move_line_id'], update['debit'], update['credit'], update['amount_currency']) for update in
                updates)
        mog = (self.env.cr.mogrify("(%s,%s,%s,%s)", tup).decode('utf-8') for tup in tups)
        args_str = ','.join(mog)
        self.env.cr.execute("INSERT INTO durpro_fix_aml_keepers (move_line_id, debit, credit, amount_currency) VALUES"
                            + args_str)
        # Update all the keepers from the temporary table
        sql = """
            UPDATE account_move_line aml set debit = k.debit, credit = k.credit, amount_currency = k.amount_currency
            FROM durpro_fix_aml_keepers k
            WHERE aml.id = k.move_line_id"""
        self.env.cr.execute(sql)
        self.env.cr.execute("DROP TABLE IF EXISTS durpro_fix_aml_keepers")

    @api.model
    def _merge_entries(self, updates):
        """ Merge the lines to delete into the keeper line, keeping all foreign key references across the DB intact.

        :param updates: A list of dictionaries with the following keys:
            move_line_id: The id of the keeper line
            debit: The new debit balance
            credit: The new credit balance
            amount_currency: The new amount_currency balance
            line_ids_to_merge: A list of ids of the lines to merge into the keeper line
        """
        # Create a temporary table to hold the ids of the lines to delete and relate them to the "keeper" id
        self.env.cr.execute("DROP TABLE IF EXISTS durpro_fix_aml_to_delete")
        self.env.cr.execute("CREATE TABLE durpro_fix_aml_to_delete (move_line_id int primary key, keeper_id int)")
        tups = ((line_id, update['move_line_id']) for update in updates for line_id in update['line_ids_to_merge'])
        mog = (self.env.cr.mogrify("(%s,%s)", tup).decode('utf-8') for tup in tups)
        args_str = ','.join(mog)
        self.env.cr.execute("INSERT INTO durpro_fix_aml_to_delete (move_line_id, keeper_id) VALUES" + args_str)
        self._merge_delete_lines()

    @api.model
    def _merge_delete_lines(self):
        sql = """
            SELECT tc.table_name "FK_tbl_name",
                   string_agg(kcu.column_name, ',') "FK_col_names"
            FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name
                JOIN information_schema.constraint_column_usage ccu ON ccu.constraint_name = tc.constraint_name
            WHERE constraint_type = 'FOREIGN KEY'
                AND ccu.table_name='account_move_line'
            GROUP BY tc.table_name"""
        self.env.cr.execute(sql)
        used_ref_table_list = self._cr.dictfetchall()

        # Update reference into table.
        for table in used_ref_table_list:
            fk_table = table.get('FK_tbl_name')
            fk_cols = table.get('FK_col_names').split(',')
            for column in fk_cols:
                params = {
                    'table': fk_table,
                    'column': column,
                }
                sql = """
                    UPDATE %(table)s as main1
                    SET %(column)s = links.keeper_id
                    FROM durpro_fix_aml_to_delete as links 
                    WHERE main1.%(column)s = links.move_line_id
                """ % params
                self.env.cr.execute(sql)
        self.env.cr.execute("""DELETE FROM account_move_line 
                               WHERE id in (SELECT move_line_id FROM durpro_fix_aml_to_delete)""")

    @api.model
    def _get_amount_currency(self, line, new_currency_id=None):
        """ Work with the following constraint:
        "account_move_line_check_amount_currency_balance_sign" CHECK
            (currency_id <> company_currency_id AND ((debit - credit) <= 0::numeric AND amount_currency <= 0::numeric
                OR (debit - credit) >= 0::numeric AND amount_currency >= 0::numeric)
                OR currency_id = company_currency_id AND round(debit - credit - amount_currency, 2) = 0::numeric
            )

        :param line: A dictionary with the following keys:
            debit: The debit balance
            credit: The credit balance
            amount_currency: The amount_currency balance
            currency_id: The currency id
        :param new_currency_id: The currency id to use as the currency of the line.
            Use when updating line's currency.
        :return: The amount_currency balance with the correct sign
        """
        new_currency_id = line['currency_id'] if not new_currency_id else new_currency_id
        if new_currency_id == 4:
            return round(line['debit'] - line['credit'], 2)
        else:
            line_balance_positive = line['debit'] - line['credit'] > 0
            if line_balance_positive:
                return abs(line['amount_currency'])
            else:
                return -abs(line['amount_currency'])

    def _fix_double_currency(self, lines) -> None:
        """
        Fix the double currency problem by switching to the journal entry currency for all lines in the journal entry.

        :param lines: A list of dictionaries with at least the keys "move_id" and "move_currency_id" representing the
                      moves to fix
        """
        move_ids = str(tuple(line['move_id'] for line in lines))
        sql = """
            SELECT aml.id as line_id, aml.currency_id as currency_id, aml.move_id as move_id, aml.debit as debit,
                   aml.credit as credit, aml.amount_currency as amount_currency
            FROM account_move_line aml
            WHERE aml.move_id in %s
        """ % move_ids
        self.env.cr.execute(sql)
        move_currency_dict = {line['move_id']: line['currency_id'] for line in lines}
        move_lines = self.env.cr.dictfetchall()
        updates = (
            {
                'id': line['line_id'],
                'currency_id': move_currency_dict[line['move_id']],
                'amount_currency': self._get_amount_currency(line, move_currency_dict[line['move_id']]),
            }
            for line in move_lines if line['currency_id'] != move_currency_dict[line['move_id']]
        )
        tups = [(update['id'], update['currency_id'], update['amount_currency']) for update in updates]
        mog = (self.env.cr.mogrify("(%s,%s,%s)", tup).decode('utf-8') for tup in tups)
        args_str = ','.join(mog)
        sql = """UPDATE account_move_line SET currency_id = v.currency_id, amount_currency = v.amount_currency
                 FROM (VALUES %s) AS v(id, currency_id, amount_currency) WHERE v.id = account_move_line.id""" % args_str
        self.env.cr.execute(sql)
