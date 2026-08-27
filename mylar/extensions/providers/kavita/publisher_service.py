"""
Kavita Publisher-Library Automation Service (Phase K5).

Orchestrates duplicate-safe, bounded synchronous publisher-library creation,
exact literal-path association, library-specific scan queueing, transactional creation leases,
crash recovery, and exponential backoff.
"""

import os
import re
import time
import uuid
import datetime
import mylar
from mylar import logger, db
from mylar.config import get_mylar_instance_slug
from mylar.extensions.providers.kavita.auth import (
    KavitaCredentials,
    KavitaConfigurationError
)
from mylar.extensions.providers.kavita.config import (
    get_kavita_config_status,
    validate_kavita_url,
    build_kavita_credentials
)
from mylar.extensions.providers.kavita.client import (
    KavitaClient,
    KavitaError,
    KavitaAuthenticationError,
    KavitaPermissionError,
    KavitaTimeoutError,
    KavitaTransportError,
    KavitaInvalidResponseError,
    KavitaIncompatibleError,
    KavitaInvalidRequestError
)

# Operation budget constants (bounded post-processing tail execution)
TOTAL_OPERATION_BUDGET = 12.0
MIN_STEP_BUDGET = 0.5
LEASE_DURATION_SECONDS = 60

# In-memory informational notice for unsupported flat layout
_LATEST_AUTOMATION_NOTICE = None

# Closed sanitized error taxonomy with static user-facing explanations
KAVITA_ERROR_TAXONOMY = {
    'kavita_unavailable': {
        'message': "Kavita server is unreachable. Check network reachability and host configuration.",
        'retry_eligible': True
    },
    'kavita_timeout': {
        'message': "Request to Kavita server exceeded operation budget.",
        'retry_eligible': True
    },
    'kavita_auth_failed': {
        'message': "Kavita API authentication failed. Verify configured API key.",
        'retry_eligible': False
    },
    'kavita_permission_denied': {
        'message': "Kavita API key lacks sufficient permissions to manage or scan libraries.",
        'retry_eligible': False
    },
    'kavita_path_rejected': {
        'message': "Kavita server cannot access the specified library folder. Verify container volume mounts.",
        'retry_eligible': True
    },
    'kavita_create_rejected': {
        'message': "Kavita server rejected library creation request.",
        'retry_eligible': True
    },
    'kavita_scan_rejected': {
        'message': "Kavita server rejected library scan request.",
        'retry_eligible': True
    },
    'kavita_remote_missing': {
        'message': "Previously mapped Kavita library was deleted or cannot be found on the remote server.",
        'retry_eligible': True
    },
    'kavita_ambiguous_exact_path': {
        'message': "Multiple remote Kavita libraries claim this exact path. Manual resolution required in Kavita.",
        'retry_eligible': False
    },
    'kavita_invalid_response': {
        'message': "Kavita server returned a malformed or non-JSON response payload.",
        'retry_eligible': True
    },
    'kavita_incompatible': {
        'message': "Kavita server does not provide a compatible comic library type.",
        'retry_eligible': True
    },
    'kavita_transient_failure': {
        'message': "A transient network error occurred while communicating with Kavita.",
        'retry_eligible': True
    }
}


def sanitize_error_code(raw_code):
    """
    Ensure every error code is strictly one of the declared keys in KAVITA_ERROR_TAXONOMY.
    Never returns an unknown, arbitrary, or raw error string.
    """
    if not raw_code or not isinstance(raw_code, str):
        return 'kavita_transient_failure'

    code_lower = raw_code.strip().lower()
    if code_lower in KAVITA_ERROR_TAXONOMY:
        return code_lower

    if 'path' in code_lower:
        return 'kavita_path_rejected'
    if 'auth' in code_lower or 'key' in code_lower:
        return 'kavita_auth_failed'
    if 'permission' in code_lower or 'forbidden' in code_lower or '403' in code_lower:
        return 'kavita_permission_denied'
    if 'timeout' in code_lower:
        return 'kavita_timeout'
    if any(k in code_lower for k in ('transport', 'connection', 'unavailable', '500', '502', '503')):
        return 'kavita_unavailable'
    if 'incompatible' in code_lower or 'type' in code_lower:
        return 'kavita_incompatible'
    if 'scan' in code_lower:
        return 'kavita_scan_rejected'
    if 'create' in code_lower:
        return 'kavita_create_rejected'
    if 'json' in code_lower or 'response' in code_lower:
        return 'kavita_invalid_response'
    if 'missing' in code_lower or '404' in code_lower:
        return 'kavita_remote_missing'
    if 'ambiguous' in code_lower:
        return 'kavita_ambiguous_exact_path'

    return 'kavita_transient_failure'


