"""
Migration: Story Arc Manifests Schema

Idempotently creates the storyarc_manifests table and ensures required columns exist.
"""

from mylar import logger


def upgrade(cursor):
    """
    Apply the storyarc_manifests table and column migrations.

    :param cursor: sqlite3.Cursor instance
    """
    logger.fdebug("[EXTENSION-MIGRATION] Running storyarc_manifests migration.")
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS storyarc_manifests (
            StoryArcID TEXT UNIQUE,
            StoryArcName TEXT,
            SourceName TEXT,
            SourceType TEXT DEFAULT "upload",
            RepoURL TEXT,
            RepoCommit TEXT,
            RepoPath TEXT,
            SHA256 TEXT UNIQUE,
            ImportTime TEXT,
            TotalIssues INT,
            RawXMLPath TEXT
        )
        """
    )
    cols = [c[1] for c in cursor.execute("PRAGMA table_info(storyarc_manifests)").fetchall()]
    if "SourceType" not in cols:
        cursor.execute('ALTER TABLE storyarc_manifests ADD COLUMN SourceType TEXT DEFAULT "staged"')
        logger.fdebug("[EXTENSION-MIGRATION] Added column SourceType to storyarc_manifests.")
    if "RawXMLPath" not in cols:
        cursor.execute("ALTER TABLE storyarc_manifests ADD COLUMN RawXMLPath TEXT")
        logger.fdebug("[EXTENSION-MIGRATION] Added column RawXMLPath to storyarc_manifests.")
