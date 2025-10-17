# Installation & Setup Guide

## Prerequisites

### System Requirements
- **Odoo Version:** 18.0 or higher
- **Python:** 3.8+
- **Operating System:** Linux, macOS, or Windows
- **Database:** PostgreSQL 12+

### Required Modules
- `base` (Odoo core)
- `web_studio` (Odoo Studio)

### Permissions
- System Administrator access in Odoo
- Write access to the target module directories
- File system permissions to create/modify files

## Installation Methods

### Method 1: Standard Installation (Recommended)

1. **Copy the module to your addons directory:**
   ```bash
   cp -r studio_to_module /path/to/odoo/addons/
   ```

2. **Restart Odoo server:**
   ```bash
   sudo systemctl restart odoo
   # or
   ./odoo-bin -c /path/to/odoo.conf
   ```

3. **Update Apps List:**
   - Go to **Apps** in Odoo
   - Click **Update Apps List**
   - Search for "Studio to Module Converter"

4. **Install the module:**
   - Click **Install** button
   - Wait for installation to complete

### Method 2: Command Line Installation

```bash
# Install directly via command line
odoo -d your_database -i studio_to_module --stop-after-init

# Or upgrade if already installed
odoo -d your_database -u studio_to_module --stop-after-init
```

### Method 3: Development Installation

For development with auto-reload:

```bash
# Start Odoo in development mode
odoo -d your_database -i studio_to_module --dev=all
```

## Configuration

### Post-Installation Setup

1. **Verify Installation:**
   - Go to **Studio to Module** menu (should appear in main menu)
   - Check that **Studio Views** and **Convert to Module** submenus are visible

2. **Check Permissions:**
   - Ensure you're logged in as System Administrator
   - Verify access to Technical Settings

3. **Configure Cron Job (Optional):**
   - Go to **Settings > Technical > Automation > Scheduled Actions**
   - Find "Studio Views: Automatic Cleanup"
   - Enable if you want automatic daily cleanup
   - Adjust schedule as needed

### Module Settings

The module works out of the box with default settings:
- **Views Folder:** `views/` (can be changed per conversion)
- **Auto Cleanup:** Enabled by default
- **Cron Job:** Disabled by default

## Verification

### Test Installation

Run the test suite to verify everything works:

```bash
# Run all tests
./addons/studio_to_module/scripts/test_module.sh your_database

# Or manually
odoo -d your_database -i studio_to_module --test-enable --stop-after-init --log-level=test
```

### Manual Verification

1. **Create a test Studio view:**
   - Open Studio
   - Customize any form (e.g., Partners)
   - Add a field or modify layout
   - Save changes

2. **Check Studio Views Manager:**
   - Go to **Studio to Module > Studio Views**
   - Your test view should appear in the list
   - It should be marked as "Not Converted"

3. **Test Conversion:**
   - Select your test view
   - Click **Action > Convert Studio Views**
   - Choose a test module
   - Click **Convert Views**
   - Check that files were created

4. **Verify Cleanup:**
   - Upgrade the test module
   - Check that Studio view was deleted
   - Verify custom view still works

## Troubleshooting Installation

### Issue: Module Not Found

**Symptoms:** Module doesn't appear in Apps list

**Solutions:**
1. Check module is in addons path:
   ```bash
   ls /path/to/odoo/addons/studio_to_module
   ```

2. Verify addons path in config:
   ```ini
   [options]
   addons_path = /path/to/odoo/addons,/path/to/custom/addons
   ```

3. Update apps list in Odoo UI

4. Check Odoo logs for errors:
   ```bash
   tail -f /var/log/odoo/odoo.log
   ```

### Issue: Installation Fails

**Symptoms:** Error during installation

**Solutions:**
1. Check dependencies are installed:
   ```python
   # In Odoo shell
   env['ir.module.module'].search([('name', '=', 'web_studio')])
   ```

2. Install web_studio if missing:
   ```bash
   odoo -d your_database -i web_studio
   ```

3. Check for syntax errors:
   ```bash
   python -m py_compile addons/studio_to_module/**/*.py
   ```

4. Validate XML files:
   ```bash
   xmllint --noout addons/studio_to_module/**/*.xml
   ```

### Issue: Permission Denied

**Symptoms:** Cannot create files in target modules

**Solutions:**
1. Check file permissions:
   ```bash
   ls -la /path/to/odoo/addons/target_module
   ```

2. Fix ownership:
   ```bash
   sudo chown -R odoo:odoo /path/to/odoo/addons/target_module
   ```

3. Fix permissions:
   ```bash
   chmod -R 755 /path/to/odoo/addons/target_module
   ```

### Issue: Menu Not Visible

**Symptoms:** Studio to Module menu doesn't appear

**Solutions:**
1. Verify you're logged in as System Administrator

