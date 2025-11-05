"""ETL Field - Custom field type for ETL mappings

This module extends Odoo's field system to support ETL field mappings.
"""

from odoo import fields, models


class ETLField(fields.Field):
    """Custom field type for ETL data mappings

    This field type is used to define mappings between source database fields
    and target Odoo model fields, with optional transformations.

    Parameters:
        source (str): Source column name in external database
        target (str): Target field name in Odoo model
        transform (callable or str): Optional transformation function
        relation (str): Optional related Odoo model for FK resolution
        relation_field (str): Field in related model to match on
        compute (callable): Optional compute function (no source)
        condition (callable): Only apply field if condition returns True
        source_relation (str): External table this FK points to (documentation)
        source_relation_field (str): Field in external table (documentation)
    """

    type = "etl_field"
    _column_type = None  # ETL fields don't create database columns

    def __init__(
        self,
        source=None,
        target=None,
        transform=None,
        relation=None,
        relation_field=None,
        compute=None,
        condition=None,
        source_relation=None,
        source_relation_field=None,
        **kwargs
    ):
        """Initialize ETL field with mapping parameters"""
        super().__init__(**kwargs)

        # Core mapping attributes
        self.source = source
        self.target = target
        self.transform = transform

        # Relation attributes (for FK resolution)
        self.relation = relation
        self.relation_field = relation_field

        # Computed field attributes
        self.compute = compute

        # Conditional application
        self.condition = condition

        # Documentation attributes (for external schema)
        self.source_relation = source_relation
        self.source_relation_field = source_relation_field


    def create_column(self, model, cr):  # pylint: disable=unused-argument
        """ETL fields don't create database columns"""
        pass

    def update_db(self, model, columns):  # pylint: disable=unused-argument
        """ETL fields don't update the database"""
        pass


# Register ETLField in the fields module so it can be imported
fields.ETLField = ETLField  # type: ignore[attr-defined]
