# Useful Commands - Studio to Module Converter

## Installation Commands

```bash
# Install module
odoo -d your_database -i studio_to_module --stop-after-init

# Upgrade module
odoo -d your_database -u studio_to_module --stop-after-init

# Install with test
odoo -d your_database -i studio_to_module --test-enable --stop-after-init

# Uninstall module
odoo -d your_database --uninstall studio_to_module
```

## Testing Commands

```bash
# Run all tests
./addons/studio_to_module/scripts/test_module.sh your_database

# Run tests with specific tag
odoo -d your_database --test-tags studio_to_module --stop-after-init

# Run tests with coverage
odoo -d your_database -i studio_to_module --test-enable --stop-after-init --log-level=test

# Run specific test class
odoo -d your_database --test-tags studio_to_module.TestStudioToModule --stop-after-init
```

## Development Commands

```bash
# Start with auto-reload
odoo -d your_database -i studio_to_module --dev=all

# Start with XML reload
odoo -d your_database -i studio_to_module --dev=xml

# Check Python syntax
python -m py_compile addons/studio_to_module/**/*.py

# Validate XML files
xmllint --noout addons/studio_to_module/**/*.xml

# Format Python code (if using black)
black addons/studio_to_module/

# Lint Python code (if using flake8)
flake8 addons/studio_to_module/
```

## Odoo Shell Commands

```bash
# Start Odoo shell
odoo shell -d your_database

# Then in shell:
```

```python
# Find all Studio views tracked by the module
studio_views = env['ir.ui.view'].search([('is_studio_view', '=', True)])
print(f"Found {len(studio_views)} Studio views")

# Find views not yet converted
pending = env['ir.ui.view'].search([
    ('is_studio_view', '=', True),
    ('converted_to_module', '=', False)
])
print(f"Pending conversion: {len(pending)}")

# Convert a batch of views to a given module
target_module = env['ir.module.module'].search([
    ('state', '=', 'installed'),
    ('name', '=', 'your_target_module')
], limit=1)

views_to_convert = env['ir.ui.view'].search([
    ('is_studio_view', '=', True),
    ('converted_to_module', '=', False),
    ('model', '=', 'res.partner')
])

if target_module and views_to_convert:
    wizard = env['studio.view.converter'].create({
        'target_module_id': target_module.id,
        'studio_view_ids': [(6, 0, views_to_convert.ids)],
    })
    wizard.action_convert_views()
    env.cr.commit()

# Check conversion status per module
status = env['ir.ui.view'].read_group(
    domain=[('is_studio_view', '=', True)],
    fields=['converted_to_module'],
    groupby=['converted_to_module']
)
print(status)

# Manual cleanup
env['ir.ui.view'].cleanup_converted_views()
env.cr.commit()

# Preview XML for a view
view = env['ir.ui.view'].search([('is_studio_view', '=', True)], limit=1)
wizard = env['studio.view.converter'].create({
    'target_module_id': env['ir.module.module'].search([('state', '=', 'installed')], limit=1).id
})
print(wizard._generate_view_xml(view))
```

## Database Commands

```bash
# Backup database
pg_dump your_database > backup_$(date +%Y%m%d_%H%M%S).sql

# Restore database
psql your_database < backup_file.sql

# Check module installation
psql your_database -c "SELECT name, state FROM ir_module_module WHERE name = 'studio_to_module';"

# Count Studio views tracked by the module
psql your_database -c "SELECT COUNT(*) FROM ir_ui_view WHERE converted_to_module IS NOT NULL OR pending_cleanup = true;"

# Check converted views
psql your_database -c "SELECT COUNT(*) FROM ir_ui_view WHERE converted_to_module = true;"
```

## File System Commands

```bash
# Find all Python files
find addons/studio_to_module -name "*.py" -type f

# Find all XML files
find addons/studio_to_module -name "*.xml" -type f

# Count lines of code
find addons/studio_to_module -name "*.py" -type f -exec wc -l {} + | tail -1

# Check module structure
tree addons/studio_to_module -L 3

# Search for TODO comments
grep -r "TODO" addons/studio_to_module/

# Search for FIXME comments
grep -r "FIXME" addons/studio_to_module/

# Check file permissions
ls -la addons/studio_to_module/
```

