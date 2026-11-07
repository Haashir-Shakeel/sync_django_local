import os
import sys
import threading
from pathlib import Path
from logger_utils import setup_logging, log_exception
import traceback

DEFAULT_SUPERUSER = {
    "username": "admin",
    "password": "admin",
    "email": "admin@example.com",
}

def fix_migrations_directory_name():
    """
    Fix migrations directory naming BEFORE Django loads settings.
    Ensures the environment variable points to 'external_migrations'.
    """
    log_info = setup_logging()
    combined_logger = log_info['combined_logger']
    combined_logger.log("FIX MIGRATION DIRECTORY FUNCTION", "INFO")
    try:
        current_external_root = os.environ.get(
            "DJANGO_MIGRATIONS_ROOT",
            str(Path(__file__).parent / "external_migrations")
        )
        current_external_dir = Path(current_external_root)

        # Rename if directory is 'migrations' instead of 'external_migrations'
        if current_external_dir.exists() and current_external_dir.name == 'migrations':
            new_external_dir = current_external_dir.parent / 'external_migrations'
            if not new_external_dir.exists():
                combined_logger.log(f"[STARTUP] Renaming migrations directory: {current_external_dir} -> {new_external_dir}", "INFO")
                current_external_dir.rename(new_external_dir)
                os.environ["DJANGO_MIGRATIONS_ROOT"] = str(new_external_dir)
                combined_logger.log(f"[STARTUP] Updated DJANGO_MIGRATIONS_ROOT to: {new_external_dir}", "INFO")

        # Ensure final env var points to external_migrations
        final_dir = Path(os.environ.get("DJANGO_MIGRATIONS_ROOT", str(Path(__file__).parent / "external_migrations")))
        if final_dir.name != 'external_migrations':
            correct_dir = final_dir.parent / 'external_migrations'
            os.environ["DJANGO_MIGRATIONS_ROOT"] = str(correct_dir)
            combined_logger.log(f"[STARTUP] Set DJANGO_MIGRATIONS_ROOT to: {correct_dir}", "INFO")

    except Exception as e:
        combined_logger.log(f"[STARTUP] Warning: Could not fix migrations directory name: {e}", "WARNING")
        default_dir = Path(__file__).parent / "external_migrations"
        os.environ["DJANGO_MIGRATIONS_ROOT"] = str(default_dir)
        combined_logger.log(f"[STARTUP] Using default migrations dir: {default_dir}", "WARNING")


def ensure_parent_dir(path_str: str):
    p = Path(path_str)
    p.parent.mkdir(parents=True, exist_ok=True)


def _create_default_superuser() -> None:
    from django.contrib.auth import get_user_model

    User = get_user_model()
    if not User.objects.filter(username=DEFAULT_SUPERUSER["username"]).exists():
        User.objects.create_superuser(**DEFAULT_SUPERUSER)
        print("[custom.py] Created default superuser 'admin'.")


def start_sync_loop(combined_logger):
    """Start the sync loop in a separate thread"""
    try:
        from django.core.management import call_command
        
        combined_logger.log("Starting sync loop thread", "INFO")
        
        call_command(
            'sync_loop',
            '--master', os.environ.get('SYNC_MASTER_URL', 'http://10.0.2.2:8000'),
            '--device-id', os.environ.get('SYNC_DEVICE_ID', 'POS-1'),
            '--batch-size', os.environ.get('SYNC_BATCH_SIZE', '200'),
            '--sleep-interval', os.environ.get('SYNC_SLEEP_INTERVAL', '30')
        )
    except Exception as exc:
        combined_logger.log(f"Sync loop error: {exc}", "ERROR")
        log_exception(sys.exc_info(), combined_logger)


