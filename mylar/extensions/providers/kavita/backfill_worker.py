"""
Kavita Publisher-Library Sync Backfill Worker (Phase K8).

Provides thread-safe, bounded, sequential background synchronization of existing
Mylar series into Kavita publisher-root mappings, reusing the exact K5 automation service.

Key Guarantees:
- Discovers candidate series only from server-side Mylar database records (Status IS NULL OR Status != 'Deleted').
- Unmapped-only targeting: Active mappings, ambiguous mappings, and mappings in retry backoff
  make ZERO remote Kavita calls and receive ZERO scans.
- Recomputes candidate roots completely server-side on confirmed start (zero trust in client input).
- Executes sequentially (1 publisher root at a time).
- One failed root does not abort remaining roots.
- Sanitized in-memory status snapshot: Zero API keys, full instance UUIDs, server URLs, lease tokens, or tracebacks.
"""

import os
import time
import uuid
import datetime
import threading
import mylar
from mylar import db, logger
from mylar.config import get_mylar_instance_slug
from mylar.extensions.providers.kavita.config import (
    get_kavita_config_status,
    validate_kavita_url
)
from mylar.extensions.providers.kavita.publisher_service import (
    KavitaPublisherService,
    derive_materialized_publisher_root,
    normalize_path_str,
    parse_db_timestamp,
    LEASE_DURATION_SECONDS
)


def _row_get(row, key, default=None):
    if row is None:
        return default
    try:
        val = row[key]
        return val if val is not None else default
    except (KeyError, IndexError, TypeError):
        return default


