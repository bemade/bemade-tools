# ETL Data Mapping Framework

## Vision: Ergonomic Data Migration

This module aims to make data migration as simple as defining Odoo models. The goal is to write declarative mappings that can:

1. **Document** the mapping between source and target systems
2. **Extract** data automatically from external databases
3. **Transform** data using simple, composable functions
4. **Load** data into Odoo models

## Usage Examples

### Example 1: Simple SAP B1 Product Mapping

```python
from odoo import models, fields
from odoo.addons.etl_data_mapping.models.etl_mapping import ETLMapping

class SAPProductMapping(models.Model):
    _name = 'sap.product.mapping'
    _inherit = 'etl.mapping'
    _description = 'SAP B1 Product to Odoo Product Mapping'
    
    # Define the source
    _source_table = 'oitm'  # SAP B1 items table
    _source_db = 'sap_b1'   # Connection reference
    
    # Define the target
    _target_model = 'product.product'
    
    # Define field mappings (similar to Odoo field definitions)
    itemcode = fields.ETLField(
        source='itemcode',
        target='sap_item_code',
        required=True,
        index=True,
    )
    
    itemname = fields.ETLField(
        source='itemname',
        target='default_code',
        transform='fix_quotes',  # Reference to transform function
    )
    
    frgnname = fields.ETLField(
        source='frgnname',
        target='name',
        transform='fix_quotes',
        default='N/A',
    )
    
    sellitem = fields.ETLField(
        source='sellitem',
        target='sale_ok',
        transform=lambda val: val == 'Y',  # Inline transform
    )
    
    prchseitem = fields.ETLField(
        source='prchseitem',
        target='purchase_ok',
        transform=lambda val: val == 'Y',
    )
    
    validfor = fields.ETLField(
        source='validfor',
        target='active',
        transform=lambda val: val == 'Y',
    )
    
    itmsgrpcod = fields.ETLField(
        source='itmsgrpcod',
        target='categ_id',
        relation='product.category',
        relation_field='sap_itms_grp_cod',  # Match on this field
    )
    
    # Computed/constant fields
    type = fields.ETLField(
        target='type',
        compute=lambda rec: 'consu',  # Always set to 'consu'
    )
    
    is_storable = fields.ETLField(
        target='is_storable',
        compute=lambda rec: True,
    )
    
    # Run the extraction and load
    def run_migration(self):
        """Auto-generated SQL extraction + transformation + load"""
        self.extract()  # SELECT * FROM oitm WHERE ...
        self.transform()  # Apply all transform functions
        self.load()  # Create product.product records
```

### Example 2: SAP Partner with Multiple Source Tables

```python
class SAPPartnerMapping(models.Model):
    _name = 'sap.partner.mapping'
    _inherit = 'etl.mapping'
    _description = 'SAP B1 Partner to Odoo Partner Mapping'
    
    # Multiple source tables (like a SQL JOIN)
    _source_tables = [
        ('ocrd', 'main'),  # Main partner table
        ('octg', 'payment_terms', 'main.groupnum = payment_terms.groupnum'),  # LEFT JOIN
    ]
    _source_db = 'sap_b1'
    _target_model = 'res.partner'
    
    # Fields from main table (ocrd)
    cardcode = fields.ETLField(
        source='main.cardcode',
        target='sap_card_code',
        required=True,
        unique=True,
    )
    
    cardname = fields.ETLField(
        source='main.cardname',
        target='name',
        transform='fix_quotes',
    )
    
    # Fields from joined table (octg)
    groupnum = fields.ETLField(
        source='payment_terms.groupnum',
        target='property_payment_term_id',
        relation='account.payment.term',
        relation_field='sap_groupnum',
    )
    
    # Complex transformation using multiple source fields
    street = fields.ETLField(
        target='street',
        compute=lambda rec: rec._extract_street(rec.source_data['address'], rec.source_data['block']),
    )
    
    def _extract_street(self, address, block):
        """Custom transformation logic"""
        if block and not address:
            return block
        return address
```

### Example 3: Virtual Model Approach (Custom SQL)

