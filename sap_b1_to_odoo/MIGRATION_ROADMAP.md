# SAP B1 to Odoo ETL Migration Roadmap

**Last Updated:** November 7, 2025  
**Status:** Phase 6 - Core Models Complete

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

## Implementation Progress

### Phase 1: Foundation ✅ COMPLETED
- [x] Create `etl_framework.py` with core classes
- [x] Implement `ETLContext`, `MultiprocessingConfig`, `ETLPipeline`
- [x] Implement `ETL` decorator class
- [x] Document API with examples (in code)

### Phase 2: Executor ✅ COMPLETED
- [x] Implement `ETLExecutor` with single-process execution
- [x] Implement multiprocessing execution
- [x] Add record counting logic
- [x] Add dynamic strategy selection
- [x] Test with real data (res.users, account.payment.term)
- [x] Add fork warning suppression

### Phase 3: Orchestrator ✅ COMPLETED
- [x] Implement `PipelineOrchestrator`
- [x] Add dependency resolution (topological sort)
- [x] Add execution order validation
- [x] Test with multiple pipelines

### Phase 4: Migration - Simple Models ✅ COMPLETED
Migrate models with no multiprocessing:
- [x] `res.users` ✅
- [x] `account.payment.term` ✅
- [x] `product.pricelist` (init_pricelists) ✅
- [x] `delivery.carrier.account` ✅

### Phase 5: Migration - Complex Models ✅ COMPLETED
Migrate models with multiprocessing:
- [x] `product.product` ✅ (split into 2 pipelines: categories, products)
- [x] `res.partner` ✅ (split into 4 pipelines: companies, addresses, contacts, post-process)
- [x] `mrp.bom` ✅ (single pipeline with BOM headers and lines)
- [x] `purchase.requisition` ✅ (blanket orders)

### Phase 6: Migration - Order Models ✅ COMPLETED
- [x] `sale.order` ✅ (split into 4 pipelines: headers, product lines, text lines, post-processor)
- [x] `sale.quotation` ✅ (split into 3 pipelines: headers, product lines, text lines)
- [x] `purchase.order` ✅ (split into 4 pipelines: headers, product lines, text lines, post-processor)
- [x] Create shared mixin `sale.purchase.order.etl.mixin` for common logic ✅

### Phase 7: Migration - Remaining Models (IN PROGRESS)
- [x] `product.category` ✅ (part of product pipeline)
- [x] `product.pricelist.item` ✅ (SAP price lists - OAT1/OOAT tables)
- [ ] `stock.quant` (inventory quantities)
- [ ] `stock.valuation.layer` (inventory valuations)
- [ ] `account.move` (invoices)
- [ ] `ir.attachment` (file attachments)

### Phase 8: Integration & Testing ✅ COMPLETED
- [x] Update `sap_database.py` to use orchestrator
- [x] End-to-end testing with real SAP data
- [ ] Performance benchmarking
- [ ] Documentation updates

### Phase 9: Cleanup (PENDING)
- [ ] Remove old legacy code
- [ ] Final code review
- [ ] Update README with new architecture
- [ ] Create migration guide for future models

---

## Migration Checklist

For each model being migrated:

- [ ] Identify all import methods in legacy code
- [ ] Separate into Extract, Transform, Load phases
- [ ] Add `@ETL.pipeline` decorator with appropriate config
- [ ] Add `@ETL.extract` decorators
- [ ] Add `@ETL.transform` decorators
- [ ] Add `@ETL.load` decorators
- [ ] Update method signatures to use `ETLContext`
- [ ] Pre-compute lookups in extract phase (store only IDs in cache)
- [ ] Remove old orchestration code
- [ ] Add/update tests
- [ ] Verify functionality with real data
- [ ] Update documentation

---

## Key Learnings