def normalize_path_str(path_str):
    """
    Format normalization for comparison and persistence only.
    Normalizes separators, collapses duplicate slashes, and strips trailing slashes.
    Never translates paths or infers volume mounts.
    """
    if not path_str or not isinstance(path_str, str):
        return ""
    cleaned = path_str.strip().replace('\\', '/')
    while '//' in cleaned:
        cleaned = cleaned.replace('//', '/')
    return cleaned.rstrip('/')


def derive_materialized_publisher_root(series_location, comic_dir):
    """
    Derives the literal publisher-root directory from an authoritative series directory.
    Guarantees zero invented paths for flat layouts.

    Path hierarchy:
      1. Final imported file:   <COMIC_DIR>/<Publisher>/<Series>/<File.cbz>
      2. Series directory:      <COMIC_DIR>/<Publisher>/<Series>
      3. Publisher root folder: <COMIC_DIR>/<Publisher>
      4. Comic root directory:  <COMIC_DIR>

    :param series_location: Path to the series directory (e.g. /comics/Image Comics/Sunstone)
    :param comic_dir: Mylar root comic directory (e.g. /comics)
    :return: dict with status, publisher_path, series_path, and is_supported flag
    """
    if not series_location:
        return {
            'status': 'missing_location',
            'publisher_path': None,
            'series_path': None,
            'is_supported': False
        }

    norm_input = os.path.abspath(series_location)
    norm_comic_dir = os.path.abspath(comic_dir) if comic_dir else ""

    # If a file path was passed, normalize to its parent series directory
    if os.path.isfile(norm_input) or norm_input.lower().endswith(('.cbz', '.cbr', '.cbt', '.zip', '.rar', '.pdf')):
        norm_series_dir = os.path.dirname(norm_input)
    else:
        norm_series_dir = norm_input

    parent_publisher_dir = os.path.dirname(norm_series_dir)

    # Flat layout check: series is placed directly inside comic_dir root
    # e.g. norm_series_dir == '/comics/Sunstone', parent_publisher_dir == '/comics' == norm_comic_dir
    if norm_comic_dir and (parent_publisher_dir == norm_comic_dir or norm_series_dir == norm_comic_dir):
        return {
            'status': 'publisher_root_not_materialized',
            'publisher_path': None,
            'series_path': normalize_path_str(norm_series_dir),
            'is_supported': False
        }

    # Verify physical existence of candidate publisher directory on local disk
    if not os.path.isdir(parent_publisher_dir):
        return {
            'status': 'publisher_root_missing_on_disk',
            'publisher_path': None,
            'series_path': normalize_path_str(norm_series_dir),
            'is_supported': False
        }

    canonical_publisher_path = normalize_path_str(parent_publisher_dir)
    canonical_series_path = normalize_path_str(norm_series_dir)

    return {
        'status': 'materialized',
        'publisher_path': canonical_publisher_path,
        'series_path': canonical_series_path,
        'is_supported': True
    }


def calculate_backoff_delay_seconds(attempt_count):
    """
    Exponential backoff formula: 5m * 2^(attempt - 1), capped at 24 hours (86400s).
    """
    attempt = max(1, int(attempt_count or 1))
    delay_minutes = min(5 * (2 ** (attempt - 1)), 1440)
    return delay_minutes * 60


def parse_db_timestamp(ts_val):
    """Safely parse SQLite timestamp string or datetime object."""
    if not ts_val:
        return None
    if isinstance(ts_val, datetime.datetime):
        return ts_val
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M:%S.%f'):
        try:
            return datetime.datetime.strptime(str(ts_val), fmt)
        except ValueError:
            continue
    return None


def format_db_timestamp(dt_val):
    """Format datetime object as standard ISO SQLite timestamp."""
    if not dt_val:
        return None
    return dt_val.strftime('%Y-%m-%d %H:%M:%S')


def build_sanitized_mapping_dto(mapping_row):
    """
    Constructs an explicit sanitized view model/DTO for Modern UI presentation and API responses.
    Strictly excludes MylarInstanceID, LeaseToken, LeaseExpiresAt, Revision, and KavitaServerUrl.
    """
    if not mapping_row:
        return None

    if hasattr(mapping_row, 'keys'):
        row = dict(mapping_row)
    elif isinstance(mapping_row, dict):
        row = mapping_row
    else:
        return {}

    return {
        'mapping_id': row.get('MappingID'),
        'publisher_display_name': row.get('PublisherDisplayName'),
        'canonical_publisher_path': row.get('CanonicalPublisherPath'),
        'kavita_library_id': row.get('KavitaLibraryID'),
        'observed_library_name': row.get('ObservedLibraryName'),
        'provenance': row.get('Provenance'),
        'mapping_state': row.get('MappingState'),
        'last_scan_queued_at': row.get('LastScanQueuedAt'),
        'last_error_code': row.get('LastErrorCode'),
        'last_error_message': row.get('LastErrorMessage'),
        'next_eligible_retry_at': row.get('NextEligibleRetryAt'),
        'updated_at': row.get('UpdatedAt')
    }


