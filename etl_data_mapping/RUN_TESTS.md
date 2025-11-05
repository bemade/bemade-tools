# Running ETL Data Mapping Tests

## Run all ETL tests

```bash
# From the Odoo root directory
./odoo-bin -c odoo.conf -d test_db --test-tags=etl --stop-after-init
```

## Run specific test class

```bash
# Test extraction only
./odoo-bin -c odoo.conf -d test_db --test-tags=etl -i etl_data_mapping --stop-after-init --test-file=addons/etl_data_mapping/tests/test_etl_extraction.py

# Test transforms only
./odoo-bin -c odoo.conf -d test_db --test-tags=etl -i etl_data_mapping --stop-after-init --test-file=addons/etl_data_mapping/tests/test_etl_transform.py
```

## Test Database Setup

The tests use a separate schema `test_external` in the same database to simulate an external database. This avoids needing a separate database connection for testing.

### Test Data Structure

- **test_external.product_categories**: 3 categories
- **test_external.products**: 5 products (1 inactive)
- **test_external.partners**: 3 partners (2 customers, 1 vendor)
- **test_external.orders**: 3 orders (1 canceled)
- **test_external.order_lines**: 4 order lines

## Current Test Status

### ✅ Implemented Tests

1. **test_etl_extraction.py** - 10 tests for SQL extraction
   - Simple table extraction
   - Filtered extraction (WHERE)
   - Joined extraction (JOIN)
   - One-to-many relationships
   - Aggregations
   - Incremental loads
   - NULL handling
   - Field aliasing
   - Computed fields

2. **test_etl_transform.py** - 9 tests for transformations
   - Boolean transforms (Y/N → True/False)
   - String cleaning
   - Numeric transforms
   - Conditional transforms
   - Multi-field transforms
   - Default values
   - Date/timezone handling
   - Currency normalization
   - Regex extraction

### 🚧 To Be Implemented

3. **test_etl_field.py** - ETLField definition tests (placeholders)
4. **test_etl_mapping.py** - ETLMapping model tests (not created yet)
5. **test_etl_load.py** - Data loading tests (not created yet)

## Next Steps

1. Run the extraction and transform tests (should pass)
2. Implement ETLField custom field type
3. Implement ETLMapping abstract model
4. Add tests for the mapping and loading phases
