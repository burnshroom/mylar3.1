"""
Migration: Kavita Publisher Mappings Foundation (Phase K5).

Idempotently creates the ext_kavita_publisher_mappings table with atomic
creation lease fields, revisions, exponential backoff tracking, and performance indexes.
"""

from mylar import logger


def upgrade(cursor):
    """
    Apply the ext_kavita_publisher_mappings table and indexes.

    :param cursor: sqlite3.Cursor instance attached to the Mylar database
    """
    logger.fdebug("[EXTENSION-MIGRATION] Running Kavita publisher mappings migration.")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS ext_kavita_publisher_mappings (
            MappingID                INTEGER PRIMARY KEY AUTOINCREMENT,
            MylarInstanceID          TEXT NOT NULL,
            PublisherKey             TEXT NOT NULL,
            PublisherDisplayName     TEXT NOT NULL,
            CanonicalPublisherPath   TEXT NOT NULL,
            KavitaServerUrl          TEXT NOT NULL,
            KavitaLibraryID          INTEGER DEFAULT NULL,
            ObservedLibraryName      TEXT DEFAULT NULL,
            LibraryTypeID            INTEGER DEFAULT NULL,
            Provenance               TEXT NOT NULL,
            MappingState             TEXT NOT NULL,
            LeaseToken               TEXT DEFAULT NULL,
            LeaseExpiresAt           TIMESTAMP DEFAULT NULL,
            Revision                 INTEGER NOT NULL DEFAULT 1,
            AttemptCount             INTEGER NOT NULL DEFAULT 0,
            NextEligibleRetryAt      TIMESTAMP DEFAULT NULL,
            LastScanQueuedAt         TIMESTAMP DEFAULT NULL,
            LastVerifiedAt           TIMESTAMP DEFAULT NULL,
            LastErrorCode            TEXT DEFAULT NULL,
            LastErrorMessage         TEXT DEFAULT NULL,
            LastErrorTimestamp       TIMESTAMP DEFAULT NULL,
            CreatedAt                TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UpdatedAt                TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # Unique constraint ensuring at most one mapping/lease record per instance, canonical path, and server URL
    cursor.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_ext_kavita_inst_path_server 
            ON ext_kavita_publisher_mappings (MylarInstanceID, CanonicalPublisherPath, KavitaServerUrl)
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ext_kavita_pub_key 
            ON ext_kavita_publisher_mappings (PublisherKey)
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ext_kavita_state 
            ON ext_kavita_publisher_mappings (MappingState)
        """
    )

    logger.fdebug("[EXTENSION-MIGRATION] Kavita publisher mappings migration completed.")