class KavitaPublisherService:
    """
    Core publisher automation service implementing atomic creation leases,
    exact-path matching, dynamic type discovery, minimal OpenAPI payloads, and monotonic budgets.
    """

    def __init__(self, db_connection=None, client_factory=None):
        self._db = db_connection or db.DBConnection()
        self._client_factory = client_factory

    def _get_client(self, server_url, credentials=None):
        """Construct KavitaClient instance or use injected test factory."""
        if self._client_factory:
            return self._client_factory(server_url, credentials)
        return KavitaClient(base_url=server_url, credentials=credentials)

    def _record_sanitized_error(self, mapping_id, error_code, now, attempt_count, lease_token=None):
        """Record strictly closed sanitized error in database with exponential backoff retry timestamp."""
        sanitized_code = sanitize_error_code(error_code)
        tax = KAVITA_ERROR_TAXONOMY.get(sanitized_code, KAVITA_ERROR_TAXONOMY['kavita_transient_failure'])
        safe_msg = tax['message']
        retry_eligible = tax['retry_eligible']

        next_retry = None
        if retry_eligible:
            delay_sec = calculate_backoff_delay_seconds(attempt_count)
            next_retry = now + datetime.timedelta(seconds=delay_sec)

        state = 'path_rejected' if sanitized_code == 'kavita_path_rejected' else 'transient_error'

        params = [
            state,
            sanitized_code,
            safe_msg,
            format_db_timestamp(now),
            format_db_timestamp(next_retry),
            int(attempt_count or 1),
            int(mapping_id)
        ]

        token_clause = " AND LeaseToken = ?" if lease_token else ""
        if lease_token:
            params.append(lease_token)

        self._db.action(
            f"""
            UPDATE ext_kavita_publisher_mappings
            SET MappingState = ?,
                LastErrorCode = ?,
                LastErrorMessage = ?,
                LastErrorTimestamp = ?,
                NextEligibleRetryAt = ?,
                AttemptCount = ?,
                LeaseToken = NULL,
                LeaseExpiresAt = NULL,
                Revision = Revision + 1,
                UpdatedAt = CURRENT_TIMESTAMP
            WHERE MappingID = ?{token_clause}
            """,
            params
        )

        logger.fdebug(f"[KAVITA-AUTOMATION] Publisher mapping ID {mapping_id} recorded sanitized error code '{sanitized_code}'.")

    def _record_active_scan_failure(self, mapping_id, error_code, now, current_attempt_count):
        """
        Record sanitized scan failure on an ACTIVE mapping without deactivating or altering provenance.
        Persists closed taxonomy error code, static message, incremented attempt count, and exponential backoff timestamp.
        """
        sanitized_code = sanitize_error_code(error_code)
        tax = KAVITA_ERROR_TAXONOMY.get(sanitized_code, KAVITA_ERROR_TAXONOMY['kavita_scan_rejected'])
        safe_msg = tax['message']

        new_attempt = int(current_attempt_count or 0) + 1
        delay_sec = calculate_backoff_delay_seconds(new_attempt)
        next_retry = now + datetime.timedelta(seconds=delay_sec)

        self._db.action(
            """
            UPDATE ext_kavita_publisher_mappings
            SET LastErrorCode = ?,
                LastErrorMessage = ?,
                LastErrorTimestamp = ?,
                NextEligibleRetryAt = ?,
                AttemptCount = ?,
                UpdatedAt = CURRENT_TIMESTAMP
            WHERE MappingID = ? AND MappingState = 'active'
            """,
            [
                sanitized_code,
                safe_msg,
                format_db_timestamp(now),
                format_db_timestamp(next_retry),
                new_attempt,
                int(mapping_id)
            ]
        )

        logger.fdebug(f"[KAVITA-AUTOMATION] Active publisher mapping ID {mapping_id} recorded scan failure '{sanitized_code}' (attempt {new_attempt}).")

    def _record_active_scan_success(self, mapping_id):
        """
        Record successful scan queueing on an ACTIVE mapping.
        Updates LastScanQueuedAt and clears all error and backoff retry fields.
        """
        self._db.action(
            """
            UPDATE ext_kavita_publisher_mappings
            SET LastScanQueuedAt = CURRENT_TIMESTAMP,
                LastErrorCode = NULL,
                LastErrorMessage = NULL,
                LastErrorTimestamp = NULL,
                NextEligibleRetryAt = NULL,
                AttemptCount = 0,
                UpdatedAt = CURRENT_TIMESTAMP
            WHERE MappingID = ?
            """,
            [int(mapping_id)]
        )

    def acquire_or_evaluate_mapping_lease(self, instance_id, pub_key, pub_display, canonical_path, server_url, now):
        """
        Transactional lease acquisition and mapping evaluation.
        Uses INSERT OR IGNORE and CAS revisions to guarantee duplicate-safe concurrency.
        """
        lease_token = str(uuid.uuid4())
        lease_expiry = now + datetime.timedelta(seconds=LEASE_DURATION_SECONDS)

        # 1. Atomic Insert Attempt for first-ever import
        try:
            self._db.action(
                """
                INSERT OR IGNORE INTO ext_kavita_publisher_mappings (
                    MylarInstanceID, PublisherKey, PublisherDisplayName, CanonicalPublisherPath,
                    KavitaServerUrl, Provenance, MappingState, LeaseToken, LeaseExpiresAt, Revision, AttemptCount
                ) VALUES (?, ?, ?, ?, ?, 'mylar_created', 'creating', ?, ?, 1, 1)
                """,
                [instance_id, pub_key, pub_display, canonical_path, server_url, lease_token, format_db_timestamp(lease_expiry)]
            )
        except Exception:
            logger.fdebug("[KAVITA-AUTOMATION] Error during lease insert.")

        # Query current row status
        rows = self._db.select(
            """
            SELECT MappingID, MappingState, KavitaLibraryID, ObservedLibraryName, LibraryTypeID,
                   Provenance, LeaseToken, LeaseExpiresAt, Revision, AttemptCount, NextEligibleRetryAt
            FROM ext_kavita_publisher_mappings
            WHERE MylarInstanceID = ? AND CanonicalPublisherPath = ? AND KavitaServerUrl = ?
            """,
            [instance_id, canonical_path, server_url]
        )

        if not rows:
            return {'action': 'abort', 'reason': 'db_unavailable'}

        row = rows[0]
        # Check if caller won initial insert
        if row['LeaseToken'] == lease_token and row['MappingState'] == 'creating':
            return {
                'action': 'execute_create',
                'lease_token': lease_token,
                'mapping_id': row['MappingID'],
                'attempt_count': 1
            }

        # 2A. Active mapping -> scan queue
        if row['MappingState'] == 'active' and row['KavitaLibraryID']:
            return {'action': 'execute_scan', 'mapping': row}

        # 2B. Terminal Ambiguity -> halt
        if row['MappingState'] == 'ambiguous_multi_path':
            return {'action': 'abort', 'reason': 'ambiguous_multi_path'}

        # 2C. Backoff active check
        if row['NextEligibleRetryAt']:
            next_retry_dt = parse_db_timestamp(row['NextEligibleRetryAt'])
            if next_retry_dt and next_retry_dt > now:
                logger.fdebug("[KAVITA-AUTOMATION] Mapping is in backoff window. Skipping create.")
                return {'action': 'abort', 'reason': 'backoff_active'}

        # 2D. Active Concurrent Lease Held by another local worker
        if row['MappingState'] == 'creating' and row['LeaseExpiresAt']:
            lease_exp_dt = parse_db_timestamp(row['LeaseExpiresAt'])
            if lease_exp_dt and lease_exp_dt > now:
                logger.fdebug("[KAVITA-AUTOMATION] Concurrent creation lease active. Skipping create.")
                return {'action': 'abort', 'reason': 'concurrent_lease_active'}

        # 2E. Stale Lease or Backoff Expired -> Atomic Compare-And-Set
        new_attempt = int(row['AttemptCount'] or 0) + 1
        self._db.action(
            """
            UPDATE ext_kavita_publisher_mappings
            SET MappingState = 'creating',
                LeaseToken = ?,
                LeaseExpiresAt = ?,
                Revision = Revision + 1,
                AttemptCount = ?,
                UpdatedAt = CURRENT_TIMESTAMP
            WHERE MappingID = ? AND Revision = ?
            """,
            [lease_token, format_db_timestamp(lease_expiry), new_attempt, row['MappingID'], row['Revision']]
        )

        check_rows = self._db.select(
            "SELECT LeaseToken FROM ext_kavita_publisher_mappings WHERE MappingID = ?",
            [row['MappingID']]
        )

        if check_rows and check_rows[0]['LeaseToken'] == lease_token:
            return {
                'action': 'execute_create',
                'lease_token': lease_token,
                'mapping_id': row['MappingID'],
                'attempt_count': new_attempt
            }
        else:
            return {'action': 'abort', 'reason': 'cas_lease_contention'}

    def derive_collision_safe_name(self, publisher_name, remote_libraries):
        """
        Derives clean publisher name or deterministic instance-qualified name
        only when another library on a different folder path claims the clean name.
        """
        clean_name = str(publisher_name).strip() if publisher_name else "Unknown Publisher"
        instance_slug = get_mylar_instance_slug()

        colliding = any(
            str(lib.get('name', '')).strip().lower() == clean_name.lower()
            for lib in (remote_libraries or [])
            if isinstance(lib, dict)
        )

        if colliding:
            slug_suffix = f" ({instance_slug})" if instance_slug else ""
            return f"{clean_name}{slug_suffix}"
        return clean_name

    def process_post_import_automation(self, comic_data):
        """
        Synchronous bounded automation hook executed after Mylar post-processing finishes.
        """
        global _LATEST_AUTOMATION_NOTICE

        # 1. Config check
        config_status = get_kavita_config_status()
        if not config_status.get('enabled', False) or not config_status.get('is_configured', False):
            return {'status': 'disabled_or_unconfigured'}

        # 2. Derive materialized publisher path
        series_location = (
            comic_data.get('SeriesLocation') or
            comic_data.get('ComicLocation') or
            comic_data.get('location') or
            ''
        )
        comic_dir = getattr(mylar.CONFIG, 'COMIC_DIR', None)
        path_info = derive_materialized_publisher_root(series_location, comic_dir)

        if not path_info['is_supported']:
            if path_info['status'] == 'publisher_root_not_materialized':
                _LATEST_AUTOMATION_NOTICE = "Automatic Kavita publisher libraries require comics to be organized in publisher subdirectories. Flat series layouts are unsupported for automated publisher library creation."
            return {'status': path_info['status']}

        canonical_path = path_info['publisher_path']
        publisher_display = str(comic_data.get('ComicPublisher') or comic_data.get('publisher') or 'Unknown Publisher').strip()
        pub_key = publisher_display.lower()
        server_url = validate_kavita_url(config_status['url'])
        instance_id = mylar.CONFIG.MYLAR_INSTANCE_ID
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

        # 3. Acquire Lease / Check Mapping
        lease_info = self.acquire_or_evaluate_mapping_lease(
            instance_id, pub_key, publisher_display, canonical_path, server_url, now
        )

        if lease_info['action'] == 'abort':
            return {'status': 'aborted', 'reason': lease_info['reason']}

        # 4. Initialize Monotonic Operation Budget
        deadline = time.monotonic() + TOTAL_OPERATION_BUDGET
        creds = build_kavita_credentials()
        client = self._get_client(server_url, credentials=creds)

        # ---------------------------------------------------------------------
        # Branch A: Execute Scan on Active Mapping
        # ---------------------------------------------------------------------
        if lease_info['action'] == 'execute_scan':
            mapping = dict(lease_info['mapping'])
            lib_id = mapping.get('KavitaLibraryID')

            # Check scan backoff eligibility
            if mapping.get('NextEligibleRetryAt'):
                next_retry_dt = parse_db_timestamp(mapping['NextEligibleRetryAt'])
                if next_retry_dt and next_retry_dt > now:
                    logger.fdebug("[KAVITA-AUTOMATION] Active mapping scan is in backoff window. Skipping scan request.")
                    return {'status': 'scan_backoff_active', 'retry_at': mapping['NextEligibleRetryAt']}

            # Check operation budget
            if time.monotonic() + MIN_STEP_BUDGET >= deadline:
                self._record_active_scan_failure(mapping['MappingID'], 'kavita_timeout', now, mapping.get('AttemptCount', 0))
                return {'status': 'timeout'}

            rem = deadline - time.monotonic()
            timeout_pair = (min(5.0, rem), min(10.0, rem))

            try:
                # Contract-exact scan: query parameter libraryId, zero JSON body
                client.post(
                    f"api/Library/scan?libraryId={lib_id}&force=false",
                    timeout_override=timeout_pair
                )
                self._record_active_scan_success(mapping['MappingID'])
                return {'status': 'scan_queued', 'library_id': lib_id}
            except KavitaInvalidRequestError as e:
                if getattr(e, 'status_code', None) == 404:
                    self._db.action(
                        """
                        UPDATE ext_kavita_publisher_mappings
                        SET MappingState = 'remote_missing',
                            LastErrorCode = 'kavita_remote_missing',
                            LastErrorMessage = ?,
                            LastErrorTimestamp = CURRENT_TIMESTAMP,
                            NextEligibleRetryAt = ?,
                            AttemptCount = ?,
                            Revision = Revision + 1,
                            UpdatedAt = CURRENT_TIMESTAMP
                        WHERE MappingID = ?
                        """,
                        [
                            KAVITA_ERROR_TAXONOMY['kavita_remote_missing']['message'],
                            format_db_timestamp(now + datetime.timedelta(seconds=calculate_backoff_delay_seconds(1))),
                            1,
                            mapping['MappingID']
                        ]
                    )
                    return {'status': 'remote_missing'}

                self._record_active_scan_failure(mapping['MappingID'], 'kavita_scan_rejected', now, mapping.get('AttemptCount', 0))
                return {'status': 'scan_rejected'}
            except KavitaError as e:
                scan_code = getattr(e, 'error_code', 'kavita_transient_failure')
                self._record_active_scan_failure(mapping['MappingID'], scan_code, now, mapping.get('AttemptCount', 0))
                return {'status': 'error', 'code': sanitize_error_code(scan_code)}

        # ---------------------------------------------------------------------
        # Branch B: Creation / Association Execution Flow
        # ---------------------------------------------------------------------
        lease_token = lease_info['lease_token']
        mapping_id = lease_info['mapping_id']
        attempt_count = lease_info['attempt_count']

        # Step B1: Remote Library Inspection
        if time.monotonic() + MIN_STEP_BUDGET >= deadline:
            self._record_sanitized_error(mapping_id, 'kavita_timeout', now, attempt_count, lease_token=lease_token)
            return {'status': 'timeout'}

        rem = deadline - time.monotonic()
        timeout_pair = (min(5.0, rem), min(10.0, rem))

        try:
            remote_libraries = client.get('api/Library/libraries', timeout_override=timeout_pair)
        except KavitaError as e:
            self._record_sanitized_error(mapping_id, getattr(e, 'error_code', 'kavita_unavailable'), now, attempt_count, lease_token=lease_token)
            return {'status': 'remote_inspection_failed'}

        if not isinstance(remote_libraries, list):
            remote_libraries = []

        # Find exact literal path matches (zero prefix/parent matching)
        exact_matches = [
            lib for lib in remote_libraries
            if isinstance(lib, dict) and canonical_path in [normalize_path_str(f) for f in lib.get('folders', []) if f]
        ]

        # Case B1: Exactly 1 Exact-Path Remote Library (Associate)
        if len(exact_matches) == 1:
            matched = exact_matches[0]
            matched_id = matched.get('id', matched.get('libraryId'))
            matched_name = matched.get('name')

            self._db.action(
                """
                UPDATE ext_kavita_publisher_mappings
                SET MappingState = 'active',
                    KavitaLibraryID = ?,
                    ObservedLibraryName = ?,
                    Provenance = 'existing_exact_path',
                    LeaseToken = NULL,
                    LeaseExpiresAt = NULL,
                    AttemptCount = 0,
                    NextEligibleRetryAt = NULL,
                    LastErrorCode = NULL,
                    LastErrorMessage = NULL,
                    LastErrorTimestamp = NULL,
                    Revision = Revision + 1,
                    UpdatedAt = CURRENT_TIMESTAMP
                WHERE MappingID = ? AND LeaseToken = ?
                """,
                [matched_id, matched_name, mapping_id, lease_token]
            )

            # Queue scan for newly associated library if budget allows
            scan_queued = False
            if time.monotonic() + MIN_STEP_BUDGET < deadline:
                rem = deadline - time.monotonic()
                try:
                    client.post(
                        f"api/Library/scan?libraryId={matched_id}&force=false",
                        timeout_override=(min(5.0, rem), min(10.0, rem))
                    )
                    self._record_active_scan_success(mapping_id)
                    scan_queued = True
                except KavitaError as scan_err:
                    scan_code = getattr(scan_err, 'error_code', 'kavita_scan_rejected')
                    self._record_active_scan_failure(mapping_id, scan_code, now, 0)
            else:
                self._record_active_scan_failure(mapping_id, 'kavita_timeout', now, 0)

            return {'status': 'associated_existing', 'library_id': matched_id, 'scan_queued': scan_queued}

        # Case B2: Multiple Exact Paths (Terminal Ambiguity)
        if len(exact_matches) > 1:
            self._db.action(
                """
                UPDATE ext_kavita_publisher_mappings
                SET MappingState = 'ambiguous_multi_path',
                    LeaseToken = NULL,
                    LeaseExpiresAt = NULL,
                    LastErrorCode = 'kavita_ambiguous_exact_path',
                    LastErrorMessage = ?,
                    LastErrorTimestamp = CURRENT_TIMESTAMP,
                    Revision = Revision + 1,
                    UpdatedAt = CURRENT_TIMESTAMP
                WHERE MappingID = ? AND LeaseToken = ?
                """,
                [KAVITA_ERROR_TAXONOMY['kavita_ambiguous_exact_path']['message'], mapping_id, lease_token]
            )
            return {'status': 'ambiguous_multi_path'}

        # Step B3: Discover Dynamic Comic Library Type (Fail Closed)
        if time.monotonic() + MIN_STEP_BUDGET >= deadline:
            self._record_sanitized_error(mapping_id, 'kavita_timeout', now, attempt_count, lease_token=lease_token)
            return {'status': 'timeout'}

        rem = deadline - time.monotonic()
        types_data = None
        types_error = None
        try:
            types_data = client.get('api/Settings/library-types', timeout_override=(min(5.0, rem), min(10.0, rem)))
        except KavitaError as err:
            types_error = err

        comic_type_id = None
        if isinstance(types_data, list):
            for item in types_data:
                if isinstance(item, dict):
                    t_name = str(item.get('name', item.get('title', ''))).lower()
                    if 'comic' in t_name:
                        comic_type_id = item.get('id', item.get('value'))
                        break

        # FAIL CLOSED: If discovery fails, response is malformed, or no comic type found
        if comic_type_id is None:
            err_code = 'kavita_incompatible'
            if types_error:
                err_code = getattr(types_error, 'error_code', 'kavita_unavailable')
            self._record_sanitized_error(mapping_id, err_code, now, attempt_count, lease_token=lease_token)
            return {'status': 'comic_type_unavailable'}

        proposed_name = self.derive_collision_safe_name(publisher_display, remote_libraries)

        # Minimal UpdateLibraryDto payload: strictly name, type, folders
        create_payload = {
            "name": proposed_name,
            "type": comic_type_id,
            "folders": [canonical_path]
        }

        if time.monotonic() + MIN_STEP_BUDGET >= deadline:
            self._record_sanitized_error(mapping_id, 'kavita_timeout', now, attempt_count, lease_token=lease_token)
            return {'status': 'timeout'}

        rem = deadline - time.monotonic()
        create_dto = None
        create_error = None

        try:
            create_dto = client.post(
                'api/Library/create',
                json_body=create_payload,
                timeout_override=(min(5.0, rem), min(10.0, rem))
            )
        except KavitaError as err:
            create_error = err

        if create_dto and isinstance(create_dto, dict):
            new_lib_id = create_dto.get('id', create_dto.get('libraryId'))
            self._db.action(
                """
                UPDATE ext_kavita_publisher_mappings
                SET MappingState = 'active',
                    KavitaLibraryID = ?,
                    ObservedLibraryName = ?,
                    LibraryTypeID = ?,
                    Provenance = 'mylar_created',
                    LeaseToken = NULL,
                    LeaseExpiresAt = NULL,
                    AttemptCount = 0,
                    NextEligibleRetryAt = NULL,
                    LastErrorCode = NULL,
                    LastErrorMessage = NULL,
                    LastErrorTimestamp = NULL,
                    Revision = Revision + 1,
                    UpdatedAt = CURRENT_TIMESTAMP
                WHERE MappingID = ? AND LeaseToken = ?
                """,
                [new_lib_id, proposed_name, comic_type_id, mapping_id, lease_token]
            )

            # Queue initial scan if budget allows
            scan_queued = False
            if time.monotonic() + MIN_STEP_BUDGET < deadline:
                rem = deadline - time.monotonic()
                try:
                    client.post(
                        f"api/Library/scan?libraryId={new_lib_id}&force=false",
                        timeout_override=(min(5.0, rem), min(10.0, rem))
                    )
                    self._record_active_scan_success(mapping_id)
                    scan_queued = True
                except KavitaError as scan_err:
                    scan_code = getattr(scan_err, 'error_code', 'kavita_scan_rejected')
                    self._record_active_scan_failure(mapping_id, scan_code, now, 0)
            else:
                self._record_active_scan_failure(mapping_id, 'kavita_timeout', now, 0)

            return {'status': 'created', 'library_id': new_lib_id, 'scan_queued': scan_queued}

        # Post-Failure Collision / Crash Recovery
        if time.monotonic() + MIN_STEP_BUDGET < deadline:
            rem = deadline - time.monotonic()
            try:
                recovery_libs = client.get('api/Library/libraries', timeout_override=(min(5.0, rem), min(10.0, rem)))
                if isinstance(recovery_libs, list):
                    rec_matches = [
                        lib for lib in recovery_libs
                        if isinstance(lib, dict) and canonical_path in [normalize_path_str(f) for f in lib.get('folders', []) if f]
                    ]
                    if len(rec_matches) == 1:
                        matched = rec_matches[0]
                        matched_id = matched.get('id', matched.get('libraryId'))
                        self._db.action(
                            """
                            UPDATE ext_kavita_publisher_mappings
                            SET MappingState = 'active',
                                KavitaLibraryID = ?,
                                ObservedLibraryName = ?,
                                Provenance = 'existing_exact_path',
                                LeaseToken = NULL,
                                LeaseExpiresAt = NULL,
                                AttemptCount = 0,
                                NextEligibleRetryAt = NULL,
                                LastErrorCode = NULL,
                                LastErrorMessage = NULL,
                                LastErrorTimestamp = NULL,
                                Revision = Revision + 1,
                                UpdatedAt = CURRENT_TIMESTAMP
                            WHERE MappingID = ? AND LeaseToken = ?
                            """,
                            [matched_id, matched.get('name'), mapping_id, lease_token]
                        )
                        return {'status': 'associated_after_recovery', 'library_id': matched_id}
                    elif len(rec_matches) > 1:
                        self._db.action(
                            """
                            UPDATE ext_kavita_publisher_mappings
                            SET MappingState = 'ambiguous_multi_path',
                                LeaseToken = NULL,
                                LeaseExpiresAt = NULL,
                                LastErrorCode = 'kavita_ambiguous_exact_path',
                                LastErrorMessage = ?,
                                LastErrorTimestamp = CURRENT_TIMESTAMP,
                                Revision = Revision + 1,
                                UpdatedAt = CURRENT_TIMESTAMP
                            WHERE MappingID = ? AND LeaseToken = ?
                            """,
                            [KAVITA_ERROR_TAXONOMY['kavita_ambiguous_exact_path']['message'], mapping_id, lease_token]
                        )
                        return {'status': 'ambiguous_multi_path'}
            except Exception:
                pass

        # Record Backoff Error
        err_msg_str = str(create_error).lower() if create_error else ''
        if 'path' in err_msg_str or (hasattr(create_error, 'status_code') and create_error.status_code == 400):
            err_code = 'kavita_path_rejected'
        else:
            err_code = getattr(create_error, 'error_code', 'kavita_create_rejected')

        self._record_sanitized_error(mapping_id, err_code, now, attempt_count, lease_token=lease_token)
        return {'status': 'create_failed', 'code': sanitize_error_code(err_code)}