```python
class SAPSalesOrderMapping(models.Model):
    _name = 'sap.sales.order.mapping'
    _inherit = 'etl.mapping.virtual'  # Virtual = custom SQL
    _description = 'SAP B1 Sales Order Mapping'
    
    _target_model = 'sale.order'
    
    def _get_source_query(self):
        """Override to provide custom SQL"""
        return """
            SELECT 
                h.docentry,
                h.docnum,
                h.cardcode,
                h.docdate,
                h.doctotal,
                c.cardname,
                c.e_mail
            FROM ordr h
            LEFT JOIN ocrd c ON h.cardcode = c.cardcode
            WHERE h.canceled = 'N'
                AND h.docentry NOT IN (
                    SELECT sap_docentry FROM sale_order WHERE sap_docentry IS NOT NULL
                )
        """
    
    # Then define mappings as usual
    docentry = fields.ETLField(source='docentry', target='sap_docentry')
    docnum = fields.ETLField(source='docnum', target='sap_docnum')
    cardcode = fields.ETLField(
        source='cardcode',
        target='partner_id',
        relation='res.partner',
        relation_field='sap_card_code',
    )
```

### Example 4: One-to-Many Relationships (Order Lines)

```python
class SAPSalesOrderLineMapping(models.Model):
    _name = 'sap.sales.order.line.mapping'
    _inherit = 'etl.mapping'
    
    _source_table = 'rdr1'
    _source_db = 'sap_b1'
    _target_model = 'sale.order.line'
    
    # Parent relationship (to Odoo record)
    docentry = fields.ETLField(
        source='docentry',
        target='order_id',
        relation='sale.order',
        relation_field='sap_docentry',
        required=True,
    )
    
    linenum = fields.ETLField(
        source='linenum',
        target='sap_line_num',
        transform=lambda val: (val or 0) + 2,  # Increment to avoid 0
    )
    
    # Foreign key to another external table (rdr1.itemcode -> oitm.itemcode)
    # This resolves through the external->Odoo mapping
    itemcode = fields.ETLField(
        source='itemcode',
        target='product_id',
        relation='product.product',  # Target Odoo model
        relation_field='sap_item_code',  # Field to match on
        # Optional: specify the external relation for documentation/validation
        source_relation='oitm',  # External table this FK points to
        source_relation_field='itemcode',  # Field in external table
    )
    
    quantity = fields.ETLField(source='quantity', target='product_uom_qty')
    price = fields.ETLField(source='price', target='price_unit')
    discprcnt = fields.ETLField(source='discprcnt', target='discount')
```

### Example 4b: Enriching Data with External Relations (Auto-JOIN)

```python
class SAPSalesOrderLineMapping(models.Model):
    _name = 'sap.sales.order.line.mapping'
    _inherit = 'etl.mapping'
    
    _source_table = 'rdr1'
    _source_db = 'sap_b1'
    _target_model = 'sale.order.line'
    
    # Define external relations - framework will auto-JOIN
    _source_relations = [
        # (table, alias, join_condition)
        ('oitm', 'product', 'rdr1.itemcode = product.itemcode'),
        ('ordr', 'header', 'rdr1.docentry = header.docentry'),
    ]
    
    # Now you can reference fields from related tables
    itemcode = fields.ETLField(
        source='itemcode',  # From rdr1
        target='product_id',
        relation='product.product',
        relation_field='sap_item_code',
    )
    
    # Access fields from the joined product table
    product_name = fields.ETLField(
        source='product.frgnname',  # From oitm via JOIN
        target='name',
        transform='fix_quotes',
    )
    
    # Access fields from the joined header table
    order_date = fields.ETLField(
        source='header.docdate',  # From ordr via JOIN
        target='order_date',  # For validation or computed fields
        store=False,  # Don't store, just use for transforms
    )
```

### Example 4c: Complex External Relations with Validation

