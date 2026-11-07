import os
import shutil
from pathlib import Path

def sync_app_migrations_to_persistent_root(combined_logger):
    """
    Automatically copy all app-level migrations (inside /flet/app/apps/*/migrations)
    into the persistent /migrations directory (DJANGO_MIGRATIONS_ROOT).
    This ensures that SeriousPython deployments retain migrations between updates.
    """

    migrations_root = os.environ.get("DJANGO_MIGRATIONS_ROOT")
    if not migrations_root:
        combined_logger.log("No DJANGO_MIGRATIONS_ROOT found — skipping migration sync", "WARNING")
        return

    base_app_path = Path("/data/data/com.example.serious_python_sample/files/flet/app/apps")
    dest_root = Path(migrations_root)
    dest_root.mkdir(parents=True, exist_ok=True)

    combined_logger.log(f"Syncing app migrations → {dest_root}", "INFO")

    for app_dir in base_app_path.iterdir():
        if not app_dir.is_dir():
            continue

        src_migrations = app_dir / "migrations"
        dest_migrations = dest_root / app_dir.name

        if src_migrations.exists():
            dest_migrations.mkdir(parents=True, exist_ok=True)

            # Copy only missing or newer files
            for src_file in src_migrations.glob("*.py"):
                dest_file = dest_migrations / src_file.name

                if not dest_file.exists() or src_file.stat().st_mtime > dest_file.stat().st_mtime:
                    shutil.copy2(src_file, dest_file)
                    combined_logger.log(f"Copied {src_file} → {dest_file}", "INFO")

    combined_logger.log("Migration sync completed.", "INFO")
