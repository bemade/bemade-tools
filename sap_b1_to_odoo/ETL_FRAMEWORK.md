# SAP B1 to Odoo ETL Framework

**Version:** 2.1  
**Last Updated:** December 5, 2025

---

## Overview

The ETL Framework is a declarative, self-optimizing system for migrating data from SAP Business One to Odoo. It provides a clean separation between Extract, Transform, and Load phases while automatically optimizing execution strategy based on data volume.

---

## Design Principles

### 1. **Declarative Over Imperative**
- Pipeline structure visible at class level through decorators
- Dependencies and execution order are explicit
- Minimal boilerplate code

### 2. **Self-Optimizing Execution**
- Framework automatically decides single-process vs. multiprocessing based on data volume
- No manual optimization decisions required
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

---

## Architecture

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

### ETLContext
Lightweight context object passed to all ETL methods.

```python
@dataclass
class ETLContext:
    cr: Any          # SAP database cursor
    env: Any         # Odoo environment
    # No data storage - prevents memory overload
```

### MultiprocessingConfig
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

### ETLPipeline
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

### ETL Decorators
Method registration decorators.

```python
class ETL:
    @classmethod
    def pipeline(cls, target_model, importer_name, sap_source=None, 
                 depends_on=None, multiprocessing_threshold=1000, 
                 chunk_size=500, max_workers=None, allow_multiprocessing=True):
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

---

## Usage Examples

### Simple Model (No Multiprocessing)

```python
@ETL.pipeline(
    target_model='account.payment.term',
    importer_name='account.payment.term.importer',
    sap_source='octg',
    allow_multiprocessing=False,  # Always single-process
)
class AccountPaymentTermImporter(models.AbstractModel):
    _name = 'account.payment.term.importer'
    _description = 'SAP Payment Term Importer'
    
    @ETL.extract('octg')
    def extract_payment_terms(self, ctx: ETLContext) -> List[Dict]:
        ctx.cr.execute("SELECT * FROM octg")
        return ctx.cr.dictfetchall()
    
    @ETL.transform()
    def transform_payment_terms(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        terms = extracted['extract_payment_terms']
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
            for term in terms
        ]
    
    @ETL.load()
    def load_payment_terms(self, ctx: ETLContext, transformed: Dict) -> None:
        term_vals = transformed['transform_payment_terms']
        ctx.env["account.payment.term"].create(term_vals)
```

### Complex Model (With Multiprocessing)

```python
@ETL.pipeline(
    target_model='sale.order',
    importer_name='sale.order.header.importer',
    sap_source='ordr',
    depends_on=['res.partner', 'account.payment.term', 'res.users'],
    multiprocessing_threshold=500,
    chunk_size=500,
    max_workers=8,
)
class SaleOrderHeaderImporter(models.AbstractModel):
    _name = 'sale.order.header.importer'
    _description = 'SAP Sale Order Header Importer (ORDR)'
    _inherit = 'sale.purchase.order.etl.mixin'
    
    _lookup_cache = {}
    
    @ETL.extract('ordr')
    def extract_headers(self, ctx: ETLContext) -> List[Dict]:
        # Get existing orders (idempotence)
        ctx.env.cr.execute(
            "SELECT DISTINCT sap_docnum FROM sale_order WHERE sap_docnum IS NOT NULL"
        )
        existing_docnums = tuple(row[0] for row in ctx.env.cr.fetchall())
        
        # Extract new order headers
        sql = "SELECT * FROM ordr"
        if existing_docnums:
            sql += " WHERE docnum NOT IN %s"
            ctx.cr.execute(SQL(sql, existing_docnums))
        else:
            ctx.cr.execute(sql)
        
        headers = ctx.cr.dictfetchall()
        
        # Pre-compute lookups for transform phase
        partners = ctx.env["res.partner"].search([...])
        partners_map = {partner.sap_card_code: partner.id for partner in partners}
        
        # Store in class-level cache (only primitive types!)
        SaleOrderHeaderImporter._lookup_cache = {
            "partners_map": partners_map,
            # ... other lookups
        }
        
        return headers
    
    @ETL.transform()
    def transform_headers(self, ctx: ETLContext, extracted: Dict) -> List[Dict]:
        headers = extracted['extract_headers']
        cache = SaleOrderHeaderImporter._lookup_cache
        
        order_vals = []
        for header in headers:
            partner_id = self.get_partner_id(header, cache)
            
            vals = {
                "sap_docnum": header["docnum"],
                "partner_id": partner_id,
                "date_order": fix_tz(header["docdate"]),
                # ... other fields
            }
            order_vals.append(vals)
        
        return order_vals
    
    @ETL.load()
    def load_headers(self, ctx: ETLContext, transformed: Dict) -> None:
        order_vals = transformed['transform_headers']
        if order_vals:
            orders = ctx.env["sale.order"].create(order_vals)
            _logger.info(f"Created {len(orders)} order headers.")