## Git Commands

```bash
# Add module to git
git add addons/studio_to_module/

# Commit module
git commit -m "Add Studio to Module Converter"

# Check status
git status addons/studio_to_module/

# View changes
git diff addons/studio_to_module/

# Create feature branch
git checkout -b feature/studio-converter

# Tag release
git tag -a v18.0.1.0.0 -m "Studio to Module Converter v18.0.1.0.0"
```

## Module Management Commands

```bash
# List all modules
odoo -d your_database --list-modules

# Update module list
odoo -d your_database --update-list

# Check module dependencies
grep "depends" addons/studio_to_module/__manifest__.py

# Check module version
grep "version" addons/studio_to_module/__manifest__.py

# Find module path
python -c "from odoo.modules import get_module_path; print(get_module_path('studio_to_module'))"
```

## Conversion Workflow Commands

```bash
# 1. Find Studio views
odoo shell -d your_database -c "
views = env['ir.ui.view'].search([('studio', '=', True)])
for v in views: print(f'{v.id}: {v.name} ({v.model})')
"

# 2. Convert views (via UI)
# Go to: Studio to Module > Studio Views
# Select views > Action > Convert Studio Views

# 3. Upgrade target module
odoo -d your_database -u target_module --stop-after-init

# 4. Verify cleanup
odoo shell -d your_database -c "
pending = env['ir.ui.view'].search([('pending_cleanup', '=', True)])
print(f'Pending cleanup: {len(pending)}')
"
```

## Debugging Commands

```bash
# Start with debugger
odoo -d your_database -i studio_to_module --dev=all --log-level=debug

# Check logs
tail -f /var/log/odoo/odoo.log

# Filter logs for module
tail -f /var/log/odoo/odoo.log | grep studio_to_module

# Check error logs
grep ERROR /var/log/odoo/odoo.log | grep studio_to_module

# Enable SQL logging
odoo -d your_database --log-sql --log-level=debug

# Python debugger in code
import pdb; pdb.set_trace()

# Or use ipdb
import ipdb; ipdb.set_trace()
```

## Performance Commands

```bash
# Profile module loading
odoo -d your_database -i studio_to_module --profile

# Check module load time
time odoo -d your_database -i studio_to_module --stop-after-init

# Memory usage
odoo -d your_database -i studio_to_module --max-cron-threads=0 --workers=0

# Database query analysis
psql your_database -c "EXPLAIN ANALYZE SELECT * FROM ir_ui_view WHERE studio = true;"
```

## Deployment Commands

```bash
# Production deployment
# 1. Backup
pg_dump production_db > backup_before_studio_converter.sql

# 2. Copy module
scp -r studio_to_module user@production:/path/to/odoo/addons/

# 3. Set permissions
ssh user@production "chown -R odoo:odoo /path/to/odoo/addons/studio_to_module"

# 4. Install
ssh user@production "odoo -d production_db -i studio_to_module --stop-after-init"

# 5. Restart Odoo
ssh user@production "sudo systemctl restart odoo"

# 6. Verify
ssh user@production "odoo shell -d production_db -c \"print(env['ir.module.module'].search([('name', '=', 'studio_to_module')]).state)\""
```

## Docker Commands

```bash
# Build image with module
docker build -t odoo-with-studio-converter .

# Run container
docker run -d --name odoo -p 8069:8069 odoo-with-studio-converter

# Install module in container
docker exec odoo odoo -d odoo -i studio_to_module --stop-after-init

# Access shell in container
docker exec -it odoo odoo shell -d odoo

# View logs
docker logs -f odoo

# Copy module to container
docker cp studio_to_module odoo:/mnt/extra-addons/
```

## Maintenance Commands

```bash
# Clean pyc files
find addons/studio_to_module -name "*.pyc" -delete
find addons/studio_to_module -name "__pycache__" -type d -exec rm -rf {} +

# Update documentation
# Edit markdown files, then commit

# Check for updates
git fetch origin
git log HEAD..origin/main --oneline -- addons/studio_to_module/

# Apply updates
git pull origin main

# Upgrade after update
odoo -d your_database -u studio_to_module --stop-after-init
```

