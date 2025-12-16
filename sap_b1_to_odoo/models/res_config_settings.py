from odoo import api, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    @api.model
    def _sap_enable_required_features(self):
        """Enable features required by SAP import.

        Called from data/res_config_settings.xml during module install.
        This properly executes the settings wizard to enable features with
        all their side effects (group implications, default record creation, etc).
        """
        self.create(
            {
                "group_product_pricelist": True,
                "group_mrp_routings": True,  # Work Orders
            }
        ).execute()
