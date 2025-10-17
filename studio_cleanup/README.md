# Studio Cleanup Helpers

A lightweight Odoo module providing reusable helper functions for cleaning up Studio views that have been migrated to module code.

## Purpose

When migrating Studio customizations to version-controlled module code, the original Studio views need to be deleted to avoid conflicts. This module provides a safe, reusable way to do this cleanup.

## Features

- ✅ **Lightweight**: No UI, no dependencies beyond `base`
- ✅ **Production-ready**: Safe to install in production
- ✅ **Reusable**: Use in any module that has migrated Studio views
- ✅ **Safe**: Deletes only specified views by exact external ID
- ✅ **Detailed logging**: Clear logs of what was deleted

## Installation

1. Add this module to your addons path
2. Install it: `Apps > Studio Cleanup Helpers > Install`
3. Add it as a dependency in modules that need cleanup

## Usage

### 1. Add Dependency

In your module's `__manifest__.py`:

```python
{
    'name': 'My Module',
    'depends': ['base', 'studio_cleanup'],  # Add studio_cleanup
    # ...
}
```

### 2. Create hooks.py

Create or update `hooks.py` in your module:

```python
# -*- coding: utf-8 -*-

from odoo.addons.studio_cleanup.tools import cleanup_studio_views_by_xmlid


def post_init_hook(env):
    """Clean up Studio views after module installation/upgrade."""
    studio_view_ids_to_delete = [
        'studio_customization.odoo_studio_stock_production_lot_tree_customization',
        'studio_customization.odoo_studio_stock_picking_form_customization',
    ]
    
    cleanup_studio_views_by_xmlid(env, studio_view_ids_to_delete, 'my_module')
```

### 3. Update __init__.py

```python
from . import models
from . import hooks  # Add this
```

### 4. Update __manifest__.py

```python
{
    'name': 'My Module',
    'depends': ['base', 'studio_cleanup'],
    'post_init_hook': 'post_init_hook',  # Add this
    # ...
}
```

## How It Works

1. When you upgrade a module with a `post_init_hook`
2. The hook calls `cleanup_studio_views_by_xmlid()`
3. The function tries to find each Studio view by its external ID
4. If found, the view is deleted
5. Detailed logs are written to `odoo.log`

## Example Log Output

```
INFO: [durpro_stock] ✓ Deleted Studio view: Odoo Studio: stock.lot.tree (ID: 10449, XML ID: studio_customization.odoo_studio_xxx)
INFO: [durpro_stock] Studio views cleanup completed: 3 deleted, 0 skipped, 0 failed
```

## Development vs Production

### Development
- Use `studio_to_module` to convert Studio views to XML
- `studio_to_module` automatically generates `hooks.py` using this module

### Production
- Only install `studio_cleanup` (not `studio_to_module`)
- Deploy your modules with `hooks.py` already generated
- Hooks run automatically on module upgrade

## API Reference

### cleanup_studio_views_by_xmlid(env, studio_view_xmlids, module_name)

Delete specific Studio views by their external IDs.

**Parameters:**
- `env` (Environment): Odoo environment
- `studio_view_xmlids` (list): List of external IDs to delete (e.g., `['studio_customization.odoo_studio_xxx']`)
- `module_name` (str): Name of the calling module (for logging)

**Returns:**
- `int`: Number of views successfully deleted

**Example:**
```python
deleted = cleanup_studio_views_by_xmlid(
    env, 
    ['studio_customization.odoo_studio_stock_lot_tree_customization'],
    'durpro_stock'
)
# deleted = 1
```

## License

LGPL-3

## Author

Durpro
