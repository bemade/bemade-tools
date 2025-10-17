# Studio to Module Converter - Module Structure

## Overview

```
studio_to_module/
├── 📄 __init__.py                          # Module initialization
├── 📄 __manifest__.py                      # Module manifest
├── 📄 README.md                            # Main documentation
├── 📄 CHANGELOG.md                         # Version history
├── 📄 .gitignore                           # Git ignore rules
│
├── 📁 models/                              # Python models
│   ├── __init__.py
│   ├── ir_ui_view.py                       # Extended ir.ui.view model
│   └── ir_module_module.py                 # Extended ir.module.module model
│
├── 📁 wizard/                              # Transient models (wizards)
│   ├── __init__.py
│   └── studio_view_converter.py            # Main conversion wizard
│
├── 📁 views/                               # XML view definitions
│   ├── menu_views.xml                      # Menu items
│   ├── studio_view_manager_views.xml       # View manager interface
│   └── studio_view_converter_views.xml     # Wizard views (in wizard/)
│
├── 📁 security/                            # Access control
│   └── ir.model.access.csv                 # Model access rights
│
├── 📁 data/                                # Data files
│   └── ir_cron_data.xml                    # Cron job for cleanup
│
├── 📁 tests/                               # Unit tests
│   ├── __init__.py
│   └── test_studio_to_module.py            # Test suite
│
├── 📁 static/                              # Static assets
│   └── description/
│       └── index.html                      # Module description page
│
├── 📁 doc/                                 # Documentation
│   ├── ADVANCED_USAGE.md                   # Advanced usage guide
│   └── MODULE_STRUCTURE.md                 # This file
│
└── 📁 scripts/                             # Helper scripts
    └── test_module.sh                      # Test runner script
```

## File Descriptions

### Core Files

#### `__manifest__.py`
Module metadata and configuration:
- Name, version, author
- Dependencies (base, web_studio)
- Data files to load
- Module settings

#### `__init__.py`
Module initialization:
- Imports models and wizard packages
- Sets up Python module structure

### Models

#### `models/ir_ui_view.py`
Extends the `ir.ui.view` model:
- **Fields:**
  - `is_studio_view`: Computed field to identify Studio views
  - `converted_to_module`: Tracks conversion status
  - `target_module_id`: References target module
  - `pending_cleanup`: Marks views for deletion
- **Methods:**
  - `mark_for_conversion()`: Marks view as converted
  - `cleanup_converted_views()`: Deletes converted views

#### `models/ir_module_module.py`
Extends the `ir.module.module` model:
- **Methods:**
  - `button_immediate_upgrade()`: Triggers cleanup after upgrade
  - `update_list()`: Triggers cleanup after module list update

### Wizard

#### `wizard/studio_view_converter.py`
Main conversion wizard:
- **Fields:**
  - `studio_view_ids`: Selected Studio views
  - `target_module_id`: Target module
  - `module_path`: Computed module path
  - `view_folder`: Views folder name
  - `auto_cleanup`: Auto cleanup flag
  - `preview_xml`: XML preview
- **Methods:**
  - `_sanitize_xml_id()`: Converts names to XML IDs
  - `_generate_view_xml()`: Generates XML for a view
  - `_get_or_create_views_file()`: Gets/creates XML file
  - `_create_xml_file()`: Writes XML to file
  - `_update_manifest()`: Updates module manifest
  - `action_convert_views()`: Main conversion action

### Views

#### `views/menu_views.xml`
Menu structure:
- Main menu: "Studio to Module"
- Sub-menu: "Studio Views" (manager)
- Sub-menu: "Convert to Module" (wizard)

#### `views/studio_view_manager_views.xml`
Studio views management interface:
- Extended tree view with decorations
- Search filters for Studio views
- Group by options
- Action to open converter wizard

#### `wizard/studio_view_converter_views.xml`
Wizard interface:
- Form view with fields
- XML preview tab
- Instructions tab
- Convert and Cancel buttons

### Security

#### `security/ir.model.access.csv`
Access control rules:
- System administrators can access wizard
- Full CRUD permissions

### Data

#### `data/ir_cron_data.xml`
Scheduled actions:
- Daily cron job for automatic cleanup
- Disabled by default
- Can be enabled for automatic maintenance

### Tests

#### `tests/test_studio_to_module.py`
Comprehensive test suite:
- Studio view detection
- Mark for conversion
- XML ID sanitization
- XML generation
- Wizard validation
- Cleanup functionality
- Default values
- Inherited views
- Multiple views handling

