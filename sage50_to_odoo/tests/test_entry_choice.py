"""Which GL entry is a document's, when its number is not unique.

Acceptance criteria
-------------------
- An invoice and a later receipt can share a number for the same customer,
  in different fiscal generations. Given the document's date, the entry dated
  that day is the document's, whichever generation it is in — not the first
  generation searched that has the number.
- Without a date the old rule stands: newest generation first.
- In strict mode, no entry on the document's date means no entry at all, so
  the caller can look for a cash entry before settling for a same-numbered
  entry from another year.
- Among entries of the same day, the control amount still disambiguates a
  posted-corrected-reposted document.
"""

from datetime import date, datetime

from odoo.tests import BaseCase

from odoo.addons.sage50_to_odoo import tools

AR = 11000000


def _rows(generation, entry_id, day, control_amount):
    stamp = datetime.combine(day, datetime.min.time())
    return [
        {"generation": generation, "lId": entry_id, "dtJourDate": stamp,
         "lAcctId": AR, "dAmount": control_amount},
        {"generation": generation, "lId": entry_id, "dtJourDate": stamp,
         "lAcctId": 41200000, "dAmount": control_amount},
    ]


class TestChooseEntry(BaseCase):

    def setUp(self):
        super().setUp()
        # Newest generation first, as `journal_entry` collects them.
        self.receipt = _rows("tjently", 200, date(2024, 9, 16), -1193.0)
        self.invoice = _rows("tjeh01", 4208, date(2024, 4, 15), 1193.0)
        self.entries = [self.receipt, self.invoice]

    def test_document_date_picks_its_own_entry(self):
        chosen = tools.choose_entry(
            self.entries, day=date(2024, 4, 15),
            control_account=AR, expected_control=1193.0,
        )
        self.assertEqual(chosen[0]["lId"], 4208)

    def test_without_a_date_newest_generation_wins(self):
        chosen = tools.choose_entry(self.entries)
        self.assertEqual(chosen[0]["lId"], 200)

    def test_amount_beats_generation_when_no_entry_is_on_the_day(self):
        # The invoice's entry is dated a day after the document, so nothing
        # is on the document's day; the newer receipt sharing its number must
        # not win just by being in the newer generation.
        chosen = tools.choose_entry(
            self.entries, day=date(2024, 4, 14),
            control_account=AR, expected_control=1193.0,
        )
        self.assertEqual(chosen[0]["lId"], 4208)

    def test_exact_amount_refuses_a_different_amount(self):
        # A 0.00 invoice has no entry at all; the receipt sharing its number
        # must not be taken for it just because nothing else matches.
        chosen = tools.choose_entry(
            [self.receipt], control_account=AR, expected_control=0.0,
            exact_amount=True,
        )
        self.assertEqual(chosen, [])

    def test_strict_day_finds_nothing_rather_than_another_year(self):
        # A counter sale paid on the spot has no entry under its own number
        # on its own date (its entry is numbered "Comptant"); an entry with
        # the same number from another year must not be taken in its place.
        chosen = tools.choose_entry(
            self.entries, day=date(2024, 2, 8), strict=True,
        )
        self.assertEqual(chosen, [])

    def test_same_day_repost_is_told_apart_by_amount(self):
        original = _rows("tjeh01", 10, date(2024, 1, 5), 500.0)
        corrected = _rows("tjeh01", 12, date(2024, 1, 5), 450.0)
        chosen = tools.choose_entry(
            [original, corrected], day=date(2024, 1, 5),
            control_account=AR, expected_control=500.0,
        )
        self.assertEqual(chosen[0]["lId"], 10)


class TestDropReversed(BaseCase):
    """A CORR entry that mirrors an entry line for line cancels it.

    Sage corrects a posted document by reversing it ("CORR <number>") and
    posting the correction — possibly under another number, as when a sale
    on account is re-entered as paid on the spot. The reversed original is
    not the document's entry, however well its number and date match.
    """

    def test_mirrored_original_is_dropped(self):
        original = _rows("tjeh01", 3061, date(2024, 2, 8), 5.0)
        reversal = [
            dict(line, lId=3062, dAmount=-line["dAmount"])
            for line in original
        ]
        other = _rows("tjeh01", 5235, date(2024, 6, 21), 40.0)
        kept = tools.drop_reversed([original, other], [reversal])
        self.assertEqual([entry[0]["lId"] for entry in kept], [5235])

    def test_one_reversal_cancels_one_entry(self):
        first = _rows("tjeh01", 10, date(2024, 1, 5), 5.0)
        second = _rows("tjeh01", 11, date(2024, 1, 5), 5.0)
        reversal = [dict(line, lId=12, dAmount=-line["dAmount"]) for line in first]
        kept = tools.drop_reversed([first, second], [reversal])
        self.assertEqual(len(kept), 1)