2. Clear browser cache and reload

3. Check user groups:
   ```python
   # In Odoo shell
   user = env.user
   print(user.has_group('base.group_system'))
   ```

4. Reinstall module:
   ```bash
   odoo -d your_database -u studio_to_module --stop-after-init
   ```

## Uninstallation

### Standard Uninstallation

1. **Via UI:**
   - Go to **Apps**
   - Find "Studio to Module Converter"
   - Click **Uninstall**
   - Confirm uninstallation

2. **Via Command Line:**
   ```bash
   # This will remove the module but keep data
   odoo -d your_database --uninstall studio_to_module
   ```

### Complete Removal

To completely remove all traces:

```bash
# 1. Uninstall module
odoo -d your_database --uninstall studio_to_module

# 2. Remove module files
rm -rf /path/to/odoo/addons/studio_to_module

# 3. Clean database (optional)
psql your_database -c "DELETE FROM ir_module_module WHERE name='studio_to_module';"
```

## Upgrade

### Upgrading to New Version

1. **Backup first:**
   ```bash
   # Backup database
   pg_dump your_database > backup.sql
   
   # Backup module
   cp -r /path/to/odoo/addons/studio_to_module /path/to/backup/
   ```

2. **Replace module files:**
   ```bash
   rm -rf /path/to/odoo/addons/studio_to_module
   cp -r new_studio_to_module /path/to/odoo/addons/
   ```

3. **Upgrade module:**
   ```bash
   odoo -d your_database -u studio_to_module --stop-after-init
   ```

4. **Verify upgrade:**
   - Check version in Apps
   - Run test suite
   - Test conversion workflow

## Docker Installation

### Using Docker

```dockerfile
# Dockerfile
FROM odoo:18.0

# Copy module
COPY studio_to_module /mnt/extra-addons/studio_to_module

# Install dependencies (if any)
RUN pip3 install -r /mnt/extra-addons/studio_to_module/requirements.txt
```

### Docker Compose

```yaml
# docker-compose.yml
version: '3.8'
services:
  odoo:
    image: odoo:18.0
    volumes:
      - ./studio_to_module:/mnt/extra-addons/studio_to_module
    environment:
      - HOST=db
      - USER=odoo
      - PASSWORD=odoo
    depends_on:
      - db
  
  db:
    image: postgres:15
    environment:
      - POSTGRES_DB=postgres
      - POSTGRES_USER=odoo
      - POSTGRES_PASSWORD=odoo
```

Start with:
```bash
docker-compose up -d
docker-compose exec odoo odoo -d odoo -i studio_to_module --stop-after-init
```

## Production Deployment

### Pre-Deployment Checklist

- [ ] Tested in development environment
- [ ] Tested in staging environment
- [ ] Database backup created
- [ ] Module files backed up
- [ ] Rollback plan prepared
- [ ] Team notified of deployment
- [ ] Maintenance window scheduled

### Deployment Steps

1. **Enable maintenance mode:**
   ```bash
   # In Odoo config
   [options]
   maintenance_mode = True
   ```

2. **Backup database:**
   ```bash
   pg_dump production_db > backup_$(date +%Y%m%d_%H%M%S).sql
   ```

3. **Deploy module:**
   ```bash
   # Copy module
   cp -r studio_to_module /path/to/production/addons/
   
   # Set permissions
   chown -R odoo:odoo /path/to/production/addons/studio_to_module
   ```

4. **Install/Upgrade:**
   ```bash
   odoo -d production_db -i studio_to_module --stop-after-init
   ```

5. **Verify deployment:**
   - Test conversion workflow
   - Check logs for errors
   - Verify permissions

6. **Disable maintenance mode:**
   ```bash
   [options]
   maintenance_mode = False
   ```

## Support

### Getting Help

1. **Documentation:**
   - [README.md](README.md) - Main documentation
   - [QUICKSTART.md](QUICKSTART.md) - Quick start guide
   - [ADVANCED_USAGE.md](doc/ADVANCED_USAGE.md) - Advanced features

2. **Logs:**
   - Check Odoo logs: `/var/log/odoo/odoo.log`
   - Check module logs: Settings > Technical > Logging

3. **Community:**
   - Contact Durpro development team
   - Check Odoo documentation

### Reporting Issues

When reporting issues, include:
- Odoo version
- Module version
- Error messages
- Steps to reproduce
- Relevant log excerpts

## Next Steps

After successful installation:

1. Read the [QUICKSTART.md](QUICKSTART.md) guide
2. Test with a simple Studio view
3. Review [ADVANCED_USAGE.md](doc/ADVANCED_USAGE.md) for advanced features
4. Integrate into your development workflow

---

**Installation complete!** 🎉

You're ready to start converting Studio views to module code.
