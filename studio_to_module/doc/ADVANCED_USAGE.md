# Advanced Usage Guide

## Table of Contents

1. [Batch Conversion Strategies](#batch-conversion-strategies)
2. [Custom XML Generation](#custom-xml-generation)
3. [Module Structure Best Practices](#module-structure-best-practices)
4. [Handling Complex Views](#handling-complex-views)
5. [Integration with CI/CD](#integration-with-cicd)
6. [Troubleshooting Advanced Scenarios](#troubleshooting-advanced-scenarios)

## Batch Conversion Strategies

### Converting by Model

When converting multiple views, it's best to group them by model:

1. Filter Studio views by model in the Studio Views Manager
2. Select all views for one model
3. Convert them together to ensure they're in the same XML file
4. Repeat for other models

**Benefits:**
- Cleaner file organization
- Easier code review
- Better version control diffs

### Converting by Feature

For feature-based development:

1. Identify all Studio views related to a feature
2. Create a dedicated module for the feature if needed
3. Convert all related views to that module
4. Test the feature thoroughly after conversion

## Custom XML Generation

### Customizing XML IDs

The tool automatically generates XML IDs from view names. To customize:

1. Convert views using the tool
2. Manually edit the generated XML files
3. Update XML IDs to match your naming convention
4. Update any references in other files

**Example:**
```xml
<!-- Generated -->
<record id="partner_form_view" model="ir.ui.view">

<!-- Customized -->
<record id="view_partner_form_custom" model="ir.ui.view">
```

### Adding Comments and Documentation

After conversion, enhance the XML files with comments:

```xml
<!-- 
    Custom Partner Form View
    Added: 2025-01-16
    Purpose: Display custom fields for partner management
    Dependencies: partner_custom_fields module
-->
<record id="view_partner_form_custom" model="ir.ui.view">
    ...
</record>
```

## Module Structure Best Practices

### Recommended Directory Structure

```
my_custom_module/
├── __init__.py
├── __manifest__.py
├── models/
│   ├── __init__.py
│   └── *.py
├── views/
│   ├── res_partner_views.xml
│   ├── sale_order_views.xml
│   └── *.xml
├── security/
│   └── ir.model.access.csv
├── data/
│   └── *.xml
└── static/
    └── description/
        └── icon.png
```

### Organizing View Files

**By Model (Recommended):**
- `res_partner_views.xml`
- `sale_order_views.xml`
- `product_product_views.xml`

**By View Type:**
- `form_views.xml`
- `tree_views.xml`
- `search_views.xml`

**By Feature:**
- `customer_portal_views.xml`
- `reporting_views.xml`
- `dashboard_views.xml`

## Handling Complex Views

### Inherited Views with Multiple Parents

When converting views that inherit from multiple parents:

1. Ensure parent views are available in dependencies
2. Check inheritance chain in generated XML
3. Test view rendering after conversion
4. Adjust priorities if needed

### Views with External Dependencies

For views that reference other modules:

1. Add dependencies to `__manifest__.py`:
```python
'depends': ['base', 'sale', 'stock', 'custom_module'],
```

2. Use proper XML ID references:
```xml
<field name="inherit_id" ref="sale.view_order_form"/>
```

### Dynamic Views with Domains

Studio views with complex domains:

1. Review generated XML for domain syntax
2. Test domain evaluation after conversion
3. Consider moving complex logic to Python

## Integration with CI/CD

### Pre-Commit Hooks

Add validation before committing converted views:

```bash
#!/bin/bash
# .git/hooks/pre-commit

# Validate XML syntax
find addons/*/views -name "*.xml" -exec xmllint --noout {} \;

if [ $? -ne 0 ]; then
    echo "XML validation failed"
    exit 1
fi
```

### Automated Testing

Include converted views in your test suite:

```python
def test_converted_views(self):
    """Test that converted views are valid"""
    views = self.env['ir.ui.view'].search([
        ('model', '=', 'res.partner'),
        ('name', 'like', 'Custom%')
    ])
    
    for view in views:
        # Test view rendering
        view._check_xml()
        
        # Test view access
        self.assertTrue(view.check_access_rights('read'))
```

### Deployment Pipeline

1. **Development:**
   - Create customizations in Studio
   - Convert to module code
   - Commit to feature branch

2. **Testing:**
   - Deploy to test environment
   - Run automated tests
   - Manual QA review

3. **Staging:**
   - Deploy to staging
   - Client acceptance testing
   - Performance testing

4. **Production:**
   - Deploy module update
   - Verify Studio views are cleaned up
   - Monitor for issues

## Troubleshooting Advanced Scenarios

### View Inheritance Chain Issues

**Problem:** Converted view doesn't render correctly

**Solution:**
1. Check inheritance chain:
```python
view = env['ir.ui.view'].search([('name', '=', 'My View')])
print(view.inherit_id.name)
print(view.inherit_id.inherit_id.name)
```

2. Verify all parent views are loaded
3. Check view priorities
4. Test with `--dev=all` flag

### Module Dependency Conflicts

**Problem:** Converted views reference unavailable modules

**Solution:**
1. Audit view dependencies:
```bash
grep -r "ref=" views/*.xml | cut -d'"' -f2 | cut -d'.' -f1 | sort -u
```

2. Add missing dependencies to manifest
3. Consider creating bridge modules for complex dependencies

### Performance Issues with Many Views

**Problem:** Slow module load time after conversion

**Solution:**
1. Split views into multiple files
2. Use `noupdate="1"` for static views
3. Optimize view inheritance depth
4. Consider lazy loading for rarely used views

### XML ID Conflicts

**Problem:** Duplicate XML IDs after conversion

**Solution:**
1. Use module prefix in XML IDs:
```xml
<record id="my_module_partner_form" model="ir.ui.view">
```

2. Check for conflicts before conversion:
```python
existing_ids = env['ir.model.data'].search([
    ('module', '=', 'my_module'),
    ('name', '=', 'partner_form')
])
```

3. Rename conflicting views before conversion

## Advanced Customization

### Extending the Converter

To add custom XML generation logic:

```python
class StudioViewConverter(models.TransientModel):
    _inherit = 'studio.view.converter'
    
    def _generate_view_xml(self, view):
        xml = super()._generate_view_xml(view)
        
        # Add custom header comment
        header = f"<!-- Generated from Studio on {fields.Date.today()} -->\n"
        return header + xml
```

### Custom Cleanup Logic

To customize view cleanup behavior:

```python
class IrUiView(models.Model):
    _inherit = 'ir.ui.view'
    
    def cleanup_converted_views(self):
        # Add custom logic before cleanup
        views = self.search([('pending_cleanup', '=', True)])
        
        # Create backup before deletion
        for view in views:
            self.env['ir.attachment'].create({
                'name': f'backup_{view.name}.xml',
                'datas': base64.b64encode(view.arch.encode()),
                'res_model': 'ir.ui.view',
                'res_id': view.id,
            })
        
        return super().cleanup_converted_views()
```

## Best Practices Summary

1. **Plan Before Converting:** Understand your module structure
2. **Test Thoroughly:** Always test in development first
3. **Review Generated Code:** Don't blindly trust automation
4. **Version Control:** Commit converted views immediately
5. **Document Changes:** Add comments and update documentation
6. **Monitor Cleanup:** Verify Studio views are removed
7. **Backup First:** Always have database backups
8. **Incremental Approach:** Convert in small batches
9. **Code Review:** Have peers review converted code
10. **Maintain Standards:** Follow your team's coding conventions

## Getting Help

For complex scenarios not covered here:
1. Check the main README.md
2. Review the test suite for examples
3. Contact the Durpro development team
4. Consult Odoo documentation for view architecture

## Contributing

If you develop useful extensions or improvements:
1. Document your changes
2. Add tests
3. Submit to the development team
4. Share knowledge with the team
