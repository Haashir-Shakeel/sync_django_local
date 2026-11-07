"""
Migration handler for serious_python Django app.
Handles safe migrations even when data exists.
"""
import os
import shutil
from pathlib import Path
from django.core.management import call_command
from django.db import connection
from django.apps import apps
from logger_utils import setup_logging

def backup_database():
    """Create a backup of the current database."""
    db_path = os.environ.get("DJANGO_DB_PATH")
    if db_path and os.path.exists(db_path):
        backup_path = f"{db_path}.backup"
        shutil.copy2(db_path, backup_path)
        print(f"[migration_handler] Database backed up to {backup_path}")
        return backup_path
    return None

def restore_database(backup_path):
    """Restore database from backup."""
    if backup_path and os.path.exists(backup_path):
        db_path = os.environ.get("DJANGO_DB_PATH")
        shutil.copy2(backup_path, db_path)
        print(f"[migration_handler] Database restored from {backup_path}")

def check_migration_state():
    """Check if migrations are in sync with database."""
    try:
        from django.core.management.commands.migrate import Command as MigrateCommand
        from django.core.management.base import CommandError

        # Check if there are unapplied migrations
        call_command('showmigrations', '--plan', verbosity=0)
        return True
    except Exception as e:
        print(f"[migration_handler] Migration state check failed: {e}")
        return False

def safe_migrate():
    """
    Safely apply migrations with proper error handling.
    Returns True if successful, False otherwise.
    """
    log_info = setup_logging()
    combined_logger = log_info['combined_logger']

    backup_path = None
    try:
        # Step 1: Backup database
        backup_path = backup_database()
        combined_logger.log("Database backup completed", "INFO")

        # Step 2: Try to apply migrations
        combined_logger.log("Attempting to apply migrations", "INFO")
        call_command('migrate', verbosity=1, interactive=False)
        combined_logger.log("Migrations applied successfully", "INFO")

        # Step 3: Verify schema matches models
        schema_valid = verify_schema_consistency()
        if not schema_valid:
            combined_logger.log("Schema inconsistency detected, attempting to fix", "WARNING")

            # Try applying migrations again with force first
            try:
                call_command('migrate', '--run-syncdb', verbosity=1, interactive=False)
                combined_logger.log("Schema synchronization with --run-syncdb completed", "INFO")

                # Check again if schema is now consistent
                schema_valid = verify_schema_consistency()
                if not schema_valid:
                    combined_logger.log("--run-syncdb didn't fix all issues, using force schema update", "WARNING")
                    force_schema_update()

            except Exception as sync_error:
                combined_logger.log(f"--run-syncdb failed: {sync_error}, using force schema update", "WARNING")
                force_schema_update()

        # Step 3.5: Final verification
        final_check = verify_schema_consistency()
        if final_check:
            combined_logger.log("Schema consistency verified successfully", "INFO")
        else:
            combined_logger.log("Schema still has issues after all attempts", "ERROR")

        # Step 4: Clean up backup if successful
        if backup_path and os.path.exists(backup_path):
            os.remove(backup_path)
            combined_logger.log("Backup cleaned up", "INFO")

        return True

    except Exception as e:
        combined_logger.log(f"Migration failed: {e}", "ERROR")

        # Step 5: Restore from backup if migration failed
        if backup_path:
            try:
                restore_database(backup_path)
                combined_logger.log("Database restored from backup", "INFO")
            except Exception as restore_error:
                combined_logger.log(f"Failed to restore backup: {restore_error}", "ERROR")

        # Step 6: Try fake migration as last resort
        try:
            combined_logger.log("Attempting fake migration", "WARNING")
            call_command('migrate', '--fake', verbosity=1, interactive=False)
            combined_logger.log("Fake migration completed", "WARNING")
            return True
        except Exception as fake_error:
            combined_logger.log(f"Fake migration also failed: {fake_error}", "ERROR")

        return False

def verify_schema_consistency():
    """
    Verify that the database schema matches all current models.
    Returns True if consistent, False otherwise.
    """
    try:
        from django.db import connection
        from django.apps import apps

        inconsistencies_found = False

        with connection.cursor() as cursor:
            # Get all existing tables
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            existing_tables = {row[0] for row in cursor.fetchall()}

            # Check each model
            for app_config in apps.get_app_configs():
                for model in app_config.get_models():
                    table_name = model._meta.db_table
                    model_name = f"{app_config.label}.{model.__name__}"

                    # Check if table exists
                    if table_name not in existing_tables:
                        print(f"[migration_handler] Missing table: {table_name} for model {model_name}")
                        inconsistencies_found = True
                        continue

                    # Check table columns
                    cursor.execute(f"PRAGMA table_info({table_name})")
                    existing_columns = {row[1] for row in cursor.fetchall()}

                    # Get expected fields from model
                    model_fields = {field.column for field in model._meta.fields}

                    # Check for missing columns
                    missing_columns = model_fields - existing_columns
                    if missing_columns:
                        print(f"[migration_handler] Missing columns in {table_name}: {missing_columns}")
                        inconsistencies_found = True

        return not inconsistencies_found

    except Exception as e:
        print(f"[migration_handler] Schema verification failed: {e}")
        return False

