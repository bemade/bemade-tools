# -*- coding: utf-8 -*-

import logging
import os
import re

from lxml import etree

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class StudioViewConverter(models.TransientModel):
    _name = 'studio.view.converter'
    _description = 'Studio View to Module Converter'

    studio_view_ids = fields.Many2many(
        comodel_name='ir.ui.view',
        string='Studio Views',
        domain=[('is_studio_view', '=', True), ('converted_to_module', '=', False)],
        required=True,
        help="Select the Studio views to convert",
    )
    module_author = fields.Selection(
        selection='_get_module_authors',
        string='Module Author',
        help="Filter modules by author",
    )
    target_module_id = fields.Many2one(
        comodel_name='ir.module.module',
        string='Target Module',
        required=True,
        domain=[('state', '=', 'installed')],
        help="Select the custom module where views will be added",
    )
    module_path = fields.Char(
        compute='_compute_module_path',
        string='Module Path',
        help="File system path of the target module",
    )
    view_folder = fields.Char(
        string='Views Folder',
        default='views',
        required=True,
        help="Folder name within the module where XML files will be created",
    )
    auto_cleanup = fields.Boolean(
        string='Auto Cleanup Studio Views',
        default=True,
        help="Automatically delete Studio views after module update",
    )
    preview_xml = fields.Text(
        compute='_compute_preview_xml',
        string='Preview XML',
        help="Preview of the XML that will be generated",
    )

    @api.model
    def _get_module_authors(self):
        """Get list of unique module authors from installed modules.
        
        :return: List of tuples (author, author)
        :rtype: list
        """
        authors = self.env['ir.module.module'].search([
            ('state', '=', 'installed'),
            ('author', '!=', False),
        ]).mapped('author')
        
        # Remove duplicates and sort
        unique_authors = sorted(set(authors))
        return [(author, author) for author in unique_authors]

    @api.onchange('module_author')
    def _onchange_module_author(self):
        """Clear target_module_id when author changes and return filtered domain."""
        if self.module_author:
            self.target_module_id = False
            return {
                'domain': {
                    'target_module_id': [
                        ('state', '=', 'installed'),
                        ('author', '=', self.module_author)
                    ]
                }
            }
        else:
            return {
                'domain': {
                    'target_module_id': [('state', '=', 'installed')]
                }
            }

    @api.depends('target_module_id')
    def _compute_module_path(self):
        for wizard in self:
            if wizard.target_module_id:
                # Try to find module path using odoo.modules.module.get_module_path
                module_name = wizard.target_module_id.name
                try:
                    from odoo.modules.module import get_module_path
                    module_path = get_module_path(module_name)
                    wizard.module_path = module_path if module_path else 'Module path not found'
                except Exception:
                    wizard.module_path = 'Module path not found'
            else:
                wizard.module_path = ''

    @api.depends('studio_view_ids', 'target_module_id')
    def _compute_preview_xml(self):
        for wizard in self:
            if wizard.studio_view_ids:
                preview_lines = []
                for view in wizard.studio_view_ids[:3]:  # Preview first 3 views
                    xml_content = wizard._generate_view_xml(view)
                    preview_lines.append(f"<!-- View: {view.name} -->")
                    preview_lines.append(xml_content[:500] + "..." if len(xml_content) > 500 else xml_content)
                    preview_lines.append("")
                
                if len(wizard.studio_view_ids) > 3:
                    preview_lines.append(f"... and {len(wizard.studio_view_ids) - 3} more views")
                
                wizard.preview_xml = "\n".join(preview_lines)
            else:
                wizard.preview_xml = ''

    def _sanitize_xml_id(self, name):
        """Convert a view name to a valid XML ID.
        
        :param str name: View name to sanitize
        :return: Valid XML ID
        :rtype: str
        """
        xml_id = re.sub(r'[^a-zA-Z0-9_]', '_', name.lower())
        xml_id = re.sub(r'_+', '_', xml_id)
        xml_id = xml_id.strip('_')
        return xml_id or 'view'

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
            arch_string = etree.tostring(arch_tree, encoding='unicode', pretty_print=True)
            # Indent arch content (add 4 more spaces to each line)
            arch_lines = arch_string.strip().split('\n')
            arch_indented = '\n'.join(['                ' + line for line in arch_lines])
        except Exception:
            arch_indented = '                ' + view.arch.strip().replace('\n', '\n                ')
        
        # Build comment header
        view_type = view.type or 'form'
        comment_lines = [
            '',
            f'        <!-- {view.name} -->',
            f'        <!-- Model: {view.model} | Type: {view_type} -->',
        ]
        
        # Build the record XML
        xml_lines = [
            *comment_lines,
            f'        <record id="{xml_id}" model="ir.ui.view">',
            f'            <field name="name">{view.name}</field>',
            f'            <field name="model">{view.model}</field>',
        ]
        
        if view.inherit_id:
            inherit_xmlid = view.inherit_id.xml_id or f"{view.inherit_id.model.replace('.', '_')}_view_{view.inherit_id.id}"
            xml_lines.append(f'            <field name="inherit_id" ref="{inherit_xmlid}"/>')
        
        if view.mode:
            xml_lines.append(f'            <field name="mode">{view.mode}</field>')
        
        if view.priority != 16:  # Default priority
            xml_lines.append(f'            <field name="priority">{view.priority}</field>')
        
        # Add arch field with properly indented content
        xml_lines.append('            <field name="arch" type="xml">')
        xml_lines.append(arch_indented)
        xml_lines.append('            </field>')
        xml_lines.append('        </record>')
        
        return '\n'.join(xml_lines)

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
                data_element = root.find('.//data')
                
                if data_element is None:
                    # Create data element if it doesn't exist
                    data_element = etree.SubElement(root, 'data')
                
                # Get existing view IDs to avoid duplicates
                existing_ids = set()
                for record in data_element.findall('.//record[@model="ir.ui.view"]'):
                    record_id = record.get('id')
                    if record_id:
                        existing_ids.add(record_id)
                
                # Add new views
                for view in views:
                    xml_id = self._sanitize_xml_id(view.name)
                    
                    # Skip if already exists
                    if xml_id in existing_ids:
                        continue
                    
                    # Generate view XML and parse it
                    view_xml = self._generate_view_xml(view)
                    # Remove leading spaces to parse correctly
                    view_xml_clean = '\n'.join([line.strip() for line in view_xml.split('\n') if line.strip()])
                    view_element = etree.fromstring(view_xml_clean)
                    
                    # Add to data element with proper indentation
                    data_element.append(view_element)
                
                # Write back with pretty print
                tree.write(file_path, encoding='utf-8', xml_declaration=True, pretty_print=True)
                
            except Exception as e:
                _logger.warning(f"Could not parse existing file {file_path}, recreating it: {e}")
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
            '<odoo>',
            '    <data>',
            ''
        ]
        
        for view in views:
            xml_content = self._generate_view_xml(view)
            xml_lines.append(xml_content)
            xml_lines.append('')
        
        xml_lines.extend([
            '    </data>',
            '</odoo>',
        ])
        
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(xml_lines))

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
        xml_lines = [
            '<?xml version="1.0" encoding="utf-8"?>',
            '<odoo>',
            '    <data>',
            ''
        ]
        
        for view in views:
            xml_content = self._generate_view_xml(view)
            xml_lines.append(xml_content)
            xml_lines.append('')
        
        xml_lines.extend([
            '    </data>',
            '</odoo>',
        ])
        
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(xml_lines))

    def _create_or_update_hooks(self, module_path, module_name, studio_external_ids):
        """Create or update hooks.py file for automatic Studio views cleanup.
        
        :param str module_path: Path to the module
        :param str module_name: Name of the module
        :param list studio_external_ids: List of Studio view external IDs to delete
        """
        hooks_path = os.path.join(module_path, 'hooks.py')
        
        # Format external IDs list for Python code
        if studio_external_ids:
            ids_formatted = ',\n        '.join([f"'{xml_id}'" for xml_id in studio_external_ids])
            ids_list = f"[\n        {ids_formatted},\n    ]"
        else:
            ids_list = "[]"
        
        # Template for hooks.py
        hooks_template = f'''# -*- coding: utf-8 -*-
# Auto-generated by studio_to_module

from odoo.addons.studio_cleanup.tools import cleanup_studio_views_by_xmlid


def post_init_hook(env):
    """Clean up Studio views after module installation/upgrade."""
    # List of Studio view external IDs to delete
    # Format: 'module.xml_id' (e.g., 'studio_customization.odoo_studio_xxx')
    studio_view_ids_to_delete = {ids_list}
    
    cleanup_studio_views_by_xmlid(env, studio_view_ids_to_delete, '{module_name}')
'''
        
        # Create or update hooks.py
        if not os.path.exists(hooks_path):
            # Create new hooks.py
            with open(hooks_path, 'w', encoding='utf-8') as f:
                f.write(hooks_template)
            _logger.info('Created hooks.py for module %s with %d external IDs', module_name, len(studio_external_ids))
            
            # Update __init__.py to import hooks
            self._update_init_py(module_path)
            
            # Update manifest to add post_init_hook
            self._update_manifest_hook(module_path)
            
            # Add studio_cleanup to dependencies
            self._add_studio_cleanup_dependency(module_path)
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
            _logger.info('No new external IDs to add to hooks.py')
            return
        
        try:
            with open(hooks_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # Find the studio_view_ids_to_delete list
            import re
            pattern = r'studio_view_ids_to_delete\s*=\s*\[(.*?)\]'
            match = re.search(pattern, content, re.DOTALL)
            
            if match:
                existing_ids_str = match.group(1)
                # Extract existing IDs
                existing_ids = re.findall(r"'([^']+)'", existing_ids_str)
                
                # Add new IDs (avoid duplicates)
                all_ids = list(set(existing_ids + new_external_ids))
                all_ids.sort()  # Sort for consistency
                
                # Format new list
                ids_formatted = ',\n        '.join([f"'{xml_id}'" for xml_id in all_ids])
                new_list = f"[\n        {ids_formatted},\n    ]"
                
                # Replace in content
                new_content = re.sub(
                    pattern,
                    f'studio_view_ids_to_delete = {new_list}',
                    content,
                    flags=re.DOTALL
                )
                
                with open(hooks_path, 'w', encoding='utf-8') as f:
                    f.write(new_content)
                
                _logger.info('Updated hooks.py for module %s: added %d new external IDs (total: %d)',
                            module_name, len(new_external_ids), len(all_ids))
            else:
                _logger.warning('Could not find studio_view_ids_to_delete list in hooks.py')
                
        except Exception as e:
            _logger.error('Failed to update existing hooks.py: %s', e)

    def _update_init_py(self, module_path):
        """Add hooks import to __init__.py if not present."""
        init_path = os.path.join(module_path, '__init__.py')
        
        if os.path.exists(init_path):
            with open(init_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # Check if hooks is already imported
            if 'from . import hooks' not in content and 'from .import hooks' not in content:
                # Add import at the end
                if not content.endswith('\n'):
                    content += '\n'
                content += 'from . import hooks\n'
                
                with open(init_path, 'w', encoding='utf-8') as f:
                    f.write(content)
                _logger.info('Updated __init__.py to import hooks')

    def _update_manifest_hook(self, module_path):
        """Add post_init_hook to manifest if not present."""
        manifest_path = os.path.join(module_path, '__manifest__.py')
        
        if os.path.exists(manifest_path):
            with open(manifest_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # Check if post_init_hook is already defined
            if "'post_init_hook'" not in content and '"post_init_hook"' not in content:
                # Add before the closing brace
                if content.rstrip().endswith('}'):
                    # Find the last }
                    last_brace = content.rfind('}')
                    # Insert before it
                    new_content = (
                        content[:last_brace].rstrip() + 
                        ",\n    'post_init_hook': 'post_init_hook',\n" +
                        content[last_brace:]
                    )
                    
                    with open(manifest_path, 'w', encoding='utf-8') as f:
                        f.write(new_content)
                    _logger.info('Updated __manifest__.py to add post_init_hook')

    def _add_studio_cleanup_dependency(self, module_path):
        """Add studio_cleanup to module dependencies if not present.
        
        :param str module_path: Path to the module
        """
        manifest_path = os.path.join(module_path, '__manifest__.py')
        
        if not os.path.exists(manifest_path):
            return
        
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # Check if studio_cleanup is already in dependencies
            if "'studio_cleanup'" in content or '"studio_cleanup"' in content:
                _logger.debug('studio_cleanup already in dependencies')
                return
            
            # Find the depends list and add studio_cleanup
            import re
            depends_pattern = r"'depends'\s*:\s*\[(.*?)\]"
            match = re.search(depends_pattern, content, re.DOTALL)
            
            if match:
                existing_deps = match.group(1).strip()
                # Remove trailing comma if present
                if existing_deps.endswith(','):
                    existing_deps = existing_deps[:-1].strip()
                # Add studio_cleanup to the list
                new_deps = existing_deps + ", 'studio_cleanup'"
                new_content = re.sub(
                    depends_pattern,
                    f"'depends': [{new_deps}]",
                    content,
                    flags=re.DOTALL
                )
                
                with open(manifest_path, 'w', encoding='utf-8') as f:
                    f.write(new_content)
                
                _logger.info('Added studio_cleanup to module dependencies')
            else:
                _logger.warning('Could not find depends list in manifest')
                
        except Exception as e:
            _logger.error('Failed to add studio_cleanup dependency: %s', e)

    def _update_manifest(self, module_path, new_data_files):
        """Update module manifest to include new view files"""
        manifest_path = os.path.join(module_path, '__manifest__.py')
        
        if not os.path.exists(manifest_path):
            raise UserError(_('Manifest file not found at %s') % manifest_path)
        
        with open(manifest_path, 'r', encoding='utf-8') as f:
            manifest_content = f.read()
        
        # Parse the manifest to find the 'data' key (support both ' and ")
        data_pattern = r"['\"]data['\"]\s*:\s*\[(.*?)\]"
        matches = list(re.finditer(data_pattern, manifest_content, re.DOTALL))
        
        if len(matches) > 1:
            # Multiple 'data' sections found - merge them
            _logger.warning('Multiple data sections found in manifest, merging them')
            # Keep only the first one and merge content
            first_match = matches[0]
            all_data = first_match.group(1)
            
            # Collect data from other sections
            for match in matches[1:]:
                other_data = match.group(1).strip()
                if other_data:
                    all_data += ',\n        ' + other_data
            
            # Add new files
            for new_file in new_data_files:
                file_entry = f"'{self.view_folder}/{new_file}'"
                if file_entry not in all_data and new_file not in all_data:
                    if all_data.strip():
                        all_data += f",\n        {file_entry}"
                    else:
                        all_data = f"\n        {file_entry}\n    "
            
            # Replace first occurrence
            new_data_section = f"'data': [{all_data}]"
            manifest_content = manifest_content[:first_match.start()] + new_data_section + manifest_content[first_match.end():]
            
            # Remove other occurrences
            for match in reversed(matches[1:]):
                # Remove the duplicate section including trailing comma
                start = match.start()
                end = match.end()
                # Check for trailing comma and newline
                if manifest_content[end:end+2] == ',,':
                    end += 2
                elif manifest_content[end:end+1] == ',':
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
                        current_data += f",\n        {file_entry}"
                    else:
                        current_data = f"\n        {file_entry}\n    "
            
            new_data_section = f"'data': [{current_data}]"
            manifest_content = re.sub(data_pattern, new_data_section, manifest_content, flags=re.DOTALL, count=1)
        else:
            # No 'data' key found, add it
            # Find a good place to insert (after 'depends' if exists)
            depends_pattern = r"(['\"]depends['\"]\s*:\s*\[.*?\],)"
            match = re.search(depends_pattern, manifest_content, re.DOTALL)
            
            data_files_str = ',\n        '.join([f"'{self.view_folder}/{f}'" for f in new_data_files])
            new_data_section = f"\n    'data': [\n        {data_files_str}\n    ],"
            
            if match:
                manifest_content = manifest_content.replace(match.group(1), match.group(1) + new_data_section)
            else:
                # Just add before the closing brace
                manifest_content = manifest_content.rstrip('\n}') + new_data_section + '\n}'
        
        with open(manifest_path, 'w', encoding='utf-8') as f:
            f.write(manifest_content)

    def action_convert_views(self):
        """Convert selected Studio views to module code"""
        self.ensure_one()
        
        if not self.studio_view_ids:
            raise ValidationError(_('Please select at least one Studio view to convert.'))
        
        if not self.target_module_id:
            raise ValidationError(_('Please select a target module.'))
        
        # Get module path
        from odoo.modules.module import get_module_path
        module_name = self.target_module_id.name
        module_path = get_module_path(module_name)
        
        if not module_path or not os.path.exists(module_path):
            raise UserError(_('Module path not found for %s. Make sure the module is in the addons path.') % module_name)
        
        # Create single XML file for all Studio views
        file_name = 'migrated_studio_views.xml'
        file_path = os.path.join(module_path, self.view_folder, file_name)
        
        # Ensure views folder exists
        views_folder = os.path.join(module_path, self.view_folder)
        if not os.path.exists(views_folder):
            os.makedirs(views_folder)
        
        # Create or update the XML file with all views
        self._create_or_update_migrated_views_file(file_path, self.studio_view_ids)
        
        # Update manifest
        try:
            self._update_manifest(module_path, [file_name])
        except Exception as e:
            raise UserError(_('Failed to update manifest: %s\n\nPlease manually add this file to the manifest:\n%s/%s') % (
                str(e),
                self.view_folder,
                file_name
            ))
        
        # Create or update hooks.py for automatic Studio views cleanup
        try:
            # Get external IDs of the views being converted
            studio_external_ids = []
            for view in self.studio_view_ids:
                if view.xml_id:
                    studio_external_ids.append(view.xml_id)
            
            self._create_or_update_hooks(module_path, module_name, studio_external_ids)
        except Exception as e:
            _logger.warning('Failed to create hooks.py: %s', e)
        
        # Mark views as converted
        for view in self.studio_view_ids:
            view.mark_for_conversion(self.target_module_id)
        
        # Check if hooks.py was created
        hooks_created = os.path.exists(os.path.join(module_path, 'hooks.py'))
        
        # Show success message
        if hooks_created:
            message = _(
                'Successfully converted %d Studio view(s) to module %s.\n\n'
                'File: %s/%s\n'
                'Hook: hooks.py (auto-generated)\n\n'
                '⚠️ IMPORTANT: Restart Odoo server before upgrading!\n\n'
                'Next steps:\n'
                '1. Restart Odoo (hooks.py needs to be loaded)\n'
                '2. Upgrade the module "%s"\n'
                '3. Studio views will be automatically deleted'
            ) % (
                len(self.studio_view_ids),
                self.target_module_id.name,
                self.view_folder,
                file_name,
                self.target_module_id.name
            )
        else:
            message = _(
                'Successfully converted %d Studio view(s) to module %s.\n\n'
                'File: %s/%s\n\n'
                'Next steps:\n'
                '1. Upgrade the module "%s"\n'
                '2. The Studio views will be automatically deleted after upgrade'
            ) % (
                len(self.studio_view_ids),
                self.target_module_id.name,
                self.view_folder,
                file_name,
                self.target_module_id.name
            )
        
        # Return wizard to ask if user wants to upgrade the module
        return {
            'type': 'ir.actions.act_window',
            'name': _('Conversion Successful'),
            'res_model': 'studio.view.converter.confirm',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_message': message,
                'default_target_module_id': self.target_module_id.id,
                'default_converted_view_count': len(self.studio_view_ids),
            }
        }

    @api.model
    def default_get(self, fields_list):
        """Set default values based on context"""
        res = super().default_get(fields_list)
        
        # If called from a view, pre-select it
        if self.env.context.get('active_model') == 'ir.ui.view' and self.env.context.get('active_ids'):
            view_ids = self.env.context['active_ids']
            studio_views = self.env['ir.ui.view'].browse(view_ids).filtered(
                lambda v: v.is_studio_view and not v.converted_to_module
            )
            if studio_views:
                res['studio_view_ids'] = [(6, 0, studio_views.ids)]
        
        return res
