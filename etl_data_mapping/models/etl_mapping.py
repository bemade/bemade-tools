# -*- coding: utf-8 -*-
"""ETL Mapping - Abstract base class for ETL data mappings

This module provides the base functionality for ETL mappings between
external databases and Odoo models using pure Python classes (not Odoo models).
"""

import logging
from abc import ABC
from odoo.tools.sql import SQL
from ..fields.etl_field import ETLField

_logger = logging.getLogger(__name__)


class ETLMapping(ABC):
    """Abstract base class for ETL mappings (Pure Python, not an Odoo model)

    This class provides the base functionality for ETL (Extract, Transform, Load)
    operations. Subclasses should define:
        - _source_table: name of the source table
        - _target_model: name of the target Odoo model
        - _source_schema: optional schema name for source table
        - _external_database_xmlid: XML ID of the external database config
        And ETLField class attributes for field mappings
    
    Usage:
        class SAPProductMapping(ETLMapping):
            _source_table = 'oitm'
            _external_database_xmlid = 'sap.external_db'
            _target_model = 'product.product'
            
            itemcode = ETLField(source='itemcode', target='default_code')
        
        # Run migration
        SAPProductMapping.run_migration(env)
        
        # Or run all mappings in dependency order
        ETLMapping.run_all_migrations(env)
    """

    # Required class attributes (subclasses must override)
    _source_table: str = None
    _target_model: str = None
    _external_database_xmlid: str = None
    
    # Optional class attributes
    _source_schema: str = None

    @classmethod
    def _get_external_database(cls, env):
        """Get the external database record

        Args:
            env: Odoo environment

        Returns:
            etl.external.database record
        """
        if not cls._external_database_xmlid:
            raise ValueError(
                f"No _external_database_xmlid defined on {cls.__name__}. "
                f"Set _external_database_xmlid class attribute."
            )

        return env.ref(cls._external_database_xmlid)

    @classmethod
    def _get_external_cursor(cls, env):
        """Get a cursor to the external database

        Args:
            env: Odoo environment

        Returns:
            Database cursor for external database
        """
        external_db = cls._get_external_database(env)
        return external_db.get_cursor()

    @classmethod
    def _get_etl_fields(cls):
        """Get all ETLField attributes defined on this class

        Returns:
            dict: {field_name: field_object} for all ETLFields
        """
        etl_fields = {}
        for attr_name in dir(cls):
            if attr_name.startswith('_'):
                continue
            attr = getattr(cls, attr_name)
            if isinstance(attr, ETLField):
                etl_fields[attr_name] = attr
        return etl_fields

    @classmethod
    def _build_select_query(cls, context=None):
        """Build SELECT query from ETLField definitions

        Args:
            context (dict): Optional context with query modifiers:
                - source_filter: WHERE clause condition (SQL object or string)
                - order_by: ORDER BY clause
                - limit: LIMIT clause value
                - offset: OFFSET clause value

        Returns:
            SQL: SQL object with query and parameters
        """
        context = context or {}
        etl_fields = cls._get_etl_fields()

        if not etl_fields:
            raise ValueError(f"No ETLField attributes defined on {cls.__name__}")

        # Build SELECT clause
        select_fields = []
        for field_name, field in etl_fields.items():
            source_field = field.source or field_name
            select_fields.append(SQL.identifier(source_field))

        select_clause = SQL("SELECT %s", SQL(", ").join(select_fields))

        # Build FROM clause
        if cls._source_schema:
            from_clause = SQL(
                " FROM %s.%s",
                SQL.identifier(cls._source_schema),
                SQL.identifier(cls._source_table),
            )
        else:
            from_clause = SQL(" FROM %s", SQL.identifier(cls._source_table))

        # Start building the query
        query_parts = [select_clause, from_clause]

        # Add WHERE clause if provided
        source_filter = context.get("source_filter")
        if source_filter:
            if isinstance(source_filter, SQL):
                where_clause = SQL(" WHERE %s", source_filter)
            else:
                # Legacy string support
                where_clause = SQL(" WHERE " + str(source_filter))
            query_parts.append(where_clause)

        # Add ORDER BY clause if provided
        order_by = context.get("order_by")
        if order_by:
            if isinstance(order_by, SQL):
                order_clause = SQL(" ORDER BY %s", order_by)
            else:
                order_clause = SQL(" ORDER BY " + str(order_by))
            query_parts.append(order_clause)

        # Add LIMIT clause if provided
        limit = context.get("limit")
        if limit:
            limit_clause = SQL(" LIMIT %s", limit)
            query_parts.append(limit_clause)

        # Add OFFSET clause if provided
        offset = context.get("offset")
        if offset:
            offset_clause = SQL(" OFFSET %s", offset)
            query_parts.append(offset_clause)

        # Combine all parts
        return SQL("").join(query_parts)

    @classmethod
    def extract(cls, env, **context):
        """Extract data from external database

        This method:
        1. Builds a SELECT query from ETLField definitions
        2. Executes the query on the external database
        3. Returns the results as a list of dictionaries

        Args:
            env: Odoo environment
            **context: Context parameters:
                - source_filter: WHERE clause condition
                - order_by: ORDER BY clause
                - limit: LIMIT clause value
                - offset: OFFSET clause value

        Returns:
            list: List of dictionaries with extracted data
        """
        # Build the query with context parameters
        query = cls._build_select_query(context=context)

        # Execute on external database
        with cls._get_external_cursor(env) as cr:
            _logger.info(f"Extracting data from {cls._source_table}")
            # SQL object provides .code and .params
            cr.execute(query.code, query.params)
            results = cr.dictfetchall()
            _logger.info(f"Extracted {len(results)} records")
            return results

    @classmethod
    def transform(cls, extracted_data):
        """Transform extracted data using ETLField transforms

        Args:
            extracted_data (list): List of dicts from extract()

        Returns:
            list: List of dicts with transformed data ready for load()
        """
        etl_fields = cls._get_etl_fields()
        transformed_data = []

        for source_record in extracted_data:
            target_record = {}

            for field_name, field in etl_fields.items():
                source_field = field.source or field_name
                target_field = field.target or field_name

                # Get source value
                source_value = source_record.get(source_field)

                # Apply transform if defined
                if field.transform and source_value is not None:
                    try:
                        transformed_value = field.transform(source_value)
                    except Exception as e:
                        _logger.error(
                            f"Error transforming {source_field}: {e}. "
                            f"Source value: {source_value}"
                        )
                        raise
                else:
                    transformed_value = source_value

                target_record[target_field] = transformed_value

            transformed_data.append(target_record)

        return transformed_data

    @classmethod
    def load(cls, env, transformed_data, batch_size=100):
        """Load transformed data into target Odoo model

        Args:
            env: Odoo environment
            transformed_data (list): List of dicts from transform()
            batch_size (int): Number of records to create per batch

        Returns:
            recordset: Created records
        """
        if not cls._target_model:
            raise ValueError(f"No _target_model defined on {cls.__name__}")

        if not transformed_data:
            _logger.info(f"No data to load for {cls.__name__}")
            return env[cls._target_model].browse()

        target_model = env[cls._target_model]
        created_records = env[cls._target_model].browse()

        _logger.info(
            f"Loading {len(transformed_data)} records into {cls._target_model}"
        )

        # Process in batches
        for i in range(0, len(transformed_data), batch_size):
            batch = transformed_data[i : i + batch_size]

            for record_vals in batch:
                # Basic duplicate check - skip if record exists
                # Subclasses can override this logic
                if "default_code" in record_vals:
                    existing = target_model.search(
                        [("default_code", "=", record_vals["default_code"])], limit=1
                    )
                    if existing:
                        _logger.debug(
                            f"Skipping duplicate: {record_vals.get('default_code')}"
                        )
                        continue

                created_record = target_model.create(record_vals)
                created_records |= created_record

            _logger.info(f"Loaded batch {i // batch_size + 1}")

        _logger.info(f"Successfully loaded {len(created_records)} records")
        return created_records

    @classmethod
    def extract_and_transform(cls, env, **context):
        """Convenience method: extract + transform

        Args:
            env: Odoo environment
            **context: Context parameters for extract()

        Returns:
            list: Transformed data ready for load()
        """
        extracted = cls.extract(env, **context)
        return cls.transform(extracted)

    @classmethod
    def run_migration(cls, env, **context):
        """Run complete ETL pipeline: extract → transform → load

        Args:
            env: Odoo environment
            **context: Context parameters for extract()

        Returns:
            dict: Statistics about the migration
        """
        _logger.info(f"Starting migration for {cls.__name__}")

        # Extract
        extracted_data = cls.extract(env, **context)
        _logger.info(f"Extracted {len(extracted_data)} records")

        # Transform
        transformed_data = cls.transform(extracted_data)
        _logger.info(f"Transformed {len(transformed_data)} records")

        # Load
        created_records = cls.load(env, transformed_data)
        _logger.info(f"Loaded {len(created_records)} records")

        return {
            "extracted_count": len(extracted_data),
            "transformed_count": len(transformed_data),
            "loaded_count": len(created_records),
            "mapping_class": cls.__name__,
        }

    @classmethod
    def get_dependencies(cls):
        """Get list of mapping classes this mapping depends on
        
        Analyzes ETLField attributes with 'relation' to determine dependencies.
        
        Returns:
            list: List of ETLMapping subclasses this depends on
        """
        # TODO: Implement dependency analysis
        # For now, return empty list
        return []

    @classmethod
    def run_all_migrations(cls, env, **context):
        """Run all mapping migrations in dependency order
        
        Args:
            env: Odoo environment
            **context: Context parameters for extract()
        
        Returns:
            list: List of migration statistics for each mapping
        """
        # TODO: Implement topological sort based on dependencies
        # For now, just run all direct subclasses
        all_mappings = cls.__subclasses__()
        results = []
        
        for mapping_cls in all_mappings:
            result = mapping_cls.run_migration(env, **context)
            results.append(result)
        
        return results