```

---

## Best Practices

### Multiprocessing
1. **Cache Primitive Types Only**: Store only IDs (integers/strings) in class-level caches, never Odoo recordsets
2. **Pre-compute in Extract**: Build all lookup dictionaries in the extract phase before multiprocessing begins
3. **Conservative Settings**: Start with lower worker counts (4-8) and larger chunk sizes (100-500)
4. **Error Propagation**: Remove try/except blocks in worker processes to ensure exceptions bubble up

### Idempotence
- Always filter existing records in the extract phase
- Use SAP's unique identifiers (docnum, itemcode, cardcode, etc.)
- For child records, use composite keys (e.g., `sap_parent_card` + `sap_address_linenum`)

### Data Quality
- **Negative quantities**: Filter out invalid data that violates Odoo constraints
- **Empty names**: Validate and provide fallbacks
- **Missing foreign keys**: Log warnings and skip records gracefully

### Split Pipeline Pattern
Complex models benefit from splitting into multiple pipelines:
1. **Parent records** - main entities
2. **Child records** - related entities
3. **Post-process** - link relationships

This allows:
- Independent idempotence checks
- Clearer separation of concerns
- Easier debugging
- Better multiprocessing control per pipeline

---

## Recommended Thresholds

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

---

## Troubleshooting

### Common Issues

**Issue**: `AttributeError: 'NoneType' object has no attribute 'id'`
- **Cause**: Trying to access recordset attributes in transform phase
- **Solution**: Pre-compute all lookups in extract phase, store only IDs in cache

**Issue**: Multiprocessing warnings about fork in multi-threaded process
- **Cause**: Debugpy and other tools warn about forking
- **Solution**: Framework automatically suppresses these warnings

**Issue**: Records being skipped
- **Cause**: Missing foreign key references (e.g., partner not found)
- **Solution**: Check logs for warnings, ensure dependencies are imported first

**Issue**: Duplicate records created
- **Cause**: Idempotence check not working
- **Solution**: Verify SAP unique field is correctly filtered in extract phase

**Issue**: `SerializationFailure: could not serialize access due to concurrent update`
- **Cause**: Multiple workers updating the same records (e.g., `res_partner.write_date`)
- **Solution**: Framework automatically retries with exponential backoff (up to 5 attempts). If persistent, reduce `max_workers` or increase `chunk_size`

**Issue**: Noisy "bad query" ERROR logs during multiprocessing
- **Cause**: PostgreSQL serialization failures logged by `odoo.sql_db`
- **Solution**: Framework v2.1+ automatically mutes these in worker processes. Errors still propagate for retry handling.

---

## API Reference

See inline documentation in `etl_framework.py` for complete API details.

---

## v2.1 Changes (December 2025)

- **Automatic log muting**: Framework now automatically mutes `odoo.sql_db` logs in multiprocessing workers to suppress noisy serialization error messages (errors still propagate for retry)
- **Enhanced logging**: All log messages now include `[importer_name]` prefix for easier debugging
- **Serialization retry**: Built-in retry logic with exponential backoff for PostgreSQL serialization failures

---

## Extracting the Framework

### Odoo Dependencies Analysis

The framework has the following Odoo-specific dependencies:

| Import | Usage | Extractable? |
|--------|-------|--------------|
| `odoo.api` | `api.Environment` for creating Odoo env in workers | ❌ Core Odoo |
| `odoo.modules.registry.Registry` | Database registry for worker processes | ❌ Core Odoo |
| `odoo.tools.mute_logger` | Suppress noisy SQL logs | ⚠️ Could be replaced |

### Tight Coupling Points

1. **ETLContext.env**: The `env` attribute is an Odoo `Environment` object used for:
   - `env.cr` - Odoo database cursor
   - `env["model.name"]` - Model access
   - `env.flush_all()` - ORM flush

2. **Worker Process Setup**: `_process_chunk_static` uses:
   - `Registry(dbname).cursor()` - Odoo's connection pool
   - `api.Environment(cr, uid, context)` - Odoo environment creation

3. **Pipeline Registration**: Pipelines are registered on `models.AbstractModel` subclasses

### Extraction Options

#### Option A: Separate Odoo Module (Recommended)

Create a standalone `etl_framework` Odoo addon:

```
addons/
├── etl_framework/
│   ├── __init__.py
│   ├── __manifest__.py
│   ├── framework.py          # Core ETL classes
│   ├── executor.py           # ETLExecutor
│   ├── orchestrator.py       # PipelineOrchestrator
│   └── README.md
└── sap_b1_to_odoo/
    ├── __manifest__.py       # depends: ['etl_framework']
    └── models/
        └── *.py              # Importer pipelines
