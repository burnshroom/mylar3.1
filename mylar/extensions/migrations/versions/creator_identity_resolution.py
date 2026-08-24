"""
Migration: Creator Identity Resolution Persistence Foundation (Phase C4.8)

Idempotently creates extension-owned tables and performance indexes for candidate
rejections and immutable resolution audit logging.
"""

from mylar import logger


def upgrade(cursor):
    """
    Apply the ext_creator_candidate_rejections and ext_creator_resolution_audit tables and indexes.

    :param cursor: sqlite3.Cursor instance attached to the Mylar database
    """
    logger.fdebug("[EXTENSION-MIGRATION] Running creator identity resolution migration.")

    # 1. Candidate Rejections (Tracks active suppression and reversal history)
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS ext_creator_candidate_rejections (
            RejectionID         INTEGER PRIMARY KEY AUTOINCREMENT,
            NameRecordID        INTEGER NOT NULL,
            Provider            TEXT NOT NULL,
            ExternalID          TEXT NOT NULL,
            ProviderDisplayName TEXT DEFAULT NULL,
            Status              TEXT NOT NULL DEFAULT 'active',
            RejectedBy          TEXT NOT NULL DEFAULT 'system',
            RejectedAt          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ReversedBy          TEXT DEFAULT NULL,
            ReversedAt          TIMESTAMP DEFAULT NULL,
            Reason              TEXT DEFAULT NULL,
            ReversalReason      TEXT DEFAULT NULL
        )
        """
    )

    # 2. Immutable Resolution Audit Log (Full auditability and reversibility)
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS ext_creator_resolution_audit (
            AuditID             INTEGER PRIMARY KEY AUTOINCREMENT,
            Action              TEXT NOT NULL,
            NameRecordID        INTEGER DEFAULT NULL,
            CreatorEntityID     INTEGER DEFAULT NULL,
            Provider            TEXT DEFAULT NULL,
            ExternalID          TEXT DEFAULT NULL,
            ProviderDisplayName TEXT DEFAULT NULL,
            Actor               TEXT NOT NULL DEFAULT 'system',
            Reason              TEXT DEFAULT NULL,
            ConfirmationSource  TEXT DEFAULT NULL,
            BeforeStateJson     TEXT DEFAULT NULL,
            AfterStateJson      TEXT DEFAULT NULL,
            CreatedAt           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # Performance and Uniqueness Indexes
    # Partial unique index prevents duplicate active rejections for the same candidate
    cursor.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_ext_rejections_active 
        ON ext_creator_candidate_rejections(NameRecordID, Provider, ExternalID) 
        WHERE Status = 'active'
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ext_rejections_lookup 
        ON ext_creator_candidate_rejections(NameRecordID, Provider, ExternalID, Status)
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ext_rejections_namerec 
        ON ext_creator_candidate_rejections(NameRecordID)
        """
    )

    # Audit log indexes
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ext_audit_namerec 
        ON ext_creator_resolution_audit(NameRecordID)
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ext_audit_entity 
        ON ext_creator_resolution_audit(CreatorEntityID)
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ext_audit_action 
        ON ext_creator_resolution_audit(Action)
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ext_audit_created 
        ON ext_creator_resolution_audit(CreatedAt)
        """
    )

    logger.fdebug("[EXTENSION-MIGRATION] Completed creator identity resolution migration.")
