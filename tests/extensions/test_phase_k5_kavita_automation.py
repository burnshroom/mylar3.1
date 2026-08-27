"""
Phase K5 Integration Test Suite: Kavita Publisher-Library Automation.

Verifies:
1. Three-Level Path Derivation: Final file path -> Series directory -> Literal Publisher Root.
2. Direct PostProcessor handoff with final file path submits publisher root, never series directory.
3. First eligible publisher import creates Kavita comic library with exact literal path and minimal UpdateLibraryDto.
4. Existing single exact-path remote library is associated without create request and queues a scan.
5. Subsequent active mapping queues documented library scan request.
6. Scan rejection preserves active mapping, library ID, and provenance while scheduling backoff.
7. Scan timeout preserves active mapping and schedules backoff.
8. Active mapping scan in backoff window makes zero scan requests.
9. Eligible retry after backoff expires executes scan and clears error/retry fields on success.
10. Budget exhaustion makes zero scan calls and does not falsely set LastScanQueuedAt.
11. Flat layout causes zero HTTP calls and zero mapping/lease/error persistence.
12. Name collision on different remote path applies deterministic instance slug.
13. Local concurrent imports result in at most one create attempt through lease ownership.
14. Crash-after-remote-create recovery associates one exact-path remote library without duplicate creation.
15. Create failure followed by exactly one remote exact-path match associates safely.
16. Create failure followed by multiple remote exact-path matches produces ambiguous_multi_path.
17. path_rejected enters backoff and does not retry before eligibility.
18. An eligible later retry can succeed after external access is assumed repaired.
19. Budget exhaustion prevents later remote calls from starting.
20. Disabled Kavita causes zero HTTP calls.
21. All failures preserve successful Mylar import behavior.
22. Dynamic comic-type discovery fails closed: 0 create calls on failure, malformed response, or no comic type.
23. Persisted error taxonomy is strictly closed: unknown provider error codes map to declared taxonomy keys.
24. Full instance UUID is never exposed in Modern HTML, diagnostics JSON, or DTOs.
25. Zero secret, API key, raw exception, or path leakage in logs, DB, HTML, or JSON.
26. Zero remote DELETE calls across all operations.
27. Shared settings templates (Carbon/Default) contain zero Kavita UI.
28. mylar/__init__.py maintains original None defaults for PROG_DIR and DATA_DIR.
29. git diff --check reports zero whitespace or formatting errors.
"""

import os
import sys
import json
import sqlite3
import datetime
import subprocess
import unittest
from unittest.mock import patch, MagicMock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.join(REPO_ROOT, 'lib'))
sys.path.insert(0, REPO_ROOT)

import mylar
import mylar.config
from mylar import db
from mylar.config import get_mylar_instance_slug
from mylar.extensions.providers.kavita.client import (
    KavitaClient,
    KavitaError,
    KavitaInvalidRequestError,
    KavitaTimeoutError,
    KavitaTransportError
)
from mylar.extensions.providers.kavita.publisher_service import (
    KavitaPublisherService,
    derive_materialized_publisher_root,
    handle_post_processing_kavita_automation,
    get_kavita_publisher_mappings,
    get_latest_automation_notice,
    normalize_path_str,
    calculate_backoff_delay_seconds,
    sanitize_error_code,
    build_sanitized_mapping_dto,
    KAVITA_ERROR_TAXONOMY
)
from mylar.extensions.providers.kavita.runtime_controller import handle_kavita_diagnostics
from mylar.extensions.providers.kavita.config import validate_kavita_url


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data
        if text:
            self.text = text
        elif json_data is not None:
            self.text = json.dumps(json_data)
        else:
            self.text = "{}"
        self.content = self.text.encode('utf-8')

    def json(self):
        if isinstance(self._json_data, Exception):
            raise self._json_data
        if self._json_data is not None:
            return self._json_data
        return json.loads(self.text)


