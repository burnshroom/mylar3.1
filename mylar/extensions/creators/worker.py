"""
Creator Indexer Background Worker.

Provides thread-safe, cancellable background indexing for local comic archives.
"""

import os
import threading
import time
from datetime import datetime
from mylar import db, logger
from mylar.extensions.creators.indexer import (
    compute_file_signature,
    compute_xml_hash,
    extract_credits_from_xml,
    read_archive_comicinfo,
)
from mylar.extensions.creators.schema import PROVENANCE_COMICINFO
from mylar.extensions.creators.service import CreatorService
from mylar.extensions.thumbnails.resolver import resolve_issue_file

MAX_BATCH_SIZE = 50


class CreatorIndexWorker:
    """
    Singleton background worker for local creator indexing.
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(CreatorIndexWorker, cls).__new__(cls)
                cls._instance._init_worker()
            return cls._instance

    def _init_worker(self):
        self._thread = None
        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()
        self._service = CreatorService()
        self._state = {
            'job_id': None,
            'status': 'idle',  # 'idle', 'running', 'completed', 'cancelled', 'failed'
            'comic_id': None,
            'series_name': None,
            'total_files': 0,
            'attempted': 0,
            'succeeded_with_credits': 0,
            'succeeded_without_credits': 0,
            'no_comicinfo': 0,
            'inaccessible': 0,
            'skipped_unchanged': 0,
            'credits_added': 0,
            'cancelled': False,
            'current_file': None,
            'start_time': None,
            'end_time': None,
            'error_log': []
        }

    def get_status(self, job_id=None):
        """
        Return a thread-safe snapshot of worker status.
        """
        with self._state_lock:
            state_copy = dict(self._state)
            state_copy['error_log'] = list(self._state['error_log'])
            return state_copy

    def cancel(self, job_id=None):
        """
        Request cooperative cancellation of the running index job.
        """
        with self._state_lock:
            if self._state['status'] == 'running':
                logger.info(f"[CREATOR-WORKER] Cancellation requested for job {self._state['job_id']}")
                self._stop_event.set()
                self._state['cancelled'] = True
                return {'status': 'cancelling', 'job_id': self._state['job_id']}
            return {'status': self._state['status'], 'job_id': self._state.get('job_id')}

    def start_series_scan(self, comic_id, issue_ids=None, max_batch=MAX_BATCH_SIZE):
        """
        Start indexing downloaded archives for a series in a background thread.
        """
        with self._state_lock:
            if self._state['status'] == 'running':
                return {
                    'status': 'busy',
                    'error': f"Another index job ({self._state['job_id']}) is already in progress",
                    'job_id': self._state['job_id']
                }

            my_db = db.DBConnection()
            comic_row = my_db.selectone(
                "SELECT ComicName, ComicLocation FROM comics WHERE ComicID = ?",
                [str(comic_id)]
            ).fetchone()

            if not comic_row:
                return {
                    'status': 'error',
                    'error': f"Series not found for ComicID {comic_id}",
                    'job_id': None
                }

            comic_name = comic_row[0]
            comic_location = comic_row[1]

            # Fetch downloaded issues and annuals
            items = []

            # 1. Regular issues
            if issue_ids:
                placeholders = ','.join(['?'] * len(issue_ids))
                q_issues = f"""
                SELECT 'issue', IssueID, Issue_Number, Location
                FROM issues
                WHERE ComicID = ? AND Status = 'Downloaded' AND IssueID IN ({placeholders})
                """
                issue_params = [str(comic_id)] + [str(iid) for iid in issue_ids]
            else:
                q_issues = """
                SELECT 'issue', IssueID, Issue_Number, Location
                FROM issues
                WHERE ComicID = ? AND Status = 'Downloaded'
                ORDER BY Int_IssueNumber ASC
                """
                issue_params = [str(comic_id)]

            for row in my_db.select(q_issues, issue_params):
                items.append({
                    'type': 'issue',
                    'is_annual': 0,
                    'issue_id': str(row[1]),
                    'issue_number': str(row[2]),
                    'location': row[3],
                    'comic_location': comic_location,
                    'comic_id': str(comic_id),
                    'comic_name': comic_name
                })

            # 2. Annuals (if not filtering on specific regular issue IDs)
            if not issue_ids:
                q_annuals = """
                SELECT 'annual', IssueID, Issue_Number, Location
                FROM annuals
                WHERE ComicID = ? AND Status = 'Downloaded'
                ORDER BY Int_IssueNumber ASC
                """
                for row in my_db.select(q_annuals, [str(comic_id)]):
                    items.append({
                        'type': 'annual',
                        'is_annual': 1,
                        'issue_id': str(row[1]),
                        'issue_number': str(row[2]),
                        'location': row[3],
                        'comic_location': comic_location,
                        'comic_id': str(comic_id),
                        'comic_name': comic_name
                    })

            # Apply batch limit
            if max_batch and len(items) > max_batch:
                logger.info(f"[CREATOR-WORKER] Capping scan items to batch limit of {max_batch} (found {len(items)})")
                items = items[:max_batch]

            job_id = f"cscan-{comic_id}-{int(time.time())}"
            self._stop_event.clear()

            self._state = {
                'job_id': job_id,
                'status': 'running',
                'comic_id': str(comic_id),
                'series_name': comic_name,
                'total_files': len(items),
                'attempted': 0,
                'succeeded_with_credits': 0,
                'succeeded_without_credits': 0,
                'no_comicinfo': 0,
                'inaccessible': 0,
                'skipped_unchanged': 0,
                'credits_added': 0,
                'cancelled': False,
                'current_file': None,
                'start_time': datetime.now().isoformat(),
                'end_time': None,
                'error_log': []
            }

            self._thread = threading.Thread(
                target=self._run_scan,
                args=(job_id, items),
                name=f"CreatorIndexer-{job_id}"
            )
            self._thread.daemon = True
            self._thread.start()

            return {
                'status': 'running',
                'job_id': job_id,
                'total_files': len(items),
                'series_name': comic_name
            }

    def _run_scan(self, job_id, items):
        """
        Background worker thread execution.
        """
        logger.info(f"[CREATOR-WORKER] Starting creator index job {job_id} ({len(items)} items)")
        my_db = db.DBConnection()
        conn = getattr(my_db, 'connection', getattr(my_db, 'conn', None))

        for idx, item in enumerate(items, 1):
            if self._stop_event.is_set():
                logger.info(f"[CREATOR-WORKER] Job {job_id} cancelled at item {idx}/{len(items)}")
                with self._state_lock:
                    self._state['status'] = 'cancelled'
                    self._state['end_time'] = datetime.now().isoformat()
                return

            filename = item['location'] or f"Issue #{item['issue_number']}"
            with self._state_lock:
                self._state['current_file'] = filename
                self._state['attempted'] += 1

            try:
                # 1. Resolve safe archive path
                resolved_path = resolve_issue_file(item['comic_location'], item['location'])
                rel_path = item['location'] or ""

                if not resolved_path or not os.path.isfile(resolved_path):
                    with conn:
                        cursor = conn.cursor()
                        self._service.record_scan_result(
                            issue_id=item['issue_id'],
                            is_annual=item['is_annual'],
                            comic_id=item['comic_id'],
                            rel_path=rel_path,
                            source_type=PROVENANCE_COMICINFO,
                            scan_status='inaccessible_or_unsupported',
                            mtime_ns=0,
                            file_size=0,
                            xml_hash=None,
                            credits_count=0,
                            error_msg=f"Archive file not found on disk: {item['location']}",
                            cursor=cursor
                        )
                    with self._state_lock:
                        self._state['inaccessible'] += 1
                        self._state['error_log'].append(f"{filename}: File not found")
                    continue

                # 2. Check filesystem signature
                mtime_ns, file_size = compute_file_signature(resolved_path)
                existing_record = self._service.get_scan_record(
                    issue_id=item['issue_id'],
                    is_annual=item['is_annual'],
                    source_type=PROVENANCE_COMICINFO
                )

                # Check if unchanged
                if existing_record:
                    last_success_mtime = existing_record[11]
                    last_success_size = existing_record[12]
                    last_status = existing_record[6]

                    if (mtime_ns > 0 and mtime_ns == last_success_mtime and
                            file_size == last_success_size and
                            last_status in ('scanned_with_credits', 'scanned_no_credits', 'no_comicinfo')):
                        logger.fdebug(f"[CREATOR-WORKER] Skipping unchanged archive: {filename}")
                        with self._state_lock:
                            self._state['skipped_unchanged'] += 1
                        continue

                # 3. Read ComicInfo.xml
                raw_xml, raw_bytes, status, error_msg = read_archive_comicinfo(resolved_path)
                xml_hash = compute_xml_hash(raw_bytes)
                source_revision = f"{mtime_ns}_{file_size}_{xml_hash[:12] if xml_hash else 'none'}"

                if status == 'inaccessible_or_unsupported':
                    # Failure: retain previously known-good credits
                    with conn:
                        cursor = conn.cursor()
                        self._service.record_scan_result(
                            issue_id=item['issue_id'],
                            is_annual=item['is_annual'],
                            comic_id=item['comic_id'],
                            rel_path=rel_path,
                            source_type=PROVENANCE_COMICINFO,
                            scan_status='inaccessible_or_unsupported',
                            mtime_ns=mtime_ns,
                            file_size=file_size,
                            xml_hash=xml_hash,
                            credits_count=0,
                            error_msg=error_msg,
                            cursor=cursor
                        )
                    with self._state_lock:
                        self._state['inaccessible'] += 1
                        self._state['error_log'].append(f"{filename}: {error_msg}")
                    continue

                elif status == 'no_comicinfo':
                    # Clean outcome: no comicinfo
                    with conn:
                        cursor = conn.cursor()
                        # Atomically clear any prior credits for this provenance
                        self._service.replace_issue_credits(
                            issue_id=item['issue_id'],
                            is_annual=item['is_annual'],
                            comic_id=item['comic_id'],
                            credits_list=[],
                            source_provenance=PROVENANCE_COMICINFO,
                            source_revision=source_revision,
                            cursor=cursor
                        )
                        self._service.record_scan_result(
                            issue_id=item['issue_id'],
                            is_annual=item['is_annual'],
                            comic_id=item['comic_id'],
                            rel_path=rel_path,
                            source_type=PROVENANCE_COMICINFO,
                            scan_status='no_comicinfo',
                            mtime_ns=mtime_ns,
                            file_size=file_size,
                            xml_hash=None,
                            credits_count=0,
                            error_msg=None,
                            cursor=cursor
                        )
                    with self._state_lock:
                        self._state['no_comicinfo'] += 1
                    continue

                # 4. Parse credits
                parsed_credits = extract_credits_from_xml(raw_xml)
                final_status = 'scanned_with_credits' if parsed_credits else 'scanned_no_credits'

                # 5. Atomic replacement in database
                with conn:
                    cursor = conn.cursor()
                    inserted = self._service.replace_issue_credits(
                        issue_id=item['issue_id'],
                        is_annual=item['is_annual'],
                        comic_id=item['comic_id'],
                        credits_list=parsed_credits,
                        source_provenance=PROVENANCE_COMICINFO,
                        source_revision=source_revision,
                        cursor=cursor
                    )
                    self._service.record_scan_result(
                        issue_id=item['issue_id'],
                        is_annual=item['is_annual'],
                        comic_id=item['comic_id'],
                        rel_path=rel_path,
                        source_type=PROVENANCE_COMICINFO,
                        scan_status=final_status,
                        mtime_ns=mtime_ns,
                        file_size=file_size,
                        xml_hash=xml_hash,
                        credits_count=inserted,
                        error_msg=None,
                        cursor=cursor
                    )

                with self._state_lock:
                    if final_status == 'scanned_with_credits':
                        self._state['succeeded_with_credits'] += 1
                        self._state['credits_added'] += inserted
                    else:
                        self._state['succeeded_without_credits'] += 1

            except Exception as e:
                logger.error(f"[CREATOR-WORKER] Unexpected error scanning {filename}: {e}")
                with self._state_lock:
                    self._state['inaccessible'] += 1
                    self._state['error_log'].append(f"{filename}: Unexpected error: {e}")

        # Completion
        with self._state_lock:
            self._state['status'] = 'completed'
            self._state['current_file'] = None
            self._state['end_time'] = datetime.now().isoformat()

        logger.info(f"[CREATOR-WORKER] Job {job_id} finished successfully. Credits added: {self._state['credits_added']}")
