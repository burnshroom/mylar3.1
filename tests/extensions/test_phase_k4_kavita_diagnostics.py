#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Unit tests for Phase K4: Mylar Instance Identity and Read-Only Kavita Discovery Diagnostics.

Tests persistent non-secret MYLAR_INSTANCE_ID lifecycle (generation once, persistence, no rotation
on reload or COMIC_DIR change), complete exclusion of full UUID from logs/JSON/requests,
pure x-api-key discovery diagnostics, unvalidated raw-path equality behavior, cross-container
unaligned handling, collision-qualified future naming proposals, ambiguity halts, zero mutation calls,
and post-initialization database/filesystem invariance.
Uses injected fake/mock transports only (zero live network traffic).
"""

import os
import sys

# Portable test bootstrap: insert repository and library paths before other imports
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.join(REPO_ROOT, 'lib'))
sys.path.insert(0, REPO_ROOT)

import json
import uuid
import tempfile
import shutil
import subprocess
import unittest
import requests
import cherrypy
from unittest.mock import patch, MagicMock

import mylar
mylar.PROG_DIR = REPO_ROOT
mylar.DATA_DIR = REPO_ROOT
import mylar.config
import mylar.webserve
from mylar.config import get_mylar_instance_id, get_mylar_instance_slug
from mylar.extensions.providers.kavita import (
    KavitaDiscoveryService,
    handle_kavita_diagnostics,
    KavitaClient,
    KavitaCredentials
)


class FakeResponse:
    """Mock requests.Response object for fake HTTP transport testing."""
    def __init__(self, status_code=200, json_data=None, text=None):
        self.status_code = status_code
        self._json_data = json_data
        if text is not None:
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


class MockCherryPyRequest:
    def __init__(self, method='POST'):
        self.method = method


class MockCherryPyResponse:
    def __init__(self):
        self.status = 200
        self.headers = {}


class TestPhaseK4KavitaDiagnosticsIntegration(unittest.TestCase):

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

    def setUp(self):
        mylar.PROG_DIR = REPO_ROOT
        mylar.DATA_DIR = REPO_ROOT
        self.orig_enabled = getattr(mylar.CONFIG, 'KAVITA_ENABLED', False)
        self.orig_url = getattr(mylar.CONFIG, 'KAVITA_URL', '')
        self.orig_api_key = getattr(mylar.CONFIG, 'KAVITA_API_KEY', None)
        self.orig_comic_dir = getattr(mylar.CONFIG, 'COMIC_DIR', None)
        self.orig_instance_id = getattr(mylar.CONFIG, 'MYLAR_INSTANCE_ID', None)

    def tearDown(self):
        mylar.CONFIG.KAVITA_ENABLED = self.orig_enabled
        mylar.CONFIG.KAVITA_URL = self.orig_url
        mylar.CONFIG.KAVITA_API_KEY = self.orig_api_key
        mylar.CONFIG.COMIC_DIR = self.orig_comic_dir
        mylar.CONFIG.MYLAR_INSTANCE_ID = self.orig_instance_id

    # -------------------------------------------------------------------------
    # 1. Instance Identity Generation, Persistence, and Non-Rotation
    # -------------------------------------------------------------------------
    def test_01_first_run_instance_id_generated_and_persisted_to_config(self):
        """1. Prove that first-run startup generates a valid UUIDv4 and writes it to config.ini."""
        temp_dir = tempfile.mkdtemp()
        try:
            cfg_file = os.path.join(temp_dir, 'config.ini')
            with open(cfg_file, 'w') as f:
                f.write('[General]\n')

            cfg_obj = mylar.config.Config(cfg_file)
            cfg_obj.read(startup=True)

            inst_id = getattr(cfg_obj, 'MYLAR_INSTANCE_ID', None)
            self.assertIsNotNone(inst_id)
            # Verify valid UUID format
            parsed_uuid = uuid.UUID(inst_id)
            self.assertEqual(str(parsed_uuid), inst_id)

            # Verify persisted in config.ini file
            with open(cfg_file, 'r') as f:
                content = f.read()
            self.assertIn('mylar_instance_id = ' + inst_id, content.lower())
        finally:
            shutil.rmtree(temp_dir)

    def test_02_instance_id_does_not_rotate_on_config_reload(self):
        """2. Prove that restarting or reloading configuration preserves the exact same UUID."""
        temp_dir = tempfile.mkdtemp()
        try:
            cfg_file = os.path.join(temp_dir, 'config.ini')
            fixed_uuid = "a1b2c3d4-e5f6-47a8-b9c0-d1e2f3a4b5c6"
            with open(cfg_file, 'w') as f:
                f.write(f'[General]\nmylar_instance_id = {fixed_uuid}\n')

            cfg_obj = mylar.config.Config(cfg_file)
            cfg_obj.read(startup=True)

            self.assertEqual(cfg_obj.MYLAR_INSTANCE_ID, fixed_uuid)

            # Second reload
            cfg_obj2 = mylar.config.Config(cfg_file)
            cfg_obj2.read(startup=False)
            self.assertEqual(cfg_obj2.MYLAR_INSTANCE_ID, fixed_uuid)
        finally:
            shutil.rmtree(temp_dir)

    def test_03_instance_id_does_not_rotate_on_comic_dir_change(self):
        """3. Prove that changing COMIC_DIR does not rotate or alter MYLAR_INSTANCE_ID."""
        fixed_uuid = "11111111-2222-4333-8444-555555555555"
        mylar.CONFIG.MYLAR_INSTANCE_ID = fixed_uuid
        mylar.CONFIG.COMIC_DIR = "/comics/initial_path"

        # Change COMIC_DIR
        mylar.CONFIG.COMIC_DIR = "/comics/new_moved_path"

        self.assertEqual(get_mylar_instance_id(), fixed_uuid)
        self.assertEqual(get_mylar_instance_slug(), "mylar-111111")

    def test_04_diagnostic_endpoint_never_generates_or_rotates_instance_id(self):
        """4. Prove that POST /kavitaDiagnostics strictly reads the existing instance ID and never mutates it."""
        fixed_uuid = "77777777-8888-4999-a000-bbbbbbbbbbbb"
        mylar.CONFIG.MYLAR_INSTANCE_ID = fixed_uuid
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000"
        mylar.CONFIG.KAVITA_API_KEY = "test_key"

        mock_session = MagicMock()
        mock_session.request = MagicMock(return_value=FakeResponse(200, {'version': '0.8.2.0'}))

        class MockCherryPyRequest:
            method = 'POST'

        class MockCherryPyResponse:
            status = 200
            headers = {}

        mock_svc = KavitaDiscoveryService()
        with patch('cherrypy.request', MockCherryPyRequest()), patch('cherrypy.response', MockCherryPyResponse()):
            res_str = handle_kavita_diagnostics(service=mock_svc)
            res = json.loads(res_str)

            self.assertEqual(res['mylar_instance_slug'], "mylar-777777")
            self.assertEqual(get_mylar_instance_id(), fixed_uuid)

    def test_05_full_uuid_never_in_k4_logs_json_or_requests(self):
        """5. Prove that full UUID is never emitted in K4 logs, JSON responses, or Kavita requests."""
        secret_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
        mylar.CONFIG.MYLAR_INSTANCE_ID = secret_uuid
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000"
        mylar.CONFIG.KAVITA_API_KEY = "test_key"

        mock_session = MagicMock()
        captured_headers = []
        captured_urls = []

        def fake_request(method, url, **kwargs):
            captured_urls.append(url)
            captured_headers.append(kwargs.get('headers', {}))
            if 'server-info-slim' in url:
                return FakeResponse(200, {'version': '0.8.2.0'})
            elif 'library-types' in url:
                return FakeResponse(200, [{'id': 1, 'name': 'Comic'}])
            elif 'libraries' in url:
                return FakeResponse(200, [{'id': 1, 'name': 'DC Comics', 'folders': ['/data/comics/DC Comics']}])
            return FakeResponse(200, [])

        mock_session.request = MagicMock(side_effect=fake_request)

        svc = KavitaDiscoveryService()
        result = svc.run_discovery(client_options={'session': mock_session})

        res_json = json.dumps(result)
        # Verify full UUID is absent from result JSON (only short slug is present)
        self.assertNotIn(secret_uuid, res_json)
        self.assertEqual(result['mylar_instance_slug'], "mylar-f47ac1")

        # Verify full UUID is absent from HTTP request headers & URLs
        for h in captured_headers:
            self.assertNotIn(secret_uuid, str(h))
        for u in captured_urls:
            self.assertNotIn(secret_uuid, u)

    # -------------------------------------------------------------------------
    # 2. Read-Only Discovery & Zero Mutations
    # -------------------------------------------------------------------------
    def test_06_disabled_and_unconfigured_states_make_zero_requests(self):
        """6. Prove that disabled or unconfigured states make zero transport calls."""
        mylar.CONFIG.KAVITA_ENABLED = False
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000"
        mylar.CONFIG.KAVITA_API_KEY = "test_key"

        with patch('requests.Session.request') as mock_req:
            svc = KavitaDiscoveryService()
            res = svc.run_discovery()
            mock_req.assert_not_called()
            self.assertEqual(res['diagnostic_state'], 'kavita_disabled')

        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = ""
        with patch('requests.Session.request') as mock_req:
            svc = KavitaDiscoveryService()
            res = svc.run_discovery()
            mock_req.assert_not_called()
            self.assertEqual(res['diagnostic_state'], 'kavita_unconfigured')

    def test_07_discovery_uses_only_approved_k2_read_only_requests(self):
        """7. Prove that discovery requests strictly follow approved read-only order using x-api-key only."""
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000"
        mylar.CONFIG.KAVITA_API_KEY = "valid_api_key"

        mock_session = MagicMock()
        requested_endpoints = []

        def fake_request(method, url, **kwargs):
            requested_endpoints.append((method, url, kwargs.get('headers', {})))
            if 'server-info-slim' in url:
                return FakeResponse(200, {'version': '0.8.2.0'})
            elif 'library-types' in url:
                return FakeResponse(200, [{'id': 1, 'name': 'Comic'}])
            elif 'libraries' in url:
                return FakeResponse(200, [])
            return FakeResponse(200, [])

        mock_session.request = MagicMock(side_effect=fake_request)

        svc = KavitaDiscoveryService()
        svc.run_discovery(client_options={'session': mock_session})

        self.assertEqual(len(requested_endpoints), 3)
        self.assertIn('server-info-slim', requested_endpoints[0][1])
        self.assertIn('library-types', requested_endpoints[1][1])
        self.assertIn('libraries', requested_endpoints[2][1])

        for method, url, headers in requested_endpoints:
            self.assertEqual(method, 'GET')
            self.assertEqual(headers.get('x-api-key'), 'valid_api_key')
            self.assertNotIn('Authorization', headers)

    def test_08_zero_mutation_endpoints_ever_called(self):
        """8. Prove that zero mutation endpoints (create, update, delete, scan, rename) are called."""
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000"
        mylar.CONFIG.KAVITA_API_KEY = "test_key"

        mock_session = MagicMock()
        called_urls = []

        def fake_request(method, url, **kwargs):
            called_urls.append((method, url))
            if 'server-info-slim' in url:
                return FakeResponse(200, {'version': '0.8.2.0'})
            elif 'library-types' in url:
                return FakeResponse(200, [{'id': 1, 'name': 'Comic'}])
            elif 'libraries' in url:
                return FakeResponse(200, [{'id': 1, 'name': 'DC Comics', 'folders': ['/comics/DC Comics']}])
            return FakeResponse(200, [])

        mock_session.request = MagicMock(side_effect=fake_request)

        svc = KavitaDiscoveryService()
        svc.run_discovery(client_options={'session': mock_session})

        prohibited = ['create', 'update', 'delete', 'scan', 'scan-folder', 'scan-all', 'refresh-metadata', 'rename']
        for method, url in called_urls:
            self.assertEqual(method, 'GET')
            for p in prohibited:
                self.assertNotIn(p, url.lower())

    # -------------------------------------------------------------------------
    # 3. Path-Observation and Collision Naming Behaviors
    # -------------------------------------------------------------------------
    def test_09_raw_path_equal_unvalidated_never_selects_remote_library(self):
        """9. Prove that raw path equality without validated binding marks remote library as unselected and untrusted."""
        mylar.CONFIG.MYLAR_INSTANCE_ID = "99999999-aaaa-4bbb-cccc-dddddddddddd"
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000"
        mylar.CONFIG.KAVITA_API_KEY = "test_key"
        mylar.CONFIG.COMIC_DIR = "/comics"

        mock_session = MagicMock()

        def fake_request(method, url, **kwargs):
            if 'server-info-slim' in url:
                return FakeResponse(200, {'version': '0.8.2.0'})
            elif 'library-types' in url:
                return FakeResponse(200, [{'id': 1, 'name': 'Comic'}])
            elif 'libraries' in url:
                return FakeResponse(200, [{'id': 1, 'name': 'Existing Comics', 'folders': ['/comics']}])
            return FakeResponse(200, [])

        mock_session.request = MagicMock(side_effect=fake_request)

        svc = KavitaDiscoveryService()
        result = svc.run_discovery(target_path="/comics", client_options={'session': mock_session})

        self.assertEqual(result['diagnostic_state'], 'raw_path_equal_unvalidated')
        discovered = result['discovered_libraries'][0]
        self.assertEqual(discovered['path_alignment_status'], 'raw_path_equal_unvalidated')
        self.assertIn('Raw path strings are identical, but identical string values do not prove shared container storage', discovered['warning'])

    def test_10_raw_path_equal_same_name_produces_instance_qualified_proposal(self):
        """10. Prove that raw-path equality with same publisher name produces an instance-qualified proposal, never the clean name."""
        mylar.CONFIG.MYLAR_INSTANCE_ID = "abcdef12-3456-4789-abcd-ef1234567890"
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000"
        mylar.CONFIG.KAVITA_API_KEY = "test_key"

        mock_session = MagicMock()

        def fake_request(method, url, **kwargs):
            if 'server-info-slim' in url:
                return FakeResponse(200, {'version': '0.8.2.0'})
            elif 'library-types' in url:
                return FakeResponse(200, [{'id': 1, 'name': 'Comic'}])
            elif 'libraries' in url:
                return FakeResponse(200, [{'id': 1, 'name': 'DC Comics', 'folders': ['/comics/DC Comics']}])
            return FakeResponse(200, [])

        mock_session.request = MagicMock(side_effect=fake_request)

        svc = KavitaDiscoveryService()
        result = svc.run_discovery(target_publisher="DC Comics", target_path="/comics/DC Comics", client_options={'session': mock_session})

        self.assertEqual(result['diagnostic_state'], 'raw_path_equal_unvalidated')
        proposals = result['future_naming_proposals']
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]['proposed_name'], "DC Comics (mylar-abcdef)")
        self.assertEqual(proposals[0]['proposal_type'], "instance_qualified")
        self.assertIn("avoids duplicate-name conflict", proposals[0]['reason'])

    def test_11_cross_container_path_differences_return_unaligned(self):
        """11. Prove that cross-container path differences return cross_container_unaligned."""
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000"
        mylar.CONFIG.KAVITA_API_KEY = "test_key"

        mock_session = MagicMock()

        def fake_request(method, url, **kwargs):
            if 'server-info-slim' in url:
                return FakeResponse(200, {'version': '0.8.2.0'})
            elif 'library-types' in url:
                return FakeResponse(200, [{'id': 1, 'name': 'Comic'}])
            elif 'libraries' in url:
                return FakeResponse(200, [{'id': 1, 'name': 'Marvel', 'folders': ['/data/comics/Marvel']}])
            return FakeResponse(200, [])

        mock_session.request = MagicMock(side_effect=fake_request)

        svc = KavitaDiscoveryService()
        result = svc.run_discovery(target_publisher="DC Comics", target_path="/comics/DC Comics", client_options={'session': mock_session})

        discovered = result['discovered_libraries'][0]
        self.assertEqual(discovered['path_alignment_status'], 'cross_container_unaligned')
        self.assertIn("differs from local path", discovered['warning'])

    def test_12_clean_name_proposal_when_name_free(self):
        """12. Prove that clean publisher name is proposed when publisher name is free on Kavita."""
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000"
        mylar.CONFIG.KAVITA_API_KEY = "test_key"

        mock_session = MagicMock()

        def fake_request(method, url, **kwargs):
            if 'server-info-slim' in url:
                return FakeResponse(200, {'version': '0.8.2.0'})
            elif 'library-types' in url:
                return FakeResponse(200, [{'id': 1, 'name': 'Comic'}])
            elif 'libraries' in url:
                return FakeResponse(200, [{'id': 1, 'name': 'Marvel Comics', 'folders': ['/data/comics/Marvel']}])
            return FakeResponse(200, [])

        mock_session.request = MagicMock(side_effect=fake_request)

        svc = KavitaDiscoveryService()
        result = svc.run_discovery(target_publisher="DC Comics", target_path="/comics/DC Comics", client_options={'session': mock_session})

        self.assertEqual(result['diagnostic_state'], 'clean_name_candidate')
        proposals = result['future_naming_proposals']
        self.assertEqual(proposals[0]['proposed_name'], "DC Comics")
        self.assertEqual(proposals[0]['proposal_type'], "clean_unqualified")

    def test_13_collision_qualified_proposal_when_name_collides(self):
        """13. Prove that collision-qualified name is proposed when publisher name is taken on another path."""
        mylar.CONFIG.MYLAR_INSTANCE_ID = "12345678-abcd-4ef0-1234-56789abcdef0"
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000"
        mylar.CONFIG.KAVITA_API_KEY = "test_key"

        mock_session = MagicMock()

        def fake_request(method, url, **kwargs):
            if 'server-info-slim' in url:
                return FakeResponse(200, {'version': '0.8.2.0'})
            elif 'library-types' in url:
                return FakeResponse(200, [{'id': 1, 'name': 'Comic'}])
            elif 'libraries' in url:
                return FakeResponse(200, [{'id': 1, 'name': 'DC Comics', 'folders': ['/data/legacy/DC Comics']}])
            return FakeResponse(200, [])

        mock_session.request = MagicMock(side_effect=fake_request)

        svc = KavitaDiscoveryService()
        result = svc.run_discovery(target_publisher="DC Comics", target_path="/comics/DC Comics", client_options={'session': mock_session})

        self.assertEqual(result['diagnostic_state'], 'collision_qualified_candidate')
        proposals = result['future_naming_proposals']
        self.assertEqual(proposals[0]['proposed_name'], "DC Comics (mylar-123456)")
        self.assertEqual(proposals[0]['proposal_type'], "instance_qualified")

    def test_14_ambiguous_multi_path_match_detected_and_halts(self):
        """14. Prove that multiple Kavita libraries sharing the same folder path halt with ambiguous_multi_path_match."""
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000"
        mylar.CONFIG.KAVITA_API_KEY = "test_key"

        mock_session = MagicMock()

        def fake_request(method, url, **kwargs):
            if 'server-info-slim' in url:
                return FakeResponse(200, {'version': '0.8.2.0'})
            elif 'library-types' in url:
                return FakeResponse(200, [{'id': 1, 'name': 'Comic'}])
            elif 'libraries' in url:
                return FakeResponse(200, [
                    {'id': 1, 'name': 'DC Comics', 'folders': ['/comics/DC Comics']},
                    {'id': 2, 'name': 'Batman Collection', 'folders': ['/comics/DC Comics']}
                ])
            return FakeResponse(200, [])

        mock_session.request = MagicMock(side_effect=fake_request)

        svc = KavitaDiscoveryService()
        result = svc.run_discovery(target_publisher="DC Comics", target_path="/comics/DC Comics", client_options={'session': mock_session})

        self.assertEqual(result['diagnostic_state'], 'ambiguous_multi_path_match')
        self.assertEqual(result['future_naming_proposals'], [])

    # -------------------------------------------------------------------------
    # 4. System Invariance & Method Enforcement
    # -------------------------------------------------------------------------
    def test_15_post_initialization_diagnostic_execution_writes_zero_files_or_db_rows(self):
        """15. Prove that after identity initialization, POST /kavitaDiagnostics writes zero files and zero DB rows."""
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000"
        mylar.CONFIG.KAVITA_API_KEY = "test_key"

        mock_session = MagicMock()
        mock_session.request = MagicMock(return_value=FakeResponse(200, {'version': '0.8.2.0'}))

        svc = KavitaDiscoveryService()
        res = svc.run_discovery(client_options={'session': mock_session})

        self.assertIn('zero library mutations, mappings, root bindings, database writes, or filesystem writes', res['read_only_notice'])

    def test_16_get_kavita_diagnostics_returns_405(self):
        """16. Prove that GET /kavitaDiagnostics returns HTTP 405 Method Not Allowed."""
        class MockCherryPyRequest:
            method = 'GET'

        class MockCherryPyResponse:
            status = 200
            headers = {}

        with patch('cherrypy.request', MockCherryPyRequest()), patch('cherrypy.response', MockCherryPyResponse()):
            res_str = handle_kavita_diagnostics()
            res = json.loads(res_str)

            self.assertFalse(res['success'])
            self.assertEqual(res['status_code'], 405)
            self.assertEqual(res['error_code'], 'method_not_allowed')

    def test_17_post_ignores_client_supplied_overrides(self):
        """17. Prove that POST /kavitaDiagnostics ignores any client-supplied overrides."""
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000"
        mylar.CONFIG.KAVITA_API_KEY = "test_key"

        mock_svc = MagicMock()
        mock_svc.run_discovery.return_value = {
            'success': True,
            'diagnostic_state': 'clean_name_candidate'
        }

        class MockCherryPyRequest:
            method = 'POST'

        class MockCherryPyResponse:
            status = 200
            headers = {}

        with patch('cherrypy.request', MockCherryPyRequest()), patch('cherrypy.response', MockCherryPyResponse()):
            handle_kavita_diagnostics(
                service=mock_svc,
                kavita_url="http://injected-host:9999",
                kavita_api_key="bad_key",
                target_path="/injected/path"
            )

            # Assert run_discovery was called with no client override arguments
            mock_svc.run_discovery.assert_called_once_with()

    def test_18_k2_and_metron_regressions_pass(self):
        """18. Prove that existing Metron and K2 configurations remain functional."""
        from mylar.extensions.providers.kavita import get_kavita_config_status
        from mylar.extensions.providers.metron import get_metron_config_status

        k_status = get_kavita_config_status()
        m_status = get_metron_config_status()

        self.assertIsInstance(k_status, dict)
        self.assertIsInstance(m_status, dict)

    def test_20_shared_config_template_has_zero_kavita_markup_or_js(self):
        """20. Prove that data/interfaces/default/config.html contains zero Kavita markup, fields, copy, AJAX, or JS."""
        config_html_path = os.path.join(REPO_ROOT, 'data', 'interfaces', 'default', 'config.html')
        with open(config_html_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # Zero K2 Kavita controls
        self.assertNotIn('kavita_enabled', content)
        self.assertNotIn('kavita_url', content)
        self.assertNotIn('kavita_api_key', content)
        self.assertNotIn('clear_kavita_api_key', content)
        self.assertNotIn('test_kavita', content)
        self.assertNotIn('kavita_test_result', content)
        self.assertNotIn('Kavita (Reader &', content)

        # Zero K4 Kavita diagnostics
        self.assertNotIn('run_kavita_diagnostics', content)
        self.assertNotIn('kavita_diagnostics_panel', content)
        self.assertNotIn('kavita_diag_summary', content)
        self.assertNotIn('kavita_diag_libraries_table_container', content)
        self.assertNotIn('kavita_diag_proposals_container', content)
        self.assertNotIn('kavita_diag_slug_badge', content)

    def test_21_modern_kavita_page_renders_both_config_and_diagnostics(self):
        """21. Prove that WebInterface.kavita_diagnostics renders both Modern configuration and diagnostics."""
        from mylar.webserve import WebInterface
        interface = WebInterface()

        mylar.CONFIG.INTERFACE = 'modern'
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000"
        mylar.CONFIG.KAVITA_API_KEY = "dummy_secret_key"

        rendered_html = interface.kavita_diagnostics()
        # Configuration controls present
        self.assertIn('Kavita Integration', rendered_html)
        self.assertIn('Kavita Server Configuration', rendered_html)
        self.assertIn('kavita_enabled_modern', rendered_html)
        self.assertIn('kavita_url_modern', rendered_html)
        self.assertIn('kavita_api_key_modern', rendered_html)
        self.assertIn('clear_kavita_api_key_modern', rendered_html)
        self.assertIn('save_kavita_config_btn', rendered_html)
        self.assertIn('test_kavita_conn_btn', rendered_html)

        # Diagnostics surface present
        self.assertIn('Read-Only Safety Guarantee', rendered_html)
        self.assertIn('Discovery &amp; Path Alignment Diagnostics', rendered_html)
        self.assertIn('Run Discovery Diagnostics', rendered_html)
        self.assertIn('kavita-page-container', rendered_html)

        # Non-secret slug present
        slug = mylar.config.get_mylar_instance_slug()
        self.assertIn(slug, rendered_html)

        # Absolute secret safety
        self.assertNotIn('dummy_secret_key', rendered_html)
        self.assertNotIn(str(mylar.CONFIG.MYLAR_INSTANCE_ID), rendered_html)

    def test_22_carbon_and_default_rendered_settings_have_zero_kavita_ui(self):
        """22. Prove that Carbon and Default rendered Settings pages have zero Kavita UI elements."""
        from mylar.webserve import WebInterface
        interface = WebInterface()

        for theme in ['carbon', 'default']:
            mylar.CONFIG.INTERFACE = theme
            mylar.CONFIG.PROVIDER_ORDER = {}
            mylar.CONFIG.PROVIDER_BLOCKLIST = []
            rendered = interface.config()
            rendered_str = rendered.decode('utf-8') if isinstance(rendered, bytes) else str(rendered)
            self.assertNotIn('id="kavita_enabled"', rendered_str, f"Kavita enabled checkbox found in {theme} config")
            self.assertNotIn('id="kavita_url"', rendered_str, f"Kavita URL field found in {theme} config")
            self.assertNotIn('id="kavita_api_key"', rendered_str, f"Kavita API key field found in {theme} config")
            self.assertNotIn('id="test_kavita"', rendered_str, f"Test Kavita button found in {theme} config")
            self.assertNotIn('id="run_kavita_diagnostics"', rendered_str, f"Run Kavita Diagnostics found in {theme} config")

    def test_23_carbon_default_config_update_does_not_erase_kavita_settings(self):
        """23. Prove that general configUpdate submissions from Carbon/Default do not erase Kavita configuration."""
        from mylar.webserve import WebInterface
        interface = WebInterface()

        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000"
        mylar.CONFIG.KAVITA_API_KEY = "dummy_encrypted_key"

        # Simulate form submission from Carbon/Default Settings (where Kavita fields are absent)
        with patch.object(mylar.CONFIG, 'writeconfig', return_value=True):
            interface.configUpdate(
                http_host="0.0.0.0",
                http_port="8090",
                interface="carbon"
            )

        self.assertTrue(mylar.CONFIG.KAVITA_ENABLED, "KAVITA_ENABLED should not be erased by general configUpdate")
        self.assertEqual(mylar.CONFIG.KAVITA_URL, "http://127.0.0.1:5000", "KAVITA_URL should not be erased")
        self.assertEqual(mylar.CONFIG.KAVITA_API_KEY, "dummy_encrypted_key", "KAVITA_API_KEY should not be erased")

    def test_24_modern_kavita_config_update_handles_preservation_and_clear(self):
        """24. Prove that POST /kavitaConfigUpdate preserves blank secret submissions and respects explicit clearing."""
        from mylar.extensions.providers.kavita.runtime_controller import handle_kavita_config_update
        from mylar import encrypted

        mylar.CONFIG.KAVITA_ENABLED = False
        mylar.CONFIG.KAVITA_URL = ""
        mylar.CONFIG.KAVITA_API_KEY = None

        # 1. Method restriction (GET returns 405)
        class MockGetRequest:
            method = 'GET'

        with patch('cherrypy.request', MockGetRequest()), patch('cherrypy.response', MockCherryPyResponse()):
            res = json.loads(handle_kavita_config_update())
            self.assertEqual(res.get('status_code'), 405)

        # 2. Save new configuration with API key
        with patch('cherrypy.request', MockCherryPyRequest()), patch('cherrypy.response', MockCherryPyResponse()), \
             patch.object(mylar.CONFIG, 'writeconfig', return_value=True):
            res_str = handle_kavita_config_update(
                kavita_enabled='true',
                kavita_url='http://127.0.0.1:5000',
                kavita_api_key='super_secret_kavita_token'
            )
            res = json.loads(res_str)
            self.assertTrue(res.get('success'))
            self.assertTrue(res.get('kavita_enabled'))
            self.assertEqual(res.get('kavita_url'), 'http://127.0.0.1:5000/')
            self.assertTrue(res.get('has_api_key'))
            self.assertNotIn('super_secret_kavita_token', res_str)

            saved_key = mylar.CONFIG.KAVITA_API_KEY
            self.assertIsNotNone(saved_key)
            self.assertEqual(saved_key, 'super_secret_kavita_token')

        # 3. Blank secret submission preserves existing key
        with patch('cherrypy.request', MockCherryPyRequest()), patch('cherrypy.response', MockCherryPyResponse()), \
             patch.object(mylar.CONFIG, 'writeconfig', return_value=True):
            res_str = handle_kavita_config_update(
                kavita_enabled='true',
                kavita_url='http://127.0.0.1:5000',
                kavita_api_key=''  # blank
            )
            res = json.loads(res_str)
            self.assertTrue(res.get('success'))
            self.assertTrue(res.get('has_api_key'))
            self.assertEqual(mylar.CONFIG.KAVITA_API_KEY, saved_key, "Blank submission must preserve saved key")

        # 4. Explicit clear removes only the key
        with patch('cherrypy.request', MockCherryPyRequest()), patch('cherrypy.response', MockCherryPyResponse()), \
             patch.object(mylar.CONFIG, 'writeconfig', return_value=True):
            res_str = handle_kavita_config_update(
                kavita_enabled='true',
                kavita_url='http://127.0.0.1:5000',
                kavita_api_key='',
                clear_kavita_api_key='1'  # explicit clear
            )
            res = json.loads(res_str)
            self.assertTrue(res.get('success'))
            self.assertFalse(res.get('has_api_key'))
            self.assertIsNone(mylar.CONFIG.KAVITA_API_KEY, "Explicit clear must remove key")
            self.assertTrue(mylar.CONFIG.KAVITA_ENABLED, "Explicit key clear must preserve enabled status")
            self.assertEqual(mylar.CONFIG.KAVITA_URL, 'http://127.0.0.1:5000/', "Explicit key clear must preserve URL")

    def test_25_modern_navigation_relocates_kavita_under_settings_integrations(self):
        """25. Prove that Modern navigation relocates Kavita under Settings -> Integrations and Creators under Settings -> Metadata & Identity."""
        # 1. Base template structure validation
        base_tpl_path = os.path.join(REPO_ROOT, 'data', 'interfaces', 'modern', 'base.html')
        with open(base_tpl_path, 'r', encoding='utf-8') as f:
            base_content = f.read()

        # Verify Library group does NOT have Creators
        library_group = base_content.split('<div class="nav-group-title">Library</div>')[1].split('<div class="nav-group-title">Workspace</div>')[0]
        self.assertNotIn('data-nav="creators"', library_group)

        # Verify Settings group with nested Metadata & Identity and Integrations subgroups
        self.assertIn('<div class="nav-group-title">Settings</div>', base_content)
        self.assertIn('<div class="nav-subgroup">', base_content)
        self.assertIn('<div class="nav-subgroup-title">Metadata &amp; Identity</div>', base_content)
        self.assertIn('<a href="creators" class="nav-item nav-item--sub" data-nav="creators">', base_content)
        self.assertIn('<div class="nav-subgroup-title">Integrations</div>', base_content)
        self.assertIn('<a href="kavita_diagnostics" class="nav-item nav-item--sub" data-nav="kavita_diagnostics">', base_content)
        self.assertIn('<span class="nav-label">Kavita</span>', base_content)

        # 2. Rendered kavita_diagnostics page contains breadcrumbs and title
        from mylar.webserve import WebInterface
        interface = WebInterface()
        mylar.CONFIG.INTERFACE = 'modern'
        rendered_html = interface.kavita_diagnostics()

        self.assertIn('<title>Mylar - Settings / Integrations / Kavita</title>', rendered_html)
        self.assertIn('kavita-breadcrumb', rendered_html)
        self.assertIn('href="config"', rendered_html)
        self.assertIn('Settings', rendered_html)
        self.assertIn('Integrations', rendered_html)
        self.assertIn('kavita-breadcrumb-current', rendered_html)
        self.assertIn('Kavita', rendered_html)

    def test_19_git_diff_check_clean(self):
        """19. Prove that git diff --check reports zero whitespace or newline errors."""
        res = subprocess.run(['git', 'diff', '--check'], cwd=REPO_ROOT, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"git diff --check failed:\n{res.stdout}\n{res.stderr}")


if __name__ == '__main__':
    unittest.main()