### Documentation

#### `README.md`
Main documentation:
- Overview and features
- Usage instructions
- Workflow explanation
- Configuration options
- Technical details
- Troubleshooting

#### `CHANGELOG.md`
Version history:
- Release notes
- Feature additions
- Bug fixes
- Future enhancements

#### `doc/ADVANCED_USAGE.md`
Advanced usage guide:
- Batch conversion strategies
- Custom XML generation
- Module structure best practices
- CI/CD integration
- Troubleshooting advanced scenarios

#### `doc/MODULE_STRUCTURE.md`
This file - module structure documentation

### Scripts

#### `scripts/test_module.sh`
Test runner script:
- Runs unit tests
- Validates installation
- Provides next steps

### Static Assets

#### `static/description/index.html`
Module description page:
- Feature highlights
- Workflow visualization
- Quick start guide
- Use cases
- Requirements

## Data Flow

```
┌─────────────────┐
│  Studio Views   │
│  (ir.ui.view)   │
└────────┬────────┘
         │
         ▼
┌─────────────────────────┐
│  Studio View Manager    │
│  (List/Filter/Select)   │
└────────┬────────────────┘
         │
         ▼
┌─────────────────────────┐
│  Conversion Wizard      │
│  (studio.view.converter)│
└────────┬────────────────┘
         │
         ├─► Select Target Module
         ├─► Generate XML
         ├─► Create Files
         ├─► Update Manifest
         └─► Mark for Cleanup
         │
         ▼
┌─────────────────────────┐
│  Module Files           │
│  (views/*.xml)          │
└────────┬────────────────┘
         │
         ▼
┌─────────────────────────┐
│  Module Upgrade         │
│  (button_immediate_     │
│   upgrade)              │
└────────┬────────────────┘
         │
         ▼
┌─────────────────────────┐
│  Automatic Cleanup      │
│  (cleanup_converted_    │
│   views)                │
└─────────────────────────┘
```

## Key Design Patterns

### 1. Extension Pattern
- Extends existing Odoo models (`ir.ui.view`, `ir.module.module`)
- Adds fields and methods without breaking existing functionality

### 2. Wizard Pattern
- Uses transient model for conversion workflow
- Provides step-by-step interface
- Validates input before processing

### 3. Hook Pattern
- Hooks into module upgrade process
- Automatically triggers cleanup
- Non-intrusive integration

### 4. Template Pattern
- XML generation follows standard Odoo format
- Consistent structure across generated files
- Easy to customize

### 5. Factory Pattern
- Creates XML files based on model type
- Groups views by model
- Generates appropriate filenames

## Extension Points

### Custom XML Generation
Override `_generate_view_xml()` in wizard to customize XML output.

### Custom Cleanup Logic
Override `cleanup_converted_views()` in `ir.ui.view` to add custom cleanup behavior.

### Custom File Organization
Override `_get_or_create_views_file()` to change file naming/organization.

### Custom Manifest Updates
Override `_update_manifest()` to customize manifest modification.

## Dependencies

### Required Modules
- `base`: Core Odoo functionality
- `web_studio`: Studio view creation

### Python Dependencies
- `lxml`: XML parsing and generation
- `os`: File system operations
- `re`: Regular expressions for text processing

### System Requirements
- Write access to module directories
- Python 3.8+
- Odoo 18.0+

## Performance Considerations

### Batch Processing
- Groups views by model for efficient file creation
- Minimizes file I/O operations
- Single manifest update per conversion

### Lazy Computation
- XML preview computed on demand
- Module path computed only when needed
- Cleanup runs only after module upgrade

### Error Handling
- Graceful degradation on errors
- Logging for failed operations
- Continues processing on non-critical errors

## Security Considerations

### Access Control
- Restricted to system administrators
- Requires write access to file system
- Validates module paths

### Data Integrity
- Validates XML before writing
- Marks views before deletion
- Logs all operations

### Audit Trail
- Tracks conversion status
- Records target module
- Maintains cleanup flags

## Future Enhancements

See `CHANGELOG.md` for planned features:
- Studio actions conversion
- Studio automations conversion
- Studio fields conversion
- Git integration
- Web interface
- Conversion history

## Contributing

To extend this module:
1. Follow Odoo coding standards
2. Add tests for new features
3. Update documentation
4. Maintain backward compatibility
5. Use proper inheritance patterns

## License

LGPL-3 - See module manifest for details
