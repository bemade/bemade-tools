from odoo import models, fields


class AccountMove(models.Model):
    _inherit = "account.move"

    sap_docentry = fields.Integer(
        index="btree", string="SAP Document Entry", copy=False
    )
    sap_docnum = fields.Integer(index="btree", string="SAP Document Number", copy=False)
    sap_table = fields.Char(index="btree", copy=False)
    sap_atcentry = fields.Integer(index="btree", copy=False)

    _sap_docnum_unique = models.Constraint(
        "EXCLUDE USING btree (sap_docnum WITH =, sap_table WITH =) WHERE (sap_docnum != 0 AND sap_table IS NOT NULL)",
        "SAP docnum must be unique when set!",
    )

    def _stock_account_prepare_realtime_out_lines_vals(self):
        """Override to skip COGS generation when importing from SAP.

        When context has 'skip_cogs_generation', we've already added COGS lines
        from SAP's historical data, so skip Odoo's automatic COGS calculation.
        """
        if self.env.context.get("skip_cogs_generation"):
            return []
        return super()._stock_account_prepare_realtime_out_lines_vals()


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    sap_line_num = fields.Integer(index="btree", copy=False)
    sap_aftlinenum = fields.Integer(index="btree", copy=False)
    sap_lineseq = fields.Integer(index="btree", copy=False)
    sap_docentry = fields.Integer(
        related="move_id.sap_docentry",
        store=True,
        index="btree",
        copy=False,
    )
    sap_table = fields.Char(
        index="btree",
        copy=False,
    )
    sap_acct_id = fields.Many2one(
        "account.account",
        string="SAP Account",
        copy=False,
        help="The account SAP posted this line to (from JDT1). "
             "Used to correct Odoo's auto-computed account after creation.",
    )

    _sap_line_type_check = models.Constraint(
        """CHECK(
            (sap_line_num != 0 AND sap_lineseq = 0 AND sap_aftlinenum = 0) OR
            (sap_line_num = 0 AND sap_lineseq != 0 AND sap_aftlinenum != 0)
        )""",
        "A line must have either a line_num (for product lines) or an aftlinenum (for text lines), but not both.",
    )
    _sap_line_docentry_table_unique = models.Constraint(
        "UNIQUE(sap_line_num, sap_aftlinenum, sap_lineseq, sap_docentry, sap_table)",
        "Another line with this line number and docentry already exists for this SAP table.",
    )
