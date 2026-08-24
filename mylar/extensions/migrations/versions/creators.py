"""
Migration: Creator Indexing & Provenance Schema (Phase C2)

Idempotently creates extension-owned tables and performance indexes for local creator indexing.
"""

from mylar import logger


def upgrade(cursor):
    """
    Apply the ext_creator_* tables and indexes.

    :param cursor: sqlite3.Cursor instance attached to the Mylar database
    """
    logger.fdebug("[EXTENSION-MIGRATION] Running creators extension migration.")

    # 1. Observed Raw Name Records
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS ext_creator_name_records (
            NameRecordID        INTEGER PRIMARY KEY AUTOINCREMENT,
            RawName             TEXT UNIQUE NOT NULL,
            NormalizedName      TEXT NOT NULL,
            NameSlug            TEXT NOT NULL,
            CreatorEntityID     INTEGER DEFAULT NULL,
            ResolutionSource    TEXT NOT NULL DEFAULT 'unresolved',
            CreatedAt           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UpdatedAt           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # 2. Confirmed Creator Entities
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS ext_creator_entities (
            CreatorEntityID     INTEGER PRIMARY KEY AUTOINCREMENT,
            DisplayName         TEXT NOT NULL,
            NormalizedName      TEXT NOT NULL,
            EntitySlug          TEXT UNIQUE NOT NULL,
            Notes               TEXT DEFAULT NULL,
            CreatedAt           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UpdatedAt           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # 3. Creator Name Aliases & Variations
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS ext_creator_aliases (
            AliasID             INTEGER PRIMARY KEY AUTOINCREMENT,
            CreatorEntityID     INTEGER NOT NULL,
            RawAlias            TEXT NOT NULL,
            NormalizedAlias     TEXT NOT NULL,
            Source              TEXT NOT NULL DEFAULT 'user',
            CreatedAt           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(CreatorEntityID, NormalizedAlias)
        )
        """
    )

    # 4. Provider-Neutral External ID Mappings
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS ext_creator_external_ids (
            ExternalMappingID   INTEGER PRIMARY KEY AUTOINCREMENT,
            CreatorEntityID     INTEGER NOT NULL,
            Provider            TEXT NOT NULL,
            ExternalID          TEXT NOT NULL,
            ExternalURL         TEXT DEFAULT NULL,
            SourceVersion       TEXT DEFAULT NULL,
            ObservedAt          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            Confidence          REAL DEFAULT 1.0,
            UNIQUE(Provider, ExternalID)
        )
        """
    )

    # 5. Issue Creator Credits (Relational Junction)
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS ext_creator_credits (
            CreditID            INTEGER PRIMARY KEY AUTOINCREMENT,
            NameRecordID        INTEGER NOT NULL,
            CreatorEntityID     INTEGER DEFAULT NULL,
            IssueID             TEXT NOT NULL,
            ComicID             TEXT NOT NULL,
            IsAnnual            INTEGER NOT NULL DEFAULT 0,
            Role                TEXT NOT NULL,
            RawRoleText         TEXT NOT NULL,
            RawCreditName       TEXT NOT NULL,
            IsCover             INTEGER NOT NULL DEFAULT 0,
            SortOrder           INTEGER NOT NULL DEFAULT 0,
            SourceProvenance    TEXT NOT NULL,
            SourceRevision      TEXT NOT NULL,
            CreatedAt           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(IssueID, IsAnnual, SourceProvenance, Role, SortOrder, RawCreditName)
        )
        """
    )

    # 6. Current Source State & Scan Records
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS ext_creator_scan_records (
            ScanRecordID        INTEGER PRIMARY KEY AUTOINCREMENT,
            IssueID             TEXT NOT NULL,
            IsAnnual            INTEGER NOT NULL DEFAULT 0,
            ComicID             TEXT NOT NULL,
            RelativePath        TEXT NOT NULL,
            SourceType          TEXT NOT NULL,
            ScanStatus          TEXT NOT NULL,
            FileMTimeNs         INTEGER NOT NULL DEFAULT 0,
            FileSize            INTEGER NOT NULL DEFAULT 0,
            ComicInfoHash       TEXT DEFAULT NULL,
            CreditsExtracted    INTEGER NOT NULL DEFAULT 0,
            LastSuccessMTimeNs  INTEGER DEFAULT 0,
            LastSuccessFileSize INTEGER DEFAULT 0,
            LastSuccessAt       TIMESTAMP DEFAULT NULL,
            LastAttemptAt       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            LastError           TEXT DEFAULT NULL,
            UNIQUE(IssueID, IsAnnual, SourceType)
        )
        """
    )

    # Performance Indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ext_names_raw ON ext_creator_name_records(RawName)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ext_names_norm ON ext_creator_name_records(NormalizedName)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ext_names_slug ON ext_creator_name_records(NameSlug)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ext_names_entity ON ext_creator_name_records(CreatorEntityID)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ext_creators_slug ON ext_creator_entities(EntitySlug)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ext_creators_norm ON ext_creator_entities(NormalizedName)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ext_aliases_norm ON ext_creator_aliases(NormalizedAlias)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ext_extids_creator ON ext_creator_external_ids(CreatorEntityID)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ext_extids_lookup ON ext_creator_external_ids(Provider, ExternalID)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ext_credits_name ON ext_creator_credits(NameRecordID)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ext_credits_creator ON ext_creator_credits(CreatorEntityID)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ext_credits_issue ON ext_creator_credits(IssueID, IsAnnual)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ext_credits_comic ON ext_creator_credits(ComicID)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ext_credits_role ON ext_creator_credits(Role)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ext_scan_lookup ON ext_creator_scan_records(IssueID, IsAnnual, SourceType)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ext_scan_status ON ext_creator_scan_records(ScanStatus)")

    logger.fdebug("[EXTENSION-MIGRATION] Completed creators extension migration.")
