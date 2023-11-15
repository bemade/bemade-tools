from odoo import models, fields, api, _, Command
from odoo.exceptions import ValidationError
import logging
from typing import List, Set, Tuple
from psycopg2.errors import UniqueViolation
import re

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

            The idea is that if we grab any lines that have account types of 'payable', 'receivable' or 'liquidity' and
            make sure that there are no more than one line per account in a journal entry, we should be able to merge
            the lines together and get closer to correct entries for migration.
        :return:
        """
        self._copy_and_verify_aml_table()
        self._correct_multi_currency_entries()

        problem_lines = self._get_problem_account_move_lines()
        # Merge each group of counterpart lines into a single line
        if problem_lines:
            _logger.info(f"Updating {len(problem_lines)} problematic lines")
            to_merge = self._run_updates(problem_lines)
        else:
            _logger.info(f"Found no problematic entries. Moving on.")
        # Check debit and credit balances
        if not self._check_debit_credit_balances():
            return
        self._check_problematic_entries()
        _logger.info("Committing changes and merging lines.")
        self.env.cr.execute(""" UPDATE account_move_line SET debit = aml.debit, credit = aml.credit,  
                                    balance = aml.balance, amount_currency = aml.amount_currency
                                FROM durpro_fix_aml aml
                                WHERE account_move_line.id = aml.id""")
        self._merge_entries(to_merge)
        _logger.info("Done merging and committing. Cleaning up.")
        self.env.cr.execute("DROP TABLE IF EXISTS durpro_fix_aml")
        self.env.cr.execute("DROP TABLE IF EXISTS durpro_fix_aml_keepers")
        self.env.cr.execute("DROP TABLE IF EXISTS durpro_fix_aml_to_delete")
        self.env.cr.commit()
        _logger.info("Done cleaning up. Fix complete.")

    @api.model
    def _get_problem_account_move_lines(self):
        _logger.info("Getting problematic lines")
        # Get the move lines grouped by account to see which ones we can match up
        self.env.cr.execute("""
            select am.id as move_id, aml.account_id, am.currency_id as currency_id, string_agg(distinct(aml.id)::text, ',') as aml_ids, 
                sum(aml.debit) as debit, sum(aml.amount_currency) as amount_currency, sum(aml.credit) as credit
            from account_move am 
                inner join durpro_fix_aml aml on am.id = aml.move_id
                inner join account_account a on aml.account_id = a.id
                inner join res_company company on am.company_id = company.id
                inner join account_journal j on am.journal_id = j.id
            where (internal_type in ('receivable', 'payable', 'liquidity')
                    or aml.account_id = company.transfer_account_id)
            group by aml.account_id, am.id, am.currency_id
            having count(*) > 1
        """)
        problem_lines = self.env.cr.dictfetchall()
        return problem_lines

    @api.model
    def _correct_multi_currency_entries(self):
        _logger.info("Getting Journal Entries with more than one currency")
        self.env.cr.execute("""
            select am.id as move_id, am.currency_id as currency_id
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
            _logger.info(f"Found {self.env.cr.rowcount} currency mismatched.")
            self._fix_double_currency(lines)
        else:
            _logger.info(f"No entries with two or more currencies found.")

    @api.model
    def _copy_and_verify_aml_table(self):
        # Start by working on a copy of the account.move.line table
        self.env.cr.execute("DROP TABLE IF EXISTS durpro_fix_aml")
        self.env.cr.execute("CREATE TABLE durpro_fix_aml AS TABLE account_move_line")
        _logger.info("Checking debit credit balances between copied table and original before proceeding.")
        if not self._check_debit_credit_balances():
            raise ValidationError("Debit and credit balances do not match between copied table and original."
                                  " This is probably a programming error.")

    @api.model
    def _check_problematic_entries(self):
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

    @api.model
    def _run_updates(self, lines) -> List[Tuple[int, Set[int]]]:
        updates = []
        for line in lines:
            move_line_id = int(line['aml_ids'].split(',')[0])
            line_ids_to_merge = set(int(line_id) for line_id in line['aml_ids'].split(',')[1:])
            if not line_ids_to_merge:
                continue
            debit = round(max(line['debit'] - line['credit'], 0), 2)
            credit = round(max(line['credit'] - line['debit'], 0), 2)
            amount_currency = self._get_amount_currency(line)
            balance = round(debit - credit, 2)
            updates.append({
                'move_line_id': move_line_id,
                'line_ids_to_merge': line_ids_to_merge,
                'debit': debit,
                'credit': credit,
                'amount_currency': amount_currency,
                'balance': balance,
            })
        _logger.info("Updating values for lines to keep ...")
        self._update_keeper_lines(updates)
        _logger.info("Deleting extra lines ...")
        self._delete_extra_lines(updates)
        return [(update['move_line_id'], update['line_ids_to_merge']) for update in updates]

    @api.model
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
        self.env.cr.execute("""CREATE TABLE durpro_fix_aml_keepers (move_line_id int primary key, debit numeric, 
        credit numeric, amount_currency numeric, balance numeric)""")
        tups = tuple(
            (update['move_line_id'], update['debit'], update['credit'], update['amount_currency'], update['balance'])
            for update in updates)
        args_str = ','.join(str(tup) for tup in tups)
        self.env.cr.execute(
            "INSERT INTO durpro_fix_aml_keepers (move_line_id, debit, credit, amount_currency, balance) "
            "VALUES " + args_str)

        # Update all the keepers from the temporary table
        sql = """
            UPDATE durpro_fix_aml aml set debit = k.debit, credit = k.credit, amount_currency = k.amount_currency
            FROM durpro_fix_aml_keepers k
            WHERE aml.id = k.move_line_id"""
        self.env.cr.execute(sql)

    @api.model
    def _delete_extra_lines(self, updates):
        extra_line_ids = tuple(line_id for update in updates for line_id in update['line_ids_to_merge'])
        sql = """DELETE FROM durpro_fix_aml WHERE id in """ + str(extra_line_ids)
        self.env.cr.execute(sql)

    @api.model
    def _merge_entries(self, updates):
        """ Merge the lines to delete into the keeper line, keeping all foreign key references across the DB intact.

        :param updates: A tuple containing:
            [0]: The id of the keeper line
            [1]: A list of ids of the lines to merge into the keeper line
        """
        # Create a temporary table to hold the ids of the lines to delete and relate them to the "keeper" id
        self.env.cr.execute("DROP TABLE IF EXISTS durpro_fix_aml_to_delete")
        self.env.cr.execute("CREATE TABLE durpro_fix_aml_to_delete (move_line_id int primary key, keeper_id int)")
        tups = tuple((line_id, update[0]) for update in updates for line_id in update[1])
        args_str = ','.join(str(tup) for tup in tups)
        self.env.cr.execute("INSERT INTO durpro_fix_aml_to_delete (move_line_id, keeper_id) VALUES" + args_str)
        self._merge_delete_lines()

    @api.model
    def _merge_delete_lines(self):
        fk_tables = self._get_applicable_foreign_keys()
        table_constraints = self._get_unique_constraints(fk_tables)
        for row in fk_tables:
            table = row['FK_tbl_name']
            columns = row['FK_col_names'].split(',')
            constraints = table_constraints.get(table)
            if constraints:
                # Add a temporary key column
                self.env.cr.execute("""ALTER TABLE %s 
                                       ADD COLUMN IF NOT EXISTS mig_temp_key SERIAL UNIQUE NOT NULL""" % table)
            for column in columns:
                if constraints:
                    self._delete_future_duplicates(column, constraints, table)
                    self._update_table_column(column, table)
            if constraints:
                # Remove the temporary key column
                self.env.cr.execute("""ALTER TABLE %s 
                                       DROP COLUMN IF EXISTS mig_temp_key""" % table)
        self.env.cr.execute(""" DELETE FROM account_move_line 
                                WHERE id in (SELECT move_line_id FROM durpro_fix_aml_to_delete)""")

    @api.model
    def _delete_future_duplicates(self, column, constraints, table):
        for constraint_cols in constraints.values():
            if column in constraint_cols:
                params = {
                    'table': table,
                    'column': column,
                    'constraint_cols': ','.join(constraint_cols),
                    'exclusive_constraint_cols': ','.join(col for col in constraint_cols if col != column),
                }
                sql = """
                                DELETE FROM %(table)s as main1
                                USING durpro_fix_aml_to_delete as links
                                WHERE 
                                    main1.%(column)s = links.move_line_id AND
                                    mig_temp_key not in (
                                    SELECT DISTINCT ON (%(constraint_cols)s) mig_temp_key 
                                    FROM (SELECT %(exclusive_constraint_cols)s, 
                                            links.keeper_id as %(column)s, 
                                            mig_temp_key 
                                          FROM %(table)s) as main2)
                            """ % params
                self.env.cr.execute(sql)
        self.env.cr.execute("DELETE FROM durpro_fix_aml WHERE id in (SELECT move_line_id FROM durpro_fix_aml_to_delete)")

    @api.model
    def _update_table_column(self, column, table):
        params = {
            'table': table,
            'column': column,
        }
        sql = """
                        UPDATE %(table)s as main1
                        SET %(column)s = links.keeper_id
                        FROM durpro_fix_aml_to_delete as links
                        WHERE main1.%(column)s = links.move_line_id
                    """ % params
        self.env.cr.execute(sql)

    @api.model
    def _get_unique_constraints(self, fk_tables):
        fk_table_names = tuple(fk_table['FK_tbl_name'] for fk_table in fk_tables)
        sql = """
                  SELECT  tc.table_name as tbl_name, tc.constraint_name as constraint_name, 
                      string_agg(kcu.column_name, ',') as col_names
                    FROM information_schema.table_constraints tc
                        JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name
                    WHERE constraint_type in ('UNIQUE', 'PRIMARY KEY') and tc.table_name in %s 
                    GROUP BY tc.table_name, tc.constraint_name
        """ % str(fk_table_names)
        self.env.cr.execute(sql)
        unique_constraints = self.env.cr.dictfetchall()
        table_constraints = {}
        for row in unique_constraints:
            table = row['tbl_name']
            constraint_name = row['constraint_name']
            col_names = row['col_names'].split(',')
            if table not in table_constraints:
                table_constraints[table] = {constraint_name: col_names}
                continue
            table_constraints[table][constraint_name] = col_names
        return table_constraints

    @api.model
    def _get_applicable_foreign_keys(self):
        sql = """
                   SELECT tc.table_name "FK_tbl_name",
                          string_agg(kcu.column_name, ',') "FK_col_names"
                   FROM information_schema.table_constraints tc
                       JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name
                       JOIN information_schema.constraint_column_usage ccu ON ccu.constraint_name = tc.constraint_name
                   WHERE constraint_type = 'FOREIGN KEY'
                       AND ccu.table_name='account_move_line'
                   GROUP BY tc.table_name
                   """
        self.env.cr.execute(sql)
        fk_tables = self.env.cr.dictfetchall()
        return fk_tables

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
                return round(abs(line['amount_currency']), 2)
            else:
                return round(-abs(line['amount_currency']), 2)

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
        tups = tuple((update['id'], update['currency_id'], update['amount_currency']) for update in updates)
        args_str = ','.join(str(tup) for tup in tups)
        sql = """UPDATE durpro_fix_aml SET currency_id = v.currency_id, amount_currency = v.amount_currency
             FROM (VALUES %s) AS v(id, currency_id, amount_currency) WHERE v.id = durpro_fix_aml.id""" % args_str
        self.env.cr.execute(sql)

    @api.model
    def _check_debit_credit_balances(self) -> bool:
        """ """
        self.env.cr.execute(
            "SELECT move_id, account_id, sum(debit) as debits, sum(credit) as credits FROM durpro_fix_aml group by move_id, account_id")
        new_balances = self.env.cr.dictfetchall()
        self.env.cr.execute(
            "SELECT move_id, account_id, sum(debit) as debits, sum(credit) as credits FROM account_move_line group by move_id, account_id")
        old_balances = self.env.cr.dictfetchall()
        old_balances_dict = {}
        for old_balance in old_balances:
            move_id = old_balance['move_id']
            account_id = old_balance['account_id']
            if move_id in old_balances_dict:
                if not old_balances_dict[move_id].get(account_id):
                    old_balances_dict[move_id][account_id] = old_balance
                else:
                    raise ValidationError(f"Duplicate account {account_id} in move {move_id}")
            else:
                old_balances_dict[move_id] = {account_id: old_balance}
        mismatch = False
        for new_balance in new_balances:
            move_id = new_balance['move_id']
            account_id = new_balance['account_id']
            new_debit = round(new_balance['debits'], 2)
            new_credit = round(new_balance['credits'], 2)
            old_debit = round(old_balances_dict[move_id][account_id]['debits'], 2)
            old_credit = round(old_balances_dict[move_id][account_id]['credits'], 2)
            if abs(round(new_debit - new_credit, 2)) != abs(round(old_debit - old_credit, 2)):
                mismatch = True
                _logger.error(
                    f"Mismatch detected in debit and credit totals for move {new_balance['move_id']}, account {new_balance['account_id']}.")
                _logger.error(f"Old debit: {old_debit}, Old credit: {old_credit}")
                _logger.error(f"New debit: {new_debit}, New credit: {new_credit}")
        if mismatch:
            self.env.cr.rollback()
            return False
        _logger.info("Debit and credit balances match between new and old entries.")
        return True
