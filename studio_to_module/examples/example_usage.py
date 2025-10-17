# -*- coding: utf-8 -*-
"""
Example Usage of Studio to Module Converter

This file demonstrates various ways to use the Studio to Module Converter
programmatically or through the Odoo shell.
"""

# ==============================================================================
# EXAMPLE 1: Find all Studio views
# ==============================================================================

def find_all_studio_views(env):
    """Find all Studio-created views in the system"""
    studio_views = env['ir.ui.view'].search([
        ('studio', '=', True),
        ('converted_to_module', '=', False)
    ])
    
    print(f"Found {len(studio_views)} Studio views:")
    for view in studio_views:
        print(f"  - {view.name} ({view.model})")
    
    return studio_views


# ==============================================================================
# EXAMPLE 2: Convert views programmatically
# ==============================================================================

def convert_views_to_module(env, view_ids, target_module_name):
    """
    Convert Studio views to a module programmatically
    
    Args:
        env: Odoo environment
        view_ids: List of view IDs to convert
        target_module_name: Name of target module (e.g., 'durpro_base')
    """
    # Get target module
    target_module = env['ir.module.module'].search([
        ('name', '=', target_module_name),
        ('state', '=', 'installed')
    ], limit=1)
    
    if not target_module:
        raise ValueError(f"Module {target_module_name} not found or not installed")
    
    # Get views
    views = env['ir.ui.view'].browse(view_ids)
    
    # Create wizard
    wizard = env['studio.view.converter'].create({
        'studio_view_ids': [(6, 0, views.ids)],
        'target_module_id': target_module.id,
        'view_folder': 'views',
        'auto_cleanup': True,
    })
    
    # Execute conversion
    result = wizard.action_convert_views()
    
    print(f"Conversion complete! Result: {result}")
    return result


# ==============================================================================
# EXAMPLE 3: Convert all views for a specific model
# ==============================================================================

def convert_model_views(env, model_name, target_module_name):
    """
    Convert all Studio views for a specific model
    
    Args:
        env: Odoo environment
        model_name: Model name (e.g., 'res.partner')
        target_module_name: Target module name
    """
    # Find Studio views for this model
    views = env['ir.ui.view'].search([
        ('model', '=', model_name),
        ('studio', '=', True),
        ('converted_to_module', '=', False)
    ])
    
    if not views:
        print(f"No Studio views found for model {model_name}")
        return
    
    print(f"Converting {len(views)} views for {model_name}...")
    
    # Convert
    return convert_views_to_module(env, views.ids, target_module_name)


# ==============================================================================
# EXAMPLE 4: Preview XML before conversion
# ==============================================================================

def preview_view_xml(env, view_id):
    """
    Preview the XML that would be generated for a view
    
    Args:
        env: Odoo environment
        view_id: ID of the view to preview
    """
    view = env['ir.ui.view'].browse(view_id)
    
    if not view.exists():
        raise ValueError(f"View {view_id} not found")
    
    # Create temporary wizard to generate XML
    wizard = env['studio.view.converter'].create({
        'target_module_id': env['ir.module.module'].search([
            ('state', '=', 'installed')
        ], limit=1).id,
    })
    
    xml_content = wizard._generate_view_xml(view)
    
    print("=" * 80)
    print(f"XML Preview for: {view.name}")
    print("=" * 80)
    print(xml_content)
    print("=" * 80)
    
    return xml_content


# ==============================================================================
# EXAMPLE 5: Batch convert with filtering
# ==============================================================================

def batch_convert_with_filter(env, target_module_name, model_filter=None, 
                              name_filter=None):
    """
    Batch convert Studio views with filtering
    
    Args:
        env: Odoo environment
        target_module_name: Target module name
        model_filter: Optional model name filter
        name_filter: Optional name pattern filter
    """
    domain = [
        ('studio', '=', True),
        ('converted_to_module', '=', False)
    ]
    
    if model_filter:
        domain.append(('model', '=', model_filter))
    
    if name_filter:
        domain.append(('name', 'ilike', name_filter))
    
    views = env['ir.ui.view'].search(domain)
    
    if not views:
        print("No views found matching criteria")
        return
    
    print(f"Found {len(views)} views to convert:")
    for view in views:
        print(f"  - {view.name} ({view.model})")
    
    # Convert
    return convert_views_to_module(env, views.ids, target_module_name)


# ==============================================================================
# EXAMPLE 6: Manual cleanup of converted views
# ==============================================================================

def manual_cleanup(env):
    """Manually trigger cleanup of converted Studio views"""
    print("Running manual cleanup of converted Studio views...")
    
    # Find views pending cleanup
    pending_views = env['ir.ui.view'].search([
        ('pending_cleanup', '=', True),
        ('converted_to_module', '=', True)
    ])
    
    print(f"Found {len(pending_views)} views pending cleanup:")
    for view in pending_views:
        print(f"  - {view.name} (target: {view.target_module_id.name})")
    
    # Run cleanup
    env['ir.ui.view'].cleanup_converted_views()
    
    print("Cleanup complete!")


# ==============================================================================
# EXAMPLE 7: Check conversion status
# ==============================================================================

