#
#    Bemade Inc.
#
#    Copyright (C) 2026-May Bemade Inc. (<https://www.bemade.org>).
#    Author: Marc Durepos (Contact : marc@bemade.org)
#
#    This program is under the terms of the GNU Lesser General Public License,
#    version 3.
#
#    For full license details, see https://www.gnu.org/licenses/lgpl-3.0.en.html.
#
"""Failing-test-first reproduction for tasks 3623 + 3624.

Production observation on the post-import RWI Odoo DB (rwi, 2026-05-25):

    SELECT count(*) FILTER (WHERE property_payment_term_id IS NOT NULL),
           count(*) FILTER (WHERE specific_property_product_pricelist IS NOT NULL)
    FROM res_partner;
    --> (0, 0) out of 18,133 rows

Yet the partner ETL clearly assembles vals containing both keys (see
``res_partner_etl.transform_companies`` line 345-347) and the load phase calls
``ctx.env["res.partner"].with_company(ctx.env.company).create(partner_vals)``.

Both v1 cycles (commits 27342ac and 89c295b) added unit tests that ONLY assert
the in-memory ORM read after create() (e.g. ``partner.with_company(c).property_payment_term_id
== net30``). Those tests pass against unpatched code, yet production stays
broken. The conclusion: the create() call sets the value in the in-memory
cache (so the ORM read returns the right value), but the value never reaches
the JSONB column on disk.

These tests are the *missing reproduction*: they call create() exactly like
``load_companies`` does, then flush the env and read the JSONB column DIRECTLY
via raw SQL. If the JSONB column is empty/null/missing-the-key after a flush,
the bug is reproduced.

Test A is an isolation test (no SAP source). Test B is an integration test
that pulls real OCRD/OCTG/OPLN rows from the live SAP source on
localhost:5433 (db ``rwiprod``, schema ``dbo``).
"""

import os
import logging
from unittest.mock import MagicMock

import psycopg2
import psycopg2.extras

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.etl_framework import ChunkableData


_logger = logging.getLogger(__name__)


# Connection details for the SAP source mirror (matches scripts/test_import.py).
SAP_SRC = {
    "host": "localhost",
    "port": 5433,
    "dbname": "rwiprod",
    "user": "postgres",
    "password": "pgpassword",
}


def _sap_conn():
    """Open a read-only connection to the SAP source DB."""
    conn = psycopg2.connect(
        host=SAP_SRC["host"],
        port=SAP_SRC["port"],
        dbname=SAP_SRC["dbname"],
        user=SAP_SRC["user"],
        password=SAP_SRC["password"],
        connect_timeout=3,
    )
    conn.set_session(readonly=True, autocommit=True)
    return conn