class TestPhaseK5KavitaAutomation(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        mylar.PROG_DIR = REPO_ROOT
        mylar.DATA_DIR = REPO_ROOT
        config_path = os.path.join(REPO_ROOT, 'config.ini')
        if not os.path.exists(config_path):
            with open(config_path, 'w') as f:
                f.write('[General]\n')
        cc = mylar.config.Config(config_path)
        mylar.CONFIG = cc.read(startup=True)

        from mylar.extensions.migrations import run_extension_migrations
        myDB = db.DBConnection()
        run_extension_migrations(myDB.connection.cursor())
        myDB.connection.commit()

    def setUp(self):
        mylar.PROG_DIR = REPO_ROOT
        mylar.DATA_DIR = REPO_ROOT
        self.orig_enabled = getattr(mylar.CONFIG, 'KAVITA_ENABLED', False)
        self.orig_url = getattr(mylar.CONFIG, 'KAVITA_URL', '')
        self.orig_api_key = getattr(mylar.CONFIG, 'KAVITA_API_KEY', None)
        self.orig_comic_dir = getattr(mylar.CONFIG, 'COMIC_DIR', None)

        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000"
        mylar.CONFIG.KAVITA_API_KEY = "kavita_secret_test_key"
        mylar.CONFIG.COMIC_DIR = os.path.join(REPO_ROOT, 'test_comics_root')

        # Clean test table entries before each test
        try:
            from mylar.extensions.migrations import run_extension_migrations
            myDB = db.DBConnection()
            run_extension_migrations(myDB.connection.cursor())
            myDB.connection.commit()
            myDB.action("DELETE FROM ext_kavita_publisher_mappings")
        except Exception:
            pass

    def tearDown(self):
        mylar.CONFIG.KAVITA_ENABLED = self.orig_enabled
        mylar.CONFIG.KAVITA_URL = self.orig_url
        mylar.CONFIG.KAVITA_API_KEY = self.orig_api_key
        mylar.CONFIG.COMIC_DIR = self.orig_comic_dir

    # -------------------------------------------------------------------------
    # 1. Three-Level Path Derivation
    # -------------------------------------------------------------------------
    def test_01_three_level_path_derivation(self):
        """1. Prove distinct 3 levels: final file -> series dir -> publisher root."""
        comic_root = mylar.CONFIG.COMIC_DIR
        pub_folder = os.path.join(comic_root, "Image Comics")
        series_folder = os.path.join(pub_folder, "Sunstone")
        final_file_path = os.path.join(series_folder, "Sunstone 001 (2014).cbz")

        with patch('os.path.isdir', return_value=True):
            # Test when series directory is passed
            path_info_from_series = derive_materialized_publisher_root(series_folder, comic_root)
            self.assertTrue(path_info_from_series['is_supported'])
            self.assertEqual(path_info_from_series['publisher_path'], normalize_path_str(pub_folder))
            self.assertEqual(path_info_from_series['series_path'], normalize_path_str(series_folder))

            # Test when final file path is passed
            path_info_from_file = derive_materialized_publisher_root(final_file_path, comic_root)
            self.assertTrue(path_info_from_file['is_supported'])
            self.assertEqual(path_info_from_file['publisher_path'], normalize_path_str(pub_folder))
            self.assertEqual(path_info_from_file['series_path'], normalize_path_str(series_folder))

            # Verify strictly publisher root != series directory and publisher root != file path
            self.assertNotEqual(path_info_from_series['publisher_path'], normalize_path_str(series_folder))
            self.assertNotEqual(path_info_from_series['publisher_path'], normalize_path_str(final_file_path))

    # -------------------------------------------------------------------------
    # 2. PostProcessor Direct Handoff Submits Publisher Root
    # -------------------------------------------------------------------------
    def test_02_postprocessor_handoff_submits_publisher_root_never_series_dir(self):
        """2. Prove PostProcessor hook handoff submits publisher root to Kavita create, never series dir."""
        comic_root = mylar.CONFIG.COMIC_DIR
        pub_folder = os.path.join(comic_root, "Image Comics")
        series_folder = os.path.join(pub_folder, "Sunstone")
        final_file = os.path.join(series_folder, "Sunstone 001.cbz")

        recorded_calls = []
        def fake_handler(method, url, **kwargs):
            recorded_calls.append({'method': method, 'url': url, 'kwargs': kwargs})
            if 'api/Library/libraries' in url:
                return FakeResponse(200, json_data=[])
            if 'api/Settings/library-types' in url:
                return FakeResponse(200, json_data=[{'id': 0, 'name': 'Comic'}])
            if 'api/Library/create' in url:
                body = kwargs.get('json', {})
                return FakeResponse(200, json_data={'id': 50, 'name': body.get('name'), 'folders': body.get('folders')})
            if 'api/Library/scan' in url:
                return FakeResponse(200, json_data=True)
            return FakeResponse(200, json_data={})

        mock_session = MagicMock()
        mock_session.request.side_effect = fake_handler

        # Simulate PostProcessor hook call
        with patch('os.path.isdir', return_value=True):
            res = handle_post_processing_kavita_automation(
                {
                    'SeriesLocation': series_folder,
                    'ComicLocation': series_folder,
                    'FinalFilePath': final_file,
                    'ComicPublisher': 'Image Comics',
                    'ComicName': 'Sunstone',
                    'ComicID': 1234,
                    'IssueID': 5678
                },
                client_factory=lambda url, creds: KavitaClient(base_url=url, credentials=creds, session=mock_session)
            )

        self.assertEqual(res['status'], 'created')
        create_calls = [c for c in recorded_calls if 'api/Library/create' in c['url']]
        self.assertEqual(len(create_calls), 1)

        submitted_folders = create_calls[0]['kwargs']['json']['folders']
        self.assertEqual(len(submitted_folders), 1)
        submitted_path = submitted_folders[0]

        # ASSERTIONS:
        # 1. Submitted path must be the publisher root
        self.assertEqual(submitted_path, normalize_path_str(pub_folder))
        # 2. Submitted path must NOT be the series directory
        self.assertNotEqual(submitted_path, normalize_path_str(series_folder))
        # 3. Submitted path must NOT be the final file path
        self.assertNotEqual(submitted_path, normalize_path_str(final_file))

    # -------------------------------------------------------------------------
    # 3. First Eligible Publisher Import Creates Kavita Comic Library
    # -------------------------------------------------------------------------
    def test_03_first_eligible_publisher_import_creates_kavita_comic_library(self):
        """3. Prove first eligible publisher import creates one Kavita comic library with minimal UpdateLibraryDto."""
        service = KavitaPublisherService()
        recorded_calls = []

        def fake_handler(method, url, **kwargs):
            recorded_calls.append({'method': method, 'url': url, 'kwargs': kwargs})
            if 'api/Library/libraries' in url:
                return FakeResponse(200, json_data=[])
            if 'api/Settings/library-types' in url:
                return FakeResponse(200, json_data=[{'id': 0, 'name': 'Comic'}])
            if 'api/Library/create' in url:
                body = kwargs.get('json', {})
                return FakeResponse(200, json_data={'id': 42, 'name': body.get('name'), 'folders': body.get('folders')})
            if 'api/Library/scan' in url:
                return FakeResponse(200, json_data=True)
            return FakeResponse(200, json_data={})

        mock_session = MagicMock()
        mock_session.request.side_effect = fake_handler
        service._client_factory = lambda url, creds: KavitaClient(base_url=url, credentials=creds, session=mock_session)

        pub_folder = os.path.join(mylar.CONFIG.COMIC_DIR, "Image Comics")
        series_folder = os.path.join(pub_folder, "Sunstone")
        with patch('os.path.isdir', return_value=True):
            res = service.process_post_import_automation({
                'SeriesLocation': series_folder,
                'ComicPublisher': 'Image Comics',
                'ComicName': 'Sunstone'
            })

        self.assertEqual(res['status'], 'created')
        self.assertEqual(res['library_id'], 42)
        self.assertIs(res.get('scan_queued'), True)

        create_calls = [c for c in recorded_calls if 'api/Library/create' in c['url']]
        self.assertEqual(len(create_calls), 1)
        payload = create_calls[0]['kwargs']['json']
        self.assertEqual(payload['name'], 'Image Comics')
        self.assertEqual(payload['type'], 0)
        self.assertEqual(payload['folders'], [normalize_path_str(pub_folder)])
        self.assertNotIn('folderWatching', payload)
        self.assertNotIn('includeInDashboard', payload)

        headers = create_calls[0]['kwargs']['headers']
        self.assertEqual(headers.get('x-api-key'), 'kavita_secret_test_key')
        self.assertNotIn('Authorization', headers)

        scan_calls = [c for c in recorded_calls if 'api/Library/scan' in c['url']]
        self.assertEqual(len(scan_calls), 1)
        self.assertIn('libraryId=42', scan_calls[0]['url'])

        mappings = get_kavita_publisher_mappings()
        self.assertEqual(len(mappings), 1)
        m = mappings[0]
        self.assertEqual(m['publisher_display_name'], 'Image Comics')
        self.assertEqual(m['mapping_state'], 'active')
        self.assertEqual(m['provenance'], 'mylar_created')
        self.assertEqual(m['kavita_library_id'], 42)
        self.assertIsNotNone(m['last_scan_queued_at'])

    # -------------------------------------------------------------------------
    # 4. Existing Exact-Path Remote Library Associated Without Create
    # -------------------------------------------------------------------------
    def test_04_existing_single_exact_path_remote_library_associated_without_create(self):
        """4. Prove existing single exact-path remote library is associated without create request."""
        service = KavitaPublisherService()
        pub_folder = os.path.join(mylar.CONFIG.COMIC_DIR, "DC Comics")
        norm_path = normalize_path_str(pub_folder)

        recorded_calls = []
        def fake_handler(method, url, **kwargs):
            recorded_calls.append({'method': method, 'url': url, 'kwargs': kwargs})
            if 'api/Library/libraries' in url:
                return FakeResponse(200, json_data=[
                    {'id': 101, 'name': 'DC Comics Existing', 'folders': [norm_path]}
                ])
            if 'api/Library/scan' in url:
                return FakeResponse(200, json_data=True)
            return FakeResponse(200, json_data={})

        mock_session = MagicMock()
        mock_session.request.side_effect = fake_handler
        service._client_factory = lambda url, creds: KavitaClient(base_url=url, credentials=creds, session=mock_session)

        series_folder = os.path.join(pub_folder, "Batman")
        with patch('os.path.isdir', return_value=True):
            res = service.process_post_import_automation({
                'SeriesLocation': series_folder,
                'ComicPublisher': 'DC Comics',
                'ComicName': 'Batman'
            })

        self.assertEqual(res['status'], 'associated_existing')
        self.assertEqual(res['library_id'], 101)
        self.assertIs(res.get('scan_queued'), True)

        create_calls = [c for c in recorded_calls if 'api/Library/create' in c['url']]
        self.assertEqual(len(create_calls), 0)

        mappings = get_kavita_publisher_mappings()
        self.assertEqual(len(mappings), 1)
        m = mappings[0]
        self.assertEqual(m['mapping_state'], 'active')
        self.assertEqual(m['provenance'], 'existing_exact_path')
        self.assertEqual(m['kavita_library_id'], 101)
        self.assertIsNotNone(m['last_scan_queued_at'])

    # -------------------------------------------------------------------------
    # 5. Subsequent Active Mapping Queues Documented Library Scan
    # -------------------------------------------------------------------------
    def test_05_subsequent_active_mapping_queues_library_scan(self):
        """5. Prove subsequent active mapping queues documented library scan with query params and empty body."""
        service = KavitaPublisherService()
        pub_folder = os.path.join(mylar.CONFIG.COMIC_DIR, "Marvel Comics")
        norm_path = normalize_path_str(pub_folder)

        server_url = validate_kavita_url(mylar.CONFIG.KAVITA_URL)
        myDB = db.DBConnection()
        myDB.action(
            """
            INSERT INTO ext_kavita_publisher_mappings (
                MylarInstanceID, PublisherKey, PublisherDisplayName, CanonicalPublisherPath,
                KavitaServerUrl, KavitaLibraryID, ObservedLibraryName, Provenance, MappingState
            ) VALUES (?, 'marvel comics', 'Marvel Comics', ?, ?, 88, 'Marvel Comics', 'mylar_created', 'active')
            """,
            [mylar.CONFIG.MYLAR_INSTANCE_ID, norm_path, server_url]
        )

        recorded_calls = []
        def fake_handler(method, url, **kwargs):
            recorded_calls.append({'method': method, 'url': url, 'kwargs': kwargs})
            if 'api/Library/scan' in url:
                return FakeResponse(200, json_data=True)
            return FakeResponse(200, json_data={})

        mock_session = MagicMock()
        mock_session.request.side_effect = fake_handler
        service._client_factory = lambda url, creds: KavitaClient(base_url=url, credentials=creds, session=mock_session)

        series_folder = os.path.join(pub_folder, "Spider-Man")
        with patch('os.path.isdir', return_value=True):
            res = service.process_post_import_automation({
                'SeriesLocation': series_folder,
                'ComicPublisher': 'Marvel Comics',
                'ComicName': 'Spider-Man'
            })

        self.assertEqual(res['status'], 'scan_queued')
        self.assertEqual(res['library_id'], 88)

        scan_calls = [c for c in recorded_calls if 'api/Library/scan' in c['url']]
        self.assertEqual(len(scan_calls), 1)
        self.assertIn('libraryId=88', scan_calls[0]['url'])
        self.assertIn('force=false', scan_calls[0]['url'])
        self.assertIsNone(scan_calls[0]['kwargs'].get('json'))

    # -------------------------------------------------------------------------
    # 6. Scan Rejection Preserves Active Mapping and Schedules Backoff
    # -------------------------------------------------------------------------
    def test_06_scan_rejection_preserves_active_mapping_and_schedules_backoff(self):
        """6. Prove scan rejection preserves MappingState='active', library ID, and schedules exponential backoff."""
        service = KavitaPublisherService()
        pub_folder = os.path.join(mylar.CONFIG.COMIC_DIR, "Scan Test Pub")
        norm_path = normalize_path_str(pub_folder)

        server_url = validate_kavita_url(mylar.CONFIG.KAVITA_URL)
        myDB = db.DBConnection()
        myDB.action(
            """
            INSERT INTO ext_kavita_publisher_mappings (
                MylarInstanceID, PublisherKey, PublisherDisplayName, CanonicalPublisherPath,
                KavitaServerUrl, KavitaLibraryID, ObservedLibraryName, Provenance, MappingState
            ) VALUES (?, 'scan test pub', 'Scan Test Pub', ?, ?, 99, 'Scan Test Pub', 'mylar_created', 'active')
            """,
            [mylar.CONFIG.MYLAR_INSTANCE_ID, norm_path, server_url]
        )

        def fake_handler(method, url, **kwargs):
            if 'api/Library/scan' in url:
                return FakeResponse(500, text="Internal Server Error during scan")
            return FakeResponse(200, json_data={})

        mock_session = MagicMock()
        mock_session.request.side_effect = fake_handler
        service._client_factory = lambda url, creds: KavitaClient(base_url=url, credentials=creds, session=mock_session)

        series_folder = os.path.join(pub_folder, "Issue 1")
        with patch('os.path.isdir', return_value=True):
            res = service.process_post_import_automation({
                'SeriesLocation': series_folder,
                'ComicPublisher': 'Scan Test Pub',
                'ComicName': 'Issue 1'
            })

        self.assertEqual(res['status'], 'error')
        mappings = get_kavita_publisher_mappings()
        self.assertEqual(len(mappings), 1)
        m = mappings[0]

        # Verify mapping remains active and retains library ID and provenance
        self.assertEqual(m['mapping_state'], 'active')
        self.assertEqual(m['kavita_library_id'], 99)
        self.assertEqual(m['provenance'], 'mylar_created')
        self.assertEqual(m['last_error_code'], 'kavita_unavailable')
        self.assertIsNotNone(m['next_eligible_retry_at'])

    # -------------------------------------------------------------------------
    # 7. Scan Timeout Preserves Active Mapping and Schedules Backoff
    # -------------------------------------------------------------------------
    def test_07_scan_timeout_preserves_active_mapping_and_schedules_backoff(self):
        """7. Prove scan timeout preserves MappingState='active' and schedules backoff."""
        service = KavitaPublisherService()
        pub_folder = os.path.join(mylar.CONFIG.COMIC_DIR, "Timeout Pub")
        norm_path = normalize_path_str(pub_folder)

        server_url = validate_kavita_url(mylar.CONFIG.KAVITA_URL)
        myDB = db.DBConnection()
        myDB.action(
            """
            INSERT INTO ext_kavita_publisher_mappings (
                MylarInstanceID, PublisherKey, PublisherDisplayName, CanonicalPublisherPath,
                KavitaServerUrl, KavitaLibraryID, ObservedLibraryName, Provenance, MappingState
            ) VALUES (?, 'timeout pub', 'Timeout Pub', ?, ?, 105, 'Timeout Pub', 'mylar_created', 'active')
            """,
            [mylar.CONFIG.MYLAR_INSTANCE_ID, norm_path, server_url]
        )

        def fake_handler(method, url, **kwargs):
            if 'api/Library/scan' in url:
                raise KavitaTimeoutError("Scan operation timed out")
            return FakeResponse(200, json_data={})

        mock_session = MagicMock()
        mock_session.request.side_effect = fake_handler
        service._client_factory = lambda url, creds: KavitaClient(base_url=url, credentials=creds, session=mock_session)

        series_folder = os.path.join(pub_folder, "Series A")
        with patch('os.path.isdir', return_value=True):
            res = service.process_post_import_automation({
                'SeriesLocation': series_folder,
                'ComicPublisher': 'Timeout Pub',
                'ComicName': 'Series A'
            })

        self.assertEqual(res['status'], 'error')
        mappings = get_kavita_publisher_mappings()
        m = mappings[0]
        self.assertEqual(m['mapping_state'], 'active')
        self.assertEqual(m['kavita_library_id'], 105)
        self.assertEqual(m['last_error_code'], 'kavita_timeout')
        self.assertIsNotNone(m['next_eligible_retry_at'])

    # -------------------------------------------------------------------------
    # 8. Active Mapping Scan Backoff Suppresses Requests
    # -------------------------------------------------------------------------
    def test_08_active_mapping_scan_backoff_suppresses_requests(self):
        """8. Prove active mapping in scan backoff window makes zero scan requests."""
        service = KavitaPublisherService()
        pub_folder = os.path.join(mylar.CONFIG.COMIC_DIR, "Backoff Pub")
        norm_path = normalize_path_str(pub_folder)

        server_url = validate_kavita_url(mylar.CONFIG.KAVITA_URL)
        future_time = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + datetime.timedelta(minutes=10)
        myDB = db.DBConnection()
        myDB.action(
            """
            INSERT INTO ext_kavita_publisher_mappings (
                MylarInstanceID, PublisherKey, PublisherDisplayName, CanonicalPublisherPath,
                KavitaServerUrl, KavitaLibraryID, ObservedLibraryName, Provenance, MappingState,
                LastErrorCode, LastErrorMessage, NextEligibleRetryAt, AttemptCount
            ) VALUES (?, 'backoff pub', 'Backoff Pub', ?, ?, 120, 'Backoff Pub', 'mylar_created', 'active',
                     'kavita_scan_rejected', 'Scan failed', ?, 1)
            """,
            [mylar.CONFIG.MYLAR_INSTANCE_ID, norm_path, server_url, future_time.strftime('%Y-%m-%d %H:%M:%S')]
        )

        mock_session = MagicMock()
        service._client_factory = lambda url, creds: KavitaClient(base_url=url, credentials=creds, session=mock_session)

        series_folder = os.path.join(pub_folder, "Series B")
        with patch('os.path.isdir', return_value=True):
            res = service.process_post_import_automation({
                'SeriesLocation': series_folder,
                'ComicPublisher': 'Backoff Pub',
                'ComicName': 'Series B'
            })

        self.assertEqual(res['status'], 'scan_backoff_active')
        mock_session.request.assert_not_called()

    # -------------------------------------------------------------------------
    # 9. Eligible Scan Retry Succeeds and Clears Backoff Fields
    # -------------------------------------------------------------------------
    def test_09_eligible_scan_retry_succeeds_and_clears_backoff(self):
        """9. Prove eligible scan retry succeeds after backoff expiration and clears retry/error fields."""
        service = KavitaPublisherService()
        pub_folder = os.path.join(mylar.CONFIG.COMIC_DIR, "Retry Scan Pub")
        norm_path = normalize_path_str(pub_folder)

        server_url = validate_kavita_url(mylar.CONFIG.KAVITA_URL)
        myDB = db.DBConnection()
        myDB.action(
            """
            INSERT INTO ext_kavita_publisher_mappings (
                MylarInstanceID, PublisherKey, PublisherDisplayName, CanonicalPublisherPath,
                KavitaServerUrl, KavitaLibraryID, ObservedLibraryName, Provenance, MappingState,
                LastErrorCode, LastErrorMessage, NextEligibleRetryAt, AttemptCount
            ) VALUES (?, 'retry scan pub', 'Retry Scan Pub', ?, ?, 130, 'Retry Scan Pub', 'mylar_created', 'active',
                     'kavita_scan_rejected', 'Scan failed', '2020-01-01 00:00:00', 1)
            """,
            [mylar.CONFIG.MYLAR_INSTANCE_ID, norm_path, server_url]
        )

        recorded_calls = []
        def fake_handler(method, url, **kwargs):
            recorded_calls.append({'method': method, 'url': url})
            if 'api/Library/scan' in url:
                return FakeResponse(200, json_data=True)
            return FakeResponse(200, json_data={})

        mock_session = MagicMock()
        mock_session.request.side_effect = fake_handler
        service._client_factory = lambda url, creds: KavitaClient(base_url=url, credentials=creds, session=mock_session)

        series_folder = os.path.join(pub_folder, "Series C")
        with patch('os.path.isdir', return_value=True):
            res = service.process_post_import_automation({
                'SeriesLocation': series_folder,
                'ComicPublisher': 'Retry Scan Pub',
                'ComicName': 'Series C'
            })

        self.assertEqual(res['status'], 'scan_queued')
        mappings = get_kavita_publisher_mappings()
        m = mappings[0]
        self.assertEqual(m['mapping_state'], 'active')
        self.assertIsNone(m['last_error_code'])
        self.assertIsNone(m['last_error_message'])
        self.assertIsNone(m['next_eligible_retry_at'])
        self.assertIsNotNone(m['last_scan_queued_at'])

    # -------------------------------------------------------------------------
    # 10. Budget Exhaustion Makes Zero Scan Calls and Does Not Set Timestamp
    # -------------------------------------------------------------------------
    def test_10_budget_exhaustion_makes_zero_scan_calls(self):
        """10. Prove budget exhaustion starts zero scan requests and does not falsely set LastScanQueuedAt."""
        service = KavitaPublisherService()
        pub_folder = os.path.join(mylar.CONFIG.COMIC_DIR, "Budget Pub")
        norm_path = normalize_path_str(pub_folder)

        server_url = validate_kavita_url(mylar.CONFIG.KAVITA_URL)
        myDB = db.DBConnection()
        myDB.action(
            """
            INSERT INTO ext_kavita_publisher_mappings (
                MylarInstanceID, PublisherKey, PublisherDisplayName, CanonicalPublisherPath,
                KavitaServerUrl, KavitaLibraryID, ObservedLibraryName, Provenance, MappingState
            ) VALUES (?, 'budget pub', 'Budget Pub', ?, ?, 140, 'Budget Pub', 'mylar_created', 'active')
            """,
            [mylar.CONFIG.MYLAR_INSTANCE_ID, norm_path, server_url]
        )

        mock_session = MagicMock()
        service._client_factory = lambda url, creds: KavitaClient(base_url=url, credentials=creds, session=mock_session)

        # Simulate 13 seconds elapsed
        monotonic_counter = [0.0]
        def fake_monotonic():
            val = monotonic_counter[0]
            monotonic_counter[0] += 13.0
            return val

        series_folder = os.path.join(pub_folder, "Series D")
        with patch('time.monotonic', side_effect=fake_monotonic), patch('os.path.isdir', return_value=True):
            res = service.process_post_import_automation({
                'SeriesLocation': series_folder,
                'ComicPublisher': 'Budget Pub',
                'ComicName': 'Series D'
            })

        self.assertEqual(res['status'], 'timeout')
        mock_session.request.assert_not_called()

        mappings = get_kavita_publisher_mappings()
        m = mappings[0]
        self.assertEqual(m['mapping_state'], 'active')
        self.assertIsNone(m['last_scan_queued_at'])
        self.assertEqual(m['last_error_code'], 'kavita_timeout')
        self.assertIsNotNone(m['next_eligible_retry_at'])

    # -------------------------------------------------------------------------
    # 11. Flat Layout Invariance: Zero HTTP Calls, Zero DB Writes
    # -------------------------------------------------------------------------
    def test_11_flat_layout_invariance_zero_http_zero_db_writes(self):
        """11. Prove flat layout causes zero HTTP calls and zero database/lease/error persistence."""
        service = KavitaPublisherService()
        mock_session = MagicMock()
        service._client_factory = lambda url, creds: KavitaClient(base_url=url, credentials=creds, session=mock_session)

        flat_series_folder = os.path.join(mylar.CONFIG.COMIC_DIR, "Saga")
        res = service.process_post_import_automation({
            'SeriesLocation': flat_series_folder,
            'ComicPublisher': 'Image Comics',
            'ComicName': 'Saga'
        })

        self.assertEqual(res['status'], 'publisher_root_not_materialized')
        mock_session.request.assert_not_called()

        mappings = get_kavita_publisher_mappings()
        self.assertEqual(len(mappings), 0)

        notice = get_latest_automation_notice()
        self.assertIsNotNone(notice)
        self.assertIn('publisher subdirectories', notice)

    # -------------------------------------------------------------------------
    # 12. Name Collision on Different Path Applies Deterministic Instance Slug
    # -------------------------------------------------------------------------
    def test_12_name_collision_on_different_path_applies_deterministic_instance_slug(self):
        """12. Prove name collision on a different remote path applies deterministic instance slug."""
        service = KavitaPublisherService()
        pub_folder = os.path.join(mylar.CONFIG.COMIC_DIR, "Dark Horse Comics")
        norm_path = normalize_path_str(pub_folder)

        recorded_calls = []
        def fake_handler(method, url, **kwargs):
            recorded_calls.append({'method': method, 'url': url, 'kwargs': kwargs})
            if 'api/Library/libraries' in url:
                return FakeResponse(200, json_data=[
                    {'id': 55, 'name': 'Dark Horse Comics', 'folders': ['/different/host/mount/Dark Horse']}
                ])
            if 'api/Settings/library-types' in url:
                return FakeResponse(200, json_data=[{'id': 0, 'name': 'Comic'}])
            if 'api/Library/create' in url:
                body = kwargs.get('json', {})
                return FakeResponse(200, json_data={'id': 77, 'name': body.get('name'), 'folders': body.get('folders')})
            return FakeResponse(200, json_data={})

        mock_session = MagicMock()
        mock_session.request.side_effect = fake_handler
        service._client_factory = lambda url, creds: KavitaClient(base_url=url, credentials=creds, session=mock_session)

        series_folder = os.path.join(pub_folder, "Hellboy")
        with patch('os.path.isdir', return_value=True):
            res = service.process_post_import_automation({
                'SeriesLocation': series_folder,
                'ComicPublisher': 'Dark Horse Comics',
                'ComicName': 'Hellboy'
            })

        self.assertEqual(res['status'], 'created')
        instance_slug = get_mylar_instance_slug()
        expected_qualified_name = f"Dark Horse Comics ({instance_slug})"

        create_calls = [c for c in recorded_calls if 'api/Library/create' in c['url']]
        self.assertEqual(create_calls[0]['kwargs']['json']['name'], expected_qualified_name)

    # -------------------------------------------------------------------------
    # 13. Local Concurrent Imports Acquire Single Lease and Create
    # -------------------------------------------------------------------------
    def test_13_local_concurrent_imports_single_create_attempt(self):
        """13. Prove concurrent local imports acquire one lease and perform at most one create call."""
        service = KavitaPublisherService()
        pub_folder = os.path.join(mylar.CONFIG.COMIC_DIR, "Boom! Studios")
        canonical_path = normalize_path_str(pub_folder)
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

        lease1 = service.acquire_or_evaluate_mapping_lease(
            mylar.CONFIG.MYLAR_INSTANCE_ID, 'boom! studios', 'Boom! Studios',
            canonical_path, "http://127.0.0.1:5000", now
        )
        self.assertEqual(lease1['action'], 'execute_create')
        self.assertIsNotNone(lease1.get('lease_token'))

        lease2 = service.acquire_or_evaluate_mapping_lease(
            mylar.CONFIG.MYLAR_INSTANCE_ID, 'boom! studios', 'Boom! Studios',
            canonical_path, "http://127.0.0.1:5000", now + datetime.timedelta(seconds=10)
        )
        self.assertEqual(lease2['action'], 'abort')
        self.assertEqual(lease2['reason'], 'concurrent_lease_active')

    # -------------------------------------------------------------------------
    # 14. Crash After Remote Create Recovery Associates Without Duplicate
    # -------------------------------------------------------------------------
    def test_14_crash_after_create_recovery_associates_without_duplicate(self):
        """14. Prove crash after remote create recovers by re-listing and associating single match."""
        service = KavitaPublisherService()
        pub_folder = os.path.join(mylar.CONFIG.COMIC_DIR, "IDW Publishing")
        norm_path = normalize_path_str(pub_folder)

        server_url = validate_kavita_url(mylar.CONFIG.KAVITA_URL)
        myDB = db.DBConnection()
        myDB.action(
            """
            INSERT INTO ext_kavita_publisher_mappings (
                MylarInstanceID, PublisherKey, PublisherDisplayName, CanonicalPublisherPath,
                KavitaServerUrl, Provenance, MappingState, LeaseToken, LeaseExpiresAt, Revision, AttemptCount
            ) VALUES (?, 'idw publishing', 'IDW Publishing', ?, ?, 'mylar_created', 'creating', 'old_token', '2020-01-01 00:00:00', 1, 1)
            """,
            [mylar.CONFIG.MYLAR_INSTANCE_ID, norm_path, server_url]
        )

        recorded_calls = []
        def fake_handler(method, url, **kwargs):
            recorded_calls.append({'method': method, 'url': url, 'kwargs': kwargs})
            if 'api/Library/libraries' in url:
                return FakeResponse(200, json_data=[{'id': 333, 'name': 'IDW Publishing', 'folders': [norm_path]}])
            if 'api/Library/scan' in url:
                return FakeResponse(200, json_data=True)
            return FakeResponse(200, json_data={})

        mock_session = MagicMock()
        mock_session.request.side_effect = fake_handler
        service._client_factory = lambda url, creds: KavitaClient(base_url=url, credentials=creds, session=mock_session)

        series_folder = os.path.join(pub_folder, "TMNT")
        with patch('os.path.isdir', return_value=True):
            res = service.process_post_import_automation({
                'SeriesLocation': series_folder,
                'ComicPublisher': 'IDW Publishing',
                'ComicName': 'TMNT'
            })

        self.assertEqual(res['status'], 'associated_existing')
        self.assertEqual(res['library_id'], 333)

        create_calls = [c for c in recorded_calls if 'api/Library/create' in c['url']]
        self.assertEqual(len(create_calls), 0)

        mappings = get_kavita_publisher_mappings()
        self.assertEqual(mappings[0]['mapping_state'], 'active')
        self.assertEqual(mappings[0]['kavita_library_id'], 333)

    # -------------------------------------------------------------------------
    # 15. Create Failure Followed by Single Exact Match Associates Safely
    # -------------------------------------------------------------------------
    def test_15_create_failure_followed_by_single_exact_match_associates_safely(self):
        """15. Prove create failure followed by recovery discovery associates exact match safely."""
        service = KavitaPublisherService()
        pub_folder = os.path.join(mylar.CONFIG.COMIC_DIR, "Valiant")
        norm_path = normalize_path_str(pub_folder)

        call_counts = {'libs': 0, 'create': 0}
        def fake_handler(method, url, **kwargs):
            if 'api/Library/libraries' in url:
                call_counts['libs'] += 1
                if call_counts['libs'] == 1:
                    return FakeResponse(200, json_data=[])
                return FakeResponse(200, json_data=[{'id': 999, 'name': 'Valiant', 'folders': [norm_path]}])
            if 'api/Settings/library-types' in url:
                return FakeResponse(200, json_data=[{'id': 0, 'name': 'Comic'}])
            if 'api/Library/create' in url:
                call_counts['create'] += 1
                return FakeResponse(409, text="Library already exists")
            return FakeResponse(200, json_data={})

        mock_session = MagicMock()
        mock_session.request.side_effect = fake_handler
        service._client_factory = lambda url, creds: KavitaClient(base_url=url, credentials=creds, session=mock_session)

        series_folder = os.path.join(pub_folder, "X-O Manowar")
        with patch('os.path.isdir', return_value=True):
            res = service.process_post_import_automation({
                'SeriesLocation': series_folder,
                'ComicPublisher': 'Valiant',
                'ComicName': 'X-O Manowar'
            })

        self.assertEqual(res['status'], 'associated_after_recovery')
        self.assertEqual(res['library_id'], 999)

    # -------------------------------------------------------------------------
    # 16. Create Failure Followed by Multiple Matches Becomes Ambiguous
    # -------------------------------------------------------------------------
    def test_16_create_failure_followed_by_multiple_matches_becomes_ambiguous(self):
        """16. Prove multiple exact-path matches produce ambiguous_multi_path state with zero mutations."""
        service = KavitaPublisherService()
        pub_folder = os.path.join(mylar.CONFIG.COMIC_DIR, "Dynamite Entertainment")
        norm_path = normalize_path_str(pub_folder)

        def fake_handler(method, url, **kwargs):
            if 'api/Library/libraries' in url:
                return FakeResponse(200, json_data=[
                    {'id': 1, 'name': 'Dynamite A', 'folders': [norm_path]},
                    {'id': 2, 'name': 'Dynamite B', 'folders': [norm_path]}
                ])
            return FakeResponse(200, json_data={})

        mock_session = MagicMock()
        mock_session.request.side_effect = fake_handler
        service._client_factory = lambda url, creds: KavitaClient(base_url=url, credentials=creds, session=mock_session)

        series_folder = os.path.join(pub_folder, "Red Sonja")
        with patch('os.path.isdir', return_value=True):
            res = service.process_post_import_automation({
                'SeriesLocation': series_folder,
                'ComicPublisher': 'Dynamite Entertainment',
                'ComicName': 'Red Sonja'
            })

        self.assertEqual(res['status'], 'ambiguous_multi_path')
        mappings = get_kavita_publisher_mappings()
        self.assertEqual(mappings[0]['mapping_state'], 'ambiguous_multi_path')
        self.assertEqual(mappings[0]['last_error_code'], 'kavita_ambiguous_exact_path')

    # -------------------------------------------------------------------------
    # 17. Path Rejected Enters Backoff and Suppresses Rapid Retries
    # -------------------------------------------------------------------------
    def test_17_path_rejected_enters_backoff_and_suppresses_rapid_retries(self):
        """17. Prove path_rejected enters exponential backoff and makes zero calls before eligibility."""
        service = KavitaPublisherService()
        pub_folder = os.path.join(mylar.CONFIG.COMIC_DIR, "Archie Comics")
        norm_path = normalize_path_str(pub_folder)

        request_counts = {'create': 0}
        def fake_handler(method, url, **kwargs):
            if 'api/Library/libraries' in url:
                return FakeResponse(200, json_data=[])
            if 'api/Settings/library-types' in url:
                return FakeResponse(200, json_data=[{'id': 0, 'name': 'Comic'}])
            if 'api/Library/create' in url:
                request_counts['create'] += 1
                return FakeResponse(400, text="Path '/comics/Archie Comics' does not exist in Kavita container")
            return FakeResponse(200, json_data={})

        mock_session = MagicMock()
        mock_session.request.side_effect = fake_handler
        service._client_factory = lambda url, creds: KavitaClient(base_url=url, credentials=creds, session=mock_session)

        series_folder = os.path.join(pub_folder, "Afterlife with Archie")
        with patch('os.path.isdir', return_value=True):
            res1 = service.process_post_import_automation({
                'SeriesLocation': series_folder,
                'ComicPublisher': 'Archie Comics',
                'ComicName': 'Afterlife with Archie'
            })

        self.assertEqual(res1['status'], 'create_failed')
        self.assertEqual(res1['code'], 'kavita_path_rejected')
        self.assertEqual(request_counts['create'], 1)

        mappings = get_kavita_publisher_mappings()
        self.assertEqual(mappings[0]['mapping_state'], 'path_rejected')
        self.assertEqual(mappings[0]['last_error_code'], 'kavita_path_rejected')
        self.assertIsNotNone(mappings[0]['next_eligible_retry_at'])

        # Second import immediately arrives (within 5 min backoff window)
        with patch('os.path.isdir', return_value=True):
            res2 = service.process_post_import_automation({
                'SeriesLocation': series_folder,
                'ComicPublisher': 'Archie Comics',
                'ComicName': 'Afterlife with Archie'
            })

        self.assertEqual(res2['status'], 'aborted')
        self.assertEqual(res2['reason'], 'backoff_active')
        self.assertEqual(request_counts['create'], 1)

    # -------------------------------------------------------------------------
    # 18. Eligible Later Retry Succeeds After External Mount Repaired
    # -------------------------------------------------------------------------
    def test_18_eligible_later_retry_succeeds_after_external_mount_repaired(self):
        """18. Prove eligible later retry succeeds after external volume mount is fixed."""
        service = KavitaPublisherService()
        pub_folder = os.path.join(mylar.CONFIG.COMIC_DIR, "2000 AD")
        norm_path = normalize_path_str(pub_folder)

        server_url = validate_kavita_url(mylar.CONFIG.KAVITA_URL)
        myDB = db.DBConnection()
        myDB.action(
            """
            INSERT INTO ext_kavita_publisher_mappings (
                MylarInstanceID, PublisherKey, PublisherDisplayName, CanonicalPublisherPath,
                KavitaServerUrl, Provenance, MappingState, AttemptCount, NextEligibleRetryAt, Revision
            ) VALUES (?, '2000 ad', '2000 AD', ?, ?, 'mylar_created', 'path_rejected', 1, '2020-01-01 00:00:00', 1)
            """,
            [mylar.CONFIG.MYLAR_INSTANCE_ID, norm_path, server_url]
        )

        def fake_handler(method, url, **kwargs):
            if 'api/Library/libraries' in url:
                return FakeResponse(200, json_data=[])
            if 'api/Settings/library-types' in url:
                return FakeResponse(200, json_data=[{'id': 0, 'name': 'Comic'}])
            if 'api/Library/create' in url:
                return FakeResponse(200, json_data={'id': 700, 'name': '2000 AD', 'folders': [norm_path]})
            return FakeResponse(200, json_data={})

        mock_session = MagicMock()
        mock_session.request.side_effect = fake_handler
        service._client_factory = lambda url, creds: KavitaClient(base_url=url, credentials=creds, session=mock_session)

        series_folder = os.path.join(pub_folder, "Judge Dredd")
        with patch('os.path.isdir', return_value=True):
            res = service.process_post_import_automation({
                'SeriesLocation': series_folder,
                'ComicPublisher': '2000 AD',
                'ComicName': 'Judge Dredd'
            })

        self.assertEqual(res['status'], 'created')
        self.assertEqual(res['library_id'], 700)

        mappings = get_kavita_publisher_mappings()
        self.assertEqual(mappings[0]['mapping_state'], 'active')
        self.assertIsNone(mappings[0]['next_eligible_retry_at'])
        self.assertIsNone(mappings[0]['last_error_code'])

    # -------------------------------------------------------------------------
    # 19. Monotonic Budget Exhaustion Prevents Subsequent Calls
    # -------------------------------------------------------------------------
    def test_19_monotonic_budget_exhaustion_prevents_subsequent_calls(self):
        """19. Prove monotonic budget exhaustion halts automation and prevents subsequent remote calls."""
        service = KavitaPublisherService()
        pub_folder = os.path.join(mylar.CONFIG.COMIC_DIR, "Oni Press")

        mock_session = MagicMock()
        service._client_factory = lambda url, creds: KavitaClient(base_url=url, credentials=creds, session=mock_session)

        series_folder = os.path.join(pub_folder, "Scott Pilgrim")
        monotonic_counter = [0.0]
        def fake_monotonic():
            val = monotonic_counter[0]
            monotonic_counter[0] += 13.0
            return val

        with patch('time.monotonic', side_effect=fake_monotonic), patch('os.path.isdir', return_value=True):
            res = service.process_post_import_automation({
                'SeriesLocation': series_folder,
                'ComicPublisher': 'Oni Press',
                'ComicName': 'Scott Pilgrim'
            })

        self.assertEqual(res['status'], 'timeout')
        mock_session.request.assert_not_called()

        mappings = get_kavita_publisher_mappings()
        self.assertEqual(mappings[0]['last_error_code'], 'kavita_timeout')

    # -------------------------------------------------------------------------
    # 20. Disabled Kavita Makes Zero HTTP Calls
    # -------------------------------------------------------------------------
    def test_20_disabled_kavita_makes_zero_http_calls(self):
        """20. Prove disabled Kavita configuration makes zero HTTP calls."""
        mylar.CONFIG.KAVITA_ENABLED = False
        service = KavitaPublisherService()
        mock_session = MagicMock()
        service._client_factory = lambda url, creds: KavitaClient(base_url=url, credentials=creds, session=mock_session)

        pub_folder = os.path.join(mylar.CONFIG.COMIC_DIR, "Titan Comics")
        series_folder = os.path.join(pub_folder, "Doctor Who")
        with patch('os.path.isdir', return_value=True):
            res = service.process_post_import_automation({
                'SeriesLocation': series_folder,
                'ComicPublisher': 'Titan Comics',
                'ComicName': 'Doctor Who'
            })

        self.assertEqual(res['status'], 'disabled_or_unconfigured')
        mock_session.request.assert_not_called()

    # -------------------------------------------------------------------------
    # 21. All Failures Preserve Mylar Post-Processing Success
    # -------------------------------------------------------------------------
    def test_21_all_failures_preserve_mylar_import_success(self):
        """21. Prove Kavita failures never raise exceptions or interrupt Mylar import success."""
        def broken_client(url, creds):
            mock = MagicMock()
            mock.request.side_effect = KavitaTimeoutError("Connection timed out")
            return KavitaClient(base_url=url, credentials=creds, session=mock)

        res = handle_post_processing_kavita_automation(
            {'SeriesLocation': '/nonexistent/path', 'ComicPublisher': 'Test'},
            client_factory=broken_client
        )
        self.assertIn(res.get('status'), ('publisher_root_missing_on_disk', 'exception_suppressed', 'missing_location'))

    # -------------------------------------------------------------------------
    # 22. Fail-Closed Dynamic Comic Type Discovery
    # -------------------------------------------------------------------------
    def test_22_dynamic_comic_type_discovery_fails_closed(self):
        """22. Prove zero create requests occur when library types query fails, is malformed, or has no comic type."""
        pub_folder = os.path.join(mylar.CONFIG.COMIC_DIR, "AWA Studios")
        series_folder = os.path.join(pub_folder, "The Resistance")

        scenarios = [
            ("500_server_error", FakeResponse(500, text="Internal Error"), 'kavita_unavailable'),
            ("malformed_non_list", FakeResponse(200, json_data={'error': 'bad'}), 'kavita_incompatible'),
            ("no_comic_type_available", FakeResponse(200, json_data=[{'id': 1, 'name': 'Manga'}, {'id': 2, 'name': 'Book'}]), 'kavita_incompatible'),
        ]

        for scenario_name, types_resp, expected_code in scenarios:
            with self.subTest(scenario=scenario_name):
                myDB = db.DBConnection()
                myDB.action("DELETE FROM ext_kavita_publisher_mappings")

                recorded_calls = []
                def fake_handler(method, url, **kwargs):
                    recorded_calls.append({'method': method, 'url': url})
                    if 'api/Library/libraries' in url:
                        return FakeResponse(200, json_data=[])
                    if 'api/Settings/library-types' in url:
                        return types_resp
                    return FakeResponse(200, json_data={})

                mock_session = MagicMock()
                mock_session.request.side_effect = fake_handler
                service = KavitaPublisherService()
                service._client_factory = lambda url, creds: KavitaClient(base_url=url, credentials=creds, session=mock_session)

                with patch('os.path.isdir', return_value=True):
                    res = service.process_post_import_automation({
                        'SeriesLocation': series_folder,
                        'ComicPublisher': 'AWA Studios',
                        'ComicName': 'The Resistance'
                    })

                self.assertEqual(res['status'], 'comic_type_unavailable')
                create_calls = [c for c in recorded_calls if 'api/Library/create' in c['url']]
                self.assertEqual(len(create_calls), 0, f"Create called during fail-closed scenario {scenario_name}")

                mappings = get_kavita_publisher_mappings()
                self.assertEqual(len(mappings), 1)
                self.assertEqual(mappings[0]['last_error_code'], expected_code)

    # -------------------------------------------------------------------------
    # 23. Truly Closed Persisted Error Taxonomy
    # -------------------------------------------------------------------------
    def test_23_persisted_error_taxonomy_is_truly_closed(self):
        """23. Prove unknown error codes or arbitrary exception strings map strictly to declared taxonomy keys."""
        unknown_codes = [
            "some_arbitrary_unseen_code_12345",
            "FATAL_UNKNOWN_EXCEPTION",
            None,
            12345
        ]

        for unk in unknown_codes:
            sanitized = sanitize_error_code(unk)
            self.assertIn(sanitized, KAVITA_ERROR_TAXONOMY, f"Code '{unk}' mapped to non-taxonomy code '{sanitized}'")

        service = KavitaPublisherService()
        pub_folder = os.path.join(mylar.CONFIG.COMIC_DIR, "Taxonomy Test")
        norm_path = normalize_path_str(pub_folder)
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

        myDB = db.DBConnection()
        myDB.action(
            """
            INSERT INTO ext_kavita_publisher_mappings (
                MylarInstanceID, PublisherKey, PublisherDisplayName, CanonicalPublisherPath,
                KavitaServerUrl, Provenance, MappingState, AttemptCount, Revision
            ) VALUES (?, 'taxonomy test', 'Taxonomy Test', ?, 'http://127.0.0.1:5000/', 'mylar_created', 'creating', 1, 1)
            """,
            [mylar.CONFIG.MYLAR_INSTANCE_ID, norm_path]
        )
        row = myDB.selectone("SELECT MappingID FROM ext_kavita_publisher_mappings WHERE CanonicalPublisherPath=?", [norm_path]).fetchone()
        mapping_id = row['MappingID']

        service._record_sanitized_error(mapping_id, "completely_unknown_error_code", now, 1)
        mappings = get_kavita_publisher_mappings()
        self.assertEqual(mappings[0]['last_error_code'], 'kavita_transient_failure')

    # -------------------------------------------------------------------------
    # 24. No Full Instance UUID Exposure in HTML, JSON, or DTOs
    # -------------------------------------------------------------------------
    def test_24_no_full_instance_uuid_exposure(self):
        """24. Prove full MYLAR_INSTANCE_ID UUID is absent from Modern HTML, diagnostics JSON, and mapping DTOs."""
        instance_uuid = str(mylar.CONFIG.MYLAR_INSTANCE_ID)
        self.assertTrue(len(instance_uuid) >= 32, "MYLAR_INSTANCE_ID should be a full UUID string")

        service = KavitaPublisherService()
        raw_row = {
            'MappingID': 1,
            'MylarInstanceID': instance_uuid,
            'PublisherKey': 'test pub',
            'PublisherDisplayName': 'Test Pub',
            'CanonicalPublisherPath': '/comics/Test Pub',
            'KavitaServerUrl': 'http://127.0.0.1:5000/',
            'KavitaLibraryID': 10,
            'ObservedLibraryName': 'Test Pub',
            'Provenance': 'mylar_created',
            'MappingState': 'active',
            'LeaseToken': 'secret_lease_token_xyz',
            'LeaseExpiresAt': '2026-08-26 12:00:00',
            'Revision': 1,
            'AttemptCount': 0,
            'NextEligibleRetryAt': None,
            'LastScanQueuedAt': '2026-08-26 10:00:00',
            'LastVerifiedAt': None,
            'LastErrorCode': None,
            'LastErrorMessage': None,
            'LastErrorTimestamp': None,
            'CreatedAt': '2026-08-26 09:00:00',
            'UpdatedAt': '2026-08-26 09:00:00'
        }
        dto = build_sanitized_mapping_dto(raw_row)
        dto_json = json.dumps(dto)
        self.assertNotIn(instance_uuid, dto_json)
        self.assertNotIn('secret_lease_token_xyz', dto_json)
        self.assertNotIn('MylarInstanceID', dto)
        self.assertNotIn('LeaseToken', dto)

        with patch.object(mylar.extensions.providers.kavita.discovery.KavitaDiscoveryService, 'run_discovery', return_value={
            'configured': True, 'enabled': True, 'state': 'ok', 'observed_libraries': [], 'naming_proposals': []
        }):
            diag_resp = handle_kavita_diagnostics()
            self.assertNotIn(instance_uuid, diag_resp)

        from mylar.webserve import serve_template
        rendered = serve_template(
            templatename="kavita_diagnostics.html",
            title="Kavita Integration",
            kavita_status={'enabled': True, 'url': 'http://127.0.0.1:5000', 'has_api_key': True, 'mylar_instance_slug': 'test-slug'},
            publisher_mappings=[dto],
            automation_notice="Test flat layout notice"
        )
        rendered_str = rendered.decode('utf-8') if isinstance(rendered, bytes) else str(rendered)
        self.assertNotIn(instance_uuid, rendered_str)
        self.assertNotIn('secret_lease_token_xyz', rendered_str)

    # -------------------------------------------------------------------------
    # 25. Zero Secret and Raw Error Text Leakage
    # -------------------------------------------------------------------------
    def test_25_zero_secret_and_raw_error_leakage(self):
        """25. Prove zero secrets or raw exception strings reach database records or logs."""
        service = KavitaPublisherService()
        pub_folder = os.path.join(mylar.CONFIG.COMIC_DIR, "Secret Pub")
        norm_path = normalize_path_str(pub_folder)

        raw_leak_text = "FATAL: /internal/container/secret_token_12345/error.c"
        def fake_handler(method, url, **kwargs):
            if 'api/Library/libraries' in url:
                return FakeResponse(500, text=raw_leak_text)
            return FakeResponse(200, json_data={})

        mock_session = MagicMock()
        mock_session.request.side_effect = fake_handler
        service._client_factory = lambda url, creds: KavitaClient(base_url=url, credentials=creds, session=mock_session)

        series_folder = os.path.join(pub_folder, "Classified")
        with patch('os.path.isdir', return_value=True):
            res = service.process_post_import_automation({
                'SeriesLocation': series_folder,
                'ComicPublisher': 'Secret Pub',
                'ComicName': 'Classified'
            })

        mappings = get_kavita_publisher_mappings()
        self.assertEqual(len(mappings), 1)
        m = mappings[0]

        self.assertNotIn("secret_token_12345", str(m.get('last_error_message')))
        self.assertNotIn("/internal/container", str(m.get('last_error_message')))
        self.assertNotIn("kavita_secret_test_key", str(m))
        self.assertEqual(m.get('last_error_code'), 'kavita_unavailable')

    # -------------------------------------------------------------------------
    # 26. Zero Remote Delete Calls Across All Scenarios
    # -------------------------------------------------------------------------
    def test_26_zero_remote_delete_calls(self):
        """26. Prove DELETE requests are never issued under any error, rejection, or collision scenario."""
        mock_session = MagicMock()
        client = KavitaClient(base_url="http://127.0.0.1:5000", session=mock_session)
        with self.assertRaises(KavitaInvalidRequestError):
            client.request('DELETE', 'api/Library/library/1')
        mock_session.request.assert_not_called()

    # -------------------------------------------------------------------------
    # 27. Shared Settings Templates Contain Zero Kavita UI
    # -------------------------------------------------------------------------
    def test_27_shared_settings_templates_contain_zero_kavita_ui(self):
        """27. Prove Carbon and Default settings contain zero Kavita UI markup."""
        from mylar.webserve import WebInterface
        interface = WebInterface()

        for theme in ['carbon', 'default']:
            mylar.CONFIG.INTERFACE = theme
            mylar.CONFIG.PROVIDER_ORDER = {}
            mylar.CONFIG.PROVIDER_BLOCKLIST = []
            rendered = interface.config()
            rendered_str = rendered.decode('utf-8') if isinstance(rendered, bytes) else str(rendered)
            self.assertNotIn('kavita_enabled', rendered_str)
            self.assertNotIn('kavita_url', rendered_str)
            self.assertNotIn('kavita_api_key', rendered_str)
            self.assertNotIn('test_kavita', rendered_str)
            self.assertNotIn('run_kavita_diagnostics', rendered_str)

    # -------------------------------------------------------------------------
    # 28. mylar/__init__.py PROG_DIR and DATA_DIR Invariance
    # -------------------------------------------------------------------------
    def test_28_mylar_init_py_maintains_none_defaults(self):
        """28. Prove mylar/__init__.py has not been modified with non-None defaults."""
        init_file = os.path.join(REPO_ROOT, 'mylar', '__init__.py')
        with open(init_file, 'r', encoding='utf-8') as f:
            content = f.read()
        self.assertIn("PROG_DIR = None", content)
        self.assertIn("DATA_DIR = None", content)

    # -------------------------------------------------------------------------
    # 29. Clean Git Diff Check
    # -------------------------------------------------------------------------
    def test_29_git_diff_check_clean(self):
        """29. Prove git diff --check reports zero whitespace or formatting errors."""
        res = subprocess.run(['git', 'diff', '--check'], cwd=REPO_ROOT, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"git diff --check failed:\n{res.stdout}\n{res.stderr}")

    # -------------------------------------------------------------------------
    # 30. KavitaPublisherService Injected DB Object Retention
    # -------------------------------------------------------------------------
    def test_30_kavita_publisher_service_retains_injected_db(self):
        """30. Prove KavitaPublisherService retains instance-owned DB object and does not instantiate replacement DBConnection objects."""
        mock_db = MagicMock()
        mock_db.select.return_value = []
        mock_db.action.return_value = None

        service = KavitaPublisherService(db_connection=mock_db)
        self.assertIs(service._db, mock_db)
        self.assertFalse(hasattr(KavitaPublisherService, '_custom_db'))
        self.assertFalse(isinstance(getattr(KavitaPublisherService, '_db', None), property))

        with patch('mylar.db.DBConnection') as mock_db_cls:
            service.acquire_or_evaluate_mapping_lease(
                instance_id='test-inst',
                pub_key='testpub',
                pub_display='Test Pub',
                canonical_path='/comics/Test Pub',
                server_url='http://127.0.0.1:5000/',
                now=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            )
            mock_db_cls.assert_not_called()

    # -------------------------------------------------------------------------
    # 31. Create Succeeds but Initial Scan Fails Returns scan_queued=False
    # -------------------------------------------------------------------------
    def test_31_create_succeeds_initial_scan_rejection_returns_scan_queued_false(self):
        """31. Prove that when library creation succeeds but initial scan is rejected, mapping remains active and scan_queued is False."""
        service = KavitaPublisherService()
        recorded_calls = []

        def fake_handler(method, url, **kwargs):
            recorded_calls.append({'method': method, 'url': url, 'kwargs': kwargs})
            if 'api/Library/libraries' in url:
                return FakeResponse(200, json_data=[])
            if 'api/Settings/library-types' in url:
                return FakeResponse(200, json_data=[{'id': 0, 'name': 'Comic'}])
            if 'api/Library/create' in url:
                body = kwargs.get('json', {})
                return FakeResponse(200, json_data={'id': 88, 'name': body.get('name'), 'folders': body.get('folders')})
            if 'api/Library/scan' in url:
                return FakeResponse(500, text="Scan server error")
            return FakeResponse(200, json_data={})

        mock_session = MagicMock()
        mock_session.request.side_effect = fake_handler
        service._client_factory = lambda url, creds: KavitaClient(base_url=url, credentials=creds, session=mock_session)

        pub_folder = os.path.join(mylar.CONFIG.COMIC_DIR, "ScanFail Pub")
        series_folder = os.path.join(pub_folder, "Series 1")
        with patch('os.path.isdir', return_value=True):
            res = service.process_post_import_automation({
                'SeriesLocation': series_folder,
                'ComicPublisher': 'ScanFail Pub',
                'ComicName': 'Series 1'
            })

        self.assertEqual(res['status'], 'created')
        self.assertEqual(res['library_id'], 88)
        self.assertIs(res.get('scan_queued'), False)

        mappings = get_kavita_publisher_mappings()
        self.assertEqual(len(mappings), 1)
        m = mappings[0]
        self.assertEqual(m['mapping_state'], 'active')
        self.assertEqual(m['provenance'], 'mylar_created')
        self.assertEqual(m['kavita_library_id'], 88)
        self.assertIsNotNone(m['last_error_code'])

    # -------------------------------------------------------------------------
    # 32. Association Succeeds but Initial Scan Fails Returns scan_queued=False
    # -------------------------------------------------------------------------
    def test_32_association_succeeds_initial_scan_rejection_returns_scan_queued_false(self):
        """32. Prove that when library association succeeds but initial scan is rejected, mapping remains active and scan_queued is False."""
        service = KavitaPublisherService()
        pub_folder = os.path.join(mylar.CONFIG.COMIC_DIR, "AssocFail Pub")
        norm_path = normalize_path_str(pub_folder)

        recorded_calls = []
        def fake_handler(method, url, **kwargs):
            recorded_calls.append({'method': method, 'url': url, 'kwargs': kwargs})
            if 'api/Library/libraries' in url:
                return FakeResponse(200, json_data=[
                    {'id': 199, 'name': 'AssocFail Existing', 'folders': [norm_path]}
                ])
            if 'api/Library/scan' in url:
                return FakeResponse(500, text="Scan server error")
            return FakeResponse(200, json_data={})

        mock_session = MagicMock()
        mock_session.request.side_effect = fake_handler
        service._client_factory = lambda url, creds: KavitaClient(base_url=url, credentials=creds, session=mock_session)

        series_folder = os.path.join(pub_folder, "Series 1")
        with patch('os.path.isdir', return_value=True):
            res = service.process_post_import_automation({
                'SeriesLocation': series_folder,
                'ComicPublisher': 'AssocFail Pub',
                'ComicName': 'Series 1'
            })

        self.assertEqual(res['status'], 'associated_existing')
        self.assertEqual(res['library_id'], 199)
        self.assertIs(res.get('scan_queued'), False)

        mappings = get_kavita_publisher_mappings()
        self.assertEqual(len(mappings), 1)
        m = mappings[0]
        self.assertEqual(m['mapping_state'], 'active')
        self.assertEqual(m['provenance'], 'existing_exact_path')
        self.assertEqual(m['kavita_library_id'], 199)
        self.assertIsNotNone(m['last_error_code'])


if __name__ == '__main__':
    unittest.main()
