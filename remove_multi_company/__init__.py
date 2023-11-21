from odoo import api, SUPERUSER_ID


def post_init_hook(cr, registry):
    sql = """
    SELECT cols.table_name as table, cols.column_name as column, tc.constraint_type as constraint_type
    FROM information_schema.columns cols
    LEFT JOIN information_schema.constraint_column_usage ccu 
        on cols.column_name = ccu.column_name and cols.table_name = ccu.table_name
    LEFT JOIN information_schema.table_constraints tc on tc.constraint_name = ccu.constraint_name
    WHERE cols.column_name = 'company_id'
    """
    env = api.Environment(cr, SUPERUSER_ID)
    env.cr.execute(sql)
    cols = env.cr.dictfetchall()
    for row in cols:
        env.cr.execute("""
            UPDATE %(table) set %(column) = null
        """) % {'table': row['table'], 'column': row['column']}
    env.cr.execute("""DELETE FROM res_company WHERE id > (select min(id) from res_company)""")