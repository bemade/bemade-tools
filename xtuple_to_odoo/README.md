# xTuple to Odoo Migration

This module provides tools to migrate data from xTuple ERP to Odoo. It currently supports:

* Customers
* Vendors
* Contacts
* Shipping Addresses

## Configuration

1. Install the module
2. Go to Settings > Technical > xTuple Migration > xTuple Databases
3. Create a new database connection with your xTuple PostgreSQL credentials
4. Use the import buttons to migrate data

## Environment Variables

The module supports the following environment variables for configuring database connections:

- `XTUPLE_HOST`: xTuple database host
- `XTUPLE_DBNAME`: xTuple database name
- `XTUPLE_USER`: xTuple database username
- `XTUPLE_PASSWORD`: xTuple database password
- `XTUPLE_PORT`: xTuple database port (default: 5432)
- `XTUPLE_SCHEMA`: xTuple database schema (default: 'public')

These environment variables will be used as default values when creating a new database connection. For security reasons, no default values are provided for host, database name, username, or password - these must be explicitly configured either through environment variables or in the user interface.

## Command Line Usage

You can run the import operations directly from the Odoo shell. Here are the commands for different import operations:

### Setup Environment Variables

```bash
export XTUPLE_HOST="your_xtuple_host"
export XTUPLE_DBNAME="your_xtuple_database"
export XTUPLE_USER="your_xtuple_username"
export XTUPLE_PASSWORD="your_xtuple_password"
export XTUPLE_PORT="5432"  # Default PostgreSQL port
export XTUPLE_SCHEMA="public"  # Default schema
```

### Start Odoo Shell

```bash
cd /path/to/odoo
./odoo-bin shell -d your_odoo_database
```

### Import Commands

Once in the Odoo shell, you can run the following commands:

#### Create or Get Database Connection

```python
# Create or get the xTuple database connection
XtupleDB = env['xtuple.database']
connection = XtupleDB.search([], limit=1)
if not connection:
    connection = XtupleDB.create({
        'database_host': os.environ.get('XTUPLE_HOST'),
        'database_name': os.environ.get('XTUPLE_DBNAME'),
        'database_username': os.environ.get('XTUPLE_USER'),
        'database_password': os.environ.get('XTUPLE_PASSWORD'),
        'database_port': int(os.environ.get('XTUPLE_PORT', '5432')),
        'database_schema': os.environ.get('XTUPLE_SCHEMA', 'public'),
    })
```

#### Import Partners (Customers, Vendors, Contacts)

```python
# Import all partners
with connection.get_cursor() as cr:
    env["xtuple.res.partner.importer"].with_company(env.company).import_partners_concurrent(cr)
```

#### Import Shipping Addresses

```python
# Import shipping addresses
with connection.get_cursor() as cr:
    env["xtuple.res.partner.importer"].with_company(env.company)._import_shipping_addresses_concurrent(cr)
```

#### Import Everything

```python
# Import all data
with connection.get_cursor() as cr:
    # Import partners
    env["xtuple.res.partner.importer"].with_company(env.company).import_partners_concurrent(cr)
    # Import shipping addresses
    env["xtuple.res.partner.importer"].with_company(env.company)._import_shipping_addresses_concurrent(cr)
```

## Technical Details

The migration process works by:

1. Connecting to the xTuple PostgreSQL database
2. Extracting data from relevant tables (custinfo, vendinfo, cntct, addr, shiptoinfo)
3. Converting the data to Odoo's format
4. Creating the corresponding records in Odoo
5. Establishing relationships between the records (parent-child, etc.)

The module uses a concurrent processing approach to handle large datasets efficiently.

## Future Enhancements

Future versions will include migration of:
- Products
- Bills of Materials
- Manufacturing Orders
- Invoices
- Purchase Orders
- Sales Orders

## Database Schema

The module assumes a standard xTuple database schema with the following tables:
- `custinfo`: Customer information
- `vendinfo`: Vendor information
- `cntct`: Contact information
- `addr`: Address information
- `shiptoinfo`: Shipping address information
