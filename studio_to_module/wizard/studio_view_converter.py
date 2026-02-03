# -*- coding: utf-8 -*-

import logging
import os
import re
import json
import shutil
from datetime import datetime

from lxml import etree

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class StudioViewConverter(models.TransientModel):
    _name = "studio.view.converter"
    _description = "Studio View to Module Converter"

    studio_view_ids = fields.Many2many(
        comodel_name="ir.ui.view",
        string="Studio Views",
        domain=[("is_studio_view", "=", True), ("converted_to_module", "=", False)],
        required=True,
        help="Select the Studio views to convert",
    )
    target_module_id = fields.Many2one(
        comodel_name="ir.module.module",
        string="Target Module",
        required=True,
        domain=lambda self: [("id", "in", self._get_allowed_module_ids())],
        help="Select the custom module where views will be added (any non-odoo/enterprise module)",
    )
    allowed_module_info = fields.Text(
        compute="_compute_allowed_module_info",
        string="Eligible Modules",
        compute_sudo=True,
        help="List of eligible modules (any non-odoo/enterprise module)",
    )
    module_path = fields.Char(
        compute="_compute_module_path",
        string="Module Path",
        help="File system path of the target module",
    )
    view_folder = fields.Char(
        string="Views Folder",
        default="views",
        required=True,
        help="Folder name within the module where XML files will be created",
    )
    auto_cleanup = fields.Boolean(
        string="Auto Cleanup Studio Views",
        default=True,
        help="Automatically delete Studio views after module update",
    )
    rename_studio_fields = fields.Boolean(
        string="Clean Field Names",
        default=False,
        help="Remove 'x_studio_' prefix from field names (e.g., x_studio_approval_date → approval_date). "
        "Data will be automatically copied from old to new field names.",
    )
    has_studio_fields = fields.Boolean(
        compute="_compute_has_studio_fields",
        string="Has Studio Fields",
        help="True if selected views contain Studio custom fields",
    )
    preview_xml = fields.Text(
        compute="_compute_preview_xml",
        string="Preview XML",
        help="Preview of the XML that will be generated",
    )
    backup_path = fields.Char(
        string="Backup Path",
        readonly=True,
        help="Path where backup is stored",
    )

    def _get_allowed_modules(self):
        try:
            from odoo.modules.module import get_module_path
        except Exception:
            get_module_path = None

        modules = self.env["ir.module.module"].search([("state", "=", "installed")])
        allowed_modules = self.env["ir.module.module"]
        allowed_info = []
        all_modules = (
            self.env["ir.module.module"].sudo().search([("state", "=", "installed")])
        )

        # Virtual/non-physical modules to exclude (no physical folder)
        virtual_modules = {
            "studio_customization",  # Odoo Studio virtual module
            "base_import_module",  # Used for module import
            "web_studio",  # Sometimes virtual
        }

        for module in all_modules:
            # Skip virtual modules that don't have physical folders
            if module.name in virtual_modules:
                continue

            module_path = None
            if get_module_path:
                try:
                    module_path = get_module_path(module.name)
                except Exception:
                    module_path = None
            if not module_path:
                try:
                    # Fallback for older Odoo versions
                    import odoo.modules as addons

                    module_path = addons.get_module_path(
                        module.name, display_warning=False
                    )
                except Exception:
                    module_path = None
            if not module_path:
                continue

            # Only exclude odoo core and enterprise modules
            # Get the full parent path to check if it's in odoo/enterprise directories
            parent_path = os.path.dirname(module_path)

            # Skip if the module is inside odoo or enterprise directories
            if "/odoo/" in parent_path or parent_path.endswith("/odoo"):
                continue
            if "/enterprise" in parent_path or parent_path.endswith("/enterprise"):
                continue
            if "/design-themes" in parent_path or parent_path.endswith(
                "/design-themes"
            ):
                continue

            allowed_modules |= module
            allowed_info.append((module.name, module_path))

        allowed_modules = (
            allowed_modules.sorted("name") if allowed_modules else allowed_modules
        )
        allowed_info.sort(key=lambda item: item[0])
        return allowed_modules, allowed_info

    def _get_allowed_module_ids(self):
        """Get IDs of allowed modules for domain filtering."""
        allowed_modules, _info = self._get_allowed_modules()
        return allowed_modules.ids if allowed_modules else []

    @api.depends_context("uid")
    def _compute_allowed_module_info(self):
        allowed_modules, allowed_info = self._get_allowed_modules()
        info_lines = [f"{name}: {path}" for name, path in allowed_info]
        info_text = (
            "\n".join(info_lines)
            if info_lines
            else _("No eligible modules found in addons/ directory.")
        )

        for wizard in self:
            wizard.allowed_module_info = info_text

    @api.constrains("target_module_id")
    def _check_target_module_id_location(self):
        allowed_modules, _info = self._get_allowed_modules()
        for wizard in self:
            if wizard.target_module_id and allowed_modules:
                if wizard.target_module_id not in allowed_modules:
                    raise ValidationError(
                        _("Only non-odoo/enterprise modules can be selected.")
                    )

    @api.depends("target_module_id")
    def _compute_module_path(self):
        for wizard in self:
            if wizard.target_module_id:
                # Try to find module path using odoo.modules.module.get_module_path
                module_name = wizard.target_module_id.name
                try:
                    from odoo.modules.module import get_module_path

                    module_path = get_module_path(module_name)
                    wizard.module_path = (
                        module_path if module_path else "Module path not found"
                    )
                except Exception:
                    wizard.module_path = "Module path not found"
            else:
                wizard.module_path = ""

    @api.depends("studio_view_ids")
    def _compute_has_studio_fields(self):
        """Compute whether selected views contain Studio custom fields."""
        for wizard in self:
            if wizard.studio_view_ids:
                studio_fields = wizard._get_studio_fields_for_views(
                    wizard.studio_view_ids
                )
                wizard.has_studio_fields = bool(studio_fields)
            else:
                wizard.has_studio_fields = False

    @api.depends("studio_view_ids", "target_module_id")
    def _compute_preview_xml(self):
        for wizard in self:
            if wizard.studio_view_ids:
                preview_lines = []
                for view in wizard.studio_view_ids[:3]:  # Preview first 3 views
                    xml_content = wizard._generate_view_xml(view)
                    preview_lines.append(f"<!-- View: {view.name} -->")
                    preview_lines.append(
                        xml_content[:500] + "..."
                        if len(xml_content) > 500
                        else xml_content
                    )
                    preview_lines.append("")

                if len(wizard.studio_view_ids) > 3:
                    preview_lines.append(
                        f"... and {len(wizard.studio_view_ids) - 3} more views"
                    )

                wizard.preview_xml = "\n".join(preview_lines)
            else:
                wizard.preview_xml = ""

    def _sanitize_xml_id(self, name):
        """Convert a view name to a valid XML ID.

        :param str name: View name to sanitize
        :return: Valid XML ID
        :rtype: str
        """
        xml_id = re.sub(r"[^a-zA-Z0-9_]", "_", name.lower())
        xml_id = re.sub(r"_+", "_", xml_id)
        xml_id = xml_id.strip("_")
        return xml_id or "view"

    def _generate_view_xml(self, view):
        """Generate XML content for a view with proper indentation and comments.

        :param view: ir.ui.view record
        :return: XML string
        :rtype: str
        """
        xml_id = self._sanitize_xml_id(view.name)

        # Parse the arch to ensure it's valid XML and indent it properly
        try:
            arch_tree = etree.fromstring(view.arch)
            arch_string = etree.tostring(
                arch_tree, encoding="unicode", pretty_print=True
            )
            # Indent arch content (add 4 more spaces to each line)
            arch_lines = arch_string.strip().split("\n")
            arch_indented = "\n".join(
                ["                " + line for line in arch_lines]
            )
        except Exception:
            arch_indented = "                " + view.arch.strip().replace(
                "\n", "\n                "
            )

        # Build comment header with documentation
        view_type = view.type or "form"

        comment_lines = [
            "",
            "        <!--",
            f"        View: {view.name}",
            f"        Model: {view.model}",
            f"        Type: {view_type}",
            f"        Priority: {view.priority}",
            f'        Mode: {view.mode or "primary"}',
            f"        Studio XML ID: {view.xml_id}",
        ]

        if view.inherit_id:
            comment_lines.append(f"        Inherits: {view.inherit_id.name}")

        comment_lines.extend(
            [
                "        ",
                "        This view was migrated from Odoo Studio.",
                "        Original Studio view will be deleted after module upgrade.",
                "        -->",
            ]
        )

        # Build the record XML
        xml_lines = [
            *comment_lines,
            f'        <record id="{xml_id}" model="ir.ui.view">',
            f'            <field name="name">{view.name}</field>',
            f'            <field name="model">{view.model}</field>',
        ]

        if view.inherit_id:
            inherit_xmlid = (
                view.inherit_id.xml_id
                or f"{view.inherit_id.model.replace('.', '_')}_view_{view.inherit_id.id}"
            )
            xml_lines.append(
                f'            <field name="inherit_id" ref="{inherit_xmlid}"/>'
            )

        if view.mode:
            xml_lines.append(f'            <field name="mode">{view.mode}</field>')

        if view.priority != 16:  # Default priority
            xml_lines.append(
                f'            <field name="priority">{view.priority}</field>'
            )

        # Add arch field with properly indented content
        xml_lines.append('            <field name="arch" type="xml">')
        xml_lines.append(arch_indented)
        xml_lines.append("            </field>")
        xml_lines.append("        </record>")

        return "\n".join(xml_lines)

    def _create_or_update_migrated_views_file(self, file_path, views):
        """Create or update the migrated_studio_views.xml file with all views.

        If the file exists, append new views. If not, create it.

        :param str file_path: Full path to the XML file
        :param views: Recordset of ir.ui.view
        """
        from lxml import etree

        # Check if file exists
        if os.path.exists(file_path):
            # Parse existing file
            try:
                tree = etree.parse(file_path)
                root = tree.getroot()
                data_element = root.find(".//data")

                if data_element is None:
                    # Create data element if it doesn't exist
                    data_element = etree.SubElement(root, "data")

                # Get existing view IDs to avoid duplicates
                existing_records = {}
                for record in data_element.findall('.//record[@model="ir.ui.view"]'):
                    record_id = record.get("id")
                    if record_id:
                        existing_records[record_id] = record

                # Add new views
                for view in views:
                    xml_id = self._sanitize_xml_id(view.name)

                    # Generate view XML and parse it
                    view_xml = self._generate_view_xml(view)
                    # Remove leading spaces to parse correctly
                    view_xml_clean = "\n".join(
                        [line.strip() for line in view_xml.split("\n") if line.strip()]
                    )
                    view_element = etree.fromstring(view_xml_clean)

                    existing_record = existing_records.get(xml_id)
                    if existing_record is not None:
                        parent = existing_record.getparent()
                        position = parent.index(existing_record)
                        parent.remove(existing_record)
                        parent.insert(position, view_element)
                        existing_records[xml_id] = view_element
                    else:
                        data_element.append(view_element)
                        existing_records[xml_id] = view_element

                # Write back with pretty print
                tree.write(
                    file_path, encoding="utf-8", xml_declaration=True, pretty_print=True
                )

            except Exception as e:
                _logger.warning(
                    f"Could not parse existing file {file_path}, recreating it: {e}"
                )
                # If parsing fails, recreate the file
                self._create_new_migrated_views_file(file_path, views)
        else:
            # Create new file
            self._create_new_migrated_views_file(file_path, views)

    def _create_new_migrated_views_file(self, file_path, views):
        """Create a new migrated_studio_views.xml file.

        :param str file_path: Full path to the XML file
        :param views: Recordset of ir.ui.view
        """
        xml_lines = [
            '<?xml version="1.0" encoding="utf-8"?>',
            "<odoo>",
            "    <data>",
            "",
        ]

        for view in views:
            xml_content = self._generate_view_xml(view)
            xml_lines.append(xml_content)
            xml_lines.append("")

        xml_lines.extend(
            [
                "    </data>",
                "</odoo>",
            ]
        )

        with open(file_path, "w", encoding="utf-8") as f:
            f.write("\n".join(xml_lines))

    def _get_or_create_views_file(self, module_path, model_name):
        """Get or create the views XML file for a model"""
        views_dir = os.path.join(module_path, self.view_folder)

        # Create views directory if it doesn't exist
        if not os.path.exists(views_dir):
            os.makedirs(views_dir)

        # Sanitize model name for filename
        file_name = f"{model_name.replace('.', '_')}_views.xml"
        file_path = os.path.join(views_dir, file_name)

        return file_path, file_name

    def _create_xml_file(self, file_path, views):
        """Create or update XML file with views"""
        from datetime import datetime

        migration_date = datetime.now().strftime("%Y-%m-%d")

        xml_lines = [
            '<?xml version="1.0" encoding="utf-8"?>',
            "<!--",
            "    ============================================================================",
            "    STUDIO TO MODULE MIGRATION",
            "    ============================================================================",
            "    ",
            "    This file was automatically generated by the studio_to_module converter.",
            "    ",
            f"    Migration Date: {migration_date}",
            "    Source: Odoo Studio customizations",
            "    Generator: studio_to_module (bemade-tools)",
            "    ",
            "    DO NOT EDIT THE METADATA ABOVE - It is used for tracking.",
            "    You can safely edit the view definitions below.",
            "    ",
            "    For more information about this migration:",
            "    - See module documentation",
            "    - Check .studio_backups/ folder for original Studio views",
            "    ============================================================================",
            "-->",
            "<odoo>",
            "    <data>",
            "",
        ]

        for view in views:
            xml_content = self._generate_view_xml(view)
            xml_lines.append(xml_content)
            xml_lines.append("")

        xml_lines.extend(
            [
                "    </data>",
                "</odoo>",
            ]
        )

        with open(file_path, "w", encoding="utf-8") as f:
            f.write("\n".join(xml_lines))

    def _create_backup(self, module_path):
        """Create a backup of views before conversion.

        :param str module_path: Path to the module
        :return: Backup directory path
        :rtype: str
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = os.path.join(module_path, ".studio_backups", timestamp)

        try:
            os.makedirs(backup_dir, exist_ok=True)

            # Backup Studio views data (XML export of views)
            views_data = []
            for view in self.studio_view_ids:
                views_data.append(
                    {
                        "id": view.id,
                        "name": view.name,
                        "xml_id": view.xml_id,
                        "model": view.model,
                        "type": view.type,
                        "arch": view.arch,
                        "inherit_id": view.inherit_id.id if view.inherit_id else None,
                        "priority": view.priority,
                        "mode": view.mode,
                    }
                )

            backup_file = os.path.join(backup_dir, "studio_views_backup.json")
            with open(backup_file, "w", encoding="utf-8") as f:
                json.dump(views_data, f, indent=2, ensure_ascii=False)

            # Backup existing module files that will be modified
            files_to_backup = [
                "__manifest__.py",
                "__init__.py",
                "hooks.py",
            ]

            for filename in files_to_backup:
                src = os.path.join(module_path, filename)
                if os.path.exists(src):
                    dst = os.path.join(backup_dir, filename)
                    shutil.copy2(src, dst)

            # Backup views folder if exists
            views_folder = os.path.join(module_path, self.view_folder)
            if os.path.exists(views_folder):
                backup_views_folder = os.path.join(backup_dir, self.view_folder)
                shutil.copytree(views_folder, backup_views_folder)

            _logger.info("Backup created at: %s", backup_dir)
            return backup_dir

        except Exception as e:
            _logger.error("Failed to create backup: %s", e)
            raise UserError(_("Failed to create backup: %s") % str(e))

    def _rollback_from_backup(self, backup_dir, module_path):
        """Rollback changes from backup.

        :param str backup_dir: Backup directory path
        :param str module_path: Module path
        """
        try:
            _logger.warning("Rolling back changes from backup: %s", backup_dir)

            # Restore backed up files
            for filename in os.listdir(backup_dir):
                src = os.path.join(backup_dir, filename)
                dst = os.path.join(module_path, filename)

                if os.path.isfile(src) and filename != "studio_views_backup.json":
                    shutil.copy2(src, dst)
                    _logger.info("Restored file: %s", filename)
                elif os.path.isdir(src) and filename == self.view_folder:
                    # Remove current views folder and restore backup
                    if os.path.exists(dst):
                        shutil.rmtree(dst)
                    shutil.copytree(src, dst)
                    _logger.info("Restored folder: %s", filename)

            _logger.info("Rollback completed successfully")

        except Exception as e:
            _logger.error("Failed to rollback: %s", e)
            raise UserError(_("Failed to rollback changes: %s") % str(e))

    def _create_or_update_hooks(
        self, module_path, module_name, studio_external_ids, field_rename_mapping=None
    ):
        """Create or update hooks.py for automatic Studio views cleanup and field data migration.

        :param str module_path: Path to the module
        :param str module_name: Name of the module
        :param list studio_external_ids: List of Studio view external IDs to clean up
        :param dict field_rename_mapping: Dict {model: {old_name: new_name}} for data migration
        """
        hooks_path = os.path.join(module_path, "hooks.py")

        # Format external IDs list for Python
        ids_list = (
            ",\n        ".join([f"'{xmlid}'" for xmlid in studio_external_ids])
            if studio_external_ids
            else ""
        )

        # Generate field migration code if needed
        field_migration_code = ""
        if field_rename_mapping:
            field_migration_code = "\n    # Migrate Studio custom fields data\n"
            field_migration_code += "    _migrate_studio_fields_data(env)\n"

            # Generate the migration function
            migration_function = "\n\ndef _migrate_studio_fields_data(env):\n"
            migration_function += (
                '    """Migrate data from Studio custom fields to Python fields."""\n'
            )
            migration_function += "    import logging\n"
            migration_function += "    _logger = logging.getLogger(__name__)\n\n"

            for model_name, field_mapping in field_rename_mapping.items():
                migration_function += f"    # Migrate fields for model '{model_name}'\n"
                migration_function += f"    try:\n"
                migration_function += f"        Model = env['{model_name}']\n"
                migration_function += f"        model_obj = env['ir.model'].search([('model', '=', '{model_name}')], limit=1)\n"
                migration_function += f"        if not model_obj:\n"
                migration_function += (
                    f"            _logger.warning('Model {model_name} not found')\n"
                )
                migration_function += f"        else:\n"
                migration_function += f"            table_name = model_obj.table\n\n"

                for old_name, new_name in field_mapping.items():
                    migration_function += (
                        f"            # Copy data from {old_name} to {new_name}\n"
                    )
                    migration_function += f"            env.cr.execute('''\n"
                    migration_function += f"                UPDATE %s \n"
                    migration_function += (
                        f"                SET {new_name} = {old_name}\n"
                    )
                    migration_function += (
                        f"                WHERE {old_name} IS NOT NULL\n"
                    )
                    migration_function += f"            ''' % table_name)\n"
                    migration_function += f"            _logger.info('Copied {{env.cr.rowcount}} rows from {old_name} to {new_name} in {model_name}')\n\n"

                    migration_function += f"            # Optionally drop old column after successful copy\n"
                    migration_function += f"            # env.cr.execute('ALTER TABLE %s DROP COLUMN IF EXISTS {old_name}' % table_name)\n"
                    migration_function += f"            # _logger.info('Dropped old column {old_name} from {model_name}')\n\n"

                migration_function += f"    except Exception as e:\n"
                migration_function += f"        _logger.error('Failed to migrate fields for {model_name}: %s', e)\n\n"

            field_migration_code = migration_function + field_migration_code

        # Generate hooks.py template
        from datetime import datetime

        migration_date = datetime.now().strftime("%Y-%m-%d")

        hooks_template = f'''# -*- coding: utf-8 -*-
# ============================================================================
# STUDIO TO MODULE MIGRATION - Post-Installation Hooks
# ============================================================================
#
# This file was automatically generated by studio_to_module converter.
#
# Migration Date: {migration_date}
# Module: {module_name}
# Generator: studio_to_module (bemade-tools)
#
# PURPOSE:
# This hook performs two main tasks after module installation/upgrade:
# 1. Migrate data from Studio custom fields to Python fields (if renaming)
# 2. Clean up original Studio view definitions
#
# IMPORTANT NOTES:
# - This hook runs automatically on module install/upgrade
# - Studio views are safely deleted after successful migration
# - Field data is preserved through SQL migrations
# - Backups are available in .studio_backups/ folder
#
# For more information:
# - See module documentation
# - Check hooks.py for cleanup logic
# ============================================================================

import logging

_logger = logging.getLogger(__name__)

{field_migration_code}

def post_init_hook(env):
    """Clean up Studio views and migrate field data after module installation/upgrade.
    
    This hook is executed automatically when the module is installed or upgraded.
    It performs the following operations:
    
    1. Migrates data from Studio custom fields to Python-defined fields (if fields were renamed)
    2. Deletes original Studio view definitions that have been converted to XML
    
    The cleanup is safe and will only delete Studio views that have been successfully
    migrated to module code.
    """
    # List of Studio view external IDs to delete
    # Format: 'module.xml_id' (e.g., 'studio_customization.odoo_studio_xxx')
    studio_view_ids_to_delete = {ids_list}
    
    # Clean up Studio views
    for xml_id in studio_view_ids_to_delete:
        try:
            view = env.ref(xml_id, raise_if_not_found=False)
            if view:
                _logger.info('Deleting Studio view: %s (ID: %s)', xml_id, view.id)
                view.unlink()
            else:
                _logger.debug('Studio view %s already deleted or not found', xml_id)
        except Exception as e:
            _logger.warning('Failed to delete Studio view %s: %s', xml_id, e)
'''

        # Create or update hooks.py
        if not os.path.exists(hooks_path):
            # Create new hooks.py
            with open(hooks_path, "w", encoding="utf-8") as f:
                f.write(hooks_template)
            _logger.info(
                "Created hooks.py for module %s with %d external IDs",
                module_name,
                len(studio_external_ids),
            )

            # Update __init__.py to import hooks
            self._update_init_py(module_path)

            # Update manifest to add post_init_hook
            self._update_manifest_hook(module_path)
        else:
            # Update existing hooks.py by adding new external IDs
            self._update_existing_hooks(hooks_path, studio_external_ids, module_name)

    def _update_existing_hooks(self, hooks_path, new_external_ids, module_name):
        """Update existing hooks.py file by adding new external IDs to the list.

        :param str hooks_path: Path to hooks.py file
        :param list new_external_ids: List of new external IDs to add
        :param str module_name: Name of the module
        """
        if not new_external_ids:
            _logger.info("No new external IDs to add to hooks.py")
            return

        try:
            with open(hooks_path, "r", encoding="utf-8") as f:
                content = f.read()

            # Find the studio_view_ids_to_delete list
            import re

            pattern = r"studio_view_ids_to_delete\s*=\s*\[(.*?)\]"
            match = re.search(pattern, content, re.DOTALL)

            if match:
                existing_ids_str = match.group(1)
                # Extract existing IDs
                existing_ids = re.findall(r"'([^']+)'", existing_ids_str)

                # Add new IDs (avoid duplicates)
                all_ids = list(set(existing_ids + new_external_ids))
                all_ids.sort()  # Sort for consistency

                # Format new list
                ids_formatted = ",\n        ".join(
                    [f"'{xml_id}'" for xml_id in all_ids]
                )
                new_list = f"[\n        {ids_formatted},\n    ]"

                # Replace in content
                new_content = re.sub(
                    pattern,
                    f"studio_view_ids_to_delete = {new_list}",
                    content,
                    flags=re.DOTALL,
                )

                with open(hooks_path, "w", encoding="utf-8") as f:
                    f.write(new_content)

                _logger.info(
                    "Updated hooks.py for module %s: added %d new external IDs (total: %d)",
                    module_name,
                    len(new_external_ids),
                    len(all_ids),
                )
            else:
                _logger.warning(
                    "Could not find studio_view_ids_to_delete list in hooks.py"
                )

        except Exception as e:
            _logger.error("Failed to update existing hooks.py: %s", e)

    def _update_init_py(self, module_path):
        """Add hooks import to __init__.py if not present."""
        init_path = os.path.join(module_path, "__init__.py")

        if os.path.exists(init_path):
            with open(init_path, "r", encoding="utf-8") as f:
                content = f.read()

            # Check if hooks is already imported
            if (
                "from . import hooks" not in content
                and "from .import hooks" not in content
            ):
                # Add import at the end
                if not content.endswith("\n"):
                    content += "\n"
                content += "from . import hooks\n"

                with open(init_path, "w", encoding="utf-8") as f:
                    f.write(content)
                _logger.info("Updated __init__.py to import hooks")

    def _update_manifest_hook(self, module_path):
        """Add post_init_hook to manifest if not present."""
        manifest_path = os.path.join(module_path, "__manifest__.py")

        if os.path.exists(manifest_path):
            with open(manifest_path, "r", encoding="utf-8") as f:
                content = f.read()

            # Check if post_init_hook is already defined
            if "'post_init_hook'" not in content and '"post_init_hook"' not in content:
                # Add before the closing brace
                if content.rstrip().endswith("}"):
                    # Find the last }
                    last_brace = content.rfind("}")
                    # Insert before it
                    new_content = (
                        content[:last_brace].rstrip()
                        + "\n    'post_init_hook': 'post_init_hook',\n"
                        + content[last_brace:]
                    )

                    # Clean up double commas
                    new_content = self._clean_manifest_commas(new_content)

                    with open(manifest_path, "w", encoding="utf-8") as f:
                        f.write(new_content)
                    _logger.info("Updated __manifest__.py to add post_init_hook")

    def _clean_manifest_commas(self, content):
        """Clean up double commas and trailing commas in manifest content.

        :param str content: Manifest file content
        :return: Cleaned content
        :rtype: str
        """
        # Remove double commas (with or without whitespace)
        content = re.sub(r",\s*,+", ",", content)
        # Remove triple+ commas that might remain
        content = re.sub(r",+", ",", content)
        # Remove trailing comma before closing bracket/brace
        content = re.sub(r",\s*\]", "]", content)
        content = re.sub(r",\s*\}", "}", content)
        return content

    def _add_studio_cleanup_dependency(self, module_path):
        """Add studio_cleanup to module dependencies if not present.

        :param str module_path: Path to the module
        """
        manifest_path = os.path.join(module_path, "__manifest__.py")

        if not os.path.exists(manifest_path):
            return

        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                content = f.read()

            # Check if studio_cleanup is already in dependencies
            if "'studio_cleanup'" in content or '"studio_cleanup"' in content:
                _logger.debug("studio_cleanup already in dependencies")
                return

            # Find the depends list (support both quote types)
            import re

            depends_pattern = r"['\"]depends['\"]\s*:\s*\[(.*?)\]"
            match = re.search(depends_pattern, content, re.DOTALL)

            if match:
                existing_deps = match.group(1).strip()
                # Ensure no trailing comma
                existing_deps = existing_deps.rstrip(",")

                # Add studio_cleanup to the list (with proper comma handling)
                if existing_deps:
                    new_deps = existing_deps + ",\n        'studio_cleanup'"
                else:
                    new_deps = "'studio_cleanup'"

                # Determine quote style used in original
                quote_char = "'" if "'depends'" in content else '"'

                new_content = re.sub(
                    depends_pattern,
                    f"{quote_char}depends{quote_char}: [{new_deps}]",
                    content,
                    flags=re.DOTALL,
                )

                # Clean up any double commas
                new_content = self._clean_manifest_commas(new_content)

                with open(manifest_path, "w", encoding="utf-8") as f:
                    f.write(new_content)

                _logger.info("Added studio_cleanup to module dependencies")
            else:
                _logger.warning("Could not find depends list in manifest")

        except Exception as e:
            _logger.error("Failed to add studio_cleanup dependency: %s", e)

    def _update_manifest(self, module_path, new_data_files):
        """Update module manifest to include new view files"""
        manifest_path = os.path.join(module_path, "__manifest__.py")

        if not os.path.exists(manifest_path):
            raise UserError(_("Manifest file not found at %s") % manifest_path)

        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest_content = f.read()

        # Parse the manifest to find the 'data' key (support both ' and ")
        data_pattern = r"['\"]data['\"]\s*:\s*\[(.*?)\]"
        matches = list(re.finditer(data_pattern, manifest_content, re.DOTALL))

        if len(matches) > 1:
            # Multiple 'data' sections found - merge them
            _logger.warning("Multiple data sections found in manifest, merging them")
            # Keep only the first one and merge content
            first_match = matches[0]
            all_data = first_match.group(1)

            # Collect data from other sections
            for match in matches[1:]:
                other_data = match.group(1).strip()
                if other_data:
                    all_data += ",\n        " + other_data

            # Add new files
            for new_file in new_data_files:
                file_entry = f"'{self.view_folder}/{new_file}'"
                if file_entry not in all_data and new_file not in all_data:
                    if all_data.strip():
                        # Check if all_data already ends with a comma
                        trimmed = all_data.rstrip()
                        if trimmed.endswith(","):
                            all_data += f"\n        {file_entry}"
                        else:
                            all_data += f",\n        {file_entry}"
                    else:
                        all_data = f"\n        {file_entry}\n    "

            # Replace first occurrence
            new_data_section = f"'data': [{all_data}]"
            manifest_content = (
                manifest_content[: first_match.start()]
                + new_data_section
                + manifest_content[first_match.end() :]
            )

            # Remove other occurrences
            for match in reversed(matches[1:]):
                # Remove the duplicate section including trailing comma
                start = match.start()
                end = match.end()
                # Check for trailing comma and newline
                if manifest_content[end : end + 2] == ",,":
                    end += 2
                elif manifest_content[end : end + 1] == ",":
                    end += 1
                manifest_content = manifest_content[:start] + manifest_content[end:]

        elif matches:
            # Single 'data' section found
            match = matches[0]
            current_data = match.group(1)
            # Add new files if not already present
            for new_file in new_data_files:
                file_entry = f"'{self.view_folder}/{new_file}'"
                if file_entry not in current_data and new_file not in current_data:
                    # Add before the closing bracket
                    if current_data.strip():
                        # Check if current_data already ends with a comma
                        trimmed = current_data.rstrip()
                        if trimmed.endswith(","):
                            current_data += f"\n        {file_entry}"
                        else:
                            current_data += f",\n        {file_entry}"
                    else:
                        current_data = f"\n        {file_entry}\n    "

            new_data_section = f"'data': [{current_data}]"
            manifest_content = re.sub(
                data_pattern,
                new_data_section,
                manifest_content,
                flags=re.DOTALL,
                count=1,
            )
        else:
            # No 'data' key found, add it
            # Find a good place to insert (after 'depends' if exists)
            depends_pattern = r"(['\"]depends['\"]\s*:\s*\[.*?\],)"
            match = re.search(depends_pattern, manifest_content, re.DOTALL)

            data_files_str = ",\n        ".join(
                [f"'{self.view_folder}/{f}'" for f in new_data_files]
            )
            new_data_section = f"\n    'data': [\n        {data_files_str}\n    ],"

            if match:
                manifest_content = manifest_content.replace(
                    match.group(1), match.group(1) + new_data_section
                )
            else:
                # Just add before the closing brace
                manifest_content = (
                    manifest_content.rstrip("\n}") + new_data_section + "\n}"
                )

        # Final cleanup: remove all double commas
        manifest_content = self._clean_manifest_commas(manifest_content)

        with open(manifest_path, "w", encoding="utf-8") as f:
            f.write(manifest_content)

    def _extract_field_names_from_arch(self, arch_xml):
        """Extract all field names from view architecture.

        :param str arch_xml: View architecture XML
        :return: Set of field names
        :rtype: set
        """
        field_names = set()

        try:
            # Parse XML
            tree = etree.fromstring(arch_xml)

            # Find all <field> elements
            for field_elem in tree.xpath("//field[@name]"):
                field_name = field_elem.get("name")
                if field_name:
                    field_names.add(field_name)
        except Exception as e:
            _logger.warning("Failed to parse arch XML: %s", e)
            # Fallback: use regex
            pattern = r'<field[^>]+name=["\']([^"\']+)["\']'
            matches = re.findall(pattern, arch_xml)
            field_names.update(matches)

        return field_names

    def _get_studio_fields_for_views(self, views):
        """Get Studio custom fields that are used in the given views."""
        if not views:
            return self.env["ir.model.fields"]

        # Get all field names referenced in the views
        field_names = set()
        for view in views:
            arch = view.arch
            # Extract field names from arch (simplified parsing)
            import re

            field_matches = re.findall(r'<field\s+name=["\']([^"\']+)["\']', arch)
            field_names.update(field_matches)

        # Filter to only Studio custom fields (x_studio_* or x_*)
        studio_field_names = [name for name in field_names if name.startswith("x_")]

        if not studio_field_names:
            return self.env["ir.model.fields"]

        # Get the models from the views
        models = views.mapped("model")

        # Find the field records
        studio_fields = self.env["ir.model.fields"].search(
            [
                ("name", "in", studio_field_names),
                ("model_id.model", "in", models),
                ("state", "=", "manual"),  # Studio fields are manual
            ]
        )

        return studio_fields

    def _analyze_view_dependencies(self, views, target_module):
        """Analyze views to detect missing module dependencies.

        Returns dict with:
        - 'missing_dependencies': list of module names not in target module depends
        - 'xpath_issues': list of potential xpath problems
        - 'warnings': list of warning messages
        """
        import re
        from lxml import etree

        result = {
            "missing_dependencies": [],
            "xpath_issues": [],
            "warnings": [],
        }

        # Get current module dependencies
        current_depends = set()
        if target_module.dependencies_id:
            current_depends = set(dep.name for dep in target_module.dependencies_id)

        detected_modules = set()

        for view in views:
            arch = view.arch

            # 1. Detect ref="module.xml_id" references
            ref_matches = re.findall(r'ref=["\']([^"\']+)["\']', arch)
            for ref in ref_matches:
                if "." in ref:
                    module_name = ref.split(".")[0]
                    if module_name not in ["base", "web"]:  # Ignore common base modules
                        detected_modules.add(module_name)

            # 2. Detect button name="module.action" references
            button_matches = re.findall(
                r'name=["\']([^"\']+\.action_[^"\']+)["\']', arch
            )
            for button_ref in button_matches:
                if "." in button_ref:
                    module_name = button_ref.split(".")[0]
                    detected_modules.add(module_name)

            # 3. Detect xpath expressions that might fail
            try:
                arch_tree = etree.fromstring(arch)
                xpaths = arch_tree.xpath(".//xpath")
                for xpath_elem in xpaths:
                    xpath_expr = xpath_elem.get("expr", "")
                    # Check for button references in xpath
                    if "@name=" in xpath_expr and "." in xpath_expr:
                        # Extract module name from expressions like //button[@name='module.action']
                        matches = re.findall(
                            r'@name=["\']([^"\']+\.[\w_]+)', xpath_expr
                        )
                        for match in matches:
                            module_name = match.split(".")[0]
                            result["xpath_issues"].append(
                                {
                                    "view": view.name,
                                    "xpath": xpath_expr,
                                    "module": module_name,
                                }
                            )
                            detected_modules.add(module_name)
            except Exception:
                pass

        # Determine missing dependencies
        missing = detected_modules - current_depends - {target_module.name}
        result["missing_dependencies"] = sorted(list(missing))

        # Generate warnings
        if result["missing_dependencies"]:
            result["warnings"].append(
                f"Detected {len(result['missing_dependencies'])} module(s) referenced but not in dependencies"
            )

        if result["xpath_issues"]:
            result["warnings"].append(
                f"Found {len(result['xpath_issues'])} xpath expression(s) that may fail if referenced modules are not installed"
            )

        return result

    def _clean_field_name(self, field_name):
        """Remove x_studio_ prefix from field name.

        :param str field_name: Original field name
        :return: Cleaned field name
        :rtype: str
        """
        if field_name.startswith("x_studio_"):
            return field_name.replace("x_studio_", "", 1)
        return field_name

    def _generate_field_python_code(self, field, rename_fields=False):
        """Generate Python code for a Studio field.

        :param field: ir.model.fields record
        :param bool rename_fields: If True, remove x_studio_ prefix from field name
        :return: Tuple (field_code, original_name, new_name, compute_method_code)
        :rtype: tuple
        """
        field_type = field.ttype
        original_field_name = field.name
        field_name = (
            self._clean_field_name(original_field_name)
            if rename_fields
            else original_field_name
        )
        compute_method_code = None

        # Map Odoo field types to Python field classes
        type_mapping = {
            "char": "fields.Char",
            "text": "fields.Text",
            "html": "fields.Html",
            "boolean": "fields.Boolean",
            "integer": "fields.Integer",
            "float": "fields.Float",
            "monetary": "fields.Monetary",
            "date": "fields.Date",
            "datetime": "fields.Datetime",
            "selection": "fields.Selection",
            "many2one": "fields.Many2one",
            "one2many": "fields.One2many",
            "many2many": "fields.Many2many",
            "binary": "fields.Binary",
        }

        field_class = type_mapping.get(field_type, "fields.Char")

        # Build parameters
        params = []

        # String (label)
        if field.field_description:
            params.append(f"string='{field.field_description}'")

        # Required
        if field.required:
            params.append("required=True")

        # Readonly
        if field.readonly:
            params.append("readonly=True")

        # Help
        if field.help:
            help_text = field.help.replace("'", "\\'").replace("\n", "\\n")
            params.append(f"help='{help_text}'")

        # Relation fields
        if field_type == "many2one" and field.relation:
            params.insert(0, f"'{field.relation}'")
        elif field_type == "one2many" and field.relation and field.relation_field:
            params.insert(0, f"'{field.relation}'")
            params.insert(1, f"'{field.relation_field}'")
        elif field_type == "many2many" and field.relation:
            params.insert(0, f"'{field.relation}'")

        # Selection
        if field_type == "selection" and field.selection_ids:
            selection_list = [(sel.value, sel.name) for sel in field.selection_ids]
            params.insert(0, str(selection_list))

        # Size for char
        if field_type == "char" and field.size:
            params.append(f"size={field.size}")

        # Digits for float
        if field_type == "float" and field.digits:
            params.append(f"digits=({field.digits}, 2)")  # Assuming 2 decimal places

        # Store
        if not field.store:
            params.append("store=False")

        # Compute - Generate proper compute method instead of inline code
        if field.compute:
            compute_code = field.compute.strip()

            # Check if compute is Python code (multiline or contains keywords)
            if "\n" in compute_code or "for " in compute_code or "self" in compute_code:
                # Generate a proper compute method
                method_name = f"_compute_{field_name}"
                params.append(f"compute='{method_name}'")

                # Format the compute code properly
                compute_lines = []
                compute_lines.append(f"    @api.depends()")
                compute_lines.append(f"    def {method_name}(self):")
                compute_lines.append(
                    f'        """Compute {field.field_description or field_name}."""'
                )

                # Try to format the compute code nicely
                # Replace 'record' with 'self' in proper context if needed
                # And ensure proper indentation
                code_lines = compute_code.split("\n")

                if "for record in self:" in compute_code:
                    # Has loop structure - preserve it
                    for line in code_lines:
                        if line.strip():
                            compute_lines.append(f"        {line}")
                else:
                    # Wrap in standard loop and adjust record references
                    compute_lines.append(f"        for record in self:")
                    for line in code_lines:
                        if line.strip():
                            # Indent the code
                            compute_lines.append(f"            {line}")

                compute_method_code = "\n".join(compute_lines)
            else:
                # Simple compute reference (method name)
                params.append(f"compute='{field.compute}'")

        # Default - Check if default attribute exists
        default_value = getattr(field, "default", None)
        if default_value:
            # Try to evaluate default safely
            try:
                default_val = eval(default_value)
                if isinstance(default_val, str):
                    params.append(f"default='{default_val}'")
                else:
                    params.append(f"default={default_val}")
            except Exception:
                pass

        params_str = ", ".join(params)
        field_code = f"    {field_name} = {field_class}({params_str})"

        return (field_code, original_field_name, field_name, compute_method_code)

    def _create_fields_python_file(
        self, module_path, fields_by_model, rename_fields=False
    ):
        """Create or update Python files for Studio custom fields.

        :param str module_path: Path to the module
        :param dict fields_by_model: Dict {model_name: [field_records]}
        :param bool rename_fields: If True, remove x_studio_ prefix from field names
        :return: Tuple (list of created file names, field rename mapping)
        :rtype: tuple
        """
        models_folder = os.path.join(module_path, "models")
        if not os.path.exists(models_folder):
            os.makedirs(models_folder)

        created_files = []
        field_rename_mapping = {}  # {model_name: {old_name: new_name}}

        for model_name, fields_list in fields_by_model.items():
            if not fields_list:
                continue

            # Generate filename from model
            model_safe = model_name.replace(".", "_")
            file_name = f"{model_safe}_custom_fields.py"
            file_path = os.path.join(models_folder, file_name)

            # Generate field definitions and track renames
            field_lines = []
            compute_methods = []
            model_renames = {}
            for field in fields_list:
                field_code, old_name, new_name, compute_method = (
                    self._generate_field_python_code(field, rename_fields)
                )
                field_lines.append(field_code)

                # Collect compute methods if generated
                if compute_method:
                    compute_methods.append(compute_method)

                # Track rename if field name changed
                if old_name != new_name:
                    model_renames[old_name] = new_name

            if model_renames:
                field_rename_mapping[model_name] = model_renames

            # Generate Python module
            from datetime import datetime

            migration_date = datetime.now().strftime("%Y-%m-%d")

            imports = "from odoo import fields, models"
            if compute_methods:
                imports = "from odoo import api, fields, models"

            # Build class content
            class_content = []
            class_content.append(
                f"class {model_safe.title().replace('_', '')}(models.Model):"
            )
            class_content.append(f'    """')
            class_content.append(f"    Custom Studio fields migrated to Python code.")
            class_content.append(f"    ")
            class_content.append(f"    Migration Info:")
            class_content.append(f"    - Date: {migration_date}")
            class_content.append(f"    - Source: Odoo Studio")
            class_content.append(f"    - Generator: studio_to_module")
            class_content.append(f"    - Model: {model_name}")
            class_content.append(f"    - Field Count: {len(fields_list)}")
            class_content.append(f'    """')
            class_content.append(f"    _inherit = '{model_name}'")
            class_content.append("")

            # Add field definitions
            for field_line in field_lines:
                class_content.append(field_line)

            # Add compute methods if any
            if compute_methods:
                class_content.append("")
                for compute_method in compute_methods:
                    class_content.append(compute_method)
                    class_content.append("")

            python_code = f"""# -*- coding: utf-8 -*-
# ============================================================================
# STUDIO TO MODULE MIGRATION - Custom Fields
# ============================================================================
#
# This file was automatically generated by studio_to_module converter.
#
# Migration Date: {migration_date}
# Source: Odoo Studio custom fields
# Generator: studio_to_module (bemade-tools)
# Model: {model_name}
# Fields: {len(fields_list)}
#
# These fields were originally created in Studio and have been converted
# to Python code for version control and deployment.
#
# IMPORTANT NOTES:
# - Backup available in .studio_backups/ folder
# - Original Studio field definitions will be cleaned up after module upgrade
# - You can safely edit these field definitions
# - Compute methods have been extracted for better maintainability
#
# For more information:
# - Check module documentation
# - See hooks.py for data migration logic
# ============================================================================

{imports}


{chr(10).join(class_content)}
"""

            with open(file_path, "w", encoding="utf-8") as f:
                f.write(python_code)

            created_files.append(file_name)
            _logger.info(
                "Created custom fields file: %s with %d fields",
                file_name,
                len(fields_list),
            )

        return created_files, field_rename_mapping

    def _update_models_init_py(self, module_path, model_files):
        """Update models/__init__.py to import new field files.

        :param str module_path: Path to the module
        :param list model_files: List of Python filenames to import
        """
        models_folder = os.path.join(module_path, "models")
        init_path = os.path.join(models_folder, "__init__.py")

        # Create __init__.py if it doesn't exist
        if not os.path.exists(init_path):
            with open(init_path, "w", encoding="utf-8") as f:
                f.write("# -*- coding: utf-8 -*-\n\n")

        # Read current content
        with open(init_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Add imports for new files
        imports_added = []
        for model_file in model_files:
            module_name = model_file.replace(".py", "")
            import_line = f"from . import {module_name}"

            if import_line not in content:
                if not content.endswith("\n"):
                    content += "\n"
                content += f"{import_line}\n"
                imports_added.append(module_name)

        if imports_added:
            with open(init_path, "w", encoding="utf-8") as f:
                f.write(content)
            _logger.info(
                "Updated models/__init__.py with %d imports", len(imports_added)
            )

    def action_convert_views(self):
        """Show preview wizard before conversion."""
        self.ensure_one()

        if not self.studio_view_ids:
            raise ValidationError(
                _("Please select at least one Studio view to convert.")
            )

        if not self.target_module_id:
            raise ValidationError(_("Please select a target module."))

        # Create preview wizard
        preview = self.env["studio.view.converter.preview"].create(
            {
                "converter_id": self.id,
            }
        )

        return {
            "type": "ir.actions.act_window",
            "name": _("Conversion Preview"),
            "res_model": "studio.view.converter.preview",
            "res_id": preview.id,
            "view_mode": "form",
            "target": "new",
        }

    def _auto_include_related_views(self, selected_views):
        """Auto-include parent and child Studio views to maintain hierarchy.

        This method ensures that when converting views, we always process complete
        view families together (parent + all children) to avoid foreign key issues.

        :param selected_views: Initially selected Studio views
        :return: Extended recordset with all related views
        """
        views_to_process = selected_views
        processed_ids = set(selected_views.ids)

        # Keep expanding until we find all related views
        changed = True
        while changed:
            changed = False
            current_views = views_to_process

            for view in current_views:
                # 1. Remonter: inclure le parent si c'est un enfant Studio
                if view.inherit_id and view.inherit_id.id not in processed_ids:
                    parent = view.inherit_id
                    # Vérifier si le parent est une vue Studio
                    if parent.is_studio_view and not parent.converted_to_module:
                        views_to_process |= parent
                        processed_ids.add(parent.id)
                        changed = True
                        _logger.info(
                            "Auto-included parent view: %s (ID: %s) of child %s",
                            parent.name,
                            parent.id,
                            view.name,
                        )

                # 2. Descendre: inclure tous les enfants Studio
                children = self.env["ir.ui.view"].search(
                    [
                        ("inherit_id", "=", view.id),
                        ("is_studio_view", "=", True),
                        ("converted_to_module", "=", False),
                        ("id", "not in", list(processed_ids)),
                    ]
                )
                if children:
                    views_to_process |= children
                    processed_ids.update(children.ids)
                    changed = True
                    _logger.info(
                        "Auto-included %d child view(s) of parent %s (ID: %s)",
                        len(children),
                        view.name,
                        view.id,
                    )

        return views_to_process

    def action_convert_views_confirmed(self):
        """Actually convert selected Studio views to module code (called after confirmation)."""
        self.ensure_one()

        if not self.studio_view_ids:
            raise ValidationError(
                _("Please select at least one Studio view to convert.")
            )

        if not self.target_module_id:
            raise ValidationError(_("Please select a target module."))

        # Auto-include related views (parents and children)
        original_count = len(self.studio_view_ids)
        all_views = self._auto_include_related_views(self.studio_view_ids)

        if len(all_views) > original_count:
            _logger.info(
                "Auto-included %d related view(s). Total views to convert: %d",
                len(all_views) - original_count,
                len(all_views),
            )
            # Update the wizard's view selection
            self.studio_view_ids = all_views

        # Get module path
        from odoo.modules.module import get_module_path

        module_name = self.target_module_id.name
        module_path = get_module_path(module_name)

        if not module_path or not os.path.exists(module_path):
            raise UserError(
                _(
                    "Module path not found for %s. Make sure the module is in the addons path."
                )
                % module_name
            )

        # AMÉLIORATION 1: Create backup before any modification
        backup_dir = None
        try:
            backup_dir = self._create_backup(module_path)
            self.backup_path = backup_dir
        except Exception as e:
            raise UserError(
                _("Failed to create backup: %s\n\nConversion aborted for safety.")
                % str(e)
            )

        # AMÉLIORATION 2: Try-catch with rollback on error
        try:
            # Ensure views folder exists
            views_folder = os.path.join(module_path, self.view_folder)
            if not os.path.exists(views_folder):
                os.makedirs(views_folder)

            # AMÉLIORATION 5: Group views by model and create separate files
            views_by_model = {}
            for view in self.studio_view_ids:
                model = view.model
                if model not in views_by_model:
                    views_by_model[model] = self.env["ir.ui.view"]
                views_by_model[model] |= view

            created_files = []

            # Create one file per model with "studio" in the name
            for model, views in views_by_model.items():
                model_safe = model.replace(".", "_")
                file_name = f"{model_safe}_studio_views.xml"
                file_path = os.path.join(views_folder, file_name)

                # Create or update the XML file for this model
                self._create_xml_file(file_path, views)
                created_files.append(file_name)
                _logger.info(
                    "Created/updated file: %s with %d views", file_name, len(views)
                )

            # AMÉLIORATION 6: Detect and migrate Studio custom fields
            studio_fields = self._get_studio_fields_for_views(self.studio_view_ids)
            field_rename_mapping = {}  # Initialize empty dict

            if studio_fields:
                _logger.info(
                    "Found %d Studio custom fields to migrate", len(studio_fields)
                )

                # Group fields by model
                fields_by_model = {}
                for field in studio_fields:
                    model_name = field.model_id.model
                    if model_name not in fields_by_model:
                        fields_by_model[model_name] = []
                    fields_by_model[model_name].append(field)

                # Create Python files for custom fields
                try:
                    field_files, field_rename_mapping = self._create_fields_python_file(
                        module_path, fields_by_model, self.rename_studio_fields
                    )

                    if field_files:
                        # Update models/__init__.py
                        self._update_models_init_py(module_path, field_files)
                        _logger.info(
                            "Created %d Python files for custom fields",
                            len(field_files),
                        )

                        if field_rename_mapping:
                            total_renamed = sum(
                                len(renames)
                                for renames in field_rename_mapping.values()
                            )
                            _logger.info(
                                "Will migrate data for %d renamed fields", total_renamed
                            )
                except Exception as e:
                    _logger.warning("Failed to create custom field files: %s", e)
            else:
                _logger.info("No Studio custom fields found in selected views")

            # Update manifest with all created files
            try:
                self._update_manifest(module_path, created_files)
            except Exception as e:
                raise UserError(
                    _(
                        "Failed to update manifest: %s\n\nPlease manually add these files to the manifest:\n%s"
                    )
                    % (
                        str(e),
                        "\n".join([f"{self.view_folder}/{f}" for f in created_files]),
                    )
                )

            # Create or update hooks.py for automatic Studio views cleanup and field data migration
            try:
                # Get external IDs of the views being converted
                studio_external_ids = []
                for view in self.studio_view_ids:
                    if view.xml_id:
                        studio_external_ids.append(view.xml_id)

                # Pass field rename mapping for data migration
                self._create_or_update_hooks(
                    module_path, module_name, studio_external_ids, field_rename_mapping
                )
            except Exception as e:
                _logger.warning("Failed to create hooks.py: %s", e)

            # Mark views as converted
            for view in self.studio_view_ids:
                view.mark_for_conversion(self.target_module_id)

            # Check if hooks.py was created
            hooks_created = os.path.exists(os.path.join(module_path, "hooks.py"))

            # Format files list for message
            files_list = "\n".join(
                [f"  • {self.view_folder}/{f}" for f in created_files]
            )

            # Add info about auto-included views if any
            auto_included_info = ""
            if len(all_views) > original_count:
                auto_included_info = _(
                    "\n🔗 Auto-included: %d related view(s) (parents/children)\n"
                ) % (len(all_views) - original_count)

            # Show success message
            if hooks_created:
                message = _(
                    "Successfully converted %d Studio view(s) to module %s.%s\n"
                    "📁 Created files (%d models):\n%s\n\n"
                    "🔧 Hook: hooks.py (auto-generated)\n"
                    "💾 Backup: %s\n\n"
                    "⚠️ IMPORTANT: Restart Odoo server before upgrading!\n\n"
                    "Next steps:\n"
                    "1. Restart Odoo (hooks.py needs to be loaded)\n"
                    '2. Upgrade the module "%s"\n'
                    "3. Studio views will be automatically deleted"
                ) % (
                    len(self.studio_view_ids),
                    self.target_module_id.name,
                    auto_included_info,
                    len(created_files),
                    files_list,
                    backup_dir,
                    self.target_module_id.name,
                )
            else:
                message = _(
                    "Successfully converted %d Studio view(s) to module %s.%s\n"
                    "📁 Created files (%d models):\n%s\n\n"
                    "💾 Backup: %s\n\n"
                    "Next steps:\n"
                    '1. Upgrade the module "%s"\n'
                    "2. The Studio views will be automatically deleted after upgrade"
                ) % (
                    len(self.studio_view_ids),
                    self.target_module_id.name,
                    auto_included_info,
                    len(created_files),
                    files_list,
                    backup_dir,
                    self.target_module_id.name,
                )

            # Log success message
            _logger.info("Conversion successful: %s", message)

            # Return to Studio views list
            return {
                "type": "ir.actions.act_window",
                "name": _("Studio Views"),
                "res_model": "ir.ui.view",
                "view_mode": "list,form",
                "domain": [("is_studio_view", "=", True)],
                "context": {
                    "default_message": message,
                },
            }

        except Exception as e:
            # AMÉLIORATION 2: Rollback on error
            if backup_dir:
                try:
                    self._rollback_from_backup(backup_dir, module_path)
                    error_msg = _(
                        "Conversion failed: %s\n\n"
                        "✓ Changes have been rolled back from backup.\n"
                        "Backup location: %s"
                    ) % (str(e), backup_dir)
                except Exception as rollback_error:
                    error_msg = _(
                        "Conversion failed: %s\n\n"
                        "✗ Rollback also failed: %s\n"
                        "Manual recovery needed from backup: %s"
                    ) % (str(e), str(rollback_error), backup_dir)
            else:
                error_msg = _("Conversion failed: %s") % str(e)

            _logger.error("Conversion failed: %s", e)
            raise UserError(error_msg)

    @api.model
    def default_get(self, fields_list):
        """Set default values based on context"""
        res = super().default_get(fields_list)

        # If called from a view, pre-select it
        if self.env.context.get(
            "active_model"
        ) == "ir.ui.view" and self.env.context.get("active_ids"):
            view_ids = self.env.context["active_ids"]
            studio_views = (
                self.env["ir.ui.view"]
                .browse(view_ids)
                .filtered(lambda v: v.is_studio_view and not v.converted_to_module)
            )
            if studio_views:
                res["studio_view_ids"] = [(6, 0, studio_views.ids)]

        return res