```python
class SAPSalesOrderLineMapping(models.Model):
    _name = 'sap.sales.order.line.mapping'
    _inherit = 'etl.mapping'
    
    _source_table = 'rdr1'
    _source_db = 'sap_b1'
    _target_model = 'sale.order.line'
    
    # Define external relations with metadata
    _source_relations = [
        {
            'table': 'oitm',
            'alias': 'product',
            'join_type': 'LEFT JOIN',
            'on': 'rdr1.itemcode = product.itemcode',
            'required': False,  # Allow missing products (will create with name only)
        },
        {
            'table': 'ordr',
            'alias': 'header',
            'join_type': 'INNER JOIN',
            'on': 'rdr1.docentry = header.docentry',
            'required': True,  # Must have a header
        },
    ]
    
    itemcode = fields.ETLField(
        source='itemcode',
        target='product_id',
        relation='product.product',
        relation_field='sap_item_code',
        # Validation: ensure the external relation exists
        validate_source_relation=True,  # Check that oitm record exists
    )
    
    # Fallback behavior when product doesn't exist in oitm
    dscription = fields.ETLField(
        source='dscription',  # From rdr1 directly
        target='name',
        condition=lambda rec: not rec.source_data.get('product.itemcode'),  # Only if no product
    )
```

### Example 5: Multiprocessing for Large Datasets

```python
class SAPProductMapping(models.Model):
    _name = 'sap.product.mapping'
    _inherit = 'etl.mapping'
    
    _source_table = 'oitm'
    _source_db = 'sap_b1'
    _target_model = 'product.product'
    _multiprocessing = True  # Enable parallel processing
    _chunk_size = 500  # Records per chunk
    _max_workers = 8  # Number of parallel workers
    
    # Field mappings...
    
    def run_migration(self):
        """Run with multiprocessing for large datasets"""
        self.extract_and_load_parallel()
```

### Example 6: Hierarchical/Nested Data (BOMs)

```python
class SAPBOMMapping(models.Model):
    _name = 'sap.bom.mapping'
    _inherit = 'etl.mapping'
    
    _source_table = 'oitt'  # BOM headers
    _source_db = 'sap_b1'
    _target_model = 'mrp.bom'
    
    # Define child lines relationship
    _child_mapping = 'sap.bom.line.mapping'  # Will be imported after parent
    
    code = fields.ETLField(source='code', target='sap_code')
    product_code = fields.ETLField(
        source='code',
        target='product_tmpl_id',
        relation='product.template',
        relation_field='sap_item_code',
    )
    quantity = fields.ETLField(source='qauntity', target='product_qty')
    bom_type = fields.ETLField(
        source='treetype',
        target='type',
        transform=lambda val: 'phantom' if val == 'A' else 'normal',
    )

class SAPBOMLineMapping(models.Model):
    _name = 'sap.bom.line.mapping'
    _inherit = 'etl.mapping'
    
    _source_table = 'itt1'  # BOM lines
    _source_db = 'sap_b1'
    _target_model = 'mrp.bom.line'
    
    # Parent relationship
    father = fields.ETLField(
        source='father',
        target='bom_id',
        relation='mrp.bom',
        relation_field='sap_code',
    )
    
    component_code = fields.ETLField(
        source='code',
        target='product_id',
        relation='product.product',
        relation_field='sap_item_code',
    )
    
    quantity = fields.ETLField(source='quantity', target='product_qty')
    sequence = fields.ETLField(source='childnum', target='sequence')
```

### Example 7: Complex Aggregations (Pricelists with Multiple Tables)

