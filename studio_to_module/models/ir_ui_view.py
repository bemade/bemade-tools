# -*- coding: utf-8 -*-

import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class IrUiView(models.Model):
    _inherit = "ir.ui.view"

    is_studio_view = fields.Boolean(
        compute="_compute_is_studio_view",
        search="_search_is_studio_view",
        string="Is Studio View",
        help="Indicates if this view was created by Studio",
    )
    converted_to_module = fields.Boolean(
        string="Converted to Module",
        default=False,
        copy=False,
        help="Indicates if this Studio view has been converted to a module",
    )
    target_module_id = fields.Many2one(
        comodel_name="ir.module.module",
        string="Target Module",
        ondelete="set null",
        copy=False,
        help="Module where this Studio view was converted to",
    )
    pending_cleanup = fields.Boolean(
        string="Pending Cleanup",
        default=False,
        copy=False,
        help="Indicates if this view should be deleted after module update",
    )

    def _compute_is_studio_view(self):
        """Compute if view is from Studio without depending on studio field."""
        for view in self:
            # Check if 'studio' field exists (from web_studio module)
            has_studio_field = bool(getattr(view, "studio", False))

            # Also check if xml_id indicates Studio customization
            has_studio_xmlid = False
            if view.xml_id:
                # Check if xml_id starts with 'studio_customization.' or 'odoo_studio_'
                if (
                    view.xml_id.startswith("studio_customization.")
                    or view.xml_id.startswith("odoo_studio_")
                    or "odoo_studio_" in view.xml_id
                ):
                    has_studio_xmlid = True

            view.is_studio_view = has_studio_field or has_studio_xmlid

    def _search_is_studio_view(self, operator, value):
        """Search method for is_studio_view field."""
        studio_domain = []
        xmlid_domain = []

        # Check if studio field exists in ir.ui.view
        if "studio" in self.env["ir.ui.view"]._fields:
            if (operator == "=" and value) or (operator == "!=" and not value):
                studio_domain = [("studio", "=", True)]
            else:
                studio_domain = ["|", ("studio", "=", False), ("studio", "=", None)]

        # Always check xml_id pattern
        IrModelData = self.env["ir.model.data"].sudo()
        if (operator == "=" and value) or (operator == "!=" and not value):
            # Find views with module 'studio_customization' or name starting with 'odoo_studio_'
            model_data = IrModelData.search(
                [
                    ("model", "=", "ir.ui.view"),
                    "|",
                    ("module", "=", "studio_customization"),
                    ("name", "=like", "odoo_studio_%"),
                ]
            )
            if model_data:
                xmlid_domain = [("id", "in", model_data.mapped("res_id"))]

        # Combine domains with OR
        if studio_domain and xmlid_domain:
            return ["|"] + studio_domain + xmlid_domain
        elif studio_domain:
            return studio_domain
        elif xmlid_domain:
            return xmlid_domain
        else:
            # No Studio views found
            return [("id", "=", False)]

    def mark_for_conversion(self, target_module):
        """Mark this Studio view as converted to a module.

        :param target_module: ir.module.module record
        """
        self.ensure_one()
        self.write(
            {
                "converted_to_module": True,
                "target_module_id": target_module.id,
                "pending_cleanup": True,
            }
        )

    def cleanup_converted_views(self):
        """Delete Studio views that have been converted and their module is updated.

        This method handles views with inheritance by:
        1. Deleting child views (that inherit from Studio views) first
        2. Then deleting the parent Studio views

        Uses savepoints to prevent transaction abortion on failure.
        """
        views_to_delete = self.search(
            [
                ("pending_cleanup", "=", True),
                ("converted_to_module", "=", True),
            ]
        )
        views_to_delete._unlink_tree()

    def _unlink_tree(self):
        """Delete a view and its children. In the case that a child view cannot be deleted, break the link to the parent instead."""
        all_views = self._get_subtree()
        while all_views:
            leaves = all_views.filtered(lambda v: not v.inherit_children_ids)
            leaves.unlink()
            all_views -= leaves

    def _get_subtree(self):
        """Get all views in the inheritance tree of the current view."""
        views = self.env["ir.ui.view"]
        for view in self:
            views |= view | view.inherit_children_ids._get_subtree()
        return views
