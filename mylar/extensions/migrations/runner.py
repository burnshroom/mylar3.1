"""
Extension Migration Runner.

Executes registered extension migrations in sequence.
"""

from mylar import logger
from mylar.extensions.migrations.versions import (
    storyarc_manifests,
    creators,
    creator_identity_resolution,
    kavita_publisher_mappings
)

# Ordered list of migration modules
MIGRATIONS = [
    storyarc_manifests,
    creators,
    creator_identity_resolution,
    kavita_publisher_mappings,
]


def run_extension_migrations(cursor):
    """
    Execute all registered extension migrations using the provided database cursor.

    :param cursor: sqlite3.Cursor instance attached to the Mylar database
    """
    logger.fdebug("[EXTENSION-MIGRATIONS] Starting extension database checks.")
    for migration in MIGRATIONS:
        module_name = getattr(migration, "__name__", str(migration))
        try:
            if hasattr(migration, "upgrade"):
                migration.upgrade(cursor)
        except Exception as e:
            logger.warn(f"[EXTENSION-MIGRATIONS] Error applying migration '{module_name}': {e}")
            raise
    logger.fdebug("[EXTENSION-MIGRATIONS] Completed extension database checks.")
