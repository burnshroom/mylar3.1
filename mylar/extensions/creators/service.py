"""
Creator Extension Database Service.

Provides transactional persistence and query methods for creator name records,
issue credits, and scan lifecycle records.
"""

from datetime import datetime
from mylar import db, logger
from mylar.extensions.creators.normalizer import normalize_name, slugify
from mylar.extensions.creators.schema import PROVENANCE_COMICINFO, RESOLUTION_UNRESOLVED


class CreatorService:
    """
    Service layer for creator extension database operations.
    """

    def __init__(self, db_conn=None):
        self._db = db_conn

    def _get_db(self):
        if self._db:
            return self._db
        return db.DBConnection()

    def get_or_create_name_record(self, raw_name, cursor=None):
        """
        Find or insert an unresolved raw name record in ext_creator_name_records.

        :param raw_name: Exact string from source
        :param cursor: Optional SQLite cursor
        :return: int NameRecordID
        """
        if not raw_name or not raw_name.strip():
            return None

        clean_raw = raw_name.strip()
        should_close = False
        if cursor is None:
            my_db = self._get_db()
            conn = getattr(my_db, 'connection', getattr(my_db, 'conn', None))
            cursor = conn.cursor()
            should_close = True

        try:
            cursor.execute(
                "SELECT NameRecordID FROM ext_creator_name_records WHERE RawName = ?",
                (clean_raw,)
            )
            row = cursor.fetchone()
            if row:
                return row[0]

            norm_name = normalize_name(clean_raw)
            name_slug = slugify(clean_raw)

            cursor.execute(
                """
                INSERT OR IGNORE INTO ext_creator_name_records 
                (RawName, NormalizedName, NameSlug, CreatorEntityID, ResolutionSource, CreatedAt, UpdatedAt)
                VALUES (?, ?, ?, NULL, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (clean_raw, norm_name, name_slug, RESOLUTION_UNRESOLVED)
            )

            cursor.execute(
                "SELECT NameRecordID FROM ext_creator_name_records WHERE RawName = ?",
                (clean_raw,)
            )
            row = cursor.fetchone()
            return row[0] if row else None

        finally:
            if should_close:
                pass

    def replace_issue_credits(self, issue_id, is_annual, comic_id, credits_list, source_provenance, source_revision, cursor):
        """
        Atomically replace all credits for an issue from a given source provenance.

        :param issue_id: str IssueID
        :param is_annual: int (0 or 1)
        :param comic_id: str ComicID
        :param credits_list: list of credit dicts
        :param source_provenance: str ('comicinfo', 'metron', etc.)
        :param source_revision: str file signature or hash
        :param cursor: SQLite cursor within active transaction
        :return: int count of credits inserted
        """
        # 1. Delete prior credits for this issue and provenance
        cursor.execute(
            """
            DELETE FROM ext_creator_credits 
            WHERE IssueID = ? AND IsAnnual = ? AND SourceProvenance = ?
            """,
            (str(issue_id), int(is_annual), str(source_provenance))
        )

        inserted_count = 0

        # 2. Insert new parsed credits
        for cred in credits_list:
            raw_name = cred['raw_credit_name']
            name_record_id = self.get_or_create_name_record(raw_name, cursor=cursor)
            if not name_record_id:
                continue

            cursor.execute(
                """
                INSERT OR IGNORE INTO ext_creator_credits (
                    NameRecordID, CreatorEntityID, IssueID, ComicID, IsAnnual,
                    Role, RawRoleText, RawCreditName, IsCover, SortOrder,
                    SourceProvenance, SourceRevision, CreatedAt
                ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    name_record_id,
                    str(issue_id),
                    str(comic_id),
                    int(is_annual),
                    cred['role'],
                    cred['raw_role_text'],
                    raw_name,
                    int(cred.get('is_cover', 0)),
                    int(cred.get('sort_order', 0)),
                    str(source_provenance),
                    str(source_revision)
                )
            )
            inserted_count += 1

        return inserted_count

    def record_scan_result(self, issue_id, is_annual, comic_id, rel_path, source_type,
                           scan_status, mtime_ns, file_size, xml_hash, credits_count,
                           error_msg, cursor):
        """
        Upsert a scan lifecycle record into ext_creator_scan_records.
        """
        cursor.execute(
            """
            SELECT ScanRecordID, LastSuccessMTimeNs, LastSuccessFileSize, LastSuccessAt
            FROM ext_creator_scan_records
            WHERE IssueID = ? AND IsAnnual = ? AND SourceType = ?
            """,
            (str(issue_id), int(is_annual), str(source_type))
        )
        existing = cursor.fetchone()

        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        if scan_status in ('scanned_with_credits', 'scanned_no_credits', 'no_comicinfo'):
            # Successful scan
            last_success_mtime = mtime_ns
            last_success_size = file_size
            last_success_at = now_str
            error_val = None
        else:
            # Failed / Inaccessible scan: retain prior success signature
            if existing:
                last_success_mtime = existing[1]
                last_success_size = existing[2]
                last_success_at = existing[3]
            else:
                last_success_mtime = 0
                last_success_size = 0
                last_success_at = None
            error_val = str(error_msg) if error_msg else "Scan error"

        if existing:
            cursor.execute(
                """
                UPDATE ext_creator_scan_records SET
                    ComicID = ?,
                    RelativePath = ?,
                    ScanStatus = ?,
                    FileMTimeNs = ?,
                    FileSize = ?,
                    ComicInfoHash = ?,
                    CreditsExtracted = ?,
                    LastSuccessMTimeNs = ?,
                    LastSuccessFileSize = ?,
                    LastSuccessAt = ?,
                    LastAttemptAt = ?,
                    LastError = ?
                WHERE IssueID = ? AND IsAnnual = ? AND SourceType = ?
                """,
                (
                    str(comic_id),
                    str(rel_path),
                    str(scan_status),
                    int(mtime_ns),
                    int(file_size),
                    xml_hash,
                    int(credits_count),
                    int(last_success_mtime),
                    int(last_success_size),
                    last_success_at,
                    now_str,
                    error_val,
                    str(issue_id),
                    int(is_annual),
                    str(source_type)
                )
            )
        else:
            cursor.execute(
                """
                INSERT INTO ext_creator_scan_records (
                    IssueID, IsAnnual, ComicID, RelativePath, SourceType,
                    ScanStatus, FileMTimeNs, FileSize, ComicInfoHash,
                    CreditsExtracted, LastSuccessMTimeNs, LastSuccessFileSize,
                    LastSuccessAt, LastAttemptAt, LastError
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(issue_id),
                    int(is_annual),
                    str(comic_id),
                    str(rel_path),
                    str(source_type),
                    str(scan_status),
                    int(mtime_ns),
                    int(file_size),
                    xml_hash,
                    int(credits_count),
                    int(last_success_mtime),
                    int(last_success_size),
                    last_success_at,
                    now_str,
                    error_val
                )
            )

    def get_scan_record(self, issue_id, is_annual, source_type=PROVENANCE_COMICINFO):
        """
        Fetch the current scan record for an issue.
        """
        my_db = self._get_db()
        row = my_db.selectone(
            """
            SELECT ScanRecordID, IssueID, IsAnnual, ComicID, RelativePath,
                   SourceType, ScanStatus, FileMTimeNs, FileSize, ComicInfoHash,
                   CreditsExtracted, LastSuccessMTimeNs, LastSuccessFileSize,
                   LastSuccessAt, LastAttemptAt, LastError
            FROM ext_creator_scan_records
            WHERE IssueID = ? AND IsAnnual = ? AND SourceType = ?
            """,
            [str(issue_id), int(is_annual), str(source_type)]
        ).fetchone()
        return row

    def get_series_creator_summary(self, comic_id):
        """
        Get indexing and creator summary for a series.
        """
        my_db = self._get_db()

        # Count scan records by status
        scan_counts = my_db.select(
            """
            SELECT ScanStatus, COUNT(*) 
            FROM ext_creator_scan_records 
            WHERE ComicID = ?
            GROUP BY ScanStatus
            """,
            [str(comic_id)]
        )
        status_map = {row[0]: row[1] for row in scan_counts}

        # Count total credits and distinct raw creators
        credit_stats = my_db.selectone(
            """
            SELECT COUNT(*), COUNT(DISTINCT NameRecordID)
            FROM ext_creator_credits
            WHERE ComicID = ?
            """,
            [str(comic_id)]
        ).fetchone()

        total_credits = credit_stats[0] if credit_stats else 0
        distinct_creators = credit_stats[1] if credit_stats else 0

        # Latest scan time
        latest_scan = my_db.selectone(
            """
            SELECT MAX(LastSuccessAt), MAX(LastAttemptAt)
            FROM ext_creator_scan_records
            WHERE ComicID = ?
            """,
            [str(comic_id)]
        ).fetchone()

        last_success = latest_scan[0] if latest_scan else None
        last_attempt = latest_scan[1] if latest_scan else None

        # Count total downloaded candidates for this series
        cand_issues_cursor = my_db.selectone(
            "SELECT COUNT(*) FROM issues WHERE ComicID = ? AND Status = 'Downloaded' AND Location IS NOT NULL",
            [str(comic_id)]
        )
        cand_issues_row = cand_issues_cursor.fetchone() if cand_issues_cursor else None
        cand_issues = cand_issues_row[0] if cand_issues_row else 0

        cand_annuals_cursor = my_db.selectone(
            "SELECT COUNT(*) FROM annuals WHERE ComicID = ? AND Status = 'Downloaded' AND Location IS NOT NULL",
            [str(comic_id)]
        )
        cand_annuals_row = cand_annuals_cursor.fetchone() if cand_annuals_cursor else None
        cand_annuals = cand_annuals_row[0] if cand_annuals_row else 0

        total_candidates = cand_issues + cand_annuals
        total_scanned = sum(status_map.values())
        has_prior_scan = total_scanned > 0
        is_fully_indexed = has_prior_scan and (total_scanned >= total_candidates)

        return {
            'comic_id': str(comic_id),
            'total_candidates': total_candidates,
            'total_scanned': total_scanned,
            'has_prior_scan': has_prior_scan,
            'is_fully_indexed': is_fully_indexed,
            'total_credits': total_credits,
            'distinct_creators': distinct_creators,
            'scanned_with_credits': status_map.get('scanned_with_credits', 0),
            'scanned_no_credits': status_map.get('scanned_no_credits', 0),
            'no_comicinfo': status_map.get('no_comicinfo', 0),
            'inaccessible': status_map.get('inaccessible_or_unsupported', 0),
            'last_success_at': last_success,
            'last_attempt_at': last_attempt,
        }
