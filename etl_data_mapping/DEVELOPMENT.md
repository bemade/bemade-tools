# ETL Data Mapping Framework - Development Plan

## Development Approach

**Test-Driven Development (TDD)**: Write ONE test → Make it pass → Refactor → Repeat

## Current Status

### Phase 1: Basic Extraction
- [x] **Test**: `test_simple_extraction_from_mapping`
- [x] **Test**: `test_extraction_with_filter` (SQL objects for WHERE)
- [x] **What works**: Extract data from external database using declarative ETLField mappings
- [x] **What works**: WHERE clause filtering using SQL objects (safe, composable)
- [x] **Models**: `etl.external.database`, `etl.mapping`, `ETLField`

### Phase 2: Data Transformation
- [x] **Methods**: `transform()`, `extract_and_transform()` (code exists)
- [ ] **Tests**: Need to verify lambda transforms work
- [ ] **Next test**: Verify `is_active: lambda val: val == 'Y'` transforms to boolean

### Phase 3: Data Loading
- [x] **Methods**: `load()`, `run_migration()` (code exists)
- [ ] **Tests**: Need to verify records are created
- [ ] **Next test**: Verify records are created in target model

### Phase 4: Advanced Extraction
- [ ] WHERE filtering, ORDER BY, LIMIT/OFFSET
- [ ] Multi-table JOINs
- [ ] Custom SQL queries

### Phase 5: Advanced Transformation
- [ ] Named transform functions
- [ ] Relation resolution (FK lookups)
- [ ] Computed fields

### Phase 6: Advanced Loading
- [ ] Configurable duplicate detection
- [ ] One2many relationships
- [ ] Post-processing hooks

### Phase 7: Production Patterns (from SAP B1 Importer)
- [ ] Post-import hooks (link parents, set accounts, update ranks)
- [ ] Bulk SQL operations (UPDATE statements after load)
- [ ] Dictionary caching/memoization for lookups
- [ ] Multi-pass orchestration (import A → import B → link them)
- [ ] Conditional record handling (text lines vs product lines)
- [ ] Computed field expressions (`linenum * 100 + lineseq`)
- [ ] Fallback chains (try cardcode, then upper, then lower)
- [ ] Multiprocessing (tested and production-ready)

## Architecture

**ETL Pipeline**: EXTRACT (SQL query) → TRANSFORM (apply functions) → LOAD (create records)

**Key Files**:
- `models/etl_external_database.py` - DB connections
- `models/etl_field.py` - Custom field type
- `models/etl_mapping.py` - ETL logic
- `test_etl_data_mapping/` - Test module with sample data

## Running Tests

```bash
python3 odoo-dev test -m test_etl_data_mapping --test-tags=etl
```

**Test Data**: `test_external` database with 5 products, 3 partners, 3 orders

## Next Test to Write (Choose One)

**A. Transformation**: Verify lambda transforms work (e.g., `'Y'` → `True`)  
**B. Loading**: Verify records are created in Odoo  
**C. Filtering**: Verify WHERE clause filtering works

## TDD Workflow

1. Write ONE test
2. Run it (RED)
3. Make it pass (GREEN)
4. Refactor
5. Update this doc
6. Repeat

## Design Principles

- Declarative over imperative
- Convention over configuration
- Test-driven
- Self-documenting

## Gaps vs Existing SAP B1 Importer

The existing `sap_b1_to_odoo` module uses imperative patterns that our framework doesn't yet support:

**Post-Processing**:
- `_link_children_parents()` - Links parent-child after import
- `_set_payable_receivable_accounts()` - Sets accounts based on currency
- `_set_partner_ranks()` - Updates customer/supplier ranks

**Bulk Operations**:
```python
self.env.cr.execute("""
    UPDATE res_partner 
    SET customer_rank = 1 
    WHERE sap_partner_type IN ('C', 'L')
""")
```

**Caching**:
```python
_products_dict = None  # Class-level cache
products_dict = {product.sap_item_code: product for product in products}
```

**Complex Line Handling**:
- Text lines vs product lines from different tables
- Sequence calculation: `linenum * 100 + lineseq`
- Conditional field mapping based on row type

**Mixin Pattern**:
```python
class SapSalePurchaseImporterMixin(models.AbstractModel):
    _sap_header_table = None  # Configured by subclass
```

**Fallback Logic**:
```python
partners_dict.get(cardcode) 
or partners_dict.get(cardcode.upper()) 
or partners_dict.get(cardcode.lower())
```

These patterns should be supported as the framework matures (Phase 7).
