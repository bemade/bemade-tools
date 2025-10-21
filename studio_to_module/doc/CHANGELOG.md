# Changelog

All notable changes to the Studio to Module Converter will be documented in this file.

## [18.0.1.2.0] - 2025-10-20

### Added
- **Field Renaming with Data Migration** 🔄 - Option to clean field names by removing x_studio_ prefix
  - New field `rename_studio_fields` (boolean) in wizard
  - New method `_clean_field_name()` - Removes x_studio_ prefix from field names
  - Modified `_generate_field_python_code()` to support renaming
  - Modified `_create_fields_python_file()` to return field rename mapping
  - Modified `_create_or_update_hooks()` to generate SQL data copy when renaming
  - **Workflow with renaming enabled:**
    - x_studio_approval_date → approval_date (Python field name cleaned)
    - Generates SQL UPDATE to copy data from old to new column
    - Optional: DROP old column after copy
  - **Workflow with renaming disabled (default):**
    - x_studio_approval_date → x_studio_approval_date (same name)
    - Data preserved automatically (same column)
    - No SQL migration needed
  - SQL migration uses PostgreSQL table name from ir.model
  - Logs number of rows copied per field
  - Toggle in wizard UI for easy enable/disable

- **Conversion Preview Wizard** 📋 - Popup confirmation before migration
  - New wizard `studio.view.converter.preview`
  - Shows detailed preview of conversion actions:
    - Number of views to convert
    - Number of Studio custom fields detected
    - List of files that will be created/updated
    - Grouped view information by model
    - Custom fields grouped by model with field details
    - Step-by-step action list
  - User must confirm before actual conversion
  - Clear visual presentation with HTML formatting
  - Cancel button to abort conversion
  - Split `action_convert_views()` → preview → `action_convert_views_confirmed()`

- **Studio Custom Fields Migration** 🎯 - Automatic detection and migration of Studio fields
  - **Field Definition Migration:**
    - New method `_extract_field_names_from_arch()` - Extract all field names from view XML
    - New method `_get_studio_fields_for_views()` - Find Studio custom fields used in views
    - New method `_generate_field_python_code()` - Generate Python field definitions
    - New method `_create_fields_python_file()` - Create Python modules for custom fields
    - New method `_update_models_init_py()` - Update models/__init__.py imports
    - Detects fields with `is_custom=True` or starting with `x_studio_`
    - Generates Python code with:
      - Correct field type mapping (Char, Text, Many2one, etc.)
      - Field parameters (string, required, readonly, help)
      - Relation fields (many2one, one2many, many2many)
      - Selection values from ir.model.fields.selection
      - Default values when applicable
    - Creates `models/{model}_custom_fields.py` per model
    - Groups fields by model for clean file organization
    - Automatically updates `models/__init__.py` with new imports
  - **Field Data Migration:** 💾
    - Modified `_create_or_update_hooks()` to generate data migration code
    - Adds `_migrate_studio_fields_data()` function in `hooks.py`
    - Automatic data preservation when using same field name
    - No data loss: Odoo keeps column data when field is redefined in Python
    - Generated `post_init_hook` verifies field existence before migration
    - Logs success for each migrated field
    - Option to clean up Studio field definitions after migration
    - Integrated in conversion workflow (no user action needed)
  - Fields and their data are fully migrated when views using them are converted
  - Logs detailed information about migrated fields and data preservation

## [18.0.1.1.8] - 2025-10-20

### Fixed
- **Critical: Double comma prevention** - Robust cleanup of manifest syntax errors
  - Added centralized `_clean_manifest_commas()` method
  - Removes double commas: `,, ` → `,`
  - Removes triple+ commas: `,,, ` → `,`
  - Removes trailing commas before `]` and `}`
  - Applied to all manifest modification methods:
    - `_update_manifest()` - Final cleanup after data section update
    - `_add_studio_cleanup_dependency()` - After adding dependency
    - `_update_manifest_hook()` - After adding post_init_hook
  - Improved `depends` pattern to support both `'depends'` and `"depends"`
  - Better comma handling when adding dependencies (no trailing commas)
  - Prevents syntax errors like `"application": False,,`

## [18.0.1.1.7] - 2025-10-20

### Fixed
- **Virtual modules warning** - Prevent "module not found" warning for virtual modules
  - Added filtering for `studio_customization`, `base_import_module`, `web_studio`
  - These modules exist in DB but have no physical folder
  - Prevents unnecessary warnings in logs when listing eligible modules
  - Cleaner module selection without false warnings

## [18.0.1.1.6] - 2025-10-20

### Fixed
- **Critical: Domain filtering** - Fixed `target_module_id` to use dynamic domain
  - Changed from static domain `('state', '=', 'installed')` to dynamic filtering
  - Added `_get_allowed_module_ids()` method for domain evaluation
  - Domain now: `('id', 'in', allowed_module_ids)`
  - Field now correctly filters dropdown to show only custom modules

## [18.0.1.1.5] - 2025-10-20

### Fixed
- **Critical: Domain filtering** - Fixed `target_module_id` to use dynamic domain
  - Changed from static domain `('state', '=', 'installed')` to dynamic filtering
  - Added `_get_allowed_module_ids()` method for domain evaluation
  - Domain now: `('id', 'in', allowed_module_ids)`
  - Field now correctly filters dropdown to show only custom modules