```python
class SAPPricelistMapping(models.Model):
    _name = 'sap.pricelist.mapping'
    _inherit = 'etl.mapping'
    
    _source_table = 'ooat'  # Blanket agreements
    _source_db = 'sap_b1'
    _target_model = 'product.pricelist'
    
    _source_relations = [
        ('oat1', 'lines', 'ooat.absid = lines.agrno'),  # Pricelist items
        ('ocrd', 'partner', 'ooat.bpcode = partner.cardcode'),  # Partner info
    ]
    
    absid = fields.ETLField(source='absid', target='sap_abs_id')
    
    name = fields.ETLField(
        target='name',
        compute=lambda rec: f"{rec.source_data['partner.cardname']} - {rec.source_data['descript']}",
    )
    
    active = fields.ETLField(
        source='enddate',
        target='active',
        transform=lambda val: datetime.now() <= val,
    )
    
    currency_id = fields.ETLField(
        source='bpcurr',
        target='currency_id',
        relation='res.currency',
        relation_field='name',
        transform=lambda val: 'CAD' if val != 'USD' else 'USD',
    )
    
    # One2many: pricelist items (handled via post-processing)
    def _post_process_record(self, odoo_record, source_data):
        """Create pricelist items after main record"""
        lines = self._get_related_lines(source_data['absid'])
        item_vals = []
        for line in lines:
            product = self.env['product.product'].search([
                ('sap_item_code', '=', line['itemcode'])
            ])
            item_vals.append({
                'pricelist_id': odoo_record.id,
                'product_tmpl_id': product.product_tmpl_id.id,
                'fixed_price': line['unitprice'],
                'date_start': source_data['startdate'],
                'date_end': source_data['enddate'],
            })
        self.env['product.pricelist.item'].create(item_vals)
```

### Example 8: File Attachments

```python
class SAPAttachmentMapping(models.Model):
    _name = 'sap.attachment.mapping'
    _inherit = 'etl.mapping'
    
    _source_table = 'atc1'
    _source_db = 'sap_b1'
    _target_model = 'ir.attachment'
    _multiprocessing = True  # File I/O benefits from parallel processing
    
    absentry = fields.ETLField(source='absentry', target='sap_absentry')
    line = fields.ETLField(source='line', target='sap_line')
    
    # Link to parent record (polymorphic)
    res_model = fields.ETLField(
        target='res_model',
        compute=lambda rec: rec._get_model_from_absentry(rec.source_data['absentry']),
    )
    
    res_id = fields.ETLField(
        target='res_id',
        compute=lambda rec: rec._get_res_id_from_absentry(
            rec.source_data['absentry'],
            rec._get_model_from_absentry(rec.source_data['absentry'])
        ),
    )
    
    # File handling
    name = fields.ETLField(
        source=['filename', 'fileext'],
        target='name',
        transform=lambda vals: f"{vals[0]}.{vals[1]}",
    )
    
    datas = fields.ETLField(
        source='filename',
        target='datas',
        transform=lambda filename: rec._load_file_from_disk(filename),
    )
    
    def _load_file_from_disk(self, filename):
        """Load file from external filestore"""
        filestore_path = self.env.context.get('filestore_path')
        file_path = os.path.join(filestore_path, filename)
        with open(file_path, 'rb') as f:
            return base64.b64encode(f.read())
```

### Example 9: Inventory/Stock Quantities

```python
class SAPStockQuantMapping(models.Model):
    _name = 'sap.stock.quant.mapping'
    _inherit = 'etl.mapping'
    
    _source_table = 'oitw'  # Warehouse stock
    _source_db = 'sap_b1'
    _target_model = 'stock.quant'
    
    _source_relations = [
        ('oitm', 'product', 'oitw.itemcode = product.itemcode'),
    ]
    
    # Filter: only items with stock
    _source_filter = 'oitw.onhand > 0'
    
    product_id = fields.ETLField(
        source='itemcode',
        target='product_id',
        relation='product.product',
        relation_field='sap_item_code',
    )
    
    quantity = fields.ETLField(source='onhand', target='quantity')
    
    # Computed: get default warehouse location
    location_id = fields.ETLField(
        target='location_id',
        compute=lambda rec: rec.env['stock.warehouse'].search([
            ('company_id', '=', rec.env.company.id)
        ], limit=1).lot_stock_id.id,
    )
    
    # Handle kit products (phantom BOMs)
    def _post_process_record(self, odoo_record, source_data):
        """If product is a kit, create quants for components instead"""
        product = odoo_record.product_id
        if product.is_kits:
            components = self._get_kit_components(product, odoo_record.quantity)
            odoo_record.unlink()  # Remove kit quant
            # Create component quants
            for component, qty in components.items():
                self.env['stock.quant'].create({
                    'product_id': component.id,
                    'quantity': qty,
                    'location_id': odoo_record.location_id.id,
                })
```