def main():
    """Main function to run Django server with proper initialization order."""
    fix_migrations_directory_name()

    # Set Django settings module
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sync_django_local.settings")
    os.environ["CRYPTOGRAPHY_OPENSSL_NO_LEGACY"] = "1"

    db_path = os.environ.get("DJANGO_DB_PATH")
    if db_path:
        ensure_parent_dir(db_path)

    # Setup logging
    log_info = setup_logging()
    combined_logger = log_info['combined_logger']
    combined_logger.log("Django initialization starting", "INFO")

    try:
        # Import Django after setting environment variables
        import django
        from django.core.management import call_command

        combined_logger.log("Django imports successful", "INFO")
    
    except ImportError as exc:
        combined_logger.log(f"REAL Django import error: {repr(exc)}", "ERROR")
        combined_logger.log("Traceback:\n" + traceback.format_exc(), "ERROR")

        raise ImportError(
            "Couldn't import Django package itself. Check if Django is installed "
            "and available on PYTHONPATH."
        ) from exc
    
    try:
        # Django project/app setup
        combined_logger.log("Starting django.setup()", "INFO")
        combined_logger.log(f"DJANGO_SETTINGS_MODULE={os.environ.get('DJANGO_SETTINGS_MODULE')}", "INFO")
        combined_logger.log(f"PYTHONPATH/sys.path={sys.path}", "INFO")
        combined_logger.log(f"Current working directory={os.getcwd()}", "INFO")

        # Setup Django - this must be called before any Django operations
        django.setup()
        combined_logger.log("Django setup completed", "INFO")

    except ImportError as exc:
        combined_logger.log(f"ImportError during django.setup(): {repr(exc)}", "ERROR")
        combined_logger.log("This means Django imported successfully, but one of your settings/apps/models imports failed.", "ERROR")
        combined_logger.log("Traceback:\n" + traceback.format_exc(), "ERROR")
        raise
    except Exception as exc:
        combined_logger.log(f"Non-import error during django.setup(): {repr(exc)}", "ERROR")
        combined_logger.log("Traceback:\n" + traceback.format_exc(), "ERROR")
        raise

    combined_logger.log("Creating and applying migrations", "INFO")
    try:
        combined_logger.log("Creating migrations", "INFO")
        try:
            call_command(
                'makemigrations',
                'master',
                verbosity=1,
                interactive=False
            )
            combined_logger.log("Custom app migrations created successfully", "INFO")
        except Exception as make_exc:
            combined_logger.log(f"Custom makemigrations failed: {make_exc}, trying general makemigrations", "WARNING")
            call_command('makemigrations', verbosity=1, interactive=False)

        combined_logger.log("Applying all migrations", "INFO")
        call_command('migrate', verbosity=1, interactive=False)
        combined_logger.log("All migrations applied successfully", "INFO")

    except Exception as exc:
        combined_logger.log(f"Migrations handler error: {exc}", "ERROR")
        try:
            combined_logger.log("Trying basic migration approach", "WARNING")
            call_command('makemigrations', verbosity=1, interactive=False)
            call_command('migrate', verbosity=1, interactive=False)
            combined_logger.log("Basic migrations completed", "INFO")
        except Exception as fallback_exc:
            combined_logger.log(f"Basic migrations also failed: {fallback_exc}", "ERROR")

        
    combined_logger.log("Creating default superuser", "INFO")
    try:
        _create_default_superuser()
    except Exception as exc:
        combined_logger.log(f"Default superuser creation error: {exc}", "ERROR")
        # Don't raise here, continue to try running server

    combined_logger.log("Starting sync loop in background thread", "INFO")
    try:
        sync_thread = threading.Thread(
            target=start_sync_loop,
            args=(combined_logger,),
            daemon=True,
            name="SyncLoopThread"
        )
        sync_thread.start()
        combined_logger.log("Sync loop thread started successfully", "INFO")
    except Exception as exc:
        combined_logger.log(f"Failed to start sync loop thread: {exc}", "ERROR")
        log_exception(sys.exc_info(), combined_logger)


    combined_logger.log("Starting Django server on port 8001", "INFO")
    try:
        # Use runserver with proper arguments for serious-python environment
         # Local runs on port 8001, master runs on 8000
        call_command('runserver', '0.0.0.0:8001', '--noreload', verbosity=1)
    except Exception as exc:
        combined_logger.log(f"Server startup error: {exc}", "ERROR")
        raise


main()