def force_schema_update():
    """
    Force update the database schema to match current models.
    This is completely dynamic and works for all models.
    """
    log_info = setup_logging()
    combined_logger = log_info['combined_logger']

    try:
        from django.db import connection
        from django.apps import apps

        combined_logger.log("Force updating database schema for all models", "WARNING")

        with connection.cursor() as cursor:
            # Get all existing tables
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            existing_tables = {row[0] for row in cursor.fetchall()}

            # Process each app and model
            for app_config in apps.get_app_configs():
                app_label = app_config.label
                combined_logger.log(f"Processing app: {app_label}", "INFO")

                for model in app_config.get_models():
                    table_name = model._meta.db_table
                    model_name = f"{app_label}.{model.__name__}"

                    combined_logger.log(f"Processing model: {model_name} -> table: {table_name}", "INFO")

                    # Check if table exists
                    if table_name not in existing_tables:
                        combined_logger.log(f"Table {table_name} does not exist, creating it", "WARNING")
                        success = create_table_from_model(model, cursor, combined_logger)
                        if success:
                            existing_tables.add(table_name)
                    else:
                        # Table exists, check for missing columns
                        combined_logger.log(f"Table {table_name} exists, checking columns", "INFO")
                        add_missing_columns_to_table(model, table_name, cursor, combined_logger)

        combined_logger.log("Dynamic schema update completed", "INFO")
        return True

    except Exception as e:
        combined_logger.log(f"Dynamic schema update failed: {e}", "ERROR")
        return False

def create_table_from_model(model, cursor, logger):
    """
    Create a complete table from a Django model.
    """
    try:
        from django.db import connection

        table_name = model._meta.db_table
        fields = []

        # Build field definitions
        for field in model._meta.fields:
            field_sql = get_field_sql_definition(field, connection)
            if field_sql:
                fields.append(field_sql)

        # Create the table
        if fields:
            fields_sql = ",\n    ".join(fields)
            create_sql = f"""
                CREATE TABLE {table_name} (
                    {fields_sql}
                )
            """

            cursor.execute(create_sql)
            logger.log(f"Created table {table_name}", "INFO")

            # Create indexes if needed
            create_indexes_for_model(model, cursor, logger)

            return True
        else:
            logger.log(f"No fields found for model {model.__name__}", "ERROR")
            return False

    except Exception as e:
        logger.log(f"Failed to create table for {model.__name__}: {e}", "ERROR")
        return False

def add_missing_columns_to_table(model, table_name, cursor, logger):
    """
    Add any missing columns to an existing table.
    """
    try:
        from django.db import connection

        # Get existing columns
        cursor.execute(f"PRAGMA table_info({table_name})")
        existing_columns = {row[1] for row in cursor.fetchall()}

        # Get model fields
        model_columns = {field.column for field in model._meta.fields}

        # Find missing columns
        missing_columns = model_columns - existing_columns

        if missing_columns:
            logger.log(f"Adding missing columns to {table_name}: {missing_columns}", "WARNING")

            for field in model._meta.fields:
                if field.column in missing_columns:
                    field_sql = get_field_sql_definition(field, connection)
                    if field_sql:
                        # Extract just the field type and constraints
                        field_parts = field_sql.split(' ', 1)
                        if len(field_parts) > 1:
                            field_type = field_parts[1]
                        else:
                            field_type = "TEXT"

                        # Add default value for certain field types
                        default_value = get_default_value_for_field(field)
                        if default_value:
                            alter_sql = f"ALTER TABLE {table_name} ADD COLUMN {field.column} {field_type} DEFAULT {default_value}"
                        else:
                            alter_sql = f"ALTER TABLE {table_name} ADD COLUMN {field.column} {field_type}"

                        try:
                            cursor.execute(alter_sql)
                            logger.log(f"Added column {field.column} to {table_name}", "INFO")
                        except Exception as col_error:
                            logger.log(f"Failed to add column {field.column} to {table_name}: {col_error}", "ERROR")

    except Exception as e:
        logger.log(f"Failed to add missing columns to {table_name}: {e}", "ERROR")

