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
- Declarative pipeline definition using decorators
- Automatic multiprocessing based on data volume
- Dependency resolution between models
- Memory-efficient execution
- Built-in retry logic for serialization failures
- Clear separation of Extract, Transform, and Load phases

Usage:
    from odoo.addons.etl_framework import ETL, ETLContext

    @ETL.pipeline(
        target_model='product.product',
        importer_name='my.product.importer',
        sap_source='products',
        depends_on=['product.category'],
    )
    class MyProductImporter(models.AbstractModel):
        _name = 'my.product.importer'

        @ETL.extract('products')
        def extract_products(self, ctx):
            ...

        @ETL.transform()
        def transform_products(self, ctx, extracted):
            ...

        @ETL.load()
        def load_products(self, ctx, transformed):
            ...

    """,
    "author": "Bemade",
    "website": "https://www.bemade.org",
    "license": "LGPL-3",
    "depends": ["base"],
    "data": [],
    "installable": True,
    "auto_install": False,
}
