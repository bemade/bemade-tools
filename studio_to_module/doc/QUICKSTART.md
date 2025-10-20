# Quick Start Guide - Studio to Module Converter

## 5-Minute Setup

### 1. Installation (1 minute)

```bash
# Navigate to your Odoo addons directory
cd /path/to/odoo/addons

# The module is already in: addons/studio_to_module/

# Start Odoo with the module path
odoo -d your_database -u studio_to_module
```

Or via the UI:
1. Go to **Apps**
2. Update Apps List
3. Search for "Studio to Module Converter"
4. Click **Install**

### 2. First Conversion (3 minutes)

#### Option A: Quick Convert from Studio Views Manager

1. Go to **Studio to Module > Studio Views**
2. You'll see all Studio-created views with filters
3. Select one or more views (use checkboxes)
4. Click **Action** dropdown → **Convert Studio Views**
5. In the wizard:
   - Select your target module (e.g., `durpro_base`)
   - Keep default settings
   - Click **Preview XML** tab to see what will be generated
6. Click **Convert Views**
7. Success! Files are created in your module

#### Option B: Direct Wizard Access

1. Go to **Studio to Module > Convert to Module**
2. Select Studio views from the dropdown
3. Choose target module
4. Click **Convert Views**

### 3. Apply Changes (1 minute)

```bash
# Upgrade the target module
odoo -d your_database -u your_target_module
```

Or via the UI:
1. Go to **Apps**
2. Find your target module
3. Click **Upgrade**
4. Studio views are automatically deleted! ✨

## Example Workflow

### Scenario: Converting a Custom Partner Form

```python
# 1. You created a custom partner form in Studio
# 2. Now you want to convert it to code

# Step 1: Find the view
Go to: Studio to Module > Studio Views
Filter: Model = "res.partner"
Result: "Custom Partner Form (Studio)"

# Step 2: Convert
Select the view
Action > Convert Studio Views
Target Module: "my_custom_module"
Convert Views

# Step 3: Check generated files
File created: my_custom_module/views/res_partner_views.xml
Manifest updated: my_custom_module/__manifest__.py

# Step 4: Upgrade module
Apps > my_custom_module > Upgrade

# Step 5: Verify
- Custom form still works ✓
- Studio view is gone ✓
- Code is in version control ✓
```

## Common Use Cases

### Use Case 1: Prototype to Production

```
Studio (Prototype) → Converter → Module (Production)
```

1. Build quickly in Studio
2. Test with users
3. Convert to module when stable
4. Deploy to production

### Use Case 2: Multi-Environment Deployment

```
Dev (Studio) → Converter → Git → Test → Staging → Production
```

1. Create in dev Studio
2. Convert to module
3. Commit to Git
4. Deploy through environments

### Use Case 3: Backup Studio Customizations

```
Studio Views → Converter → Module → Git Repository
```

1. Regularly convert Studio views
2. Commit to version control
3. Have backup of all customizations

## Tips & Tricks

### 🎯 Tip 1: Preview Before Converting
Always check the **Preview XML** tab to see what will be generated.

### 🎯 Tip 2: Group by Model
Convert all views for one model together - they'll be in the same file.

### 🎯 Tip 3: Test in Development
Always test conversions in a development database first.

### 🎯 Tip 4: Use Filters
Use the Studio Views Manager filters:
- **Not Converted**: Shows views ready to convert
- **Converted**: Shows views already converted
- **Pending Cleanup**: Shows views waiting for module upgrade

### 🎯 Tip 5: Batch Processing
Select multiple views at once for faster conversion.

## Keyboard Shortcuts

When in Studio Views Manager:
- `Ctrl/Cmd + Click`: Select multiple views
- `Shift + Click`: Select range of views

## Troubleshooting Quick Fixes

### Problem: "Module path not found"
**Fix:** Ensure the target module is in your addons path and installed.

### Problem: "Manifest update failed"
**Fix:** Manually add the view file to `__manifest__.py`:
```python
'data': [
    'views/res_partner_views.xml',
],
```

### Problem: Studio view not deleted after upgrade
**Fix:** Run cleanup manually:
```python
# In Odoo shell
env['ir.ui.view'].cleanup_converted_views()
```

### Problem: View doesn't render after conversion
**Fix:** Check dependencies in `__manifest__.py`:
```python
'depends': ['base', 'sale', 'stock'],  # Add missing modules
```

## Next Steps

### Learn More
- Read the full [README.md](README.md)
- Check [ADVANCED_USAGE.md](doc/ADVANCED_USAGE.md) for advanced features
- Review [MODULE_STRUCTURE.md](doc/MODULE_STRUCTURE.md) for technical details

### Customize
- Extend the wizard for custom XML generation
- Add custom cleanup logic
- Integrate with your CI/CD pipeline

### Get Help
- Check the logs: Settings > Technical > Logging
- Review test cases: `tests/test_studio_to_module.py`
- Contact Durpro development team

## Cheat Sheet

```bash
# Install module
odoo -d DB -i studio_to_module

# Run tests
odoo -d DB -i studio_to_module --test-enable --stop-after-init

# Upgrade target module
odoo -d DB -u target_module

# Check module path
python -c "from odoo.modules import get_module_path; print(get_module_path('module_name'))"
```

## Quick Reference

| Task | Menu Path |
|------|-----------|
| View all Studio views | Studio to Module > Studio Views |
| Convert views | Studio to Module > Convert to Module |
| Filter not converted | Studio Views > Filter: Not Converted |
| Check conversion status | Studio Views > Columns: Converted to Module |
| Manual cleanup | Technical > Views > Action > Cleanup |

## Success Checklist

- [ ] Module installed
- [ ] Studio views visible in manager
- [ ] Target module selected
- [ ] XML preview looks correct
- [ ] Conversion successful
- [ ] Files created in module directory
- [ ] Manifest updated
- [ ] Module upgraded
- [ ] Studio views deleted
- [ ] Custom views still working
- [ ] Changes committed to Git

## You're Ready! 🚀

You now know how to:
- ✅ Install the module
- ✅ Find Studio views
- ✅ Convert them to code
- ✅ Apply changes
- ✅ Troubleshoot issues

Start converting your Studio customizations to production-ready code!

---

**Need help?** Check the full documentation or contact the Durpro team.
