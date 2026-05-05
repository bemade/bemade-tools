from odoo import models
import itertools

XTUPLE_UNIQUE_FIELDS = (
    "xtuple_cust_id",
    "xtuple_vend_id",
    "xtuple_cntct_id",
    "xtuple_shipto_id",
)

class BasePartnerMergeAutomaticWizard(models.TransientModel):
    _inherit = "base.partner.merge.automatic.wizard"

    def _merge(self, partner_ids, dst_partner=None, extra_checks=True):
        self.env.cr.execute("SET CONSTRAINTS ALL DEFERRED")
        super()._merge(partner_ids, dst_partner, extra_checks)

    def _update_values(self, src_partners, dst_partner):
        """
        Use a buffer to move the unique-constrained values introduced by this module
        so that contact merge operations can cleanly complete. Use the destination
        partner vals as the last in the chain so that they take precedence.
        """
        unique_vals = [
            {
                field: getattr(partner, field)
                for field in XTUPLE_UNIQUE_FIELDS
            }
            for partner in itertools.chain(src_partners, dst_partner)
        ]

        src_partners.write({
            field: False
            for field in XTUPLE_UNIQUE_FIELDS
        })

        dst_partner.write({
            k: v for d in unique_vals for k, v in d.items()
        })
        super()._update_values(src_partners, dst_partner)
