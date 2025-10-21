# Studio to Module Converter

Convert Odoo Studio customizations into version-controlled module code.

## Overview

This module provides a tool to convert Odoo Studio customizations into proper module code. Perfect for development workflows where you prototype in Studio and then want to version-control and deploy your customizations as code.

## Features

- **List Studio Views**: Browse all views created through Odoo Studio
- **Batch Conversion**: Convert multiple Studio views at once
- **Smart XML Generation**: Automatically generates proper XML view files
- **Module Integration**: Automatically updates target module's `__manifest__.py`
- **Auto Cleanup**: Optionally removes Studio views after successful module upgrade
- **Preview**: See the generated XML before conversion
- **🆕 Automatic Backup**: Creates timestamped backups before any conversion
- **🆕 Rollback on Error**: Automatically restores from backup if conversion fails
- **🆕 Organized by Model**: Creates separate files per model with "studio" identifier

## Usage

### Method 1: From Studio Views Manager

1. Go to **Studio to Module > Studio Views**
2. Select the Studio views you want to convert
3. Click **Action > Convert Studio Views**
4. Choose your target module
5. Click **Convert Views**

### Method 2: From Views Menu

1. Go to **Studio to Module > Convert to Module**
2. Select Studio views from the dropdown
3. Choose target module
4. Configure options (views folder, auto cleanup)
5. Click **Convert Views**

### Method 3: From Technical Views

1. Go to **Settings > Technical > User Interface > Views**
2. Filter for Studio views
3. Select views and use **Action > Convert Studio Views**

## Workflow

1. **Create in Studio**: Build your customizations using Odoo Studio
2. **Convert**: Use this tool to convert Studio views to XML files
3. **Review**: Check the generated XML files in your module
4. **Upgrade**: Upgrade the target module to apply changes
5. **Auto Cleanup**: Studio views are automatically deleted after upgrade

## Configuration

- **Views Folder**: Specify where XML files should be created (default: `views`)
- **Auto Cleanup**: Enable/disable automatic deletion of Studio views after module upgrade
- **Target Module**: Must be an installed custom module in your addons path

## Technical Details

### Generated Files

The tool creates XML files following this pattern:
- File name: `{model_name}_studio_views.xml` (e.g., `sale_order_studio_views.xml`)
- Location: `{module_path}/{views_folder}/`
- Format: Standard Odoo XML data files with proper structure
- Organization: One file per model for better maintainability

### Backup System

Before each conversion, the tool automatically creates a backup:
- Location: `{module_path}/.studio_backups/{timestamp}/`
- Contents:
  - `studio_views_backup.json`: Complete view data export
  - `__manifest__.py`: Original manifest file
  - `__init__.py`: Original init file
  - `hooks.py`: Original hooks (if exists)
  - `views/`: Complete views folder backup
- Retention: Backups are kept indefinitely for manual recovery

### Manifest Update

The tool automatically updates your module's `__manifest__.py` to include the new view files in the `data` list.

### Cleanup Process

When auto cleanup is enabled:
1. Views are marked with `pending_cleanup = True`
2. After module upgrade, the system checks for pending views
3. If the target module is installed and updated, Studio views are deleted
4. Errors are logged but don't block the process

## Requirements

- Odoo 18.0+
- `web_studio` module installed
- Write access to target module directory
- System Administrator rights

## Safety Features

- **Validation**: Ensures XML is valid before writing files
- **Automatic Backup**: Creates timestamped backups before any modification
- **Rollback on Error**: Automatically restores from backup if conversion fails
- **Manual Recovery**: Backups retained indefinitely for manual intervention
- **Original Views Preserved**: Studio views remain until module is upgraded
- **Error Handling**: Graceful error handling with informative messages
- **Detailed Logging**: All operations logged with success/failure status

## Best Practices

1. **Test First**: Always test conversions on a development server
2. **Version Control**: Commit generated files to your repository
3. **Review XML**: Check generated XML before deploying to production
4. **Backup**: Keep database backups before major conversions
5. **Incremental**: Convert views in small batches for easier troubleshooting

## Troubleshooting

### Module Path Not Found
- Ensure the target module is in your addons path
- Check that the module is properly installed
- Verify file system permissions

### Manifest Update Failed
- Manually add the view files to `__manifest__.py`
- Check for syntax errors in the manifest
- Ensure proper Python dictionary format

### Views Not Deleted After Upgrade
- Check the module upgrade completed successfully
- Verify `pending_cleanup` flag is set on views
- Review logs for cleanup errors

## Support

For issues or questions, contact the Durpro development team.

## License

LGPL-3