### Example 10: State Management & Conditional Updates

```python
class SAPSalesOrderStateMapping(models.Model):
    _name = 'sap.sales.order.state.mapping'
    _inherit = 'etl.mapping.update'  # Update existing records
    
    _source_table = 'ordr'
    _source_db = 'sap_b1'
    _target_model = 'sale.order'
    
    # Match existing records
    _match_field = 'sap_docnum'
    
    # Conditional state updates based on SAP status
    state = fields.ETLField(
        target='state',
        compute=lambda rec: rec._compute_state(
            rec.source_data['docstatus'],
            rec.source_data['invntsttus'],
            rec.source_data['canceled']
        ),
    )
    
    def _compute_state(self, docstatus, invntsttus, canceled):
        """Complex state logic"""
        if canceled == 'Y':
            return 'cancel'
        elif docstatus == 'C' and invntsttus == 'C':
            return 'done'
        elif docstatus == 'O' and invntsttus == 'O':
            return 'sale'
        else:
            return 'draft'
    
    # Update delivery quantities
    def _post_process_record(self, odoo_record, source_data):
        """Set delivered quantities for closed orders"""
        if source_data['docstatus'] == 'C':
            for line in odoo_record.order_line:
                line.qty_delivered = line.product_uom_qty
```

### Example 11: Fuzzy Matching & Data Cleaning

```python
class SAPCarrierMapping(models.Model):
    _name = 'sap.carrier.mapping'
    _inherit = 'etl.mapping'
    
    _source_table = 'oshp'  # Transporters
    _source_db = 'sap_b1'
    _target_model = 'delivery.carrier'
    
    # Fuzzy matching for carrier names
    name = fields.ETLField(
        source='trnspname',
        target='name',
        transform='clean_carrier_name',  # Named transform function
        deduplicate=True,  # Merge similar names
        fuzzy_threshold=80,  # Similarity threshold
    )
    
    def clean_carrier_name(self, value):
        """Extract carrier name from complex string"""
        # "FedEx #123456" -> "FedEx"
        # "UPS (Ground)" -> "UPS"
        import re
        return re.split(r'[#(]', value)[0].strip()
```

### Example 12: Running Migrations

```python
# In Python code or shell
env['sap.product.mapping'].run_migration()

# Or with filters
env['sap.product.mapping'].with_context(
    source_filter="validfor = 'Y'",  # Only active products
    batch_size=500,
).run_migration()

# Or step by step
mapping = env['sap.product.mapping']
data = mapping.extract()  # Returns recordset-like object with source data
transformed = mapping.transform(data)  # Apply transformations
mapping.load(transformed)  # Create Odoo records

# Gap analysis
gaps = mapping.analyze_gaps()
# Returns: {
#   'unmapped_source_fields': ['u_custom_field', 'u_fcsdk_coo'],
#   'unmapped_target_fields': ['barcode', 'weight'],
#   'missing_relations': [('itmsgrpcod', 'product.category')],
# }
```

## Key Features

1. **Auto-generated SQL**: Framework builds SELECT queries from field definitions
2. **Declarative transforms**: Simple functions or lambdas for data transformation
3. **Relation handling**: Automatic FK resolution using relation fields
4. **Batch processing**: Built-in chunking for large datasets
5. **Multiprocessing**: Parallel processing for large datasets (products, attachments, etc.)
6. **Gap analysis**: Identify unmapped fields automatically
7. **Incremental loads**: Track what's already imported
8. **Validation**: Pre-flight checks before loading
9. **Rollback**: Transaction management for safe migrations
10. **Hierarchical data**: Parent-child relationships (BOMs, order lines)
11. **One2many handling**: Post-processing hooks for complex relationships
12. **File attachments**: Binary data and file system integration
13. **State management**: Update existing records based on source state
14. **Fuzzy matching**: Deduplicate and clean data during import
15. **Computed fields**: Generate values from multiple source fields
16. **Conditional mapping**: Apply fields based on conditions
17. **Custom SQL**: Virtual models for complex queries
18. **Post-processing**: Hooks for complex business logic after record creation