### Split Pipeline Pattern
Complex models like `sale.order` and `purchase.order` benefit from splitting into multiple pipelines:
1. **Headers** - Create order records without lines
2. **Product Lines** - Create order lines for products
3. **Text Lines** - Create order lines for text/notes
4. **Post-Processor** - Confirm orders, set quantities, validate pickings

Benefits:
- Independent idempotence checks
- Clearer separation of concerns
- Easier debugging
- Better multiprocessing control per pipeline
- Avoids concurrent updates to same order

### Shared Mixin Pattern
For models with similar logic (sale/purchase orders), create a shared mixin:
- `sale.purchase.order.etl.mixin` contains common helper methods
- Both `SaleOrderHeaderImporter` and `PurchaseOrderHeaderImporter` inherit from it
- Reduces code duplication
- Ensures consistent behavior

### Field Name Differences
Watch out for field name differences between sale and purchase orders:
- Sale: `product_uom_qty`, Purchase: `product_qty`
- Sale: `qty_delivered`, Purchase: `qty_received`
- Sale: `state='sale'`, Purchase: `state='purchase'`
- Sale: has `discount`, Purchase: no `discount`
- Sale: has `pricelist_id`, Purchase: no `pricelist_id`

### Odoo Version Migration Issues
When migrating custom modules to Odoo 19:
- `group_id` field was removed from `stock.move` and `purchase.order.line`
- Use `sale_line_id` on moves to get sale order (via `sale_stock` module)
- Use `sale_order_id` on purchase lines (via `sale_purchase` module)
- Check for field existence with `hasattr()` when field depends on optional modules

---

## Success Criteria

### Code Quality
- [x] Core models use declarative ETL pattern
- [x] No code duplication in orchestration logic
- [x] Clear separation of concerns (E/T/L)
- [x] Type hints on all methods
- [x] Comprehensive docstrings

### Performance
- [x] Multiprocessing triggers correctly based on data volume
- [ ] No performance regression vs. old implementation (needs benchmarking)
- [x] Memory usage remains stable

### Maintainability
- [x] New models can be added with <100 lines of code
- [x] Dependencies are explicit and validated
- [x] Error messages are clear and actionable

### Testing
- [ ] 80%+ code coverage (needs unit tests)
- [x] Integration tests pass (manual testing with real data)
- [x] End-to-end tests with real SAP data pass

---

## Future Enhancements

### Phase 10+ (Post-Launch)
- [ ] Add retry logic for transient failures
- [ ] Add progress bars for long-running imports
- [ ] Add dry-run mode
- [ ] Add data validation framework
- [ ] Add incremental import support
- [ ] Add rollback/undo functionality
- [ ] Add import scheduling
- [ ] Add web UI for monitoring
- [ ] Write comprehensive unit tests
- [ ] Add performance benchmarking suite

---

## File Structure

```
sap_b1_to_odoo/
├── __init__.py
├── __manifest__.py
├── README.md                         # User-facing documentation
├── ETL_FRAMEWORK.md                  # Framework documentation
├── MIGRATION_ROADMAP.md              # This document
├── etl_framework.py                  # Core framework
├── models/
│   ├── __init__.py
│   ├── sap_database.py              # Orchestrator integration
│   ├── sale_purchase_order_etl_mixin.py  # Shared mixin
│   ├── sale_order_etl.py            # Sale order pipelines
│   ├── sale_quotation_etl.py        # Sale quotation pipelines
│   ├── purchase_order_etl.py        # Purchase order pipelines
│   ├── product_product.py           # Product pipelines
│   ├── res_partner.py               # Partner pipelines
│   ├── mrp_bom.py                   # BOM pipelines
│   ├── account_payment_term.py      # Payment term pipeline
│   ├── product_pricelist.py         # Pricelist pipelines
│   ├── purchase_requisition.py      # Blanket order pipeline
│   └── ...
├── tests/
│   └── ... (to be added)
└── tools.py                          # Existing utilities
```

---

**Document Status:** Living document, updated as migration progresses.
