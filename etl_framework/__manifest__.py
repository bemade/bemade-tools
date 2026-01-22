{
    "name": "ETL Framework",
    "version": "2.1.0",
    "category": "Technical",
    "summary": "Declarative, self-optimizing ETL framework for Odoo",
    "description": """
ETL Framework for Odoo
======================

A declarative, self-optimizing ETL (Extract, Transform, Load) framework
for migrating data into Odoo from external sources.

Key Features:

* Declarative pipeline definition using decorators
* Automatic multiprocessing based on data volume
* Dependency resolution between models
* Memory-efficient execution
* Built-in retry logic for serialization failures
* Clear separation of Extract, Transform, and Load phases

See the module README for usage examples.
    """,
    "author": "Bemade",
    "website": "https://www.bemade.org",
    "license": "LGPL-3",
    "depends": ["base"],
    "data": [],
    "installable": True,
    "auto_install": False,
}
