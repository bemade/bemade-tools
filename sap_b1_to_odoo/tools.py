from odoo.tools.sql import SQL
from datetime import datetime, date


class PagingIterator:
    def __init__(
        self,
        cr,
        fetch_query,
        count_query,
        fetch_args=[],
        count_args=[],
        limit=1000,
        orderby=None,
        logger=None,
    ):
        self.logger = logger
        self.cr = cr
        self.orderby = orderby
        orderby_str = f" ORDER BY %s" if orderby else ""
        self.fetch_query = f"{fetch_query}{orderby_str} OFFSET %s  LIMIT %s"
        self.fetch_args = fetch_args
        cr.execute(SQL(count_query, *count_args))
        self.count = cr.fetchall()[0][0]
        self.limit = limit
        self.offset = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self.offset >= self.count:
            raise StopIteration
        if self.logger:
            self.logger.info(f"{self.count - self.offset} to go...")
        if not self.orderby:
            self.cr.execute(
                SQL(
                    self.fetch_query,
                    *self.fetch_args,
                    self.offset,
                    self.limit,
                )
            )
        else:
            self.cr.execute(
                SQL(
                    self.fetch_query,
                    *self.fetch_args,
                    SQL.identifier(self.orderby),
                    self.offset,
                    self.limit,
                )
            )
        self.offset += self.limit
        return self.cr.dictfetchall()
