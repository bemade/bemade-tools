# SAP B1 to Odoo ETL Framework - Design Document

**Version:** 2.0  
**Date:** November 7, 2025  
**Status:** Major Progress - Core Models Migrated

---

## Executive Summary

This document outlines the design and implementation plan for refactoring the SAP B1 to Odoo migration module into a declarative, self-optimizing ETL (Extract, Transform, Load) framework. The goal is to improve code maintainability, testability, and expressiveness while maintaining backward compatibility and performance.

---

## Design Principles

### 1. **Declarative Over Imperative**
- Pipeline structure should be visible at the class level through decorators
- Dependencies and execution order should be explicit
- Reduce boilerplate code

### 2. **Self-Optimizing Execution**
- Framework automatically decides single-process vs. multiprocessing based on data volume
- No manual optimization decisions required by developers
- Configurable thresholds per model

### 3. **Separation of Concerns**
- Clear separation between Extract, Transform, and Load phases
- Each phase can be tested independently
- Pure functions where possible (especially Transform)

### 4. **Memory Efficiency**
- No large data structures passed between steps
- Context object contains only cursors and environment references
- Data flows through pipeline without accumulation

### 5. **Fail Fast**
- Errors are logged with full traceback and re-raised
- No silent failures or recovery attempts
- Clear error messages for debugging

### 6. **Backward Compatible**
- Existing functionality must continue to work
- Migration path should be incremental
- Old and new code can coexist during transition

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    ETL Pipeline Registry                     │
│  (Stores all decorated pipelines and their configurations)  │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                   Pipeline Orchestrator                      │
│  • Resolves model dependencies (topological sort)            │
│  • Executes pipelines in correct order                       │
│  • Manages database connections and commits                  │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                      ETL Executor                            │
│  1. Execute Extract methods                                  │
│  2. Count extracted records                                  │
│  3. Decide: Single-process or Multiprocessing?              │
│  4. Execute Transform + Load accordingly                     │
└─────────────────────────────────────────────────────────────┘
                              │
                    ┌─────────┴─────────┐
                    ▼                   ▼
         ┌──────────────────┐  ┌──────────────────┐
         │ Single Process   │  │ Multiprocessing  │
         │ • Sequential     │  │ • Chunk data     │
         │ • Simple         │  │ • Fork workers   │
         │ • Fast for small │  │ • Fast for large │
         └──────────────────┘  └──────────────────┘
```

---

## Core Components

### 1. **ETLContext**
Lightweight context object passed to all ETL methods.

```python
@dataclass
class ETLContext:
    cr: Any          # SAP database cursor
    env: Any         # Odoo environment
    # No data storage - prevents memory overload
```

### 2. **MultiprocessingConfig**
Configuration for dynamic multiprocessing decisions.

```python
@dataclass
class MultiprocessingConfig:
    enabled: bool = True              # Allow multiprocessing
    threshold: int = 1000             # Min records to trigger MP
    chunk_size: int = 500             # Records per chunk
    max_workers: Optional[int] = None # None = cpu_count - 1
    
    def should_use_multiprocessing(self, record_count: int) -> bool:
        return self.enabled and record_count >= self.threshold
```

### 3. **ETLPipeline**
Declarative pipeline definition.

```python
@dataclass
class ETLPipeline:
    target_model: str                      # e.g., 'product.product'
    sap_source: str                        # e.g., 'oitm'
    depends_on: List[str]                  # Model dependencies
    multiprocessing: MultiprocessingConfig
    
    # Registered methods (populated by decorators)
    extract_methods: List[Callable]
    transform_methods: List[Callable]
    load_methods: List[Callable]
```

### 4. **ETL Decorators**
Method registration decorators.

```python
class ETL:
    @classmethod
    def pipeline(cls, target_model, sap_source, depends_on=None, 
                 multiprocessing_threshold=1000, ...):
        """Class decorator to define a pipeline"""
    
    @classmethod
    def extract(cls, source_table: str):
        """Method decorator for extraction"""
    
    @classmethod
    def transform(cls):
        """Method decorator for transformation"""
    
    @classmethod
    def load(cls):
        """Method decorator for loading"""
```

### 5. **ETLExecutor**
Executes a single pipeline with dynamic optimization.

```python
class ETLExecutor:
    def execute(self):
        # 1. Extract
        extracted_data = self._execute_extract()
        
        # 2. Count records
        record_count = self._get_record_count(extracted_data)
        
        # 3. Decide execution strategy
        use_mp = self.pipeline.multiprocessing.should_use_multiprocessing(
            record_count
        )
        
        # 4. Execute Transform + Load
        if use_mp:
            self._execute_parallel(extracted_data)
        else:
            self._execute_sequential(extracted_data)
