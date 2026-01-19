# QuickBooks Online to Odoo ETL Module

This module provides a clean, structured way to migrate data from QuickBooks Online (QBO) to Odoo using the ETL Framework.

## Features

- **OAuth2 Authentication**: Secure connection to QBO API
- **Rate Limiting**: Respects QBO's 500 requests/minute limit
- **ETL Framework Integration**: Uses declarative pipelines for reliable data migration
- **Incremental Imports**: Tracks last sync timestamps to avoid re-importing data
- **Comprehensive Data Migration**:
  - Chart of Accounts
  - Customers
  - Vendors
  - Products/Services (Items)
  - Journal Entries

## Installation

1. Install the module dependencies:
   ```bash
   pip install requests requests_oauthlib
   ```

2. Install the module in Odoo

3. Configure environment variables (optional):
   ```bash
   export QBO_CLIENT_ID="your_client_id"
   export QBO_CLIENT_SECRET="your_client_secret"
   export QBO_REALM_ID="your_company_id"
   export QBO_SANDBOX="true"  # For testing
   ```

## Configuration

### Getting QBO API Credentials

1. Go to [Intuit Developer Portal](https://developer.intuit.com/)
2. Create an app and get your Client ID and Client Secret
3. Configure the redirect URI: `https://your-odoo-url/qbo/callback`

### Setting Up the Connection

1. Go to **Accounting > Configuration > QuickBooks Online > QBO Connection**
2. Enter your Client ID and Client Secret
3. Click **Authorize** to connect to QBO
4. Once connected, use the import buttons to migrate data

## ETL Pipelines

The module uses the ETL Framework with the following pipelines:

| Pipeline | Target Model | QBO Entity | Dependencies |
|----------|-------------|------------|--------------|
| `qbo.account.importer` | `account.account` | Account | None |
| `qbo.customer.importer` | `res.partner` | Customer | None |
| `qbo.vendor.importer` | `res.partner` | Vendor | None |
| `qbo.item.importer` | `product.product` | Item | Account |
| `qbo.journal.entry.importer` | `account.move` | JournalEntry | Account |

## Rate Limiting

QBO API has a limit of 500 requests per minute. The module includes a `QBORateLimiter` class that:

- Tracks request timestamps within a sliding window
- Automatically waits when the limit is reached
- Logs when rate limiting is applied

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `QBO_CLIENT_ID` | OAuth2 Client ID | Yes |
| `QBO_CLIENT_SECRET` | OAuth2 Client Secret | Yes |
| `QBO_REALM_ID` | Company/Realm ID | No |
| `QBO_SANDBOX` | Use sandbox environment | No |
| `QBO_ACCESS_TOKEN` | Pre-configured access token | No |
| `QBO_REFRESH_TOKEN` | Pre-configured refresh token | No |

## Extending the Module

To add new pipelines, create a new file in `models/pipelines/` following this pattern:

```python
from odoo import models
from odoo.addons.etl_framework import ETL, ETLContext

@ETL.pipeline(
    target_model="your.model",
    importer_name="qbo.your.importer",
    sap_source="QBOEntity",
    depends_on=["qbo.account.importer"],
)
class QboYourImporter(models.AbstractModel):
    _name = "qbo.your.importer"
    _description = "QBO Your Importer"
    
    @ETL.extract("QBOEntity")
    def extract_data(self, ctx: ETLContext):
        api_client = ctx.get_config("api_client")
        return api_client.query_all(entity="QBOEntity")
    
    @ETL.transform()
    def transform_data(self, ctx: ETLContext, extracted):
        # Transform QBO data to Odoo format
        pass
    
    @ETL.load()
    def load_data(self, ctx: ETLContext, transformed):
        # Create/update Odoo records
        pass
```

## License

LGPL-3
