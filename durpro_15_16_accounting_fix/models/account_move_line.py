from odoo import models, fields, api, _, Command
from odoo.exceptions import ValidationError
from odoo.tools import mute_logger


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    @api.model
    def _get_problem_account_payments(self):
        """
        The following account.payment entries are problematic for migration:
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

        # Get the move lines grouped by account to see which ones we can match up
        self.env.cr.execute("""
            select am.id, aml.account_id, string_agg(aml.id::text, ',') as aml_ids, sum(aml.debit) as debit, 
                sum(aml.credit) as credit
            from account_move am 
                inner join account_move_line aml on am.id = aml.move_id
                inner join account_account a on aml.account_id = a.id
                inner join res_company company on am.company_id = company.id
                inner join account_journal j on am.journal_id = j.id
            where j.type in ('bank', 'cash') 
                and (internal_type in ('receivable', 'payable')
                    or aml.account_id = company.transfer_account_id)
            group by aml.account_id, am.id
            having count(*) > 1
        """)
        counterpart_lines = self.env.cr.dictfetchall()
        # Merge each group of counterpart lines into a single line
        self.run_updates(counterpart_lines)

        # Get the account.payment entries that have more than 1 liquidity line
        self.env.cr.execute("""
            select am.id, aml.account_id, string_agg(aml.id::text, ',') as aml_ids, sum(aml.debit) as debit, 
                sum(aml.credit) as credit
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
            group by aml.account_id, am.id
            having count(*) > 1
        """)
        liquidity_lines = self.env.cr.dictfetchall()
        self.run_updates(liquidity_lines)

    def run_updates(self, counterpart_lines):
        updates = [
            {
                'move_line_id': int(line['aml_ids'].split(',')[0]),
                'line_ids_to_merge': [int(line_id) for line_id in line['aml_ids'].split(',')[1:]],
                'debit': max(line['debit'] - line['credit'], 0),
                'credit': max(line['credit'] - line['debit'], 0),
            }
            for line in counterpart_lines]
        for update in updates:
            self.env.cr.execute("""
                UPDATE account_move_line
                SET debit = %(debit)s, credit = %(credit)s
                WHERE id = %(move_line_id)s
            """ % update)
            self.action_merge_entries(update['move_line_id'], update['line_ids_to_merge'])

    @api.model
    def action_merge_entries(self, line_id_to_keep, line_ids_to_merge):
        duplicate_ids = str(line_ids_to_merge).strip('[]')
        if line_id_to_keep in line_ids_to_merge:
            raise ValidationError(_('Please select different Record ID.'))

        self._cr.execute('''SELECT tc.constraint_name "constraint_name",
                                   tc.table_name "FK_tbl_name",
                                   kcu.column_name "FK_col_name",
                                   ccu.table_name "PK_tbl_name",
                                   ccu.column_name "PK_col_name"
                            FROM information_schema.table_constraints tc
                                JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name
                                JOIN information_schema.constraint_column_usage ccu ON ccu.constraint_name = tc.constraint_name
                            WHERE constraint_type = 'FOREIGN KEY'
                                AND ccu.table_name='account_move_line' ''')
        used_ref_table_list = self._cr.dictfetchall()

        list_of_fields = self.env['ir.model.fields'].sudo().search([('model', '=', "account.move.line"),
                                                                    ('ttype', '=', 'many2one'),
                                                                    ('relation', '=', 'account.move.line')]).mapped(
            'name')
        # Update reference into table.
        for each in used_ref_table_list:
            fk_table = each.get('FK_tbl_name')
            fk_col = each.get('FK_col_name')
            # Get column of all Table
            result = self._cr.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = '%s'" % fk_table)
            other_column = []
            for data in self._cr.fetchall():
                if data[0] != fk_col:
                    other_column.append(data[0])
            params = {
                'table': fk_table,
                'column': fk_col,
                'value': other_column[0],
                'duplicate_ids': duplicate_ids,
                'original_id': line_id_to_keep,
            }
            if len(other_column) <= 1:
                self._cr.execute("""
                    UPDATE "%(table)s" as main1
                    SET "%(column)s" = %(original_id)s
                    WHERE
                        "%(column)s" in (%(duplicate_ids)s) AND
                        NOT EXISTS (
                            SELECT 1
                            FROM "%(table)s" as sub1
                            WHERE
                                "%(column)s" = %(original_id)s AND
                                main1.%(value)s = sub1.%(value)s
                        )""" % params)
            else:
                try:
                    with mute_logger('odoo.sql_db'), self._cr.savepoint():
                        qry = '''UPDATE %(table)s SET %(column)s = %(original_id)s
                                    WHERE %(column)s = %(duplicate_id)s''' % params
                        self._cr.execute(qry)
                except Exception as e:
                    raise ValidationError(_('Error %s') % e)

        self._cr.execute(""" DELETE FROM account_move_line WHERE id in (%s)""" % duplicate_ids)
