#!/usr/bin/env python3
"""
Setup script for migration testing.

This script helps prepare the test environment for migration testing.
"""

import os
import sys
import subprocess
import logging

logging.basicConfig(level=logging.INFO)
_logger = logging.getLogger(__name__)

def run_command(cmd, cwd=None):
    """Run a shell command and return the result."""
    _logger.info(f"Running: {cmd}")
    result = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        _logger.error(f"Command failed: {result.stderr}")
        return False
    _logger.info(f"Success: {result.stdout.strip()}")
    return True

def setup_test_database():
    """Set up the migration test database."""
    _logger.info("Setting up migration test database...")
    
    # Check if database exists
    check_cmd = "psql -lqt | cut -d \\| -f 1 | grep -qw migration_test"
    result = subprocess.run(check_cmd, shell=True, capture_output=True)
    
    if result.returncode == 0:
        _logger.info("Database 'migration_test' already exists")
        return True
    
    # Create database
    if not run_command("createdb migration_test"):
        return False
    
    _logger.info("Migration test database created successfully")
    return True

def check_environment():
    """Check that required environment variables are set."""
    required_vars = [
        'ODOO16_HOST',
        'ODOO16_DBNAME', 
        'ODOO16_USER',
        'ODOO16_PASSWORD',
        'ODOO16_PORT'
    ]
    
    missing_vars = []
    for var in required_vars:
        if not os.environ.get(var):
            missing_vars.append(var)
    
    if missing_vars:
        _logger.warning(f"Missing environment variables: {missing_vars}")
        _logger.info("These will be set by the launch.json configuration")
    else:
        _logger.info("All environment variables are set")
    
    return True

def main():
    """Main setup function."""
    _logger.info("=" * 60)
    _logger.info("MIGRATION TEST SETUP")
    _logger.info("=" * 60)
    
    # Check environment
    if not check_environment():
        sys.exit(1)
    
    # Setup test database
    if not setup_test_database():
        sys.exit(1)
    
    _logger.info("=" * 60)
    _logger.info("SETUP COMPLETE!")
    _logger.info("=" * 60)
    _logger.info("Next steps:")
    _logger.info("1. Use 'Test Migration Integration' launch config to run integration tests")
    _logger.info("2. Use 'Run Migration Server' launch config to start server with migration module")
    _logger.info("3. Navigate to Settings > Technical > Database Structure > Odoo 16 Database")
    _logger.info("4. Create a new migration record and test the migration manually")

if __name__ == '__main__':
    main()
