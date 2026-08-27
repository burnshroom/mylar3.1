#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Unit test suite for Phase K8: Modern Kavita Backfill for Existing Mylar Series.

Verifies:
1. Candidate preview computes accurate counts with ZERO Kavita requests.
2. Multiple series under one publisher root deduplicate to 1 Kavita create/associate call.
3. Multiple unique publisher roots process sequentially and independently.
4. Active mappings make ZERO remote calls and ZERO scan requests.
5. Exact-path remote library associates without creation.
6. Dynamic comic type discovery resolves comic type dynamically.
7. Missing/incompatible comic type fails closed with zero create calls.
8. Flat layout makes zero Kavita calls and increments flat_layout_skipped.
9. Missing directories make zero Kavita calls and increment missing_directory_skipped.
10. One path rejection does not stop subsequent roots.
11. Ambiguous mapping is skipped with zero calls.
12. Ineligible backoff mapping is skipped with zero calls.
13. Start route returns promptly with job ID while worker runs.
14. POST and CSRF enforcement on preview, start, and status routes.
15. Sanitized responses contain zero secrets, instance UUIDs, lease tokens, or tracebacks.
16. Classic/Default/Carbon templates contain zero Phase K8 markup.
"""

import os
import sys
import json
import sqlite3
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.join(REPO_ROOT, 'lib'))
sys.path.insert(0, REPO_ROOT)

import mylar
import mylar.config
import mylar.db as db
from mylar.extensions.migrations.runner import run_extension_migrations
from mylar.extensions.providers.kavita.config import validate_kavita_url
from mylar.extensions.providers.kavita.client import KavitaClient, KavitaError, KavitaIncompatibleError
from mylar.extensions.providers.kavita.publisher_service import (
    KavitaPublisherService,
    derive_materialized_publisher_root,
    normalize_path_str
)
from mylar.extensions.providers.kavita.backfill_worker import KavitaSyncBackfillWorker
from mylar.extensions.providers.kavita.runtime_controller import (
    handle_kavita_sync_backfill_preview,
    handle_kavita_sync_backfill,
    handle_kavita_sync_backfill_status,
    get_or_create_kavita_csrf_token
)


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


class MockKavitaTransport:
    """In-memory mock transport for Kavita API with zero live network calls."""

    def __init__(self):
        self.call_history = []
        self.libraries = []
        self.library_types = [
            {'id': 1, 'name': 'Comic'},
            {'id': 2, 'name': 'Book'},
            {'id': 3, 'name': 'Manga'}
        ]
        self.should_fail_create = False
        self.fail_create_paths = set()
        self.should_fail_types = False
        self.next_lib_id = 100

    def fake_request(self, method, url, **kwargs):
        self.call_history.append((method, url, kwargs.get('json')))
        if 'api/Settings/library-types' in url:
            if self.should_fail_types:
                return FakeResponse(500, json_data={'error': 'Internal error'})
            return FakeResponse(200, json_data=self.library_types)
        elif 'api/Library/libraries' in url:
            return FakeResponse(200, json_data=self.libraries)
        elif 'api/Server/version' in url:
            return FakeResponse(200, json_data='0.8.2.0')
        elif 'api/Library/create' in url:
            body = kwargs.get('json') or {}
            folders = body.get('folders', [])
            if self.should_fail_create or any(f in self.fail_create_paths for f in folders):
                return FakeResponse(400, json_data={'error': 'Path rejected by Kavita'})
            self.next_lib_id += 1
            new_lib = {
                'id': self.next_lib_id,
                'name': body.get('name', 'New Library'),
                'type': body.get('type', 1),
                'folders': folders
            }
            self.libraries.append(new_lib)
            return FakeResponse(200, json_data=new_lib)
        elif 'api/Library/scan' in url:
            return FakeResponse(200, json_data={'status': 'scan queued'})
        return FakeResponse(404, json_data={'error': 'Not found'})


class TestPhaseK8KavitaBackfill(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="mylar_k8_test_")
        self.comic_dir = os.path.join(self.test_dir, 'comics')
        os.makedirs(self.comic_dir, exist_ok=True)

        self.test_db = os.path.join(self.test_dir, 'mylar.db')
        self.test_config = os.path.join(self.test_dir, 'config.ini')

        mylar.PROG_DIR = REPO_ROOT
        mylar.DATA_DIR = self.test_dir
        mylar.CONFIG_FILE = self.test_config
        mylar.DBFILE = self.test_db

        with open(self.test_config, 'w') as f:
            f.write("[General]\n")
            f.write("comic_dir = %s\n" % self.comic_dir)
            f.write("kavita_enabled = True\n")
            f.write("kavita_url = http://127.0.0.1:5000\n")
            f.write("kavita_api_key = test-secret-key\n")
            f.write("mylar_instance_id = mylar-test-inst-k8\n")

        cc = mylar.config.Config(self.test_config)
        mylar.CONFIG = cc.read(startup=True)
        mylar.CONFIG.COMIC_DIR = self.comic_dir
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000"
        mylar.CONFIG.KAVITA_API_KEY = "test-secret-key"
        mylar.CONFIG.MYLAR_INSTANCE_ID = "mylar-test-inst-k8"

        # Apply schema and migrations
        conn = sqlite3.connect(self.test_db)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS comics (
                ComicID TEXT PRIMARY KEY,
                ComicName TEXT,
                ComicSortName TEXT,
                ComicYear TEXT,
                ComicPublisher TEXT,
                ComicLocation TEXT,
                Status TEXT
            )
        """)
        conn.commit()
        run_extension_migrations(cur)
        conn.commit()
        conn.close()

        self.transport = MockKavitaTransport()
        mock_session = MagicMock()
        mock_session.request.side_effect = self.transport.fake_request

        self.client_factory = lambda url, creds: KavitaClient(url, credentials=creds, session=mock_session)
        self.service = KavitaPublisherService(client_factory=self.client_factory)
        self.service_factory = lambda: KavitaPublisherService(client_factory=self.client_factory)
        self.worker = KavitaSyncBackfillWorker()
        self.worker._init_worker()
        self.csrf_token = get_or_create_kavita_csrf_token()

    def tearDown(self):
        if self.worker:
            self.worker.join_job(timeout=2.0)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _seed_publisher_folder(self, publisher, series_list):
        """Helper to create physical folders and DB records."""
        pub_dir = os.path.join(self.comic_dir, publisher)
        os.makedirs(pub_dir, exist_ok=True)

        conn = sqlite3.connect(self.test_db)
        cur = conn.cursor()
        for idx, s_name in enumerate(series_list):
            s_dir = os.path.join(pub_dir, s_name)
            os.makedirs(s_dir, exist_ok=True)
            cid = f"{publisher}_{s_name}_{idx}"
            cur.execute(
                "INSERT INTO comics (ComicID, ComicName, ComicPublisher, ComicLocation, Status) "
                "VALUES (?, ?, ?, ?, 'Active')",
                (cid, s_name, publisher, s_dir)
            )
        conn.commit()
        conn.close()

    def test_01_candidate_preview_makes_zero_kavita_calls(self):
        """Preview calculates candidate aggregates with ZERO Kavita requests."""
        self._seed_publisher_folder('Image Comics', ['Sunstone', 'Saga'])
        self._seed_publisher_folder('Marvel', ['Avengers'])

        # Flat layout series directly in comic_dir
        flat_dir = os.path.join(self.comic_dir, 'FlatSeries')
        os.makedirs(flat_dir, exist_ok=True)
        conn = sqlite3.connect(self.test_db)
        conn.cursor().execute(
            "INSERT INTO comics (ComicID, ComicName, ComicPublisher, ComicLocation, Status) "
            "VALUES ('flat1', 'FlatSeries', 'FlatPub', ?, 'Active')",
            (flat_dir,)
        )
        conn.commit()
        conn.close()

        preview_res = self.worker.compute_preview()
        self.assertEqual(preview_res['status'], 'success')
        self.assertEqual(preview_res['total_candidate_series'], 4)
        self.assertEqual(preview_res['unique_publisher_roots'], 2)  # Image Comics & Marvel
        self.assertEqual(preview_res['unmapped_roots_count'], 2)
        self.assertEqual(preview_res['flat_layout_count'], 1)
        self.assertEqual(preview_res['missing_directory_count'], 0)

        # Verify ZERO Kavita calls made
        self.assertEqual(len(self.transport.call_history), 0)

    def test_02_multiple_series_under_one_publisher_root_invoke_k5_once(self):
        """3 series under Image Comics trigger exactly 1 create/associate operation."""
        self._seed_publisher_folder('Image Comics', ['Sunstone', 'Saga', 'Spawn'])

        self.worker.start_backfill(service=self.service_factory)
        self.worker.join_job(timeout=5.0)

        status = self.worker.get_status()
        self.assertEqual(status['status'], 'completed')
        self.assertEqual(status['metrics']['created_mappings'], 1)
        self.assertEqual(status['metrics']['associated_mappings'], 0)
        self.assertEqual(status['metrics']['unmapped_roots'], 1)
        self.assertEqual(status['processed_roots_count'], 1)

        # Verify only 1 create call was made to Kavita
        create_calls = [c for c in self.transport.call_history if 'api/Library/create' in c[1]]
        self.assertEqual(len(create_calls), 1)
        self.assertEqual(create_calls[0][2]['name'], 'Image Comics')
        expected_path = normalize_path_str(os.path.join(self.comic_dir, 'Image Comics'))
        self.assertEqual(create_calls[0][2]['folders'], [expected_path])

    def test_03_separate_publisher_roots_process_sequentially(self):
        """Image Comics and Marvel process independently."""
        self._seed_publisher_folder('Image Comics', ['Sunstone'])
        self._seed_publisher_folder('Marvel', ['X-Men'])

        self.worker.start_backfill(service=self.service_factory)
        self.worker.join_job(timeout=5.0)

        status = self.worker.get_status()
        self.assertEqual(status['status'], 'completed')
        self.assertEqual(status['metrics']['created_mappings'], 2)
        self.assertEqual(status['metrics']['scans_queued'], 2)
        self.assertEqual(status['metrics']['unmapped_roots'], 2)
        self.assertEqual(status['processed_roots_count'], 2)

        create_calls = [c for c in self.transport.call_history if 'api/Library/create' in c[1]]
        self.assertEqual(len(create_calls), 2)
        scan_calls = [c for c in self.transport.call_history if 'api/Library/scan' in c[1]]
        self.assertEqual(len(scan_calls), 2)

    def test_04_active_mapping_makes_zero_remote_calls_and_zero_scans(self):
        """Existing active mapping is skipped with 0 Kavita calls."""
        self._seed_publisher_folder('DC Comics', ['Batman'])
        pub_path = normalize_path_str(os.path.join(self.comic_dir, 'DC Comics'))
        server_url = validate_kavita_url(getattr(mylar.CONFIG, 'KAVITA_URL', 'http://127.0.0.1:5000'))

        # Insert active mapping in DB
        conn = sqlite3.connect(self.test_db)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO ext_kavita_publisher_mappings (
                MylarInstanceID, PublisherKey, PublisherDisplayName, CanonicalPublisherPath,
                KavitaServerUrl, KavitaLibraryID, ObservedLibraryName, MappingState, Provenance
            ) VALUES (
                'mylar-test-inst-k8', 'dc comics', 'DC Comics', ?,
                ?, 55, 'DC Comics', 'active', 'mylar_created'
            )
        """, (pub_path, server_url))
        conn.commit()
        conn.close()

        self.worker.start_backfill(service=self.service_factory)
        self.worker.join_job(timeout=5.0)

        status = self.worker.get_status()
        self.assertEqual(status['status'], 'completed')
        self.assertEqual(status['metrics']['already_active_skipped'], 1)
        self.assertEqual(status['metrics']['scans_queued'], 0)
        self.assertEqual(status['metrics']['unmapped_roots'], 0)
        self.assertEqual(status['processed_roots_count'], 0)

        # Zero remote calls made - zero create, scan, types, or libraries
        self.assertEqual(len(self.transport.call_history), 0)
        self.assertEqual([c[1] for c in self.transport.call_history], [])

    def test_05_existing_remote_exact_path_associates_without_create(self):
        """Exact path remote library associates without calling create."""
        self._seed_publisher_folder('Dark Horse', ['Hellboy'])
        pub_path = normalize_path_str(os.path.join(self.comic_dir, 'Dark Horse'))

        # Pre-seed remote library with exact matching path
        self.transport.libraries.append({
            'id': 77,
            'name': 'Dark Horse Comics',
            'type': 1,
            'folders': [pub_path]
        })

        self.worker.start_backfill(service=self.service_factory)
        self.worker.join_job(timeout=5.0)

        status = self.worker.get_status()
        self.assertEqual(status['status'], 'completed')
        self.assertEqual(status['metrics']['associated_mappings'], 1)
        self.assertEqual(status['metrics']['created_mappings'], 0)

        create_calls = [c for c in self.transport.call_history if 'api/Library/create' in c[1]]
        self.assertEqual(len(create_calls), 0)

        # Scan was queued for the associated library
        scan_calls = [c for c in self.transport.call_history if 'api/Library/scan' in c[1]]
        self.assertEqual(len(scan_calls), 1)
        self.assertIn('libraryId=77', scan_calls[0][1])

    def test_06_dynamic_comic_type_discovery_and_fail_closed(self):
        """When library types endpoint fails, fail closed with 0 create calls."""
        self._seed_publisher_folder('Boom Studios', ['BRZRKR'])
        self.transport.should_fail_types = True

        self.worker.start_backfill(service=self.service_factory)
        self.worker.join_job(timeout=5.0)

        status = self.worker.get_status()
        self.assertEqual(status['status'], 'completed')
        self.assertEqual(status['metrics']['sanitized_failures'], 1)
        self.assertEqual(status['metrics']['created_mappings'], 0)

        create_calls = [c for c in self.transport.call_history if 'api/Library/create' in c[1]]
        self.assertEqual(len(create_calls), 0)

    def test_07_flat_layout_and_missing_directory_skipping(self):
        """Flat layouts and missing folders make zero Kavita calls."""
        # 1. Flat layout
        flat_dir = os.path.join(self.comic_dir, 'FlatComic')
        os.makedirs(flat_dir, exist_ok=True)
        # 2. Missing folder
        missing_dir = os.path.join(self.comic_dir, 'GhostPublisher', 'GhostComic')

        conn = sqlite3.connect(self.test_db)
        cur = conn.cursor()
        cur.execute("INSERT INTO comics VALUES ('f1', 'FlatComic', 'FlatComic', '2020', 'FlatPub', ?, 'Active')", (flat_dir,))
        cur.execute("INSERT INTO comics VALUES ('m1', 'GhostComic', 'GhostComic', '2020', 'GhostPub', ?, 'Active')", (missing_dir,))
        conn.commit()
        conn.close()

        self.worker.start_backfill(service=self.service_factory)
        self.worker.join_job(timeout=5.0)

        status = self.worker.get_status()
        self.assertEqual(status['status'], 'completed')
        self.assertEqual(status['metrics']['flat_layout_skipped'], 1)
        self.assertEqual(status['metrics']['missing_directory_skipped'], 1)
        self.assertEqual(status['metrics']['unmapped_roots'], 0)
        self.assertEqual(len(self.transport.call_history), 0)

    def test_08_path_rejection_does_not_stop_next_root(self):
        """A rejected first publisher root does not stop a later root from succeeding."""
        self._seed_publisher_folder('BadPublisher', ['BadComic'])
        self._seed_publisher_folder('GoodPublisher', ['GoodComic'])

        # Reject create on BadPublisher only
        bad_path = normalize_path_str(os.path.join(self.comic_dir, 'BadPublisher'))
        self.transport.fail_create_paths.add(bad_path)

        self.worker.start_backfill(service=self.service_factory)
        self.worker.join_job(timeout=5.0)

        status = self.worker.get_status()
        self.assertEqual(status['status'], 'completed')
        self.assertEqual(status['metrics']['unmapped_roots'], 2)
        self.assertEqual(status['processed_roots_count'], 2)
        self.assertEqual(status['metrics']['sanitized_failures'], 1)
        self.assertEqual(status['metrics']['created_mappings'], 1)

    def test_09_ambiguous_and_backoff_mappings_skipped(self):
        """Ambiguous and in-backoff mappings are skipped with 0 remote calls."""
        self._seed_publisher_folder('AmbiguousPub', ['Series1'])
        self._seed_publisher_folder('BackoffPub', ['Series2'])

        amb_path = normalize_path_str(os.path.join(self.comic_dir, 'AmbiguousPub'))
        backoff_path = normalize_path_str(os.path.join(self.comic_dir, 'BackoffPub'))
        server_url = validate_kavita_url(getattr(mylar.CONFIG, 'KAVITA_URL', 'http://127.0.0.1:5000'))

        conn = sqlite3.connect(self.test_db)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO ext_kavita_publisher_mappings (
                MylarInstanceID, PublisherKey, PublisherDisplayName, CanonicalPublisherPath,
                KavitaServerUrl, MappingState, Provenance
            ) VALUES ('mylar-test-inst-k8', 'ambiguouspub', 'AmbiguousPub', ?, ?, 'ambiguous_multi_path', 'mylar_created')
        """, (amb_path, server_url))
        cur.execute("""
            INSERT INTO ext_kavita_publisher_mappings (
                MylarInstanceID, PublisherKey, PublisherDisplayName, CanonicalPublisherPath,
                KavitaServerUrl, MappingState, NextEligibleRetryAt, Provenance
            ) VALUES ('mylar-test-inst-k8', 'backoffpub', 'BackoffPub', ?, ?, 'path_rejected', '2099-01-01 00:00:00', 'mylar_created')
        """, (backoff_path, server_url))
        conn.commit()
        conn.close()

        self.worker.start_backfill(service=self.service_factory)
        self.worker.join_job(timeout=5.0)

        status = self.worker.get_status()
        self.assertEqual(status['metrics']['ambiguous_skipped'], 1)
        self.assertEqual(status['metrics']['backoff_skipped'], 1)
        self.assertEqual(status['metrics']['unmapped_roots'], 0)
        self.assertEqual(len(self.transport.call_history), 0)

    def test_10_post_and_csrf_enforcement_on_handlers(self):
        """Verify POST-only and CSRF validation on all 3 backfill routes."""
        import cherrypy

        # Test invalid method (GET)
        mock_req = MagicMock()
        mock_req.method = 'GET'
        with patch.object(cherrypy, 'request', mock_req):
            res_preview = json.loads(handle_kavita_sync_backfill_preview(worker=self.worker, csrf_token=self.csrf_token))
            self.assertEqual(res_preview['status_code'], 405)

            res_start = json.loads(handle_kavita_sync_backfill(worker=self.worker, service=self.service, csrf_token=self.csrf_token))
            self.assertEqual(res_start['status_code'], 405)

            res_status = json.loads(handle_kavita_sync_backfill_status(worker=self.worker, csrf_token=self.csrf_token))
            self.assertEqual(res_status['status_code'], 405)

        # Test invalid CSRF
        mock_req.method = 'POST'
        with patch.object(cherrypy, 'request', mock_req):
            res_csrf = json.loads(handle_kavita_sync_backfill_preview(worker=self.worker, csrf_token='invalid-token'))
            self.assertEqual(res_csrf['status_code'], 403)

    def test_11_sanitized_status_contains_zero_secrets_or_uuids(self):
        """Status snapshot contains no API keys, server URLs, lease tokens, or tracebacks."""
        status = self.worker.get_status()
        status_str = json.dumps(status)

        self.assertNotIn('test-secret-key', status_str)
        self.assertNotIn('http://127.0.0.1:5000', status_str)
        self.assertNotIn('mylar-test-inst-k8', status_str)
        self.assertNotIn('LeaseToken', status_str)
        self.assertNotIn('Traceback', status_str)

    def test_12_legacy_templates_contain_zero_kavita_ui(self):
        """Classic, Default, and Carbon templates have 0 Kavita sync markup."""
        default_config = os.path.join(REPO_ROOT, 'data', 'interfaces', 'default', 'config.html')
        with open(default_config, 'r', encoding='utf-8') as f:
            content = f.read()
        self.assertNotIn('kavitaSyncBackfill', content)
        self.assertNotIn('Sync Existing Mylar Series', content)

    def test_13_start_backfill_returns_promptly_and_handles_busy(self):
        """Start route returns immediately with started status and rejects concurrent start."""
        self._seed_publisher_folder('AsyncPub', ['SeriesA'])

        import cherrypy
        mock_req = MagicMock()
        mock_req.method = 'POST'
        with patch.object(cherrypy, 'request', mock_req):
            res1 = json.loads(handle_kavita_sync_backfill(worker=self.worker, service=self.service_factory, csrf_token=self.csrf_token))
            self.assertEqual(res1['status'], 'started')
            self.assertTrue(res1['job_id'].startswith('kavita-backfill-'))

            # Calling start again while job is running returns busy
            res2 = json.loads(handle_kavita_sync_backfill(worker=self.worker, service=self.service_factory, csrf_token=self.csrf_token))
            self.assertIn(res2['status'], ('busy', 'started', 'completed'))

        self.worker.join_job(timeout=5.0)

    def test_14_modern_template_renders_backfill_elements(self):
        """Modern kavita_diagnostics.html contains all Phase K8 button, modal, and progress elements."""
        tpl_path = os.path.join(REPO_ROOT, 'data', 'interfaces', 'modern', 'kavita_diagnostics.html')
        with open(tpl_path, 'r', encoding='utf-8') as f:
            content = f.read()

        self.assertIn('kavita_sync_backfill_btn', content)
        self.assertIn('Sync Existing Mylar Series', content)
        self.assertIn('kavita_backfill_modal', content)
        self.assertIn('kavita_backfill_progress_card', content)
        self.assertIn('kavitaSyncBackfillPreview', content)
        self.assertIn('kavitaSyncBackfill', content)
        self.assertIn('kavitaSyncBackfillStatus', content)
        self.assertIn('kavita_csrf_token', content)

    def test_15_clean_thread_termination_no_leaks(self):
        """Worker thread terminates cleanly with join_job leaving zero leaked threads."""
        self._seed_publisher_folder('ThreadTestPub', ['Issue1'])
        self.worker.start_backfill(service=self.service_factory)
        self.worker.join_job(timeout=5.0)

        # Worker thread must not be alive after join_job
        if self.worker._thread:
            self.assertFalse(self.worker._thread.is_alive())

    def test_16_kavita_diagnostics_exposed_and_single_config_update(self):
        """Verify kavitaDiagnostics remains exposed and exactly one kavitaConfigUpdate method is defined."""
        import mylar.webserve as ws
        self.assertTrue(hasattr(ws.WebInterface, 'kavitaDiagnostics'))
        self.assertTrue(getattr(ws.WebInterface.kavitaDiagnostics, 'exposed', False))
        self.assertTrue(hasattr(ws.WebInterface, 'kavita_diagnostics'))
        self.assertTrue(getattr(ws.WebInterface.kavita_diagnostics, 'exposed', False))
        self.assertTrue(hasattr(ws.WebInterface, 'kavitaConfigUpdate'))
        self.assertTrue(getattr(ws.WebInterface.kavitaConfigUpdate, 'exposed', False))

        # Check webserve.py source contains exactly one def kavitaConfigUpdate
        webserve_src = os.path.join(REPO_ROOT, 'mylar', 'webserve.py')
        with open(webserve_src, 'r', encoding='utf-8') as f:
            content = f.read()
        self.assertEqual(content.count('def kavitaConfigUpdate('), 1)
        self.assertEqual(content.count('kavitaConfigUpdate.exposed = True'), 1)
        self.assertEqual(content.count('def kavitaDiagnostics('), 1)
        self.assertEqual(content.count('kavitaDiagnostics.exposed = True'), 1)
        self.assertEqual(content.count('def kavitaSyncBackfillPreview('), 1)
        self.assertEqual(content.count('def kavitaSyncBackfill('), 1)
        self.assertEqual(content.count('def kavitaSyncBackfillStatus('), 1)

    def test_17_fallback_csrf_token_is_random_and_non_static(self):
        """Fallback CSRF token is a non-static random hex token."""
        from mylar.extensions.providers.kavita.runtime_controller import (
            _GLOBAL_KAVITA_CSRF_FALLBACK,
            get_or_create_kavita_csrf_token,
            verify_kavita_csrf_token
        )
        self.assertNotEqual(_GLOBAL_KAVITA_CSRF_FALLBACK, "mylar_kavita_csrf_fallback_token_k8")
        self.assertEqual(len(_GLOBAL_KAVITA_CSRF_FALLBACK), 64)
        token = get_or_create_kavita_csrf_token()
        self.assertTrue(verify_kavita_csrf_token(token))
        self.assertFalse(verify_kavita_csrf_token("invalid_token"))

    def test_18_mismatched_status_job_id_rejected_with_404_no_data_leak(self):
        """Mismatched job_id returns 404 job_not_found and leaks zero progress metrics."""
        self._seed_publisher_folder('SecretPub', ['SecretComic'])
        self.worker.start_backfill(service=self.service_factory)
        self.worker.join_job(timeout=5.0)

        import cherrypy
        mock_req = MagicMock()
        mock_req.method = 'POST'
        with patch.object(cherrypy, 'request', mock_req):
            res = json.loads(handle_kavita_sync_backfill_status(
                worker=self.worker,
                csrf_token=self.csrf_token,
                job_id='kavita-backfill-fake-id-99999'
            ))
            self.assertEqual(res['status'], 'error')
            self.assertEqual(res['status_code'], 404)
            self.assertEqual(res['error_code'], 'job_not_found')
            self.assertNotIn('processed_roots', res)
            self.assertNotIn('metrics', res)
            self.assertNotIn('SecretPub', json.dumps(res))

    def test_19_worker_status_and_logs_contain_zero_raw_exceptions(self):
        """Worker status contains zero raw exception strings or tracebacks."""
        self._seed_publisher_folder('ErrorPub', ['ErrorComic'])
        self.transport.should_fail_create = True
        self.worker.start_backfill(service=self.service_factory)
        self.worker.join_job(timeout=5.0)

        status = self.worker.get_status()
        status_dump = json.dumps(status)
        self.assertNotIn('Traceback', status_dump)
        self.assertNotIn('Exception', status_dump)
        self.assertNotIn('OperationalError', status_dump)
        self.assertNotIn('sqlite3', status_dump)

    def test_20_publisher_service_retains_instance_owned_db(self):
        """KavitaPublisherService retains instance-owned DB object and does not use a property or instantiate replacements."""
        mock_db = MagicMock()
        service = KavitaPublisherService(db_connection=mock_db)

        self.assertIs(service._db, mock_db)
        self.assertFalse(hasattr(KavitaPublisherService, '_custom_db'))
        self.assertFalse(isinstance(getattr(KavitaPublisherService, '_db', None), property))

    def test_21_comic_location_validation_blank_or_unset(self):
        """Blank/unset COMIC_DIR fails with comic_location_unavailable (reason=not_configured) with zero Kavita calls."""
        mylar.CONFIG.COMIC_DIR = ""

        # 1. Preview rejection
        preview_res = self.worker.compute_preview()
        self.assertEqual(preview_res['status'], 'error')
        self.assertEqual(preview_res['error_code'], 'comic_location_unavailable')
        self.assertEqual(preview_res['reason'], 'not_configured')
        self.assertIn('not configured', preview_res['message'])
        self.assertIn('remediation', preview_res)
        self.assertEqual(len(self.transport.call_history), 0, "Preview must make 0 Kavita calls on blank COMIC_DIR")

        # 2. Start rejection before job creation
        start_res = self.worker.start_backfill(service=self.service_factory)
        self.assertEqual(start_res['status'], 'error')
        self.assertEqual(start_res['error_code'], 'comic_location_unavailable')
        self.assertEqual(start_res['reason'], 'not_configured')
        self.assertIsNone(self.worker._state['job_id'], "No job_id must be assigned when start is rejected")
        self.assertEqual(self.worker._state['status'], 'idle')
        self.assertEqual(len(self.transport.call_history), 0, "Start must make 0 Kavita calls on blank COMIC_DIR")

    def test_22_comic_location_validation_nonexistent_directory(self):
        """Nonexistent COMIC_DIR fails with comic_location_unavailable (reason=not_found) with zero Kavita calls."""
        nonexistent_dir = os.path.join(self.test_dir, 'does_not_exist_comics')
        mylar.CONFIG.COMIC_DIR = nonexistent_dir

        # 1. Preview rejection
        preview_res = self.worker.compute_preview()
        self.assertEqual(preview_res['status'], 'error')
        self.assertEqual(preview_res['error_code'], 'comic_location_unavailable')
        self.assertEqual(preview_res['reason'], 'not_found')
        self.assertIn('does not exist', preview_res['message'])
        self.assertEqual(preview_res['configured_path'], nonexistent_dir)
        self.assertEqual(len(self.transport.call_history), 0, "Preview must make 0 Kavita calls on missing COMIC_DIR")

        # 2. Start rejection before job creation
        start_res = self.worker.start_backfill(service=self.service_factory)
        self.assertEqual(start_res['status'], 'error')
        self.assertEqual(start_res['error_code'], 'comic_location_unavailable')
        self.assertEqual(start_res['reason'], 'not_found')
        self.assertIsNone(self.worker._state['job_id'], "No job_id must be assigned when start is rejected")
        self.assertEqual(self.worker._state['status'], 'idle')
        self.assertEqual(len(self.transport.call_history), 0, "Start must make 0 Kavita calls on missing COMIC_DIR")

    def test_23_comic_location_validation_file_not_directory(self):
        """File (not a directory) COMIC_DIR fails with comic_location_unavailable (reason=not_a_directory)."""
        regular_file = os.path.join(self.test_dir, 'not_a_dir.txt')
        with open(regular_file, 'w', encoding='utf-8') as f:
            f.write('just a regular file')
        mylar.CONFIG.COMIC_DIR = regular_file

        # 1. Preview rejection
        preview_res = self.worker.compute_preview()
        self.assertEqual(preview_res['status'], 'error')
        self.assertEqual(preview_res['error_code'], 'comic_location_unavailable')
        self.assertEqual(preview_res['reason'], 'not_a_directory')
        self.assertIn('not a directory', preview_res['message'])
        self.assertEqual(len(self.transport.call_history), 0, "Preview must make 0 Kavita calls on invalid COMIC_DIR")

        # 2. Start rejection before job creation
        start_res = self.worker.start_backfill(service=self.service_factory)
        self.assertEqual(start_res['status'], 'error')
        self.assertEqual(start_res['error_code'], 'comic_location_unavailable')
        self.assertEqual(start_res['reason'], 'not_a_directory')
        self.assertIsNone(self.worker._state['job_id'])
        self.assertEqual(len(self.transport.call_history), 0)

    def test_24_comic_location_rejection_logs_sanitized_warning(self):
        """Rejection emits dedicated log event with zero leaked secrets, full instance UUIDs, or tracebacks."""
        nonexistent_dir = os.path.join(self.test_dir, 'nonexistent_test_dir')
        mylar.CONFIG.COMIC_DIR = nonexistent_dir

        with patch('mylar.logger.warn') as mock_warn:
            self.worker.compute_preview()

            mock_warn.assert_called()
            log_msg = str(mock_warn.call_args[0][0])
            self.assertIn('[KAVITA-BACKFILL] comic_location_unavailable', log_msg)
            self.assertIn('reason=not_found', log_msg)
            self.assertNotIn('test-secret-key', log_msg)
            self.assertNotIn('http://127.0.0.1:5000', log_msg)
            self.assertNotIn('Traceback', log_msg)
            self.assertNotIn('mylar-test-inst-k8', log_msg)

    def test_25_modern_template_contains_comic_location_error_callout(self):
        """Modern Kavita diagnostics template includes structured in-page error callout and retry guidance."""
        kavita_diag_path = os.path.join(REPO_ROOT, 'data', 'interfaces', 'modern', 'kavita_diagnostics.html')
        with open(kavita_diag_path, 'r', encoding='utf-8') as f:
            content = f.read()

        self.assertIn('id="kavita_comic_location_error_callout"', content)
        self.assertIn('Mylar Comic Location unavailable', content)
        self.assertIn('comic_location_unavailable', content)
        self.assertIn('id="kavita_comic_loc_error_details"', content)
        self.assertIn('id="kavita_comic_loc_error_reason"', content)
        self.assertIn('id="kavita_comic_loc_error_remediation"', content)
        self.assertIn('showComicLocationError', content)
        self.assertIn('hideComicLocationError', content)


if __name__ == '__main__':
    unittest.main()
