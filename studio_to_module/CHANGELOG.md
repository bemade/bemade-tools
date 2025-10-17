# Changelog

All notable changes to the Studio to Module Converter will be documented in this file.

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

### Version 18.0.1.1.0
- [ ] Support for converting Studio actions
- [ ] Support for converting Studio automations
- [ ] Export/Import functionality for sharing conversions
- [ ] Dry-run mode to preview changes without writing files
- [ ] Backup creation before conversion

### Version 18.0.1.2.0
- [ ] Support for converting Studio fields
- [ ] Support for converting Studio models
- [ ] Git integration for automatic commits
- [ ] Conflict detection and resolution
- [ ] Rollback functionality

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
