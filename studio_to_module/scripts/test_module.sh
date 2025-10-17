#!/bin/bash

# Test script for Studio to Module Converter
# Usage: ./test_module.sh [database_name]

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Get database name from argument or use default
DB_NAME=${1:-"durpro18"}

echo -e "${GREEN}Testing Studio to Module Converter${NC}"
echo "Database: $DB_NAME"
echo "-----------------------------------"

# Check if Odoo is available
if ! command -v odoo &> /dev/null; then
    echo -e "${RED}Error: Odoo command not found${NC}"
    echo "Please ensure Odoo is in your PATH"
    exit 1
fi

# Run tests
echo -e "${YELLOW}Running unit tests...${NC}"
odoo -d "$DB_NAME" -i studio_to_module --test-enable --stop-after-init --log-level=test

if [ $? -eq 0 ]; then
    echo -e "${GREEN}✓ All tests passed!${NC}"
else
    echo -e "${RED}✗ Tests failed${NC}"
    exit 1
fi

echo ""
echo -e "${GREEN}Testing complete!${NC}"
echo ""
echo "Next steps:"
echo "1. Start Odoo: odoo -d $DB_NAME"
echo "2. Go to Apps and install 'Studio to Module Converter'"
echo "3. Navigate to Studio to Module menu"
echo "4. Test the conversion workflow"
