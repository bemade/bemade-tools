# SAP B1 to Odoo Migration Module

Automated data migration from SAP Business One to Odoo using the [ETL Framework](../etl_framework/README.md).

---

## Quick Start (Developers)

### Prerequisites
- Odoo 19.0 development environment
- SAP B1 database accessible (PostgreSQL mirror or SQL Server)
- Python dependencies: `pyodbc`, `fuzzywuzzy`, `python-Levenshtein`

### Setting Up SAP B1 Database (Optional)

If you need to migrate a SAP B1 `.bak` file from MSSQL to PostgreSQL for testing:

1. **Place your backup file** in the `.mssql-to-postgres/backup/` directory:
   ```bash
   cp /path/to/your/sapb1.bak .mssql-to-postgres/backup/rwiprod.bak
   ```

2. **Start the migration**:
   ```bash
   cd .mssql-to-postgres
   docker-compose up
   ```

3. **Wait for completion** - The process will:
   - Restore the `.bak` to MSSQL Server
   - Migrate all tables to PostgreSQL using pgloader
   - Leave PostgreSQL running on `localhost:5432`

4. **Use the migrated database** in your run configuration:
   - Host: `localhost`
   - Port: `5432`
   - Database: `rwiprod`
   - User: `postgres`
   - Password: `pgpassword`

### Running the Import

The module automatically imports SAP data on installation/upgrade when `SAP_AUTO_IMPORT=true` is set.

#### VS Code Run Configuration

Add this to your `.vscode/launch.json` (simply insert the configuration dictionary and update the inputs section with these inputs):

```json
{
    "version": "0.2.0",
    "configurations": [
        {
            "name": "Odoo Local: Test SAP Import",
            "type": "debugpy",
            "request": "launch",
            "program": "${workspaceFolder}/odoo/odoo-bin",
            "console": "integratedTerminal",
            "args": [
                "-c",
                "${workspaceFolder}/conf/odoo.conf",
                "-u",
                "sap_b1_to_odoo",
                "-i",
                "sap_b1_to_odoo",
                "-d",
                "${input:database}",
                "--stop-after-init",
                "--no-http"
            ],
            "env": {
                "PYTHONWARNINGS": "ignore:FutureWarning",
                "GEVENT_SUPPORT": "True",
                "SAP_DB_HOST": "${input:externalDbHost}",
                "SAP_DB_PORT": "${input:externalDbPort}",
                "SAP_DB_NAME": "${input:externalDbName}",
                "SAP_DB_SCHEMA": "dbo",
                "SAP_DB_USER": "postgres",
                "SAP_DB_PASSWORD": "pgpassword",
                "SAP_AUTO_IMPORT": "true"
            }
        }
    ],
    "inputs": [
        {
            "id": "database",
            "type": "pickString",
            "description": "Select database",
            "options": [
                "rwi-test9",
                "rwi-dev",
                "rwi-local"
            ],
            "default": "rwi-test9"
        },
        {
            "id": "externalDbHost",
            "type": "promptString",
            "description": "SAP Database Host",
            "default": "localhost"
        },
        {
            "id": "externalDbPort",
            "type": "promptString",
            "description": "SAP Database Port",
            "default": "5432"
        },
        {
            "id": "externalDbName",
            "type": "promptString",
            "description": "SAP Database Name",
            "default": "sap_b1_mirror"
        }
    ]
}
```

**Usage:**
1. Press `F5` or click "Run and Debug"
2. Select "Odoo Local: Test SAP Import"
3. Choose database and enter SAP connection details
4. Import runs automatically on module upgrade

---

## What Gets Imported

The framework automatically imports in dependency order:

1. Users, Payment Terms, Pricelists, Carriers
2. Partners (Companies → Addresses → Contacts)
3. Products (Categories → Products → BOMs)
4. Orders (Sale Orders → Sale Quotations → Purchase Orders)
5. Pricelist Items, Purchase Requisitions

Each model is split into pipelines (e.g., Sale Orders = Headers + Lines + Text Lines + Post-processor).

---

## Monitoring

Watch the console output for progress:

```
INFO: Extracted 1250 records. Using multiprocessing mode.
INFO: Processing 3 chunks with 8 workers.
INFO: Completed chunk 1/3
WARNING: Skipping order 12345: partner not found (cardcode=C00001)
```

- **INFO**: Normal progress
- **WARNING**: Skipped records (missing foreign keys, data quality issues)
- **ERROR**: Critical failures

---

## Common Issues

### Import Fails
- Check SAP database connection (host, port, credentials)
- Verify SAP database is accessible from Odoo server
- Check logs for specific error messages

### Records Skipped
- **"partner not found"**: Partners not imported yet or SAP cardcode mismatch
- **"product not found"**: Products not imported yet or SAP itemcode mismatch
- **Solution**: Ensure dependencies are imported first, check SAP data quality

### Duplicates Created
- Import is idempotent - safe to run multiple times
- If duplicates occur, check SAP unique fields (sap_docnum, sap_item_code, etc.)

### Odoo 19 Compatibility
- Some custom modules may need updates (e.g., `group_id` field removed)
- See `MIGRATION_ROADMAP.md` for migration notes

---

## Performance Tuning

Adjust multiprocessing settings in pipeline decorators:

```python
@ETL.pipeline(
    multiprocessing_threshold=500,  # Min records for multiprocessing
    chunk_size=500,                  # Records per worker chunk
    max_workers=8,                   # Max parallel workers
)
```

---

## Adding New Pipelines

See the [ETL Framework README](../etl_framework/README.md) for complete documentation. Quick template:

```python
from odoo.addons.etl_framework import ETL, ETLContext

@ETL.pipeline(
    target_model='your.model',
    importer_name='your.model.importer',
    sap_source='sap_table',
    depends_on=['dependency.importer'],
)
class YourModelImporter(models.AbstractModel):
    _name = 'your.model.importer'
    
    @ETL.extract('sap_table')
    def extract_data(self, ctx: ETLContext):
        ctx.cr.execute("SELECT * FROM sap_table")
        return ctx.cr.dictfetchall()
    
    @ETL.transform()
    def transform_data(self, ctx: ETLContext, extracted: Dict):
        return [{"name": row["name"]} for row in extracted['extract_data']]
    
    @ETL.load()
    def load_data(self, ctx: ETLContext, transformed: Dict):
        ctx.env['your.model'].create(transformed['transform_data'])
```

---

**Last Updated:** January 8, 2026
