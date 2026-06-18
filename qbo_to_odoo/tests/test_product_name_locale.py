"""Tests for QBO product import locale-keying of translatable names (#3763).

Acceptance criteria covered:

AC1/AC2 — A freshly-imported QBO product lands its name (and description_sale)
          under the company's active locale key, NOT under Odoo's default en_US.
          Verified by reading the raw JSONB key name->>'<locale>' via SQL.

AC2     — The one-shot backfill fixer (qbo.product.name.locale.fixer) repairs
          EXISTING rows whose name was historically written under en_US only:
          it copies the en_US value into the active-locale key, idempotently.

AC3     — Because names now land under the active locale, the locale-keyed read
          that dedup / name-matching relies on (name->>'<locale>') is populated
          and therefore matches.

Mechanism note (mirrors test_product_linker.py):
    The test DB defaults to en_US, so ORM-created translatable fields land under
    {"en_US": ...}. To reproduce Verajet's production layout (active locale
    en_CA), we activate en_CA and set the company partner's lang to en_CA, then
    exercise the REAL load step. We assert against the raw JSONB key via SQL so
    the test is independent of whatever lang the ORM read would resolve.
"""

from unittest.mock import MagicMock

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

LOCALE = "en_CA"


def _make_ctx(env):
    """Minimal ETLContext mock backed by the real test DB env/cursor."""
    ctx = MagicMock()
    ctx.env = env
    return ctx


def _raw_name_key(env, tmpl_id, key):
    """Read product_template.name->>key directly via SQL (locale-independent)."""
    env.flush_all()
    env.cr.execute(
        "SELECT name ->> %s FROM product_template WHERE id = %s", (key, tmpl_id)
    )
    row = env.cr.fetchone()
    return row[0] if row else None


def _raw_desc_key(env, tmpl_id, key):
    env.flush_all()
    env.cr.execute(
        "SELECT description_sale ->> %s FROM product_template WHERE id = %s",
        (key, tmpl_id),
    )
    row = env.cr.fetchone()
    return row[0] if row else None


@tagged("post_install", "-at_install")
class TestQboProductNameLocale(TransactionCase):
    """Locale-keying of QBO product names on import + existing-row backfill."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Reproduce Verajet's production locale: activate en_CA and make it the
        # company's configured language (the key the importer now pins on).
        cls.env["res.lang"]._activate_lang(LOCALE)
        cls.env.company.partner_id.lang = LOCALE
        cls.importer = cls.env["qbo.item.importer"]
        cls.fixer = cls.env["qbo.product.name.locale.fixer"]
        cls.ProductTemplate = cls.env["product.template"]

    # ------------------------------------------------------------------
    # AC1 — fresh import lands name + description under the active locale
    # ------------------------------------------------------------------

    def test_fresh_import_writes_name_under_active_locale(self):
        """load_items must write name / description_sale under en_CA, not en_US."""
        transformed = {
            "transform_items": [
                {
                    "name": "DTF3200C-3763",
                    "description_sale": "DTF printer 3763",
                    "default_code": None,
                    "type": "consu",
                    "is_storable": True,
                    "qbo_item_id": 73630,
                }
            ]
        }
        ctx = _make_ctx(self.env)
        self.importer.load_items(ctx, transformed)

        self.env.cr.execute(
            "SELECT product_tmpl_id FROM product_product WHERE qbo_item_id = %s",
            (73630,),
        )
        tmpl_id = self.env.cr.fetchone()[0]

        self.assertEqual(
            _raw_name_key(self.env, tmpl_id, LOCALE),
            "DTF3200C-3763",
            "name must be stored under the active locale key (en_CA).",
        )
        self.assertEqual(
            _raw_desc_key(self.env, tmpl_id, LOCALE),
            "DTF printer 3763",
            "description_sale must be stored under the active locale key.",
        )
        # AC3: the locale-keyed read dedup relies on is populated → matches.
        self.env.cr.execute(
            "SELECT 1 FROM product_template WHERE id = %s "
            "AND name ->> %s = %s",
            (tmpl_id, LOCALE, "DTF3200C-3763"),
        )
        self.assertTrue(
            self.env.cr.fetchone(),
            "name->>'en_CA' must match for dedup/name-linking to work.",
        )

    # ------------------------------------------------------------------
    # AC2 — backfill repairs an existing en_US-only row, idempotently
    # ------------------------------------------------------------------

    def test_backfill_repairs_existing_en_US_only_row(self):
        """A QBO product blank under en_CA but set under en_US is repaired."""
        # Create a QBO product, then force its name/description to the broken
        # production state: present under en_US, absent under en_CA.
        product = self.env["product.product"].create(
            {"name": "PLACEHOLDER-3763B", "qbo_item_id": 73631}
        )
        self.env.flush_all()
        tmpl_id = product.product_tmpl_id.id
        self.env.cr.execute(
            "UPDATE product_template "
            "SET name = '{\"en_US\": \"28020-C1L-3763\"}'::jsonb, "
            "    description_sale = '{\"en_US\": \"Ink 3763\"}'::jsonb "
            "WHERE id = %s",
            (tmpl_id,),
        )
        product.product_tmpl_id.invalidate_recordset(["name", "description_sale"])

        # Precondition: blank under the active locale.
        self.assertIn(
            _raw_name_key(self.env, tmpl_id, LOCALE), (None, ""),
            "Precondition: name must be blank under en_CA before backfill.",
        )

        ctx = _make_ctx(self.env)
        self.fixer.load_locale_backfill(ctx, {"transform_locale": {"lang": LOCALE}})

        self.assertEqual(
            _raw_name_key(self.env, tmpl_id, LOCALE),
            "28020-C1L-3763",
            "Backfill must copy the en_US name into the en_CA key.",
        )
        self.assertEqual(
            _raw_desc_key(self.env, tmpl_id, LOCALE),
            "Ink 3763",
            "Backfill must copy the en_US description_sale into the en_CA key.",
        )

    def test_backfill_is_idempotent_and_preserves_good_rows(self):
        """Re-running the backfill must not overwrite a correctly-keyed name."""
        product = self.env["product.product"].create(
            {"name": "GOOD-3763C", "qbo_item_id": 73632}
        )
        self.env.flush_all()
        tmpl_id = product.product_tmpl_id.id
        # Correctly keyed under en_CA already, with a stale en_US value that must
        # NOT clobber the good en_CA value on backfill.
        self.env.cr.execute(
            "UPDATE product_template "
            "SET name = '{\"en_CA\": \"GOOD-3763C\", \"en_US\": \"STALE\"}'::jsonb "
            "WHERE id = %s",
            (tmpl_id,),
        )
        product.product_tmpl_id.invalidate_recordset(["name"])

        ctx = _make_ctx(self.env)
        # Run twice — must be a no-op for this row both times.
        self.fixer.load_locale_backfill(ctx, {"transform_locale": {"lang": LOCALE}})
        self.fixer.load_locale_backfill(ctx, {"transform_locale": {"lang": LOCALE}})

        self.assertEqual(
            _raw_name_key(self.env, tmpl_id, LOCALE),
            "GOOD-3763C",
            "Backfill must not overwrite an already-correct en_CA name.",
        )