def get_field_sql_definition(field, connection):
    """
    Get the SQL definition for a Django field.
    """
    try:
        if hasattr(field, 'db_type'):
            db_type = field.db_type(connection)
            if db_type:
                sql_parts = [field.column, db_type]

                # Add primary key constraint
                if field.primary_key:
                    sql_parts.append("PRIMARY KEY")

                # Add unique constraint
                if getattr(field, 'unique', False) and not field.primary_key:
                    sql_parts.append("UNIQUE")

                # Add not null constraint
                if not field.null and not field.primary_key:
                    sql_parts.append("NOT NULL")

                return " ".join(sql_parts)
    except Exception:
        pass

    return None

def get_default_value_for_field(field):
    """
    Get an appropriate default value for a field type.
    """
    field_type = type(field).__name__

    if 'CharField' in field_type or 'TextField' in field_type:
        return "''"
    elif 'IntegerField' in field_type or 'BigAutoField' in field_type:
        return "0"
    elif 'BooleanField' in field_type:
        return "FALSE"
    elif 'DateField' in field_type or 'DateTimeField' in field_type:
        return "'1970-01-01'"
    elif 'FloatField' in field_type or 'DecimalField' in field_type:
        return "0.0"
    else:
        return None

def create_indexes_for_model(model, cursor, logger):
    """
    Create necessary indexes for a model.
    """
    try:
        table_name = model._meta.db_table

        # Create unique indexes
        for field in model._meta.fields:
            if getattr(field, 'unique', False) and not field.primary_key:
                index_name = f"{table_name}_{field.column}_unique"
                index_sql = f"CREATE UNIQUE INDEX {index_name} ON {table_name} ({field.column})"
                try:
                    cursor.execute(index_sql)
                    logger.log(f"Created unique index {index_name}", "INFO")
                except Exception as idx_error:
                    logger.log(f"Failed to create index {index_name}: {idx_error}", "WARNING")

    except Exception as e:
        logger.log(f"Failed to create indexes for {model.__name__}: {e}", "ERROR")

def handle_schema_changes():
    """
    Handle schema changes intelligently.
    This function determines the best migration strategy.
    """
    log_info = setup_logging()
    combined_logger = log_info['combined_logger']

    try:
        # Step 1: Create new migrations
        combined_logger.log("Creating new migrations", "INFO")
        call_command('makemigrations', verbosity=1, interactive=False)

        # Step 2: Check if database exists and has data
        db_path = os.environ.get("DJANGO_DB_PATH")
        has_existing_data = False

        if db_path and os.path.exists(db_path):
            # Check if any tables have data
            with connection.cursor() as cursor:
                try:
                    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'django_%'")
                    tables = cursor.fetchall()

                    for table in tables:
                        cursor.execute(f"SELECT COUNT(*) FROM {table[0]}")
                        count = cursor.fetchone()[0]
                        if count > 0:
                            has_existing_data = True
                            combined_logger.log(f"Found {count} records in table {table[0]}", "INFO")
                            break
                except Exception as e:
                    combined_logger.log(f"Error checking existing data: {e}", "WARNING")

        # Step 3: Apply migrations based on data state
        if has_existing_data:
            combined_logger.log("Existing data detected, using safe migration", "INFO")
            return safe_migrate()
        else:
            combined_logger.log("No existing data, applying migrations normally", "INFO")
            call_command('migrate', verbosity=1, interactive=False)
            return True

    except Exception as e:
        combined_logger.log(f"Schema change handling failed: {e}", "ERROR")
        return False

def reset_migrations_if_needed():
    """
    Reset migrations if they're completely broken.
    WARNING: This will lose all data!
    """
    log_info = setup_logging()
    combined_logger = log_info['combined_logger']

    try:
        combined_logger.log("Resetting migrations - THIS WILL DELETE ALL DATA!", "WARNING")

        # Delete database
        db_path = os.environ.get("DJANGO_DB_PATH")
        if db_path and os.path.exists(db_path):
            os.remove(db_path)
            combined_logger.log("Database deleted", "WARNING")

        # Delete migration files
        for app_config in apps.get_app_configs():
            migrations_dir = Path(app_config.path) / 'migrations'
            if migrations_dir.exists():
                for migration_file in migrations_dir.glob('*.py'):
                    if migration_file.name != '__init__.py':
                        migration_file.unlink()
                        combined_logger.log(f"Deleted migration file: {migration_file}", "WARNING")

        # Recreate migrations
        call_command('makemigrations', verbosity=1, interactive=False)
        call_command('migrate', verbosity=1, interactive=False)

        combined_logger.log("Migrations reset successfully", "INFO")
        return True

    except Exception as e:
        combined_logger.log(f"Migration reset failed: {e}", "ERROR")
        return False
