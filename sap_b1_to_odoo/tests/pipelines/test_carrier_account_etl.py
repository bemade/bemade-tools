#
#    Bemade Inc.
#
#    Copyright (C) 2026-June Bemade Inc. (<https://www.bemade.org>).
#    Author: Marc Durepos (Contact : marc@bemade.org)
#
#    This program is under the terms of the GNU Lesser General Public License,
#    version 3.
#
#    For full license details, see https://www.gnu.org/licenses/lgpl-3.0.en.html.
#
"""Tests for the carrier importer after delivery_carrier_partner_account removal.

Acceptance criteria:

1. (test_load_carriers_creates_delivery_carrier_with_transporters) running the full
   extract→transform→load chain with mocked OCRD/OSHP rows still creates
   delivery.carrier records with the correct sap_transporter_ids (sap_trnspcode)
   links — the carrier/transporter half of the pipeline is unaffected by the
   account-loading removal.
2. (test_load_carriers_does_not_create_carrier_account_model) delivery.carrier.account
   is absent from the registry entirely (task #3924 removes the model, not just its
   usage here) — proves there is no remaining model for the loader to write to.
3. (test_load_carrier_accounts_method_removed) the old _load_carrier_accounts method
   no longer exists on the importer — the account-loading code path is gone, not just
   dormant.
4. (test_extract_returns_carriers_only) extract/transform now return a plain
   Dict[str, Set[int]] of carriers, not a (carriers, accounts) tuple — guards the
   reshaped return type that addons/rwi_sap_b1_to_odoo's
   RWIDeliveryCarrierImporter.extract_carriers_and_accounts override passes through
   unchanged.
"""

from unittest.mock import MagicMock

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.sap_b1_to_odoo.models.pipelines.carrier_account_etl import (
    DeliveryCarrierAccountImporter,
)


def _make_ctx(env, sap_rows):
    ctx = MagicMock()
    ctx.env = env
    ctx.cr.dictfetchall.return_value = sap_rows
    return ctx


@tagged("-at_install", "post_install", "carrier_account_etl")
class TestCarrierAccountEtl(TransactionCase):
    """Guards the carrier/transporter import after carrier-account removal."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.importer = cls.env["delivery.carrier.importer"]
        # Pipeline keeps class-level state across imports; reset it on the
        # registry class (not the recordset, which is read-only for this
        # attribute) so test ordering/reuse can't leak carrier names between
        # cases.
        type(cls.importer)._unique_carrier_names = set()
        # extract_carriers_and_accounts short-circuits unless there is
        # exactly one pre-existing delivery.carrier; make sure that's true
        # regardless of demo data.
        existing = cls.env["delivery.carrier"].search([])
        if len(existing) != 1:
            existing.unlink()
            cls.env["delivery.carrier"].create({
                "name": "Test Default Carrier",
                "product_id": cls.env["product.product"].create({
                    "name": "Test Default Carrier Product",
                    "type": "service",
                }).id,
            })

    def _run_pipeline(self, sap_rows):
        """Drive extract -> transform -> load over mocked OCRD/OSHP rows.

        Bind the BASE implementations explicitly: extending modules (e.g.
        rwi_sap_b1_to_odoo) override these steps with extra SQL the mocked
        cursor cannot serve. These tests guard the base pipeline's logic
        only; overlay behaviour is covered by the extending module's tests.
        """
        ctx = _make_ctx(self.env, sap_rows)
        extracted = DeliveryCarrierAccountImporter.extract_carriers_and_accounts(
            self.importer, ctx
        )
        transformed = DeliveryCarrierAccountImporter.transform_carriers_and_accounts(
            self.importer, ctx, {"extract_carriers_and_accounts": extracted}
        )
        DeliveryCarrierAccountImporter.load_carriers_and_accounts(
            self.importer, ctx, {"transform_carriers_and_accounts": transformed}
        )
        return extracted, transformed

    def test_load_carriers_creates_delivery_carrier_with_transporters(self):
        """Carrier + sap_transporter_ids creation still works end to end."""
        sap_rows = [
            {"cardcode": "C001", "shiptype": 1, "trnspname": "UPS#12345"},
            {"cardcode": "C002", "shiptype": 2, "trnspname": "FedEx"},
        ]
        self._run_pipeline(sap_rows)
        self.env.flush_all()

        ups = self.env["delivery.carrier"].search([("name", "=", "UPS")])
        self.assertTrue(ups, "UPS carrier must be created.")
        self.assertEqual(
            set(ups.sap_transporter_ids.mapped("sap_trnspcode")),
            {1},
            "UPS carrier must link to its SAP transport code.",
        )

        fedex = self.env["delivery.carrier"].search([("name", "=", "FedEx")])
        self.assertTrue(fedex, "FedEx carrier must be created.")
        self.assertEqual(
            set(fedex.sap_transporter_ids.mapped("sap_trnspcode")),
            {2},
            "FedEx carrier must link to its SAP transport code.",
        )

    def test_load_carriers_does_not_create_carrier_account_model(self):
        """delivery.carrier.account no longer exists in the registry at all."""
        self.assertNotIn(
            "delivery.carrier.account",
            self.env.registry.models,
            "delivery.carrier.account must be fully removed from the registry.",
        )

    def test_load_carrier_accounts_method_removed(self):
        """The old account-loading method is gone from the importer, not dormant."""
        self.assertFalse(
            hasattr(self.importer, "_load_carrier_accounts"),
            "_load_carrier_accounts must be deleted, not merely unused.",
        )

    def test_extract_returns_carriers_only(self):
        """extract/transform return a plain carriers dict, no account tuple element."""
        sap_rows = [{"cardcode": "C001", "shiptype": 1, "trnspname": "UPS#12345"}]
        extracted, transformed = self._run_pipeline(sap_rows)

        self.assertIsInstance(extracted, dict)
        self.assertIsInstance(transformed, dict)
        for value in transformed.values():
            self.assertIsInstance(
                value,
                set,
                "Each carrier's value must be a set of SAP transport codes, "
                "not part of a (carriers, accounts) tuple.",
            )