```

### 6. **PipelineOrchestrator**
Manages execution of multiple pipelines with dependency resolution.

```python
class PipelineOrchestrator:
    def execute_all(self, cr):
        # 1. Resolve dependencies (topological sort)
        execution_order = self._resolve_dependencies()
        
        # 2. Execute each pipeline in order
        for model_name in execution_order:
            pipeline = ETL._pipelines[model_name]
            executor = ETLExecutor(pipeline, ctx)
            executor.execute()
            self.env.cr.commit()
```

---

## Key Learnings & Patterns

### Multiprocessing Best Practices
1. **Cache Primitive Types Only**: Store only IDs (integers/strings) in class-level caches, never Odoo recordsets. Recordsets cannot be pickled across processes.
2. **Pre-compute in Extract**: Build all lookup dictionaries in the extract phase (main process) before multiprocessing begins.
3. **Conservative Settings**: Start with lower worker counts (4-8) and larger chunk sizes (100-500) to avoid database contention.
4. **Error Propagation**: Remove try/except blocks in worker processes to ensure exceptions bubble up and fail fast.

### Address Idempotence Pattern
For addresses (CRD1), use `sap_parent_card` + `sap_address_linenum` as the unique key:
- Added `sap_address_linenum` field to store SAP's line number
- This allows multiple addresses of the same type per company
- Matches SAP's data model where `cardcode` + `linenum` is the primary key

### Name Validation Pattern
For records with potentially empty names:
- **Companies**: Skip records with empty names (log warning)
- **Addresses**: Use fallback name like "Delivery Address" or "Invoice Address"
- **Contacts**: Use email or phone as fallback name

### Split Pipeline Pattern
Complex models like `res.partner` benefit from splitting into multiple pipelines:
1. **Companies** (OCRD) - parent records
2. **Addresses** (CRD1) - child records with type
3. **Contacts** (OCPR) - child records
4. **Post-process** - link children to parents (no extract/transform, just load)

This allows:
- Independent idempotence checks
- Clearer separation of concerns
- Easier debugging
- Better multiprocessing control per pipeline

### Data Quality Handling
- **Negative quantities**: Filter out BOM lines with qty ≤ 0 (violates Odoo constraints)
- **Empty names**: Validate and provide fallbacks
- **Missing foreign keys**: Log warnings and skip records gracefully

---

## Model Dependencies

Based on analysis of `sap_database.py::_import_all()`:

```
1. res.users                    (no dependencies)
2. product.pricelist            (no dependencies)
3. res.partner                  (no dependencies)
4. product.category             (no dependencies)
5. product.product              (depends on: product.category)
6. mrp.bom                      (depends on: product.product)
7. delivery.carrier.account     (no dependencies)
8. stock.quant                  (depends on: product.product)
9. product.pricelist.item       (depends on: product.product, product.pricelist)
10. account.payment.term        (no dependencies)
11. sale.order                  (depends on: res.partner, product.product, res.users)
12. purchase.order              (depends on: res.partner, product.product, res.users)
13. account.move                (depends on: sale.order, purchase.order)
14. ir.attachment               (depends on: all models with attachments)
```

---

## Multiprocessing Thresholds

Recommended thresholds based on existing code analysis:

| Model | Threshold | Chunk Size | Rationale |
|-------|-----------|------------|-----------|
| `product.product` | 1000 | 500 | Large datasets, CPU-intensive transforms |
| `res.partner` | 500 | 500 | Medium datasets, I/O intensive |
| `sale.order` | 500 | 500 | Medium datasets, complex transforms |
| `purchase.order` | 500 | 500 | Medium datasets, complex transforms |
| `ir.attachment` | 500 | 500 | File I/O intensive |
| `account.move` | 500 | 500 | Medium datasets |
| `mrp.bom` | 1000 | 500 | Large datasets |
| `account.payment.term` | ∞ (disabled) | N/A | Always small (<50 records) |
| `product.pricelist` | ∞ (disabled) | N/A | Always small |
| `delivery.carrier.account` | ∞ (disabled) | N/A | Always small |

---

## Implementation Plan

### Phase 1: Foundation ✅ COMPLETED
- [x] Create `etl_framework.py` with core classes
- [x] Implement `ETLContext`, `MultiprocessingConfig`, `ETLPipeline`
- [x] Implement `ETL` decorator class
- [ ] Write unit tests for core components
- [x] Document API with examples (in code)

### Phase 2: Executor ✅ COMPLETED
- [x] Implement `ETLExecutor` with single-process execution
- [x] Implement multiprocessing execution
- [x] Add record counting logic
- [x] Add dynamic strategy selection
- [x] Test with real data (res.users, account.payment.term)

### Phase 3: Orchestrator ✅ COMPLETED
- [x] Implement `PipelineOrchestrator`
- [x] Add dependency resolution (topological sort)
- [x] Add execution order validation
- [x] Test with multiple pipelines (res.users + account.payment.term working!)

### Phase 4: Migration - Simple Models ✅ COMPLETED
Migrate models with no multiprocessing:
- [x] `res.users` ✅
- [x] `account.payment.term` ✅
- [x] `product.pricelist` (init_pricelists) ✅
- [x] `delivery.carrier.account` ✅

### Phase 5: Migration - Complex Models ✅ COMPLETED
Migrate models with multiprocessing:
- [x] `product.product` ✅ (split into 2 pipelines: categories, products with multiprocessing)
- [x] `res.partner` ✅ (split into 4 pipelines: companies, addresses, contacts, post-process)
- [x] `mrp.bom` ✅ (single pipeline with BOM headers and lines)
- [ ] `sale.order` (complex, in progress)
- [ ] `purchase.order` (complex, in progress)

### Phase 6: Migration - Remaining Models (IN PROGRESS)
- [x] `product.category` ✅ (part of product pipeline)
- [ ] `product.pricelist.item` (SAP price lists - OAT1/OOAT tables)
- [ ] `stock.quant` (inventory quantities)
- [ ] `stock.valuation.layer` (inventory valuations)
- [ ] `account.move` (invoices)
- [ ] `ir.attachment` (file attachments)

### Phase 7: Integration & Testing
- [x] Update `sap_database.py` to use orchestrator
- [x] End-to-end testing with real SAP data
- [ ] Performance benchmarking
- [ ] Documentation updates

### Phase 8: Cleanup
- [ ] Remove old code patterns
- [ ] Final code review
- [ ] Update README with new architecture
- [ ] Create migration guide for future models

---

## Usage Examples

### Simple Model (No Multiprocessing)

```python
@ETL.pipeline(
    target_model='account.payment.term',
    sap_source='octg',
    allow_multiprocessing=False,  # Always single-process
)
class SapPaymentTermImporter(models.AbstractModel):
    # _name auto-generated as 'account.payment.term.importer'
    
    @ETL.extract('octg')
    def extract_payment_terms(self, ctx: ETLContext) -> List[Dict]:
        ctx.cr.execute("SELECT * FROM octg")
        return ctx.cr.dictfetchall()
    
    @ETL.transform()
    def transform_payment_terms(self, ctx: ETLContext, sap_terms: List[Dict]) -> List[Dict]:
        return [
            {
                "name": term["pymntgroup"],
                "sap_groupnum": term["groupnum"],
                "line_ids": [Command.create({
                    "value_amount": 100.0,
                    "value": "percent",
                    "nb_days": term["extradays"],
                })],
            }
            for term in sap_terms
        ]
    
    @ETL.load()
    def load_payment_terms(self, ctx: ETLContext, term_vals: List[Dict]) -> None:
        ctx.env["account.payment.term"].create(term_vals)