@tagged("-at_install", "post_install", "repro_jsonb")
class TestPropertyFieldWriteFailureIsolated(TransactionCase):
    """Test A — isolation. No SAP source involved.

    Reproduces the production failure with the *minimum* moving parts: build
    a vals dict containing the two property fields with REAL in-DB ids, call
    the same ``with_company(env.company).create(...)`` that load_companies
    uses, flush, then read the JSONB column DIRECTLY via raw SQL.

    Asserts the JSONB has the company id as a key with the expected value.
    If this fails: the bug is in the create() write-path or post-create flush.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company

        # Create a payment term and a pricelist using the same shape the ETL
        # would have produced via OCTG / OPLN imports.
        cls.term = cls.env["account.payment.term"].create(
            {"name": "NET 30 - 3623 repro", "sap_groupnum": 9001}
        )
        cls.pricelist = cls.env["product.pricelist"].create(
            {
                "name": "Repro Pricelist - 3624",
                "currency_id": cls.env.ref("base.USD").id,
                "sap_listnum": 9001,
            }
        )

    def _build_vals(self, cardcode):
        """Mimic the exact shape res_partner_etl.transform_companies produces."""
        return {
            "sap_card_code": cardcode,
            "name": f"Repro {cardcode}",
            "is_company": True,
            "company_id": self.company.id,
            "property_payment_term_id": self.term.id,
            "property_supplier_payment_term_id": False,
            "property_product_pricelist": self.pricelist.id,
        }

    def _read_raw_jsonb(self, partner_id):
        """Read both JSONB columns directly (bypass Odoo's ORM cache)."""
        self.env.flush_all()
        self.env.cr.execute(
            """
            SELECT property_payment_term_id, specific_property_product_pricelist
            FROM res_partner WHERE id = %s
            """,
            (partner_id,),
        )
        return self.env.cr.fetchone()

    def test_create_with_company_persists_payment_term_to_jsonb(self):
        """A — create(...) must persist property_payment_term_id to JSONB.

        Calls .with_company(env.company).create([vals]) exactly like load_companies,
        flushes, then reads the JSONB column. Asserts the JSONB has the company
        id as a key with the expected term id as the value.

        Expected to FAIL against current code if the create() write-path does
        not actually persist the company-dependent value.
        """
        vals_list = [self._build_vals("REPRO_C_TERM_01")]

        partner = (
            self.env["res.partner"]
            .with_company(self.company)
            .create(vals_list)
        )

        raw_term, raw_pricelist = self._read_raw_jsonb(partner.id)

        # The in-memory ORM read (what the existing unit tests assert) — sanity:
        self.assertEqual(
            partner.with_company(self.company).property_payment_term_id,
            self.term,
            "ORM read should return the term — if this fails, vals never reached the cache.",
        )

        # The DISK read — the production-truth check.
        self.assertIsNotNone(
            raw_term,
            "property_payment_term_id JSONB column is NULL after create(). "
            "This is the production bug: ORM read works (cache), but disk is empty.",
        )
        self.assertIn(
            str(self.company.id),
            raw_term,
            f"JSONB key for company {self.company.id} not present. "
            f"Actual JSONB on disk: {raw_term!r}. "
            "Either the write went to no company, or to a different key.",
        )
        self.assertEqual(
            raw_term[str(self.company.id)],
            self.term.id,
            f"JSONB has company key but value is wrong. Got {raw_term!r}, "
            f"expected term id {self.term.id}.",
        )

    def test_create_with_company_persists_pricelist_to_jsonb(self):
        """A — create(...) must persist specific_property_product_pricelist to JSONB.

        Same shape as the payment-term test but for the pricelist field. The
        ETL passes ``property_product_pricelist`` in vals (a computed alias with
        inverse); the inverse should write to specific_property_product_pricelist.
        Asserts the stored column is non-null after a flush.
        """
        vals_list = [self._build_vals("REPRO_C_PL_01")]

        partner = (
            self.env["res.partner"]
            .with_company(self.company)
            .create(vals_list)
        )

        raw_term, raw_pricelist = self._read_raw_jsonb(partner.id)

        self.assertEqual(
            partner.with_company(self.company).property_product_pricelist,
            self.pricelist,
            "ORM read should return the pricelist — if this fails, vals never reached the cache.",
        )

        self.assertIsNotNone(
            raw_pricelist,
            "specific_property_product_pricelist JSONB column is NULL after create(). "
            "This is the production bug for task 3624.",
        )
        self.assertIn(
            str(self.company.id),
            raw_pricelist,
            f"JSONB key for company {self.company.id} not present. "
            f"Actual JSONB on disk: {raw_pricelist!r}.",
        )
        self.assertEqual(
            raw_pricelist[str(self.company.id)],
            self.pricelist.id,
            f"JSONB has company key but value is wrong. Got {raw_pricelist!r}, "
            f"expected pricelist id {self.pricelist.id}.",
        )

    def test_batch_create_persists_both_properties_to_jsonb(self):
        """A — batch create (matches production: 3632 partners in one call).

        Production calls ``create([3632 vals dicts])``. If the bug is
        batch-size-dependent (e.g. the company-dependent write path mishandles
        multi-record creates), the single-row tests above could pass while
        this fails. Asserts JSONB persistence on every partner in a batch.
        """
        vals_list = [self._build_vals(f"REPRO_BATCH_{i:03d}") for i in range(5)]

        partners = (
            self.env["res.partner"]
            .with_company(self.company)
            .create(vals_list)
        )
        self.assertEqual(len(partners), 5)

        self.env.flush_all()
        self.env.cr.execute(
            """
            SELECT id, property_payment_term_id, specific_property_product_pricelist
            FROM res_partner WHERE id = ANY(%s) ORDER BY id
            """,
            (partners.ids,),
        )
        rows = self.env.cr.fetchall()
        self.assertEqual(len(rows), 5)

        failures = []
        for pid, raw_term, raw_pl in rows:
            if not raw_term or str(self.company.id) not in raw_term:
                failures.append(f"partner {pid}: term JSONB = {raw_term!r}")
            if not raw_pl or str(self.company.id) not in raw_pl:
                failures.append(f"partner {pid}: pricelist JSONB = {raw_pl!r}")
        self.assertFalse(
            failures,
            "Batch create did not persist company-dependent properties for all "
            f"partners. Failures: {failures}",
        )


@tagged("-at_install", "post_install", "repro_jsonb_empty_lookups")
class TestPropertyFieldWriteFailureWithEmptyLookups(TransactionCase):
    """Test C — reproduces the actual production failure mode.

    res.partner.company.importer declares depends_on=[]. It does NOT depend on
    account.payment.term.importer (also depends_on=[]) or on
    product.pricelist.item.importer (which itself depends on the partner
    importer, creating an effective circular dependency where pricelist items
    can only get sap_listnum populated AFTER partners run).

    So in production, when extract_companies builds:
        terms_dict = {t.sap_groupnum: t.id for t in env["account.payment.term"].search([])}
        pricelists_by_listnum = {} (pricelists with sap_listnum=False)

    both are empty. transform_companies then assigns property_payment_term_id=False
    and property_product_pricelist=False to every partner. The JSONB stays empty,
    and the form view falls back to the lowest-sequence pricelist ("Military").

    This test simulates that exact state and asserts the symptom matches
    production: 0/N partners get the fields set, with vals carrying False
    rather than the expected ids.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.importer = cls.env["res.partner.company.importer"]
        # Note: we deliberately do NOT create payment terms or pricelists here,
        # to mirror the production state where partner ETL runs before those
        # importers create their records.

    def test_empty_terms_dict_yields_false_payment_term(self):
        """C — empty terms_dict → every customer's vals carries
        property_payment_term_id=False, matching 0/18133 production rows."""
        sap_partner = {
            "cardcode": "REPRO_EMPTY_TERMS_01",
            "cardname": "Empty terms repro",
            "cardtype": "C",
            "groupnum": 2,  # any non-sentinel groupnum
            "listnum": 1,
            "country": False, "state1": False, "address": False, "block": False,
            "slpcode": False, "currency": False, "partdelivr": "N",
            "e_mail": False, "phone1": False, "phone2": False, "zipcode": False,
            "city": False, "fathercard": False, "notes": False,
            "atcentry": False, "debpayacct": False,
        }
        cache = {
            "countries_dict": {}, "states_dict": {}, "users_dict": {},
            "terms_dict": {},  # ← EMPTY (production state when partner ETL runs first)
            "currencies_dict": {},
            "company_currency_id": self.company.currency_id.id,
            "company_id": self.company.id,
            "accounts_dict": {},
            "pricelists_by_listnum": {},  # ← EMPTY too
        }
        ctx = MagicMock()
        ctx.env = self.env
        extracted = {
            "extract_companies": ChunkableData(records=[sap_partner], context=cache),
        }
        vals_list = self.importer.transform_companies(ctx, extracted)
        self.assertEqual(len(vals_list), 1)

        # This is the bug surfacing in vals: lookup misses produce False.
        self.assertFalse(
            vals_list[0]["property_payment_term_id"],
            "With empty terms_dict, transform produces False for the term. "
            "This is the actual production bug: vals carries no term to write.",
        )
        self.assertFalse(
            vals_list[0]["property_product_pricelist"],
            "With empty pricelists_by_listnum, transform produces False for the pricelist.",
        )

        # And after the load, JSONB is empty — exactly like production.
        partner = self.env["res.partner"].with_company(self.company).create(vals_list)
        self.env.flush_all()
        self.env.cr.execute(
            "SELECT property_payment_term_id, specific_property_product_pricelist "
            "FROM res_partner WHERE id = %s",
            (partner.id,),
        )
        raw_term, raw_pl = self.env.cr.fetchone()
        self.assertFalse(
            raw_term,
            "Confirmed: empty terms_dict at transform time → JSONB stays empty "
            "(or contains False). Matches the 0/18133 production symptom.",
        )
        self.assertFalse(
            raw_pl,
            "Confirmed: empty pricelists_by_listnum → JSONB stays empty. "
            "Form view will then fall back to lowest-sequence pricelist (Military).",
        )


@tagged("-at_install", "post_install", "repro_jsonb_depends_on")
class TestPartnerETLDeclaresDependencies(TransactionCase):
    """Test D — the FAILING fix-target test.

    The production bug for payment terms (task 3623) is that
    res.partner.company.importer declares depends_on=[] and so can run before
    account.payment.term.importer in the orchestration graph. When that
    happens, ``extract_companies`` calls
    ``env["account.payment.term"].search([])`` against an empty table,
    builds ``terms_dict = {}``, and every partner ends up with
    property_payment_term_id=False in vals.

    This test asserts the orchestration contract: the partner importer MUST
    declare account.payment.term.importer in its depends_on list, so the
    framework guarantees terms exist before partners are extracted.

    Fails against the unfixed code (depends_on=[]) and passes after the fix.
    """

    def test_partner_company_importer_depends_on_payment_term_importer(self):
        importer = self.env["res.partner.company.importer"]
        pipeline = importer._etl_pipeline
        self.assertIn(
            "account.payment.term.importer",
            pipeline.depends_on,
            f"res.partner.company.importer must depend on "
            f"account.payment.term.importer so terms_dict is populated when "
            f"extract_companies runs. Current depends_on: {pipeline.depends_on!r}. "
            f"Without this dependency, every customer with a non-sentinel "
            f"groupnum has property_payment_term_id silently written as False "
            f"(observed in production: 0/18,133 partners have the field set).",
        )


@tagged("-at_install", "post_install", "repro_jsonb_live_sap")
class TestPropertyFieldWriteFailureIntegration(TransactionCase):
    """Test B — integration against the LIVE SAP source on localhost:5433.

    Pulls 10 real OCRD rows (with mixed groupnums and listnums), runs the
    actual ``transform_companies`` + ``load_companies`` pipeline (same code
    that ran during Marc's failing 2026-05-25 migration), flushes, and reads
    JSONB directly. Reproduces the failure with real data.

    Skipped if the SAP source DB is unreachable (so the test can run in CI
    without the live source).
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            conn = _sap_conn()
            conn.close()
        except Exception as e:
            raise cls.skipTest(cls, f"SAP source DB unreachable on {SAP_SRC['host']}:{SAP_SRC['port']}: {e}")

        cls.company = cls.env.company
        cls.importer = cls.env["res.partner.company.importer"]

        # Mirror the term and pricelist data the OCTG/OPLN importers would
        # have created. We pull the actual groupnum/listnum values from the
        # SAP source and create one Odoo record per distinct value seen.
        conn = _sap_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT DISTINCT groupnum FROM dbo.ocrd "
                    "WHERE cardtype='C' AND groupnum IS NOT NULL "
                    "AND groupnum NOT IN (0, -1)"
                )
                cls.distinct_groupnums = [r["groupnum"] for r in cur.fetchall()]
                cur.execute(
                    "SELECT DISTINCT listnum FROM dbo.ocrd "
                    "WHERE cardtype='C' AND listnum IS NOT NULL AND listnum > 0"
                )
                cls.distinct_listnums = [r["listnum"] for r in cur.fetchall()]
        finally:
            conn.close()

        cls.terms_by_gn = {}
        for gn in cls.distinct_groupnums:
            term = cls.env["account.payment.term"].create(
                {"name": f"Repro Term gn={gn}", "sap_groupnum": gn}
            )
            cls.terms_by_gn[gn] = term

        cls.pricelists_by_ln = {}
        for ln in cls.distinct_listnums:
            pl = cls.env["product.pricelist"].create(
                {
                    "name": f"Repro PL ln={ln}",
                    "currency_id": cls.env.ref("base.USD").id,
                    "sap_listnum": ln,
                }
            )
            cls.pricelists_by_ln[ln] = pl

    def _fetch_real_ocrd_sample(self, limit=10):
        """Pull a small sample of real OCRD rows from localhost:5433."""
        conn = _sap_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Pick distinct (groupnum, listnum) combos for coverage.
                cur.execute(
                    """
                    SELECT DISTINCT ON (groupnum, listnum)
                        cardcode, cardname, cardtype, groupnum, listnum,
                        country, state1, address, block, slpcode,
                        currency, partdelivr, e_mail, phone1, phone2,
                        zipcode, city, fathercard, notes, atcentry,
                        debpayacct
                    FROM dbo.ocrd
                    WHERE cardtype = 'C'
                      AND groupnum IS NOT NULL AND groupnum NOT IN (0, -1)
                      AND listnum IS NOT NULL AND listnum > 0
                    ORDER BY groupnum, listnum, cardcode
                    LIMIT %s
                    """,
                    (limit,),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
        # Prefix cardcodes so we never collide with already-imported partners
        # in the (test-rolled-back-but-still-noisy) DB.
        for row in rows:
            row["cardcode"] = "REPRO_" + row["cardcode"]
        return [dict(row) for row in rows]

    def test_real_sap_data_round_trip_persists_to_jsonb(self):
        """B — full transform + load against real SAP source row sample.

        Pulls real OCRD rows, runs transform_companies + load_companies,
        flushes, reads JSONB directly. Asserts every customer with a
        non-sentinel groupnum has property_payment_term_id JSONB populated,
        and every customer with a positive listnum has
        specific_property_product_pricelist JSONB populated.
        """
        ocrd_rows = self._fetch_real_ocrd_sample(limit=10)
        self.assertTrue(ocrd_rows, "SAP source returned no rows; check connection.")

        terms_dict = {gn: term.id for gn, term in self.terms_by_gn.items()}
        pricelists_by_listnum = {ln: pl.id for ln, pl in self.pricelists_by_ln.items()}

        cache = {
            "countries_dict": {},
            "states_dict": {},
            "users_dict": {},
            "terms_dict": terms_dict,
            "currencies_dict": {},
            "company_currency_id": self.company.currency_id.id,
            "company_id": self.company.id,
            "accounts_dict": {},
            "pricelists_by_listnum": pricelists_by_listnum,
        }
        ctx = MagicMock()
        ctx.env = self.env

        extracted = {
            "extract_companies": ChunkableData(records=ocrd_rows, context=cache),
        }
        vals_list = self.importer.transform_companies(ctx, extracted)
        self.assertEqual(len(vals_list), len(ocrd_rows))

        # Sanity: every vals dict in this sample has BOTH property keys set.
        for v in vals_list:
            self.assertTrue(
                v["property_payment_term_id"],
                f"transform produced empty payment_term for vals={v!r}",
            )
            self.assertTrue(
                v["property_product_pricelist"],
                f"transform produced empty pricelist for vals={v!r}",
            )

        # Now call load_companies's actual create call.
        partners = (
            self.env["res.partner"]
            .with_company(self.company)
            .create(vals_list)
        )

        self.env.flush_all()
        self.env.cr.execute(
            """
            SELECT id, sap_card_code,
                   property_payment_term_id,
                   specific_property_product_pricelist
            FROM res_partner WHERE id = ANY(%s) ORDER BY id
            """,
            (partners.ids,),
        )
        rows = self.env.cr.fetchall()

        failures = []
        for pid, cc, raw_term, raw_pl in rows:
            if not raw_term or str(self.company.id) not in raw_term:
                failures.append(
                    f"{cc}: property_payment_term_id JSONB = {raw_term!r}"
                )
            if not raw_pl or str(self.company.id) not in raw_pl:
                failures.append(
                    f"{cc}: specific_property_product_pricelist JSONB = {raw_pl!r}"
                )
        self.assertFalse(
            failures,
            f"Real-SAP integration test: {len(failures)} JSONB persistence failures "
            f"out of {len(rows) * 2} field-writes:\n  " + "\n  ".join(failures),
        )