## [18.0.1.1.4] - 2025-10-20

### Fixed
- **Module filtering improved** - Fixed detection to properly exclude odoo/enterprise/themes
  - Changed from path component check to full path string check
  - Now checks if `/odoo/`, `/enterprise/`, or `/design-themes/` appear in parent path
  - More reliable exclusion of non-custom modules
  - Only custom modules in `addons/` folder are shown

## [18.0.1.1.3] - 2025-10-20

### Fixed
- **Double comma cleanup** - Added regex cleanup to remove double commas in manifest
  - Prevents `"application": False,,` type errors
  - Cleans up both single and multiple data section scenarios
  - Regex pattern: `,\s*,` → `,`
  - Applied after adding new files but before writing manifest

## [18.0.1.1.2] - 2025-10-20

### Improved
- **Target module selection** - Enhanced filtering with portable path detection
  - Uses relative path analysis instead of absolute paths (works in any setup)
  - Excludes modules with `odoo`, `enterprise`, or `design-themes` in path
  - Only shows modules in folders named `addons` (parent directory check)
  - Still excludes symlinks as before
  - Portable solution that works regardless of installation location
  - No dependency on specific configuration or hardcoded paths

## [18.0.1.1.1] - 2025-10-20

### Fixed
- **Critical: Manifest syntax error** - Fixed bug where adding files to manifest created orphan comma
  - Issue: When `data` list ended with trailing comma, adding new file created syntax error
  - Example: `'file.xml',` + `,\n 'new.xml'` = invalid syntax
  - Solution: Check if content ends with comma before adding new one
  - Affected methods: `_update_manifest()` (both single and multiple data sections)

## [18.0.1.1.0] - 2025-10-20

### Added
- **🆕 Automatic Backup System**: Creates timestamped backups before any conversion
  - Backup location: `{module_path}/.studio_backups/{timestamp}/`
  - Includes: Studio views data (JSON), manifest, init, hooks, and views folder
  - Backups retained indefinitely for manual recovery
- **🆕 Rollback on Error**: Automatically restores from backup if conversion fails
  - Graceful error handling with detailed error messages
  - Manual recovery instructions provided if rollback fails
- **🆕 Organized File Structure**: Views now organized by model
  - One file per model: `{model}_studio_views.xml`
  - "studio" identifier in filename for easy identification
  - Better maintainability and version control

### Changed
- File naming convention: From `migrated_studio_views.xml` to `{model}_studio_views.xml`
- Success messages now include emoji indicators and detailed file lists
- Backup path displayed in success/error messages

### Technical
- Added imports: `json`, `shutil`, `datetime`
- New methods:
  - `_create_backup(module_path)`: Creates comprehensive backup
  - `_rollback_from_backup(backup_dir, module_path)`: Restores from backup
- Modified method: `action_convert_views()` with try-catch wrapper
- New field: `backup_path` (Char) to store backup location

### Documentation
- Updated README.md with new features
- Enhanced Safety Features section
- Added Backup System documentation

## [18.0.1.0.0] - 2025-01-16

### Added
- Initial release for Odoo 18.0
- Studio view detection and listing
- Batch conversion of Studio views to module XML files
- Automatic module manifest update
- Auto cleanup of Studio views after module upgrade
- Wizard interface for easy conversion
- Studio Views Manager with filtering and grouping
- XML preview before conversion
- Support for inherited views
- Comprehensive test suite
- Optional cron job for automatic cleanup
- Full documentation and README

### Features
- **View Detection**: Automatically identifies all Studio-created views
- **Smart XML Generation**: Generates proper Odoo XML view files
- **Module Integration**: Updates target module's `__manifest__.py`
- **Auto Cleanup**: Removes Studio views after successful module upgrade
- **Preview**: Shows generated XML before conversion
- **Batch Processing**: Convert multiple views at once
- **Error Handling**: Graceful error handling with informative messages
- **Logging**: Failed operations are logged for review

### Technical
- Compatible with Odoo 18.0
- Requires `web_studio` module
- Extends `ir.ui.view` and `ir.module.module` models
- Provides `studio.view.converter` wizard
- Includes security rules and access rights
- Full test coverage with TransactionCase tests

### Documentation
- Comprehensive README.md
- HTML description page
- Inline code documentation
- Usage examples and best practices
- Troubleshooting guide

## Future Enhancements (Planned)

### Version 18.0.1.2.0
- [ ] Support for converting Studio actions
- [ ] Support for converting Studio automations
- [ ] Export/Import functionality for sharing conversions
- [ ] Dry-run mode to preview changes without writing files
- [ ] XML validation with XSD schema

### Version 18.0.1.3.0
- [ ] Support for converting Studio fields
- [ ] Support for converting Studio models
- [ ] Git integration for automatic commits
- [ ] Conflict detection and resolution
- [ ] Backup retention policy (automatic old backup cleanup)

### Version 18.0.2.0.0
- [ ] Web interface for remote conversions
- [ ] Batch operations across multiple modules
- [ ] Conversion history and audit trail
- [ ] Advanced filtering and search
- [ ] Custom templates for XML generation

## Known Issues

None at this time.

## Support

For issues, questions, or feature requests, please contact the Durpro development team.

## License

LGPL-3
