from odoo import models, fields, api, _, Command
from odoo.exceptions import ValidationError
from odoo.tools import mute_logger
import logging
from typing import List, Set, Tuple
from pprint import pprint

_logger = logging.getLogger(__name__)


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    @api.model
    def fix(self):
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
        # Start by working on a copy of the account.move.line table
        self.env.cr.execute("DROP TABLE IF EXISTS durpro_fix_aml")
        self.env.cr.execute("CREATE TABLE durpro_fix_aml AS TABLE account_move_line")
        _logger.info("Checking debit credit balances between copied table and original before proceeding.")
        if not self._check_debit_credit_balances():
            return

        _logger.info("Getting Journal Entries with more than one currency")
        self.env.cr.execute("""
            select am.id as move_id, am.currency_id as move_currency_id
            from durpro_fix_aml aml
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
                inner join durpro_fix_aml aml on am.id = aml.move_id
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
        to_merge = self._run_updates(counterpart_lines)
        # Check debit and credit balances
        if not self._check_debit_credit_balances():
            return

        # Get the account.payment entries that have more than 1 liquidity line
        _logger.info("Getting Liquidity Lines.")
        self.env.cr.execute("""
            select am.id, aml.account_id, am.currency_id as currency_id, string_agg(distinct(aml.id)::text, ',') as aml_ids, 
                sum(aml.debit) as debit, sum(aml.amount_currency) as amount_currency, sum(aml.credit) as credit
            from account_move am 
                inner join durpro_fix_aml aml on am.id = aml.move_id
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
        to_merge.append(self._run_updates(liquidity_lines))
        # Check debit and credit balances
        if not self._check_debit_credit_balances():
            return
        # Check that all entries are now ok
        sql = """
            select am.id, am.name, count(aml.id), a.internal_type, string_agg(a.name, ', ')
            from durpro_fix_aml aml
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
        _logger.info("Balances match. Committing changes and merging lines.")
        self.env.cr.execute(""" UPDATE account_move_line FROM durpro_fix_aml aml SET account_move_line.debit = aml.debit,
                                account_move_line.credit = aml.credit, account_move_line.amount_currency = aml.amount_currency
                                WHERE account_move_line.id = aml.id""")
        self._merge_entries(set(to_merge))
        _logger.info("Done merging and committing. Cleaning up.")
        self.env.cr.execute("DROP TABLE IF EXISTS durpro_fix_aml")
        self.env.cr.commit()

    @api.model
    def _run_updates(self, lines) -> List[Tuple[int, Set[int]]]:
        updates = []
        for line in lines:
            move_line_id = int(line['aml_ids'].split(',')[0])
            line_ids_to_merge = set(int(line_id) for line_id in line['aml_ids'].split(',')[1:])
            if not line_ids_to_merge:
                continue
            debit = max(line['debit'] - line['credit'], 0)
            credit = max(line['credit'] - line['debit'], 0)
            amount_currency = self._get_amount_currency(line)
            updates.append({
                'move_line_id': move_line_id,
                'line_ids_to_merge': line_ids_to_merge,
                'debit': debit,
                'credit': credit,
                'amount_currency': amount_currency,
            })
        _logger.info("Updating values for lines to keep ...")
        self._update_keeper_lines(updates)
        _logger.info("Deleting extra lines ...")
        self._delete_extra_lines(updates)
        return [(update['move_line_id'], update['line_ids_to_merge']) for update in updates]

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
        # Update all the keepers from the temporary table
        sql = """
            UPDATE durpro_fix_aml aml set debit = k.debit, credit = k.credit, amount_currency = k.amount_currency
            FROM durpro_fix_aml_keepers k
            WHERE aml.id = k.move_line_id"""
        self.env.cr.execute(sql)
        self.env.cr.execute("DROP TABLE IF EXISTS durpro_fix_aml_keepers")

    def _delete_extra_lines(self, updates):
        extra_line_ids = [line_id for update in updates for line_id in update['line_ids_to_merge']]
        sql = """DELETE FROM durpro_fix_aml WHERE id in %s""" % str(tuple(extra_line_ids))
        self.env.cr.execute(sql)

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
        "durpro_fix_aml_check_amount_currency_balance_sign" CHECK
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
            FROM durpro_fix_aml aml
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
        sql = """UPDATE durpro_fix_aml SET currency_id = v.currency_id, amount_currency = v.amount_currency
                 FROM (VALUES %s) AS v(id, currency_id, amount_currency) WHERE v.id = durpro_fix_aml.id""" % args_str
        self.env.cr.execute(sql)

    @api.model
    def _check_debit_credit_balances(self) -> bool:
        """ """
        self.env.cr.execute("SELECT sum(debit) as debits, sum(credit) as credits FROM durpro_fix_aml")
        new_balances = self.env.cr.dictfetchone()
        self.env.cr.execute("SELECT sum(debit) as debits, sum(credit) as credits FROM account_move_line")
        old_balances = self.env.cr.dictfetchone()
        new_debit = round(new_balances['debits'], 2)
        new_credit = round(new_balances['credits'], 2)
        old_debit = round(old_balances['debits'], 2)
        old_credit = round(old_balances['credits'], 2)
        if new_debit != old_debit or new_credit != old_credit:
            _logger.error(f"Mismatch detected in debit and credit totals. Debit difference is {new_debit - new_debit}, "
                          f"credit difference is {new_credit - old_credit}"
                          f"Calculating biggest discrepancies before aborting.")
            sql = """
                SELECT new.move_id, (sum(new.debit) - sum(old.debit)) as debit_diff, 
                       (sum(new.credit) - sum(old.credit)) as credit_diff
                FROM durpro_fix_aml new INNER JOIN account_move_line old ON new.move_id = old.move_id
                GROUP BY new.move_id
                HAVING sum(new.credit) != sum(old.credit) OR sum(new.debit) != sum(old.debit)
                ORDER BY credit_diff DESC, debit_diff DESC
                LIMIT 10
            """
            self.env.cr.execute(sql)
            discrepancies = self.env.cr.dictfetchall()
            pprint(discrepancies)
            self.env.cr.rollback()
            return False
        _logger.info("Debit and credit balances match between new and old entries.")
        return True
