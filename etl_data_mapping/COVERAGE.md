# SAP B1 to Odoo Migration - Use Case Coverage

This document maps the existing `sap_b1_to_odoo` module patterns to the new ETL framework.

## ✅ Covered Use Cases

### 1. **Simple Table Mappings**
- **Example**: Products (OITM → product.product)
- **Pattern**: Direct field-to-field mapping with transforms
- **Framework Support**: Example 1 in README

### 2. **Multi-Table JOINs**
- **Example**: Partners with payment terms (OCRD + OCTG)
- **Pattern**: `_source_relations` for auto-JOIN
- **Framework Support**: Example 2 in README

### 3. **External Table Relations**
- **Example**: Order lines referencing products (RDR1.itemcode → OITM.itemcode)
- **Pattern**: `source_relation` and `source_relation_field` for documentation
- **Framework Support**: Examples 4, 4b, 4c in README

### 4. **Multiprocessing for Large Datasets**
- **Example**: Products (8 workers, 500 chunk size)
- **Pattern**: `_multiprocessing = True`, `_chunk_size`, `_max_workers`
- **Framework Support**: Example 5 in README

### 5. **Hierarchical/Parent-Child Data**
- **Example**: BOMs (OITT → ITT1)
- **Pattern**: `_child_mapping` attribute, parent-child relationships
- **Framework Support**: Example 6 in README

### 6. **Complex Aggregations with One2many**
- **Example**: Pricelists with items (OOAT + OAT1)
- **Pattern**: `_post_process_record()` hook for creating child records
- **Framework Support**: Example 7 in README

### 7. **File Attachments**
- **Example**: Attachments (ATC1 + filestore)
- **Pattern**: Binary data handling, file I/O, polymorphic relations
- **Framework Support**: Example 8 in README

### 8. **Inventory/Stock Quantities**
- **Example**: Stock quants (OITW → stock.quant)
- **Pattern**: Computed fields, kit handling, post-processing
- **Framework Support**: Example 9 in README

### 9. **State Management & Updates**
- **Example**: Order state sync (ORDR.docstatus → sale.order.state)
- **Pattern**: `etl.mapping.update` for updating existing records
- **Framework Support**: Example 10 in README

### 10. **Fuzzy Matching & Deduplication**
- **Example**: Carrier names (OSHP → delivery.carrier)
- **Pattern**: `deduplicate=True`, `fuzzy_threshold`
- **Framework Support**: Example 11 in README

### 11. **Incremental Loads**
- **Example**: All importers check existing records
- **Pattern**: Filter out already-imported records via SQL
- **Framework Support**: Built into framework

### 12. **Custom Transformations**
- **Example**: `fix_quotes()`, date conversions, Y/N to boolean
- **Pattern**: Named transform functions or lambdas
- **Framework Support**: All examples

### 13. **Conditional Field Mapping**
- **Example**: Fallback to description when product missing
- **Pattern**: `condition` parameter on ETLField
- **Framework Support**: Example 4c in README

### 14. **Computed/Constant Fields**
- **Example**: `type='consu'`, `is_storable=True`
- **Pattern**: `compute` parameter with lambda/function
- **Framework Support**: Example 1 in README

### 15. **Virtual Models (Custom SQL)**
- **Example**: Complex queries with subqueries
- **Pattern**: `etl.mapping.virtual` with `_get_source_query()`
- **Framework Support**: Example 3 in README

## 📋 Additional Patterns Found

### From `sale_purchase_common.py`:
- **Mixin pattern**: Shared logic for sales/purchase orders
- **Configuration attributes**: `_sap_header_table`, `_sap_lines_table`, etc.
- **Text lines handling**: RDR10/POR10 for order notes
- **Sequence calculation**: `linenum * 100 + lineseq`
- **State transitions**: Draft → Confirmed → Done → Cancel
- **Delivery quantity updates**: `qty_delivered`, `qty_received`

### From `res_partner.py`:
- **Multiple source tables**: OCRD (companies), CRD1 (addresses), OCPR (contacts)
- **Address extraction logic**: Complex street/street2 parsing
- **Parent-child linking**: Post-import relationship setup
- **Rank setting**: Customer vs supplier classification
- **Currency-based account assignment**: USD vs CAD receivable/payable

### From `product_pricelist.py`:
- **Date-based activation**: `active = datetime.now() <= enddate`
- **Partner-specific pricelists**: Blanket agreements
- **Purchase blankets**: Separate from sales pricelists
- **Status mapping**: SAP status codes → Odoo states
- **Default pricelist fallback**: Global item at end

### From `account_move.py`:
- **Invoice/Bill distinction**: Same pattern, different tables
- **Order line linking**: INV1/PCH1 → sale.order.line/purchase.order.line
- **Invoiced quantity tracking**: `sap_qty_invoiced` field
- **Text lines**: INV10/PCH10 for invoice notes

### From `customer_product_code.py`:
- **Simple mapping table**: OSCN → product.customer.code
- **Many-to-many relationships**: Partner × Product

### From `carrier_account.py`:
- **Account extraction**: Parsing "#123456" from strings
- **Fuzzy name matching**: 80% threshold
- **Dynamic carrier creation**: Based on unique names
- **Supplier vs customer logic**: Different account ownership

## 🎯 Framework Design Implications

### Core Features Needed:
1. **ETLField** custom field type with all parameters
2. **ETLMapping** abstract model with extract/transform/load
3. **ETLMapping.virtual** for custom SQL
4. **ETLMapping.update** for updating existing records
5. **Multiprocessing support** with chunking
6. **Post-processing hooks** for complex logic
7. **External schema models** for documentation
8. **Transform registry** for named functions
9. **Fuzzy matching utilities**
10. **Relation resolver** for FK lookups

### Optional Enhancements:
- **Migration wizard** UI for running migrations
- **Progress tracking** with status updates
- **Dry-run mode** to preview changes
- **Rollback capability** for failed migrations
- **Mapping visualization** to show data flow
- **Gap analysis report** generator
- **Data validation** rules engine
- **Conflict resolution** strategies

## 🚀 Next Steps

1. Implement core `ETLField` and `ETLMapping` models
2. Add database connection management
3. Build SQL query generator
4. Implement transform registry
5. Add multiprocessing support
6. Create example SAP B1 mappings using new framework
7. Test with real SAP data
8. Document migration patterns
9. Build UI for managing mappings
10. Add gap analysis tools
