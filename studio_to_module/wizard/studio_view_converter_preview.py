# -*- coding: utf-8 -*-

from odoo import _, api, fields, models


class StudioViewConverterPreview(models.TransientModel):
    _name = 'studio.view.converter.preview'
    _description = 'Studio View Converter - Preview'

    converter_id = fields.Many2one(
        'studio.view.converter',
        string='Converter',
        required=True,
        ondelete='cascade',
    )
    
    preview_html = fields.Html(
        compute='_compute_preview_html',
        string='Preview',
        sanitize=False,
    )
    
    view_count = fields.Integer(
        compute='_compute_counts',
        string='Views Count',
    )
    
    field_count = fields.Integer(
        compute='_compute_counts',
        string='Fields Count',
    )
    
    @api.depends('converter_id')
    def _compute_counts(self):
        for wizard in self:
            if wizard.converter_id:
                wizard.view_count = len(wizard.converter_id.studio_view_ids)
                
                # Count Studio fields
                studio_fields = wizard.converter_id._get_studio_fields_for_views(
                    wizard.converter_id.studio_view_ids
                )
                wizard.field_count = len(studio_fields)
            else:
                wizard.view_count = 0
                wizard.field_count = 0
    
    @api.depends('converter_id')
    def _compute_preview_html(self):
        for wizard in self:
            if not wizard.converter_id:
                wizard.preview_html = '<p>No converter selected</p>'
                continue
            
            converter = wizard.converter_id
            views = converter.studio_view_ids
            target_module = converter.target_module_id
            
            # Analyze what will be created
            html_parts = ['<div style="font-family: sans-serif; padding: 10px;">']
            
            # Header
            html_parts.append('<h3 style="color: #875A7B; margin-top: 0;">📋 Conversion Preview</h3>')
            
            # Target module info
            html_parts.append('<div style="background: #f0f0f0; padding: 10px; border-radius: 4px; margin-bottom: 15px;">')
            html_parts.append(f'<strong>Target Module:</strong> <code>{target_module.name}</code><br/>')
            html_parts.append(f'<strong>Path:</strong> <code>{converter.module_path}</code>')
            html_parts.append('</div>')
            
            # Views section
            html_parts.append('<h4 style="color: #875A7B; margin-bottom: 10px;">🎨 Studio Views to Convert</h4>')
            html_parts.append(f'<p><strong>{len(views)} view(s)</strong> will be converted:</p>')
            html_parts.append('<ul style="margin: 5px 0; padding-left: 25px;">')
            
            views_by_model = {}
            for view in views:
                if view.model not in views_by_model:
                    views_by_model[view.model] = []
                views_by_model[view.model].append(view)
            
            for model, model_views in views_by_model.items():
                html_parts.append(f'<li><strong>{model}</strong>: {len(model_views)} view(s)')
                html_parts.append('<ul style="padding-left: 20px;">')
                for view in model_views[:3]:  # Show first 3
                    html_parts.append(f'<li style="font-size: 0.9em;">{view.name}</li>')
                if len(model_views) > 3:
                    html_parts.append(f'<li style="font-size: 0.9em; color: #888;">... and {len(model_views) - 3} more</li>')
                html_parts.append('</ul>')
                html_parts.append('</li>')
            
            html_parts.append('</ul>')
            
            # Files that will be created
            html_parts.append('<h4 style="color: #875A7B; margin-bottom: 10px;">📁 Files to Create/Update</h4>')
            html_parts.append('<ul style="margin: 5px 0; padding-left: 25px;">')
            
            # View XML files
            for model in views_by_model.keys():
                model_safe = model.replace('.', '_')
                file_name = f'{model_safe}_studio_views.xml'
                html_parts.append(f'<li><code>{converter.view_folder}/{file_name}</code> - View definitions</li>')
            
            # Check for Studio fields
            studio_fields = converter._get_studio_fields_for_views(views)
            
            if studio_fields:
                html_parts.append(f'<li style="margin-top: 5px;"><strong>🎯 {len(studio_fields)} Studio custom field(s) detected:</strong></li>')
                
                fields_by_model = {}
                for field in studio_fields:
                    model_name = field.model_id.model
                    if model_name not in fields_by_model:
                        fields_by_model[model_name] = []
                    fields_by_model[model_name].append(field)
                
                html_parts.append('<ul style="padding-left: 20px;">')
                for model_name, fields_list in fields_by_model.items():
                    model_safe = model_name.replace('.', '_')
                    file_name = f'{model_safe}_custom_fields.py'
                    html_parts.append(f'<li><code>models/{file_name}</code> - {len(fields_list)} field(s)')
                    
                    # Show field names
                    html_parts.append('<ul style="padding-left: 15px; font-size: 0.85em; color: #555;">')
                    for field in fields_list[:5]:  # Show first 5
                        field_type = field.ttype
                        html_parts.append(f'<li><code>{field.name}</code> ({field_type}): {field.field_description}</li>')
                    if len(fields_list) > 5:
                        html_parts.append(f'<li>... and {len(fields_list) - 5} more fields</li>')
                    html_parts.append('</ul>')
                    html_parts.append('</li>')
                html_parts.append('</ul>')
                
                html_parts.append('<li><code>models/__init__.py</code> - Updated with imports</li>')
            
            html_parts.append('<li><code>__manifest__.py</code> - Updated with data files</li>')
            html_parts.append('<li><code>hooks.py</code> - Auto-cleanup hook</li>')
            html_parts.append('<li><code>__init__.py</code> - Updated to import hooks</li>')
            
            html_parts.append('</ul>')
            
            # Analyze dependencies
            dep_analysis = converter._analyze_view_dependencies(views, target_module)
            
            # Always show dependency analysis section
            html_parts.append('<h4 style="color: #875A7B; margin-bottom: 10px;">🔍 Dependency Analysis</h4>')
            
            if dep_analysis['warnings']:
                # Show issues if found
                if dep_analysis['missing_dependencies']:
                    html_parts.append('<div style="background: #fff3cd; border-left: 4px solid #ffc107; padding: 10px; margin-bottom: 10px;">')
                    html_parts.append(f'<strong>⚠️ Missing Dependencies ({len(dep_analysis["missing_dependencies"])}):</strong><br/>')
                    html_parts.append('<ul style="margin: 5px 0; padding-left: 25px;">')
                    for dep in dep_analysis['missing_dependencies']:
                        html_parts.append(f'<li><code>{dep}</code> - Will be added to module dependencies</li>')
                    html_parts.append('</ul>')
                    html_parts.append('</div>')
                
                if dep_analysis['xpath_issues']:
                    html_parts.append('<div style="background: #f8d7da; border-left: 4px solid #d9534f; padding: 10px; margin-bottom: 10px;">')
                    html_parts.append(f'<strong>⚠️ Potential XPath Issues ({len(dep_analysis["xpath_issues"])}):</strong><br/>')
                    html_parts.append('<p style="margin: 5px 0; font-size: 0.9em;">These xpaths reference elements that may not exist:</p>')
                    html_parts.append('<ul style="margin: 5px 0; padding-left: 25px; font-size: 0.85em;">')
                    for issue in dep_analysis['xpath_issues'][:5]:  # Show first 5
                        html_parts.append(f'<li><strong>{issue["view"]}</strong>: <code>{issue["xpath"][:80]}...</code> (requires <code>{issue["module"]}</code>)</li>')
                    if len(dep_analysis['xpath_issues']) > 5:
                        html_parts.append(f'<li>... and {len(dep_analysis["xpath_issues"]) - 5} more issues</li>')
                    html_parts.append('</ul>')
                    html_parts.append('<p style="margin: 5px 0; font-size: 0.85em; color: #856404;"><em>💡 Tip: These xpaths will be commented out in the generated XML if the modules are not in dependencies.</em></p>')
                    html_parts.append('</div>')
            else:
                # Show success message when no issues
                html_parts.append('<div style="background: #d4edda; border-left: 4px solid #28a745; padding: 10px; margin-bottom: 10px;">')
                html_parts.append('<strong>✅ All dependencies satisfied!</strong><br/>')
                html_parts.append('<p style="margin: 5px 0; font-size: 0.9em;">No missing module dependencies detected. All referenced modules are already in the target module dependencies.</p>')
                html_parts.append('</div>')
            
            # Actions section
            html_parts.append('<h4 style="color: #875A7B; margin-bottom: 10px;">⚙️ Actions to Perform</h4>')
            html_parts.append('<ol style="margin: 5px 0; padding-left: 25px;">')
            html_parts.append('<li>Create backup of module files</li>')
            html_parts.append('<li>Generate XML files for Studio views</li>')
            
            if studio_fields:
                html_parts.append(f'<li>Generate Python code for {len(studio_fields)} custom field(s)</li>')
            
            if dep_analysis['missing_dependencies']:
                html_parts.append(f'<li>Add {len(dep_analysis["missing_dependencies"])} missing dependencies to manifest</li>')
            
            html_parts.append('<li>Update module manifest</li>')
            html_parts.append('<li>Create/update hooks for auto-cleanup</li>')
            html_parts.append('<li>Mark views as converted</li>')
            html_parts.append('</ol>')
            
            # Warning box
            html_parts.append('<div style="background: #fff3cd; border: 1px solid #ffc107; padding: 10px; border-radius: 4px; margin-top: 15px;">')
            html_parts.append('<strong style="color: #856404;">⚠️ Important:</strong><br/>')
            html_parts.append('After conversion, you must:<br/>')
            html_parts.append('<ol style="margin: 5px 0; padding-left: 25px;">')
            html_parts.append(f'<li>Restart Odoo server (for hooks.py to load)</li>')
            html_parts.append(f'<li>Upgrade module <strong>{target_module.name}</strong></li>')
            html_parts.append('<li>Studio views will be automatically deleted</li>')
            html_parts.append('</ol>')
            html_parts.append('</div>')
            
            html_parts.append('</div>')
            
            wizard.preview_html = ''.join(html_parts)
    
    def action_confirm_conversion(self):
        """Confirm and proceed with conversion."""
        self.ensure_one()
        
        if self.converter_id:
            # Call the actual conversion method
            return self.converter_id.action_convert_views_confirmed()
        
        return {'type': 'ir.actions.act_window_close'}
    
    def action_cancel(self):
        """Cancel conversion."""
        return {'type': 'ir.actions.act_window_close'}
