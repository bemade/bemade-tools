from odoo.tools.sql import SQL
from datetime import datetime, date, timedelta
import pytz


def fix_quotes(string):
    return string and string.strip('"').replace('""', '"')


def fix_tz(date, source_timezone="America/Toronto"):
    """Fix timezone for datetime values imported from SAP.

    SAP datetimes are actually in America/Toronto timezone, but may come in two forms:
    1. Naive datetimes (no timezone info)
    2. Timezone-aware datetimes incorrectly marked as UTC

    In both cases, we want to treat the datetime as being in Local time.
    For timezone-aware datetimes, we first strip the incorrect timezone info.
    Then we mark the datetime as Local and convert to UTC for Odoo.

    For example, if SAP shows "2025-01-29 00:00:00":
    - Whether naive or incorrectly marked as UTC, we treat it as Local time
    - During EST (UTC-5), we convert to "2025-01-29 05:00:00"
    - This ensures when Odoo converts back to the source timezonefor display,
      it will show the original "2025-01-29 00:00:00"

    Args:
        date (datetime): A datetime from SAP (naive or wrongly marked as UTC)
        source_timezone (str): The actual timezone the datetime is in (default: 'America/Toronto')

    Returns:
        datetime: Naive datetime in UTC that will display correctly in Odoo
    """
    if not date:
        return date

    import pytz

    # Get the source timezone
    source_tz = pytz.timezone(source_timezone)

    # If it's naive, first make it timezone-aware in the source timezone
    if not date.tzinfo:
        date = source_tz.localize(date)
    # If it has a timezone, it's wrong. Remove it and re-localize it.
    else:
        date = date.replace(tzinfo=None)
        date = source_tz.localize(date)

    # Now convert to UTC and make it naive
    return date.astimezone(pytz.UTC).replace(tzinfo=None)


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
