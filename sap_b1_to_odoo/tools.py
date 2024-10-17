from odoo.tools.sql import SQL
from datetime import datetime, date


class PagingIterator:
    def __init__(self, cr, fetch_query, count_query, limit=1000):
        self.cr = cr
        self.fetch_query = f"{fetch_query} OFFSET %s  LIMIT %s"
        cr.execute(SQL(count_query))
        self.count = cr.fetchall()[0][0]
        self.limit = limit
        self.offset = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self.offset >= self.count:
            raise StopIteration
        self.cr.execute(SQL(self.fetch_query, self.offset, self.limit))
        self.offset += self.limit
        return self.cr.dictfetchall()