class KavitaSyncBackfillWorker:
    """
    Singleton background worker for Kavita publisher-library backfill.
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(KavitaSyncBackfillWorker, cls).__new__(cls)
                cls._instance._init_worker()
            return cls._instance

    def _init_worker(self):
        self._thread = None
        self._state_lock = threading.Lock()
        self._service = None
        self._state = {
            'job_id': None,
            'status': 'idle',  # 'idle', 'running', 'completed', 'failed'
            'start_time': None,
            'end_time': None,
            'current_publisher': None,
            'total_candidate_series': 0,
            'unique_publisher_roots': 0,
            'processed_roots_count': 0,
            'metrics': {
                'already_active_skipped': 0,
                'unmapped_roots': 0,
                'created_mappings': 0,
                'associated_mappings': 0,
                'scans_queued': 0,
                'flat_layout_skipped': 0,
                'missing_directory_skipped': 0,
                'backoff_skipped': 0,
                'ambiguous_skipped': 0,
                'sanitized_failures': 0
            },
            'processed_roots': [],
            'error_log': []
        }

    def get_status(self, job_id=None):
        """
        Return a thread-safe sanitized snapshot of worker status.
        Guarantees zero API keys, full instance UUIDs, server URLs, lease tokens, or tracebacks.
        Rejects a supplied job_id that does not match the active/latest worker job with a 404 response.
        """
        with self._state_lock:
            if job_id is not None and str(job_id).strip():
                current_jid = self._state.get('job_id')
                if current_jid is None or str(job_id).strip() != current_jid:
                    return {
                        'status': 'error',
                        'status_code': 404,
                        'message': 'Backfill job not found or expired.',
                        'error_code': 'job_not_found'
                    }

            state_copy = dict(self._state)
            state_copy['metrics'] = dict(self._state['metrics'])
            state_copy['processed_roots'] = list(self._state['processed_roots'])
            state_copy['error_log'] = list(self._state['error_log'])
            return state_copy

    def compute_preview(self):
        """
        Compute candidate series and publisher roots preview.
        Read-only inspection: strictly ZERO remote Kavita API requests.
        """
        config_status = get_kavita_config_status()
        if not config_status.get('enabled', False) or not config_status.get('is_configured', False):
            return {
                'status': 'error',
                'message': 'Kavita integration is not enabled or configured.'
            }

        comic_dir = getattr(mylar.CONFIG, 'COMIC_DIR', None)
        if not comic_dir or not os.path.isdir(comic_dir):
            return {
                'status': 'error',
                'message': 'Mylar Comic Directory is invalid or missing.'
            }

        instance_id = getattr(mylar.CONFIG, 'MYLAR_INSTANCE_ID', None)
        server_url = validate_kavita_url(config_status['url'])

        my_db = db.DBConnection()
        series_rows = my_db.select(
            "SELECT ComicID, ComicName, ComicPublisher, ComicLocation, Status "
            "FROM comics "
            "WHERE Status IS NULL OR Status != 'Deleted' "
            "ORDER BY ComicPublisher ASC, ComicName ASC"
        ) or []

        total_series = len(series_rows)
        candidate_roots = {}
        flat_layout_count = 0
        missing_directory_count = 0

        for row in series_rows:
            loc = _row_get(row, 'ComicLocation')
            if not loc or not str(loc).strip():
                missing_directory_count += 1
                continue

            path_res = derive_materialized_publisher_root(loc, comic_dir)
            if not path_res['is_supported']:
                if path_res['status'] == 'publisher_root_not_materialized':
                    flat_layout_count += 1
                else:
                    missing_directory_count += 1
                continue

            pub_path = path_res['publisher_path']
            if pub_path not in candidate_roots:
                candidate_roots[pub_path] = {
                    'canonical_publisher_path': pub_path,
                    'publisher_name': _row_get(row, 'ComicPublisher') or os.path.basename(pub_path),
                    'representative_comic_id': _row_get(row, 'ComicID'),
                    'representative_comic_name': _row_get(row, 'ComicName'),
                    'representative_series_location': path_res['series_path']
                }

        unique_roots_count = len(candidate_roots)
        already_active_count = 0
        ambiguous_skipped_count = 0
        backoff_skipped_count = 0
        unmapped_roots_count = 0
        now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

        for pub_path, root_info in candidate_roots.items():
            map_rows = my_db.select(
                "SELECT MappingState, KavitaLibraryID, NextEligibleRetryAt, LeaseToken, LeaseExpiresAt "
                "FROM ext_kavita_publisher_mappings "
                "WHERE MylarInstanceID = ? AND CanonicalPublisherPath = ? AND KavitaServerUrl = ?",
                [instance_id, pub_path, server_url]
            )
            map_row = map_rows[0] if map_rows else None

            if map_row:
                state = _row_get(map_row, 'MappingState')
                if state == 'active':
                    already_active_count += 1
                elif state == 'ambiguous_multi_path':
                    ambiguous_skipped_count += 1
                elif _row_get(map_row, 'NextEligibleRetryAt'):
                    retry_dt = parse_db_timestamp(_row_get(map_row, 'NextEligibleRetryAt'))
                    if retry_dt and retry_dt > now_utc:
                        backoff_skipped_count += 1
                    else:
                        unmapped_roots_count += 1
                else:
                    unmapped_roots_count += 1
            else:
                unmapped_roots_count += 1

        try:
            my_db.connection.close()
        except Exception:
            pass

        return {
            'status': 'success',
            'total_candidate_series': total_series,
            'unique_publisher_roots': unique_roots_count,
            'already_active_count': already_active_count,
            'unmapped_roots_count': unmapped_roots_count,
            'flat_layout_count': flat_layout_count,
            'missing_directory_count': missing_directory_count,
            'backoff_skipped_count': backoff_skipped_count,
            'ambiguous_skipped_count': ambiguous_skipped_count
        }

    def start_backfill(self, service=None):
        """
        Start the background sync job. Returns promptly with job ID.
        Recomputes candidate roots server-side upon start.
        """
        with self._state_lock:
            if self._state['status'] == 'running':
                return {
                    'status': 'busy',
                    'job_id': self._state['job_id'],
                    'message': 'A Kavita publisher synchronization job is already running.'
                }

            config_status = get_kavita_config_status()
            if not config_status.get('enabled', False) or not config_status.get('is_configured', False):
                return {
                    'status': 'error',
                    'message': 'Kavita integration is not enabled or configured.'
                }

            job_id = f"kavita-backfill-{int(time.time())}-{uuid.uuid4().hex[:6]}"
            self._state = {
                'job_id': job_id,
                'status': 'running',
                'start_time': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'end_time': None,
                'current_publisher': None,
                'total_candidate_series': 0,
                'unique_publisher_roots': 0,
                'processed_roots_count': 0,
                'metrics': {
                    'already_active_skipped': 0,
                    'unmapped_roots': 0,
                    'created_mappings': 0,
                    'associated_mappings': 0,
                    'scans_queued': 0,
                    'flat_layout_skipped': 0,
                    'missing_directory_skipped': 0,
                    'backoff_skipped': 0,
                    'ambiguous_skipped': 0,
                    'sanitized_failures': 0
                },
                'processed_roots': [],
                'error_log': []
            }

            self._service = service
            self._thread = threading.Thread(
                target=self._run_backfill_job,
                name=f"KavitaBackfill-{job_id}",
                daemon=True
            )
            self._thread.start()

            return {
                'status': 'started',
                'job_id': job_id,
                'message': 'Kavita publisher sync started in background.'
            }

    def _run_backfill_job(self):
        """
        Internal worker loop executed in a background thread.
        Processes unmapped/recoverable publisher roots sequentially (1 at a time).
        """
        logger.info(f"[KAVITA-BACKFILL] Starting backfill job {self._state['job_id']}")
        try:
            config_status = get_kavita_config_status()
            server_url = validate_kavita_url(config_status['url'])
            instance_id = mylar.CONFIG.MYLAR_INSTANCE_ID
            comic_dir = getattr(mylar.CONFIG, 'COMIC_DIR', None)

            if callable(self._service):
                service = self._service()
            elif self._service is not None:
                service = self._service
            else:
                service = KavitaPublisherService()

            my_db = db.DBConnection()
            series_rows = my_db.select(
                "SELECT ComicID, ComicName, ComicPublisher, ComicLocation, Status "
                "FROM comics "
                "WHERE Status IS NULL OR Status != 'Deleted' "
                "ORDER BY ComicPublisher ASC, ComicName ASC"
            ) or []

            total_series = len(series_rows)
            candidate_roots = {}
            flat_layout_count = 0
            missing_directory_count = 0

            for row in series_rows:
                loc = _row_get(row, 'ComicLocation')
                if not loc or not str(loc).strip():
                    missing_directory_count += 1
                    continue

                path_res = derive_materialized_publisher_root(loc, comic_dir)
                if not path_res['is_supported']:
                    if path_res['status'] == 'publisher_root_not_materialized':
                        flat_layout_count += 1
                    else:
                        missing_directory_count += 1
                    continue

                pub_path = path_res['publisher_path']
                if pub_path not in candidate_roots:
                    candidate_roots[pub_path] = {
                        'canonical_publisher_path': pub_path,
                        'publisher_name': _row_get(row, 'ComicPublisher') or os.path.basename(pub_path),
                        'representative_comic_id': _row_get(row, 'ComicID'),
                        'representative_comic_name': _row_get(row, 'ComicName'),
                        'representative_series_location': path_res['series_path']
                    }

            now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            unmapped_list = []
            already_active_cnt = 0
            ambiguous_cnt = 0
            backoff_cnt = 0

            for pub_path, root_info in candidate_roots.items():
                map_rows = my_db.select(
                    "SELECT MappingState, KavitaLibraryID, NextEligibleRetryAt "
                    "FROM ext_kavita_publisher_mappings "
                    "WHERE MylarInstanceID = ? AND CanonicalPublisherPath = ? AND KavitaServerUrl = ?",
                    [instance_id, pub_path, server_url]
                )
                map_row = map_rows[0] if map_rows else None

                if map_row:
                    state = _row_get(map_row, 'MappingState')
                    if state == 'active':
                        already_active_cnt += 1
                        continue
                    elif state == 'ambiguous_multi_path':
                        ambiguous_cnt += 1
                        continue
                    elif _row_get(map_row, 'NextEligibleRetryAt'):
                        retry_dt = parse_db_timestamp(_row_get(map_row, 'NextEligibleRetryAt'))
                        if retry_dt and retry_dt > now_utc:
                            backoff_cnt += 1
                            continue

                unmapped_list.append(root_info)

            with self._state_lock:
                self._state['total_candidate_series'] = total_series
                self._state['unique_publisher_roots'] = len(candidate_roots)
                self._state['metrics']['already_active_skipped'] = already_active_cnt
                self._state['metrics']['ambiguous_skipped'] = ambiguous_cnt
                self._state['metrics']['backoff_skipped'] = backoff_cnt
                self._state['metrics']['flat_layout_skipped'] = flat_layout_count
                self._state['metrics']['missing_directory_skipped'] = missing_directory_count
                self._state['metrics']['unmapped_roots'] = len(unmapped_list)

            try:
                my_db.connection.close()
            except Exception:
                pass

            # Process unmapped roots sequentially (1 at a time)
            for root_info in unmapped_list:
                pub_name = root_info['publisher_name']
                pub_path = root_info['canonical_publisher_path']

                with self._state_lock:
                    self._state['current_publisher'] = pub_name

                logger.info(f"[KAVITA-BACKFILL] Processing unmapped publisher root: '{pub_name}' ({pub_path})")

                comic_data = {
                    'ComicID': root_info['representative_comic_id'],
                    'ComicName': root_info['representative_comic_name'],
                    'ComicPublisher': pub_name,
                    'ComicLocation': root_info['representative_series_location'],
                    'SeriesLocation': root_info['representative_series_location']
                }

                try:
                    res = service.process_post_import_automation(comic_data)
                    status_res = res.get('status')
                    lib_id = res.get('library_id')

                    with self._state_lock:
                        self._state['processed_roots_count'] += 1

                        if status_res == 'created':
                            self._state['metrics']['created_mappings'] += 1
                            if res.get('scan_queued'):
                                self._state['metrics']['scans_queued'] += 1
                            self._state['processed_roots'].append({
                                'publisher': pub_name,
                                'path': pub_path,
                                'status': 'created',
                                'library_id': lib_id
                            })
                        elif status_res == 'associated_existing':
                            self._state['metrics']['associated_mappings'] += 1
                            if res.get('scan_queued'):
                                self._state['metrics']['scans_queued'] += 1
                            self._state['processed_roots'].append({
                                'publisher': pub_name,
                                'path': pub_path,
                                'status': 'associated_existing',
                                'library_id': lib_id
                            })
                        else:
                            self._state['metrics']['sanitized_failures'] += 1
                            self._state['processed_roots'].append({
                                'publisher': pub_name,
                                'path': pub_path,
                                'status': status_res or 'failed'
                            })

                except Exception:
                    logger.error(f"[KAVITA-BACKFILL] Sanitized error processing publisher root: {pub_name}")
                    with self._state_lock:
                        self._state['processed_roots_count'] += 1
                        self._state['metrics']['sanitized_failures'] += 1
                        self._state['processed_roots'].append({
                            'publisher': pub_name,
                            'path': pub_path,
                            'status': 'error'
                        })

            with self._state_lock:
                self._state['status'] = 'completed'
                self._state['current_publisher'] = None
                self._state['end_time'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            logger.info(f"[KAVITA-BACKFILL] Completed backfill job {self._state['job_id']}")

        except Exception:
            logger.error("[KAVITA-BACKFILL] Fatal error in backfill worker execution.")
            with self._state_lock:
                self._state['status'] = 'failed'
                self._state['current_publisher'] = None
                self._state['end_time'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    def join_job(self, timeout=10.0):
        """
        Helper for unit tests: Await background worker thread completion deterministically.
        """
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