## Defining External Database Schema

For documentation and validation, you can define the external database schema:

```python
class SAPDatabase(models.Model):
    _name = 'etl.external.database'
    _description = 'External Database Connection'
    
    name = fields.Char(string='Database Name', required=True)
    db_type = fields.Selection([
        ('postgresql', 'PostgreSQL'),
        ('mssql', 'MS SQL Server'),
        ('mysql', 'MySQL'),
    ], required=True)
    host = fields.Char()
    port = fields.Integer()
    # ... connection details

class SAPTable(models.Model):
    _name = 'etl.external.table'
    _description = 'External Database Table'
    
    name = fields.Char(string='Table Name', required=True)
    database_id = fields.Many2one('etl.external.database', required=True)
    description = fields.Text()
    field_ids = fields.One2many('etl.external.field', 'table_id', string='Fields')
    
    # Define relationships to other external tables
    relation_ids = fields.One2many('etl.external.relation', 'source_table_id')

class SAPTableRelation(models.Model):
    _name = 'etl.external.relation'
    _description = 'External Table Relationship'
    
    name = fields.Char(compute='_compute_name', store=True)
    source_table_id = fields.Many2one('etl.external.table', required=True)
    source_field_id = fields.Many2one('etl.external.field', required=True)
    target_table_id = fields.Many2one('etl.external.table', required=True)
    target_field_id = fields.Many2one('etl.external.field', required=True)
    relation_type = fields.Selection([
        ('one2many', 'One to Many'),
        ('many2one', 'Many to One'),
        ('many2many', 'Many to Many'),
    ], required=True)
    
    @api.depends('source_table_id', 'target_table_id', 'source_field_id')
    def _compute_name(self):
        for rec in self:
            rec.name = f"{rec.source_table_id.name}.{rec.source_field_id.name} -> {rec.target_table_id.name}"

# Example: Define SAP B1 schema
# rdr1 (order lines) -> oitm (products) via itemcode
env['etl.external.relation'].create({
    'source_table_id': env.ref('sap_b1.table_rdr1').id,
    'source_field_id': env.ref('sap_b1.field_rdr1_itemcode').id,
    'target_table_id': env.ref('sap_b1.table_oitm').id,
    'target_field_id': env.ref('sap_b1.field_oitm_itemcode').id,
    'relation_type': 'many2one',
})
```

## Architecture

```
ETLMapping (Abstract Model)
├── extract() - Auto-generate SQL from field definitions
│   ├── Build SELECT clause from ETLFields
│   ├── Build JOIN clauses from _source_relations
│   ├── Build WHERE clause from filters
│   └── Execute on external database
├── transform() - Apply transformation functions
│   ├── Apply field-level transforms
│   ├── Resolve relations (FK lookups)
│   └── Handle computed fields
├── load() - Create target records
│   ├── Batch creation
│   ├── Handle duplicates
│   └── Transaction management
├── validate() - Pre-flight validation
│   ├── Check required fields
│   ├── Validate relations exist
│   └── Check data types
└── analyze_gaps() - Find unmapped fields
    ├── Compare source schema to mapping
    ├── Compare target model to mapping
    └── Report missing mappings

ETLField (Custom Field Type)
├── source - Source column name (can be aliased: 'product.frgnname')
├── target - Target Odoo field name
├── transform - Transformation function (callable or string reference)
├── relation - Related Odoo model for FK resolution
├── relation_field - Field in related model to match on
├── source_relation - External table this FK points to (for documentation)
├── source_relation_field - Field in external table (for documentation)
├── compute - Computed value function (no source)
├── condition - Only apply this field if condition is True
└── validate_source_relation - Ensure external FK exists

External Schema Models (for documentation/validation)
├── etl.external.database - Database connections
├── etl.external.table - Table definitions
├── etl.external.field - Field definitions
└── etl.external.relation - FK relationships between external tables
```
