# xTuple to Odoo ETL Refactoring Progress

This checklist tracks the refactoring of `xtuple_to_odoo` to use the `etl_framework` module pattern (like `sap_b1_to_odoo`).

## Goals
- Separate model field extensions from ETL/import logic
- Use `@ETL.pipeline`, `@ETL.extract`, `@ETL.transform`, `@ETL.load` decorators
- Use `PipelineOrchestrator` for dependency resolution and execution
- Add proper schema support to database cursor

## Scope Notes
- **Sales Orders**: Handled by QuickBooks (skip for now)
- **Purchase Orders**: TBD - explore xTuple data to determine if needed
- **Production Orders**: May import history - explore data

---

## Phase 1: Setup & Core Infrastructure

- [x] **Update `__manifest__.py`**
  - [x] Add `etl_framework` to dependencies
  - [x] Verify version for Odoo 19

- [x] **Refactor `xtuple_database.py`**
  - [x] Add schema support to `get_cursor()` 
  - [x] Add `_get_source_config()` method
  - [x] Add `_execute_pipeline()` helper
  - [x] Add `_execute_pipelines()` helper
  - [x] Refactor action methods to use `PipelineOrchestrator`
  - [x] Add `setup_from_env()` for auto-configuration

---

## Phase 2: Model File Refactoring

### `res_partner.py`
- [x] Keep only `ResPartner` model with field extensions
- [x] Move `XtupleResPartnerImporter` to pipelines
- [x] Remove old importer class from file

### `product.py`
- [x] Keep only `ProductCategory` field extensions
- [x] Keep only `ProductProduct` field extensions  
- [x] Keep only `ProductSupplierInfo` field extensions
- [x] Move `XtupleProductImporter` to pipelines
- [x] Remove old importer class from file

### `mrp_bom.py`
- [x] Keep only `MrpBom` field extensions
- [x] Keep only `MrpBomLine` field extensions
- [x] Move `XtupleBomImporter` to pipelines
- [x] Remove old importer class from file

---

## Phase 3: ETL Pipelines

### Create `models/pipelines/` directory
- [x] Create `__init__.py`

### Partner Pipelines (`res_partner_etl.py`)
- [x] `xtuple.partner.customer.importer` - Customers as companies
- [x] `xtuple.partner.vendor.importer` - Vendors as companies
- [x] `xtuple.partner.standalone.importer` - Standalone CRM accounts
- [x] `xtuple.partner.contact.importer` - Contacts
- [x] `xtuple.partner.shipto.importer` - Ship-to addresses
- [x] `xtuple.partner.postprocessor` - Link parents, set ranks

### Product Pipelines (`product_etl.py`)
- [x] `xtuple.product.category.importer`
- [x] `xtuple.product.importer`
- [x] `xtuple.product.supplierinfo.importer`

### BOM Pipelines (`mrp_bom_etl.py`)
- [x] `xtuple.mrp.bom.importer` - BOM headers
- [x] `xtuple.mrp.bom.line.importer` - BOM components
- [x] `xtuple.mrp.bom.postprocessor` - Set routes on products

---

## Phase 4: Data Exploration

### Purchase Orders
- [x] Explore xTuple `pohead` / `poitem` tables (1,133 POs: 1,092 closed, 40 unreleased, 1 open)
- [x] Determine if PO import is needed - YES, QuickBooks only has vendor invoices
- [x] Create `purchase_order_etl.py`
  - [x] `xtuple.purchase.order.importer` - PO headers
  - [x] `xtuple.purchase.order.line.importer` - PO lines
- [x] Create `purchase_order.py` model extensions
- [x] Add `purchase` module dependency

### Production Orders / Manufacturing History
- [x] Explore xTuple `wo` (work order) tables (4,860 WOs: 4,856 closed, 4 exploded)
- [x] Create `mrp_production_etl.py`
  - [x] `xtuple.mrp.production.importer` - Manufacturing orders
- [x] Add `MrpProduction` model extension to `mrp_bom.py`

---

## Phase 5: Finalization

- [x] Update `models/__init__.py` to import pipelines
- [x] Update `security/ir.model.access.csv` for new models
- [ ] Update/fix tests for new structure
- [x] Clean up any remaining old code
- [ ] Test full import workflow

---

## Pipeline Dependency Graph

```
xtuple.partner.customer.importer
xtuple.partner.vendor.importer
xtuple.partner.standalone.importer
    └── xtuple.partner.contact.importer
    └── xtuple.partner.shipto.importer
    └── xtuple.partner.postprocessor

xtuple.product.category.importer
    └── xtuple.product.importer
        └── xtuple.product.supplierinfo.importer (depends on vendors)

xtuple.mrp.bom.importer (depends on products)
    └── xtuple.mrp.bom.line.importer
    └── xtuple.mrp.bom.postprocessor

xtuple.purchase.order.importer (depends on vendors, products)
    └── xtuple.purchase.order.line.importer

xtuple.mrp.production.importer (depends on products, BOMs)
```

---

## Notes / Decisions Log

- *2025-01-14*: Started refactoring. Sales orders handled by QuickBooks. POs and production history TBD.
- *2026-01-14*: Fixed Odoo 19 compatibility issues (uom_po_id, uom.category removed, uom_id on product.template).
- *2026-01-14*: Added PO import (1,133 orders) and MO import (4,860 work orders) pipelines.