```

### Complex Model (With Multiprocessing)

```python
@ETL.pipeline(
    target_model='product.product',
    sap_source='oitm',
    depends_on=['product.category'],
    multiprocessing_threshold=1000,
    chunk_size=500,
)
class SapProductImporter(models.AbstractModel):
    _name = 'sap.product.importer'
    
    @ETL.extract('oitm')
    def extract_products(self, ctx: ETLContext) -> List[Dict]:
        # Get existing products
        existing = tuple(
            ctx.env["product.product"]
            .search_read([("sap_item_code", "!=", False)], ["sap_item_code"])
        )
        
        # Query SAP
        sql = "SELECT * FROM oitm"
        if existing:
            sql += " WHERE itemcode NOT IN %s"
            ctx.cr.execute(SQL(sql, existing))
        else:
            ctx.cr.execute(sql)
        
        return ctx.cr.dictfetchall()
    
    @ETL.extract('oitb')
    def extract_categories(self, ctx: ETLContext) -> List[Dict]:
        ctx.cr.execute("SELECT * FROM oitb")
        return ctx.cr.dictfetchall()
    
    @ETL.transform()
    def transform_products(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        sap_products = extracted['extract_products']
        sap_categories = extracted['extract_categories']
        
        # Build category map
        categories = ctx.env["product.category"].search([
            ("sap_itms_grp_cod", "!=", False)
        ])
        categories_map = {c.sap_itms_grp_cod: c.id for c in categories}
        
        # Transform products
        return [
            {
                "sap_item_code": p["itemcode"],
                "default_code": p["itemname"],
                "name": p["frgnname"] or "N/A",
                "categ_id": categories_map.get(p["itmsgrpcod"]),
                "active": p["validfor"] == "Y",
            }
            for p in sap_products
        ]
    
    @ETL.load()
    def load_products(self, ctx: ETLContext, product_vals: List[Dict]) -> None:
        ctx.env["product.product"].create(product_vals)
```

---

## Testing Strategy

### Unit Tests
- Test each decorator independently
- Test `MultiprocessingConfig.should_use_multiprocessing()`
- Test record counting logic
- Test chunk creation

### Integration Tests
- Test single-process execution path
- Test multiprocessing execution path
- Test threshold boundary conditions (999 vs 1000 records)
- Test dependency resolution

### End-to-End Tests
- Test full pipeline execution with mock SAP data
- Test orchestrator with multiple pipelines
- Test error handling and rollback

### Performance Tests
- Benchmark single-process vs multiprocessing
- Measure memory usage
- Compare with old implementation

---

## Migration Checklist

For each model being migrated:

- [ ] Identify all import methods
- [ ] Separate into Extract, Transform, Load
- [ ] Add `@ETL.pipeline` decorator with appropriate config
- [ ] Add `@ETL.extract` decorators
- [ ] Add `@ETL.transform` decorators
- [ ] Add `@ETL.load` decorators
- [ ] Update method signatures to use `ETLContext`
- [ ] Remove old orchestration code
- [ ] Add/update tests
- [ ] Verify functionality with real data
- [ ] Update documentation

---

## Success Criteria

### Code Quality
- [ ] All models use declarative ETL pattern
- [ ] No code duplication in orchestration logic
- [ ] Clear separation of concerns (E/T/L)
- [ ] Type hints on all methods
- [ ] Comprehensive docstrings

### Performance
- [ ] No performance regression vs. old implementation
- [ ] Multiprocessing triggers correctly based on data volume
- [ ] Memory usage remains stable

### Maintainability
- [ ] New models can be added with <50 lines of code
- [ ] Dependencies are explicit and validated
- [ ] Error messages are clear and actionable

### Testing
- [ ] 80%+ code coverage
- [ ] All critical paths tested
- [ ] Integration tests pass
- [ ] End-to-end tests with real SAP data pass

---

## Risk Mitigation

### Risk: Breaking existing functionality
**Mitigation:** 
- Incremental migration, one model at a time
- Comprehensive testing before each commit
- Keep old code until new code is proven

### Risk: Performance degradation
**Mitigation:**
- Benchmark before and after
- Profile memory usage
- Adjust thresholds based on real data

### Risk: Complexity increase
**Mitigation:**
- Clear documentation
- Simple examples
- Code reviews

### Risk: Multiprocessing bugs
**Mitigation:**
- Extensive testing with different data volumes
- Fallback to single-process if errors occur
- Clear logging of execution strategy

---

## Future Enhancements

### Phase 9+ (Post-Launch)
- [ ] Add retry logic for transient failures
- [ ] Add progress bars for long-running imports
- [ ] Add dry-run mode
- [ ] Add data validation framework
- [ ] Add incremental import support
- [ ] Add rollback/undo functionality
- [ ] Add import scheduling
- [ ] Add web UI for monitoring

---

## Appendix A: File Structure

```
sap_b1_to_odoo/
├── __init__.py
├── __manifest__.py
├── ETL_FRAMEWORK_DESIGN.md          # This document
├── etl_framework.py                  # NEW: Core framework
├── models/
│   ├── __init__.py
│   ├── sap_database.py              # Updated: Uses orchestrator
│   ├── product_product.py           # Migrated: Uses @ETL decorators
│   ├── res_partner.py               # Migrated: Uses @ETL decorators
│   ├── sale_order.py                # Migrated: Uses @ETL decorators
│   ├── purchase_order.py            # Migrated: Uses @ETL decorators
│   ├── account_move.py              # Migrated: Uses @ETL decorators
│   ├── ir_attachment.py             # Migrated: Uses @ETL decorators
│   └── ...
├── tests/
│   ├── test_etl_framework.py        # NEW: Framework tests
│   ├── test_product_import.py       # Updated: Test new pattern
│   └── ...
└── tools.py                          # Existing utilities
```

---

## Appendix B: Glossary

- **ETL**: Extract, Transform, Load - a data integration pattern
- **Pipeline**: A complete ETL workflow for a single model
- **Orchestrator**: Manages execution of multiple pipelines
- **Executor**: Executes a single pipeline
- **Context**: Shared state passed to ETL methods
- **Threshold**: Minimum record count to trigger multiprocessing
- **Chunk**: Subset of data processed by a single worker

---

**Document Status:** Living document, updated as implementation progresses.