```

**Pros:**
- Reusable across projects
- Clear separation of concerns
- Can be versioned independently
- Easy to install via Odoo module system

**Cons:**
- Still requires Odoo runtime
- Can't be used outside Odoo

#### Option B: Hybrid Python Package + Odoo Adapter

```
etl_framework/                 # Pure Python package
├── __init__.py
├── core.py                    # ETLPipeline, ETLMethod, ETLPhase
├── config.py                  # MultiprocessingConfig
├── executor.py                # Abstract executor interface
└── adapters/
    └── odoo.py               # Odoo-specific implementation

addons/
└── odoo_etl_framework/       # Odoo adapter module
    ├── __manifest__.py
    └── adapter.py            # Imports from etl_framework
```

**Pros:**
- Core logic is framework-agnostic
- Could theoretically support other ORMs
- Testable without Odoo

**Cons:**
- More complex architecture
- Adapter layer adds overhead
- Most value is in Odoo integration anyway

#### Option C: Keep as Single Module (Current)

Keep everything in `sap_b1_to_odoo` but document the framework well.

**Pros:**
- Simple, no refactoring needed
- All code in one place
- Easy to understand

**Cons:**
- Not reusable for other SAP→Odoo projects
- Framework mixed with SAP-specific code

### Recommendation

**Option A (Separate Odoo Module)** is the best balance:

1. Extract `etl_framework.py` into `addons/etl_framework/`
2. Keep SAP-specific importers in `sap_b1_to_odoo`
3. Add `'etl_framework'` to `sap_b1_to_odoo` dependencies

This allows:
- Reuse for other migration projects (e.g., Sage→Odoo, QuickBooks→Odoo)
- Independent versioning and testing
- Clean separation without over-engineering

### Migration Path

```python
# Before (sap_b1_to_odoo/models/product_etl.py)
from odoo.addons.sap_b1_to_odoo.etl_framework import ETL, ETLContext

# After (sap_b1_to_odoo/models/product_etl.py)
from odoo.addons.etl_framework import ETL, ETLContext
```

---

**Document Status:** Living document, updated as framework evolves.