def handle_post_processing_kavita_automation(comic_data, db_connection=None, client_factory=None):
    """
    Public entrypoint for Mylar PostProcessor.
    Guarantees zero exceptions escape and zero Mylar import rollbacks occur.
    """
    try:
        service = KavitaPublisherService(db_connection=db_connection, client_factory=client_factory)
        return service.process_post_import_automation(comic_data)
    except Exception:
        logger.fdebug("[KAVITA-AUTOMATION] Caught unhandled exception during automation.")
        return {'status': 'exception_suppressed'}


def get_kavita_publisher_mappings(db_connection=None):
    """
    Retrieve sanitized publisher mappings list for Modern UI presentation.
    Guarantees zero full instance UUIDs, lease tokens, or internal fields are returned.
    """
    my_db = db_connection or db.DBConnection()
    try:
        rows = my_db.select(
            """
            SELECT MappingID, PublisherKey, PublisherDisplayName,
                   CanonicalPublisherPath, KavitaLibraryID, ObservedLibraryName,
                   LibraryTypeID, Provenance, MappingState, AttemptCount, NextEligibleRetryAt,
                   LastScanQueuedAt, LastVerifiedAt, LastErrorCode, LastErrorMessage,
                   LastErrorTimestamp, CreatedAt, UpdatedAt
            FROM ext_kavita_publisher_mappings
            ORDER BY UpdatedAt DESC
            """
        )
        if not rows:
            return []
        return [build_sanitized_mapping_dto(r) for r in rows if r]
    except Exception:
        return []


def get_latest_automation_notice():
    """Retrieve in-memory automation notice (e.g. flat layout informational message)."""
    global _LATEST_AUTOMATION_NOTICE
    return _LATEST_AUTOMATION_NOTICE