def check_conversion_status(env):
    """Check the status of Studio views and conversions"""
    total_studio = env['ir.ui.view'].search_count([('studio', '=', True)])
    converted = env['ir.ui.view'].search_count([('converted_to_module', '=', True)])
    pending = env['ir.ui.view'].search_count([
        ('studio', '=', True),
        ('converted_to_module', '=', False)
    ])
    pending_cleanup = env['ir.ui.view'].search_count([('pending_cleanup', '=', True)])
    
    print("=" * 80)
    print("Studio Views Conversion Status")
    print("=" * 80)
    print(f"Total Studio Views:        {total_studio}")
    print(f"Converted to Modules:      {converted}")
    print(f"Pending Conversion:        {pending}")
    print(f"Pending Cleanup:           {pending_cleanup}")
    print("=" * 80)
    
    # Group by model
    if pending > 0:
        print("\nPending views by model:")
        views = env['ir.ui.view'].search([
            ('studio', '=', True),
            ('converted_to_module', '=', False)
        ])
        
        by_model = {}
        for view in views:
            if view.model not in by_model:
                by_model[view.model] = []
            by_model[view.model].append(view.name)
        
        for model, view_names in sorted(by_model.items()):
            print(f"  {model}: {len(view_names)} views")
            for name in view_names[:3]:  # Show first 3
                print(f"    - {name}")
            if len(view_names) > 3:
                print(f"    ... and {len(view_names) - 3} more")


# ==============================================================================
# EXAMPLE 8: Rollback a conversion (before module upgrade)
# ==============================================================================

def rollback_conversion(env, view_ids):
    """
    Rollback a conversion before module upgrade
    
    Args:
        env: Odoo environment
        view_ids: List of view IDs to rollback
    """
    views = env['ir.ui.view'].browse(view_ids)
    
    for view in views:
        if view.converted_to_module and view.pending_cleanup:
            view.write({
                'converted_to_module': False,
                'target_module_id': False,
                'pending_cleanup': False,
            })
            print(f"Rolled back: {view.name}")
        else:
            print(f"Skipped (not converted): {view.name}")


# ==============================================================================
# USAGE IN ODOO SHELL
# ==============================================================================

"""
To use these examples in Odoo shell:

1. Start Odoo shell:
   odoo shell -d your_database

2. Import this file:
   from odoo.addons.studio_to_module.examples import example_usage

3. Use the functions:
   
   # Find all Studio views
   views = example_usage.find_all_studio_views(env)
   
   # Convert specific views
   example_usage.convert_views_to_module(env, [view_id1, view_id2], 'durpro_base')
   
   # Convert all views for a model
   example_usage.convert_model_views(env, 'res.partner', 'durpro_base')
   
   # Preview XML
   example_usage.preview_view_xml(env, view_id)
   
   # Batch convert with filter
   example_usage.batch_convert_with_filter(env, 'durpro_base', model_filter='sale.order')
   
   # Check status
   example_usage.check_conversion_status(env)
   
   # Manual cleanup
   example_usage.manual_cleanup(env)
   
   # Rollback
   example_usage.rollback_conversion(env, [view_id1, view_id2])
"""


# ==============================================================================
# ADVANCED EXAMPLE: Custom XML generation
# ==============================================================================

def custom_xml_generation_example(env):
    """
    Example of extending the wizard to customize XML generation
    
    This shows how to inherit and customize the converter
    """
    
    # This would be in a custom module that depends on studio_to_module
    class CustomStudioViewConverter(env['studio.view.converter'].__class__):
        
        def _generate_view_xml(self, view):
            """Override to add custom header"""
            xml = super()._generate_view_xml(view)
            
            # Add custom header comment
            header = f"""
        <!-- 
            Converted from Studio
            Original View ID: {view.id}
            Conversion Date: {env.context.get('conversion_date', 'N/A')}
            Converted By: {env.user.name}
        -->
"""
            return header + xml
    
    print("Custom XML generation example defined")
    print("To use: inherit studio.view.converter in your custom module")


# ==============================================================================
# INTEGRATION EXAMPLE: CI/CD Pipeline
# ==============================================================================

def cicd_integration_example():
    """
    Example of integrating with CI/CD pipeline
    
    This could be part of a deployment script
    """
    
    script = """
#!/bin/bash
# deploy_studio_views.sh

set -e

DB_NAME="$1"
MODULE_NAME="$2"

echo "Converting Studio views to module: $MODULE_NAME"

# Run conversion via Odoo shell
odoo shell -d "$DB_NAME" <<EOF
from odoo.addons.studio_to_module.examples import example_usage

# Find and convert all pending Studio views
views = env['ir.ui.view'].search([
    ('studio', '=', True),
    ('converted_to_module', '=', False)
])

if views:
    print(f"Converting {len(views)} Studio views...")
    example_usage.convert_views_to_module(env, views.ids, '$MODULE_NAME')
    env.cr.commit()
    print("Conversion complete!")
else:
    print("No Studio views to convert")
EOF

# Upgrade module
echo "Upgrading module: $MODULE_NAME"
odoo -d "$DB_NAME" -u "$MODULE_NAME" --stop-after-init

echo "Deployment complete!"
"""
    
    print("CI/CD Integration Example:")
    print(script)
    print("\nUsage: ./deploy_studio_views.sh your_database your_module")


if __name__ == '__main__':
    print(__doc__)
    print("\nThis file contains examples for using the Studio to Module Converter.")
    print("Import it in Odoo shell to use the functions.")