## Batch Operations

```bash
# Convert Studio views for multiple databases (example)
for db in db1 db2 db3; do
    echo "Processing $db..."
    odoo shell -d "$db" -c "
target_module = env['ir.module.module'].search([('name', '=', 'your_target_module'), ('state', '=', 'installed')], limit=1)
views = env['ir.ui.view'].search([('is_studio_view', '=', True), ('converted_to_module', '=', False)])
if target_module and views:
    wizard = env['studio.view.converter'].create({
        'target_module_id': target_module.id,
        'studio_view_ids': [(6, 0, views.ids)],
    })
    wizard.action_convert_views()
    env.cr.commit()
"
done

# Install on multiple databases
for db in db1 db2 db3; do
    echo "Installing on $db..."
    odoo -d "$db" -i studio_to_module --stop-after-init
done

# Backup multiple databases
for db in db1 db2 db3; do
    echo "Backing up $db..."
    pg_dump "$db" > "backup_${db}_$(date +%Y%m%d).sql"
done
```

## Quick Reference

| Task | Command |
|------|---------|
| Install | `odoo -d DB -i studio_to_module` |
| Upgrade | `odoo -d DB -u studio_to_module` |
| Test | `./scripts/test_module.sh DB` |
| Shell | `odoo shell -d DB` |
| Logs | `tail -f /var/log/odoo/odoo.log` |
| Backup | `pg_dump DB > backup.sql` |
| Check Status | `odoo shell -d DB -c "env['ir.ui.view'].search_count([('studio', '=', True)])"` |

## Environment Variables

```bash
# Set Odoo config
export ODOO_RC=/path/to/odoo.conf

# Set database
export PGDATABASE=your_database

# Set addons path
export ODOO_ADDONS_PATH=/path/to/addons

# Use in commands
odoo -i studio_to_module
```

## Troubleshooting Commands

```bash
# Check if module is installed
odoo shell -d your_database -c "print(env['ir.module.module'].search([('name', '=', 'studio_to_module')]).state)"

# Check for errors in views
odoo shell -d your_database -c "env['ir.ui.view'].search([]).mapped('name')"

# Validate all XML
for file in $(find addons/studio_to_module -name "*.xml"); do
    echo "Checking $file..."
    xmllint --noout "$file" || echo "ERROR in $file"
done

# Check Python imports
python -c "from odoo.addons.studio_to_module import models, wizard"

# Reset module state (if stuck)
psql your_database -c "UPDATE ir_module_module SET state='uninstalled' WHERE name='studio_to_module';"
```

## Useful Aliases

Add to your `.bashrc` or `.zshrc`:

```bash
# Odoo aliases
alias odoo-shell='odoo shell -d'
alias odoo-install='odoo -d $1 -i'
alias odoo-upgrade='odoo -d $1 -u'
alias odoo-test='odoo -d $1 --test-enable --stop-after-init'

# Studio to Module aliases
alias stm-install='odoo -d $1 -i studio_to_module --stop-after-init'
alias stm-test='./addons/studio_to_module/scripts/test_module.sh'
alias stm-status="odoo shell -d $1 -c \"print(env['ir.ui.view'].search_count([('is_studio_view', '=', True), ('converted_to_module', '=', False)]))\""
```

## One-Liners

```bash
# Count Studio views
odoo shell -d DB -c "print(env['ir.ui.view'].search_count([('is_studio_view', '=', True)]))"

# List Studio views
odoo shell -d DB -c "for v in env['ir.ui.view'].search([('is_studio_view', '=', True)]): print(v.name)"

# Convert all pending views
odoo shell -d DB -c "from odoo.addons.studio_to_module.examples import example_usage; example_usage.batch_convert_with_filter(env, 'target_module'); env.cr.commit()"

# Manual cleanup
odoo shell -d DB -c "env['ir.ui.view'].cleanup_converted_views(); env.cr.commit()"

# Check module version
grep version addons/studio_to_module/__manifest__.py | cut -d"'" -f2
```

---

**Pro Tip:** Save frequently used commands in a script or alias for quick access!
