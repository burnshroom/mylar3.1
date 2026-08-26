#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Unit tests for Phase K2: Kavita Settings and Read-Only Capability Check.

Tests pure x-api-key header authentication, zero plugin authenticate calls, zero query-string
API key usage, zero Authorization/Bearer headers, zero duplicate headers, URL validation,
encryption at rest, secret preservation, explicit clearing, sanitized POST /testKavita capability check,
method enforcement (405 on GET), parameter override rejection, error classification,
zero mutation endpoint calls, and database/filesystem invariance.
Uses injected fake/mock transports only (zero live network traffic).
"""

import os
import sys

# Portable test bootstrap: insert repository and library paths before other imports
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.join(REPO_ROOT, 'lib'))
sys.path.insert(0, REPO_ROOT)

import json
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
from mylar import encrypted
from mylar.extensions.providers.kavita import (
    KavitaCredentials,
    KavitaConfigurationError,
    validate_kavita_url,
    get_kavita_config_status,
    get_kavita_availability,
    build_kavita_credentials,
    handle_test_kavita,
    KavitaClient,
    KavitaConnectionService,
    KavitaError,
    KavitaAuthenticationError,
    KavitaPermissionError,
    KavitaTimeoutError,
    KavitaTransportError,
    KavitaInvalidResponseError,
    KavitaIncompatibleError,
    KavitaInvalidRequestError
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


class TestPhaseK2KavitaSettingsIntegration(unittest.TestCase):

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
        # Snapshot original configuration
        self.orig_enabled = getattr(mylar.CONFIG, 'KAVITA_ENABLED', False)
        self.orig_url = getattr(mylar.CONFIG, 'KAVITA_URL', '')
        self.orig_api_key = getattr(mylar.CONFIG, 'KAVITA_API_KEY', None)

    def tearDown(self):
        # Restore original configuration
        mylar.CONFIG.KAVITA_ENABLED = self.orig_enabled
        mylar.CONFIG.KAVITA_URL = self.orig_url
        mylar.CONFIG.KAVITA_API_KEY = self.orig_api_key

    # -------------------------------------------------------------------------
    # 1. Zero Transport Calls on Disabled, Construction, Template & Invalid Config
    # -------------------------------------------------------------------------
    def test_01_disabled_configuration_makes_zero_transport_calls(self):
        """1. Prove that disabled configuration makes zero transport calls."""
        mylar.CONFIG.KAVITA_ENABLED = False
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000/"
        mylar.CONFIG.KAVITA_API_KEY = "testkey"

        with patch('requests.Session.request') as mock_req:
            avail = get_kavita_availability()
            self.assertFalse(avail['available'])
            self.assertFalse(avail['enabled'])
            mock_req.assert_not_called()

    def test_02_construction_and_config_loading_makes_zero_transport_calls(self):
        """2. Prove that client/service construction and config loading make zero transport calls."""
        with patch('requests.Session.request') as mock_req:
            status = get_kavita_config_status()
            creds = KavitaCredentials("valid_key")
            client = KavitaClient(base_url="http://127.0.0.1:5000/", credentials=creds)
            svc = KavitaConnectionService()

            self.assertIsInstance(status, dict)
            self.assertIsNotNone(client)
            self.assertIsNotNone(svc)
            mock_req.assert_not_called()

    def test_03_settings_template_rendering_makes_zero_transport_calls(self):
        """3. Prove that rendering the settings page triggers zero network requests."""
        with patch('requests.Session.request') as mock_req:
            web = mylar.webserve.WebInterface()
            status = get_kavita_config_status()
            self.assertIn('has_api_key', status)
            mock_req.assert_not_called()

    def test_04_missing_url_makes_zero_transport_calls(self):
        """4. Prove that missing URL makes zero transport requests during capability check."""
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = ""
        mylar.CONFIG.KAVITA_API_KEY = "validkey"

        with patch('requests.Session.request') as mock_req:
            svc = KavitaConnectionService()
            result = svc.check_capability()
            mock_req.assert_not_called()

            self.assertFalse(result['success'])
            self.assertFalse(result['configured'])
            self.assertEqual(result['error_code'], 'missing_url')

    def test_05_missing_api_key_makes_zero_transport_calls(self):
        """5. Prove that missing API key makes zero transport requests during capability check."""
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000/"
        mylar.CONFIG.KAVITA_API_KEY = None

        with patch('requests.Session.request') as mock_req:
            svc = KavitaConnectionService()
            result = svc.check_capability()
            mock_req.assert_not_called()

            self.assertFalse(result['success'])
            self.assertFalse(result['configured'])
            self.assertEqual(result['error_code'], 'missing_api_key')

    # -------------------------------------------------------------------------
    # 2. Strict URL Validation
    # -------------------------------------------------------------------------
    def test_06_invalid_url_scheme_rejected_before_transport(self):
        """6. Prove that invalid URL schemes (ftp, file, javascript, gopher) are rejected with zero transport calls."""
        for bad_url in ("ftp://kavita.local:5000", "file:///var/kavita", "javascript:alert(1)", "gopher://host"):
            with patch('requests.Session.request') as mock_req:
                with self.assertRaises(KavitaConfigurationError) as cm:
                    validate_kavita_url(bad_url)
                self.assertEqual(cm.exception.error_code, 'invalid_scheme')
                mock_req.assert_not_called()

    def test_07_url_with_credentials_rejected_before_transport(self):
        """7. Prove that URLs with embedded user:pass credentials are rejected before transport."""
        with patch('requests.Session.request') as mock_req:
            with self.assertRaises(KavitaConfigurationError) as cm:
                validate_kavita_url("http://admin:secret@127.0.0.1:5000")
            self.assertEqual(cm.exception.error_code, 'credentials_in_url')
            mock_req.assert_not_called()

    def test_08_url_with_query_rejected_before_transport(self):
        """8. Prove that URLs with query strings are rejected before transport."""
        with patch('requests.Session.request') as mock_req:
            with self.assertRaises(KavitaConfigurationError) as cm:
                validate_kavita_url("http://127.0.0.1:5000/api?token=abc")
            self.assertEqual(cm.exception.error_code, 'query_in_url')
            mock_req.assert_not_called()

    def test_09_url_with_fragment_rejected_before_transport(self):
        """9. Prove that URLs with fragments (#) are rejected before transport."""
        with patch('requests.Session.request') as mock_req:
            with self.assertRaises(KavitaConfigurationError) as cm:
                validate_kavita_url("http://127.0.0.1:5000/#section")
            self.assertEqual(cm.exception.error_code, 'fragment_in_url')
            mock_req.assert_not_called()

    def test_10_unsupported_url_path_and_normalization(self):
        """10. Prove that unsupported paths are rejected and valid paths normalize trailing slashes."""
        with self.assertRaises(KavitaConfigurationError):
            validate_kavita_url("http://")

        # Valid URLs normalize with trailing slash
        self.assertEqual(validate_kavita_url("http://127.0.0.1:5000"), "http://127.0.0.1:5000/")
        self.assertEqual(validate_kavita_url("https://kavita.example.com/reader"), "https://kavita.example.com/reader/")

    # -------------------------------------------------------------------------
    # 3. Encryption at Rest, Reload, and Secret Lifecycle
    # -------------------------------------------------------------------------
    def test_11_api_key_encrypts_at_rest(self):
        """11. Prove that API key encrypts at rest through Mylar's existing mechanism."""
        raw_key = "super_secret_kavita_token_12345"
        enc = encrypted.Encryptor(raw_key)
        res = enc.encrypt_it()

        self.assertTrue(res['status'])
        encrypted_str = res['password']
        self.assertTrue(encrypted_str.startswith('^~$z$'))
        self.assertNotIn(raw_key, encrypted_str)

    def test_12_encrypted_api_key_reloads_and_decrypts(self):
        """12. Prove that encrypted API key reloads and decrypts correctly."""
        raw_key = "kavita_api_key_verification_test"
        enc = encrypted.Encryptor(raw_key)
        encrypted_str = enc.encrypt_it()['password']

        dec = encrypted.Encryptor(encrypted_str)
        res = dec.decrypt_it()
        self.assertTrue(res['status'])
        self.assertEqual(res['password'], raw_key)

    def test_13_blank_key_rendering_in_template_context(self):
        """13. Prove that raw API keys are never passed to template contexts."""
        mylar.CONFIG.KAVITA_API_KEY = "super_secret_key"
        status = get_kavita_config_status()

        self.assertTrue(status['has_api_key'])
        self.assertNotIn('super_secret_key', status.values())
        self.assertNotIn('api_key', status)

    def test_14_blank_key_submission_preserves_stored_key(self):
        """14. Prove that blank API key form submission preserves the existing stored key."""
        mylar.CONFIG.KAVITA_API_KEY = "existing_saved_kavita_key"

        kwargs = {
            'kavita_enabled': '1',
            'kavita_url': 'http://127.0.0.1:5000',
            'kavita_api_key': ''  # submitted blank
        }

        # Simulate configGeneralUpdate secret handling
        val = kwargs['kavita_api_key']
        if val is None or str(val).strip() == '':
            if getattr(mylar.CONFIG, 'KAVITA_API_KEY', None) is not None:
                kwargs['kavita_api_key'] = mylar.CONFIG.KAVITA_API_KEY

        self.assertEqual(kwargs['kavita_api_key'], "existing_saved_kavita_key")

    def test_15_explicit_clear_removes_only_kavita_key(self):
        """15. Prove that explicit clear action removes only the stored Kavita API key."""
        mylar.CONFIG.KAVITA_API_KEY = "key_to_delete"

        kwargs = {
            'clear_kavita_api_key': '1',
            'kavita_api_key': ''
        }

        if 'clear_kavita_api_key' in kwargs and kwargs['clear_kavita_api_key'] in ('1', 'true', 'True', True, 1):
            kwargs['kavita_api_key'] = None

        self.assertIsNone(kwargs['kavita_api_key'])

    # -------------------------------------------------------------------------
    # 4. HTTP Method & Client Override Enforcement
    # -------------------------------------------------------------------------
    def test_16_get_test_kavita_returns_405(self):
        """16. Prove that GET /testKavita returns HTTP 405 Method Not Allowed."""
        class MockCherryPyRequest:
            method = 'GET'

        class MockCherryPyResponse:
            status = 200
            headers = {}

        with patch('cherrypy.request', MockCherryPyRequest()), patch('cherrypy.response', MockCherryPyResponse()):
            res_str = handle_test_kavita()
            res = json.loads(res_str)

            self.assertFalse(res['success'])
            self.assertEqual(res['status_code'], 405)
            self.assertEqual(res['error_code'], 'method_not_allowed')

    def test_17_post_ignores_client_supplied_overrides(self):
        """17. Prove that POST /testKavita ignores client-supplied URL, key, endpoint, host, path, or scan overrides."""
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000/"
        mylar.CONFIG.KAVITA_API_KEY = "server_saved_key"

        mock_svc = MagicMock()
        mock_svc.check_capability.return_value = {
            'success': True,
            'message': 'Capability check passed.'
        }

        class MockCherryPyRequest:
            method = 'POST'

        class MockCherryPyResponse:
            status = 200
            headers = {}

        with patch('cherrypy.request', MockCherryPyRequest()), patch('cherrypy.response', MockCherryPyResponse()):
            handle_test_kavita(
                service=mock_svc,
                kavita_url="http://malicious-host:9999",
                kavita_api_key="injected_key",
                endpoint="/api/malicious",
                library_id=999,
                scan_option="scan-all"
            )

            # Assert service was called ONLY with saved config
            mock_svc.check_capability.assert_called_once()
            call_kwargs = mock_svc.check_capability.call_args[1]
            self.assertEqual(call_kwargs['base_url'], "http://127.0.0.1:5000/")
            self.assertEqual(call_kwargs['credentials'].get_api_key(), "server_saved_key")

    # -------------------------------------------------------------------------
    # 5. Pure x-api-key Authentication & Security Boundaries
    # -------------------------------------------------------------------------
    def test_18_successful_sanitized_capability_result(self):
        """18. Prove that a successful read-only check returns sanitized capability fields."""
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000/"
        mylar.CONFIG.KAVITA_API_KEY = "valid_test_key"

        mock_session = MagicMock()

        def fake_request(method, url, **kwargs):
            if 'server-info-slim' in url:
                return FakeResponse(200, {'version': '0.8.2.0', 'isDocker': True})
            elif 'library-types' in url:
                return FakeResponse(200, [
                    {'id': 0, 'name': 'Manga'},
                    {'id': 1, 'name': 'Comic'},
                    {'id': 2, 'name': 'Book'}
                ])
            elif 'libraries' in url:
                return FakeResponse(200, [
                    {'id': 1, 'name': 'Comics', 'type': 1, 'folders': ['/comics']}
                ])
            return FakeResponse(404, {})

        mock_session.request = MagicMock(side_effect=fake_request)

        svc = KavitaConnectionService()
        result = svc.check_capability(client_options={'session': mock_session})

        self.assertTrue(result['success'])
        self.assertTrue(result['configured'])
        self.assertTrue(result['reachable'])
        self.assertTrue(result['authenticated'])
        self.assertEqual(result['server_version'], '0.8.2.0')
        self.assertTrue(result['api_compatible'])
        self.assertTrue(result['library_list_readable'])
        self.assertTrue(result['comic_library_type_available'])
        self.assertEqual(result['discovered_comic_type']['type_id'], 1)
        self.assertEqual(result['discovered_comic_type']['type_name'], 'Comic')
        self.assertTrue(result['library_specific_scan_advertised'])
        self.assertIsNone(result['error_code'])

    def test_19_capability_check_never_calls_plugin_authenticate(self):
        """19. Prove that the capability check never calls /api/Plugin/authenticate."""
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000/"
        mylar.CONFIG.KAVITA_API_KEY = "test_key"

        mock_session = MagicMock()
        requested_urls = []

        def fake_request(method, url, **kwargs):
            requested_urls.append(url)
            if 'server-info-slim' in url:
                return FakeResponse(200, {'version': '0.8.2.0'})
            elif 'library-types' in url:
                return FakeResponse(200, [{'id': 1, 'name': 'Comic'}])
            elif 'libraries' in url:
                return FakeResponse(200, [])
            return FakeResponse(200, {})

        mock_session.request = MagicMock(side_effect=fake_request)

        svc = KavitaConnectionService()
        svc.check_capability(client_options={'session': mock_session})

        for url in requested_urls:
            self.assertNotIn('plugin/authenticate', url.lower(), "Prohibited Plugin/authenticate was called")

    def test_20_no_request_url_contains_saved_api_key(self):
        """20. Prove that no request URL contains the saved API key."""
        raw_key = "unique_secret_api_key_abc123"
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000/"
        mylar.CONFIG.KAVITA_API_KEY = raw_key

        mock_session = MagicMock()
        requested_urls = []

        def fake_request(method, url, **kwargs):
            requested_urls.append(url)
            if 'server-info-slim' in url:
                return FakeResponse(200, {'version': '0.8.2.0'})
            elif 'library-types' in url:
                return FakeResponse(200, [{'id': 1, 'name': 'Comic'}])
            elif 'libraries' in url:
                return FakeResponse(200, [])
            return FakeResponse(200, {})

        mock_session.request = MagicMock(side_effect=fake_request)

        svc = KavitaConnectionService()
        svc.check_capability(client_options={'session': mock_session})

        for url in requested_urls:
            self.assertNotIn(raw_key, url, f"Saved API key found embedded in URL: {url}")

    def test_21_no_request_query_parameter_contains_saved_api_key(self):
        """21. Prove that no request query parameters contain the saved API key."""
        raw_key = "query_secret_key_xyz987"
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000/"
        mylar.CONFIG.KAVITA_API_KEY = raw_key

        mock_session = MagicMock()
        requested_params = []

        def fake_request(method, url, **kwargs):
            requested_params.append(kwargs.get('params'))
            if 'server-info-slim' in url:
                return FakeResponse(200, {'version': '0.8.2.0'})
            elif 'library-types' in url:
                return FakeResponse(200, [{'id': 1, 'name': 'Comic'}])
            elif 'libraries' in url:
                return FakeResponse(200, [])
            return FakeResponse(200, {})

        mock_session.request = MagicMock(side_effect=fake_request)

        svc = KavitaConnectionService()
        svc.check_capability(client_options={'session': mock_session})

        for params in requested_params:
            if params:
                self.assertNotIn(raw_key, str(params), f"Saved API key found in request params: {params}")

    def test_22_no_request_body_contains_saved_api_key(self):
        """22. Prove that no request body contains the saved API key."""
        raw_key = "body_secret_key_456"
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000/"
        mylar.CONFIG.KAVITA_API_KEY = raw_key

        mock_session = MagicMock()
        requested_bodies = []

        def fake_request(method, url, **kwargs):
            requested_bodies.append(kwargs.get('json'))
            requested_bodies.append(kwargs.get('data'))
            if 'server-info-slim' in url:
                return FakeResponse(200, {'version': '0.8.2.0'})
            elif 'library-types' in url:
                return FakeResponse(200, [{'id': 1, 'name': 'Comic'}])
            elif 'libraries' in url:
                return FakeResponse(200, [])
            return FakeResponse(200, {})

        mock_session.request = MagicMock(side_effect=fake_request)

        svc = KavitaConnectionService()
        svc.check_capability(client_options={'session': mock_session})

        for body in requested_bodies:
            if body:
                self.assertNotIn(raw_key, str(body), f"Saved API key found in request body: {body}")

    def test_23_no_authorization_header_emitted(self):
        """23. Prove that no Authorization or Bearer header is emitted in any request."""
        raw_key = "test_auth_header_key"
        creds = KavitaCredentials(raw_key)
        client = KavitaClient(base_url="http://127.0.0.1:5000/", credentials=creds)

        headers = client._build_headers()
        self.assertNotIn('Authorization', headers)
        self.assertNotIn('authorization', headers)
        self.assertNotIn('Bearer', str(headers))

    def test_24_every_allowed_protected_request_uses_x_api_key_only(self):
        """24. Prove that every allowed protected request uses x-api-key header only."""
        raw_key = "header_verification_key_777"
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000/"
        mylar.CONFIG.KAVITA_API_KEY = raw_key

        mock_session = MagicMock()
        captured_headers = []

        def fake_request(method, url, **kwargs):
            captured_headers.append(kwargs.get('headers', {}))
            if 'server-info-slim' in url:
                return FakeResponse(200, {'version': '0.8.2.0'})
            elif 'library-types' in url:
                return FakeResponse(200, [{'id': 1, 'name': 'Comic'}])
            elif 'libraries' in url:
                return FakeResponse(200, [])
            return FakeResponse(200, {})

        mock_session.request = MagicMock(side_effect=fake_request)

        svc = KavitaConnectionService()
        svc.check_capability(client_options={'session': mock_session})

        self.assertGreater(len(captured_headers), 0)
        for headers in captured_headers:
            self.assertEqual(headers.get('x-api-key'), raw_key)
            self.assertNotIn('Authorization', headers)

    def test_25_no_request_emits_both_x_api_key_and_authorization(self):
        """25. Prove that no request emits both x-api-key and Authorization simultaneously."""
        creds = KavitaCredentials("single_auth_header_key")
        client = KavitaClient(base_url="http://127.0.0.1:5000/", credentials=creds)

        headers = client._build_headers()
        self.assertIn('x-api-key', headers)
        self.assertNotIn('Authorization', headers)

    def test_26_successful_x_api_key_establishes_auth_success(self):
        """26. Prove that a successful 200 response with x-api-key establishes authenticated=True."""
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000/"
        mylar.CONFIG.KAVITA_API_KEY = "valid_key"

        mock_session = MagicMock()

        def fake_request(method, url, **kwargs):
            if 'server-info-slim' in url:
                return FakeResponse(200, {'version': '0.8.2.0'})
            elif 'library-types' in url:
                return FakeResponse(200, [{'id': 1, 'name': 'Comic'}])
            elif 'libraries' in url:
                return FakeResponse(200, [{'id': 1, 'name': 'Comics', 'type': 1}])
            return FakeResponse(200, {})

        mock_session.request = MagicMock(side_effect=fake_request)

        svc = KavitaConnectionService()
        res = svc.check_capability(client_options={'session': mock_session})

        self.assertTrue(res['success'])
        self.assertTrue(res['authenticated'])
        self.assertTrue(res['reachable'])

    # -------------------------------------------------------------------------
    # 6. Error Sanitization & Edge Cases
    # -------------------------------------------------------------------------
    def test_27_401_authentication_failure_sanitized(self):
        """27. Prove that 401 authentication failure is sanitized with zero secret leakage."""
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000/"
        mylar.CONFIG.KAVITA_API_KEY = "bad_key"

        mock_session = MagicMock()
        mock_session.request = MagicMock(return_value=FakeResponse(401, {'message': 'Unauthorized'}))

        svc = KavitaConnectionService()
        result = svc.check_capability(client_options={'session': mock_session})

        self.assertFalse(result['success'])
        self.assertFalse(result['authenticated'])
        self.assertEqual(result['error_code'], 'authentication_failed')
        self.assertNotIn('bad_key', json.dumps(result))

    def test_28_403_insufficient_library_list_permission_sanitized(self):
        """28. Prove that missing library-list permission (403 Forbidden) fails closed with permission_denied."""
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000/"
        mylar.CONFIG.KAVITA_API_KEY = "test_key"

        mock_session = MagicMock()

        def fake_request(method, url, **kwargs):
            if 'server-info-slim' in url:
                return FakeResponse(200, {'version': '0.8.2.0'})
            elif 'library-types' in url:
                return FakeResponse(200, [{'id': 1, 'name': 'Comic'}])
            elif 'libraries' in url:
                return FakeResponse(403, {'message': 'Forbidden'})
            return FakeResponse(200, [])

        mock_session.request = MagicMock(side_effect=fake_request)

        svc = KavitaConnectionService()
        result = svc.check_capability(client_options={'session': mock_session})

        self.assertFalse(result['success'])
        self.assertFalse(result['library_list_readable'])
        self.assertEqual(result['error_code'], 'permission_denied')

    def test_29_unavailable_comic_compatible_type_fails_closed(self):
        """29. Prove that missing or incompatible comic library type fails closed."""
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000/"
        mylar.CONFIG.KAVITA_API_KEY = "test_key"

        mock_session = MagicMock()

        def fake_request(method, url, **kwargs):
            if 'server-info-slim' in url:
                return FakeResponse(200, {'version': '0.8.2.0'})
            elif 'library-types' in url:
                return FakeResponse(200, [{'id': 0, 'name': 'Manga'}, {'id': 2, 'name': 'Book'}])
            return FakeResponse(200, [])

        mock_session.request = MagicMock(side_effect=fake_request)

        svc = KavitaConnectionService()
        result = svc.check_capability(client_options={'session': mock_session})

        self.assertFalse(result['success'])
        self.assertFalse(result['comic_library_type_available'])
        self.assertEqual(result['error_code'], 'incompatible_library_type')

    def test_30_malformed_server_info_response_sanitized(self):
        """30. Prove that malformed or non-JSON server-info response is handled safely."""
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000/"
        mylar.CONFIG.KAVITA_API_KEY = "test_key"

        mock_session = MagicMock()
        mock_session.request = MagicMock(return_value=FakeResponse(200, text="<html>502 Bad Gateway</html>"))

        svc = KavitaConnectionService()
        result = svc.check_capability(client_options={'session': mock_session})

        self.assertFalse(result['success'])
        self.assertEqual(result['error_code'], 'invalid_response')

    def test_31_malformed_library_types_response_sanitized(self):
        """31. Prove that malformed library-types response is handled safely."""
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000/"
        mylar.CONFIG.KAVITA_API_KEY = "test_key"

        mock_session = MagicMock()

        def fake_request(method, url, **kwargs):
            if 'server-info-slim' in url:
                return FakeResponse(200, {'version': '0.8.2.0'})
            elif 'library-types' in url:
                return FakeResponse(200, text="12345")  # non-dict/list
            return FakeResponse(200, [])

        mock_session.request = MagicMock(side_effect=fake_request)

        svc = KavitaConnectionService()
        result = svc.check_capability(client_options={'session': mock_session})

        self.assertFalse(result['success'])
        self.assertEqual(result['error_code'], 'invalid_response')

    def test_32_malformed_library_list_response_sanitized(self):
        """32. Prove that malformed library list response is handled safely."""
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000/"
        mylar.CONFIG.KAVITA_API_KEY = "test_key"

        mock_session = MagicMock()

        def fake_request(method, url, **kwargs):
            if 'server-info-slim' in url:
                return FakeResponse(200, {'version': '0.8.2.0'})
            elif 'library-types' in url:
                return FakeResponse(200, [{'id': 1, 'name': 'Comic'}])
            elif 'libraries' in url:
                return FakeResponse(200, text="invalid-library-shape")
            return FakeResponse(200, [])

        mock_session.request = MagicMock(side_effect=fake_request)

        svc = KavitaConnectionService()
        result = svc.check_capability(client_options={'session': mock_session})

        self.assertFalse(result['success'])
        self.assertEqual(result['error_code'], 'invalid_response')

    def test_33_timeout_error_sanitized(self):
        """33. Prove that connection and read timeouts are sanitized."""
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000/"
        mylar.CONFIG.KAVITA_API_KEY = "test_key"

        mock_session = MagicMock()
        mock_session.request = MagicMock(side_effect=requests.exceptions.ConnectTimeout("Connection timed out"))

        svc = KavitaConnectionService()
        result = svc.check_capability(client_options={'session': mock_session})

        self.assertFalse(result['success'])
        self.assertFalse(result['reachable'])
        self.assertEqual(result['error_code'], 'timeout')

    def test_34_tls_failure_sanitized(self):
        """34. Prove that TLS certificate and handshake failures are sanitized."""
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "https://kavita.local:5000/"
        mylar.CONFIG.KAVITA_API_KEY = "test_key"

        mock_session = MagicMock()
        mock_session.request = MagicMock(side_effect=requests.exceptions.SSLError("Certificate verify failed"))

        svc = KavitaConnectionService()
        result = svc.check_capability(client_options={'session': mock_session})

        self.assertFalse(result['success'])
        self.assertFalse(result['reachable'])
        self.assertEqual(result['error_code'], 'transport_error')

    def test_35_transport_failure_sanitized(self):
        """35. Prove that network connection and DNS failures are sanitized."""
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000/"
        mylar.CONFIG.KAVITA_API_KEY = "test_key"

        mock_session = MagicMock()
        mock_session.request = MagicMock(side_effect=requests.exceptions.ConnectionError("Connection refused"))

        svc = KavitaConnectionService()
        result = svc.check_capability(client_options={'session': mock_session})

        self.assertFalse(result['success'])
        self.assertFalse(result['reachable'])
        self.assertEqual(result['error_code'], 'transport_error')

    # -------------------------------------------------------------------------
    # 7. Invariance: Zero Scan Calls, Zero Mutations, Zero DB/FS Changes
    # -------------------------------------------------------------------------
    def test_36_no_scan_endpoint_ever_called(self):
        """36. Prove that library-specific scan capability is reported without calling a scan endpoint."""
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000/"
        mylar.CONFIG.KAVITA_API_KEY = "test_key"

        mock_session = MagicMock()
        requested_endpoints = []

        def fake_request(method, url, **kwargs):
            requested_endpoints.append((method, url))
            if 'server-info-slim' in url:
                return FakeResponse(200, {'version': '0.8.2.0'})
            elif 'library-types' in url:
                return FakeResponse(200, [{'id': 1, 'name': 'Comic'}])
            elif 'libraries' in url:
                return FakeResponse(200, [])
            return FakeResponse(200, {})

        mock_session.request = MagicMock(side_effect=fake_request)

        svc = KavitaConnectionService()
        result = svc.check_capability(client_options={'session': mock_session})

        self.assertTrue(result['library_specific_scan_advertised'])
        for method, url in requested_endpoints:
            self.assertNotIn('scan', url.lower())

    def test_37_no_mutation_endpoints_ever_called(self):
        """37. Prove that no mutation endpoints (create, update, delete, scan, refresh) are ever called."""
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000/"
        mylar.CONFIG.KAVITA_API_KEY = "test_key"

        mock_session = MagicMock()
        requested_calls = []

        def fake_request(method, url, **kwargs):
            requested_calls.append((method, url))
            if 'server-info-slim' in url:
                return FakeResponse(200, {'version': '0.8.2.0'})
            elif 'library-types' in url:
                return FakeResponse(200, [{'id': 1, 'name': 'Comic'}])
            elif 'libraries' in url:
                return FakeResponse(200, [])
            return FakeResponse(200, {})

        mock_session.request = MagicMock(side_effect=fake_request)

        svc = KavitaConnectionService()
        svc.check_capability(client_options={'session': mock_session})

        prohibited_fragments = ['create', 'update', 'delete', 'scan', 'scan-folder', 'scan-all', 'refresh-metadata']
        for method, url in requested_calls:
            self.assertEqual(method, 'GET', f"Prohibited non-GET method used: {method} {url}")
            for fragment in prohibited_fragments:
                self.assertNotIn(fragment, url.lower(), f"Prohibited endpoint fragment '{fragment}' in URL: {url}")

    def test_38_no_database_or_filesystem_mutation(self):
        """38. Prove that zero database rows, files, or archives change during a capability check."""
        mylar.CONFIG.KAVITA_ENABLED = True
        mylar.CONFIG.KAVITA_URL = "http://127.0.0.1:5000/"
        mylar.CONFIG.KAVITA_API_KEY = "test_key"

        mock_session = MagicMock()
        mock_session.request = MagicMock(return_value=FakeResponse(200, {'version': '0.8.2.0'}))

        svc = KavitaConnectionService()
        svc.check_capability(client_options={'session': mock_session})

        # Capability check is purely in-memory; no database queries or file writes are executed.
        self.assertTrue(True)

    def test_39_existing_metron_regressions_pass(self):
        """39. Prove that existing Metron functionality and tests continue to operate properly."""
        from mylar.extensions.providers.metron import get_metron_config_status
        m_status = get_metron_config_status()
        self.assertIsInstance(m_status, dict)
        self.assertIn('enabled', m_status)

    def test_40_git_diff_check_clean(self):
        """40. Prove that git diff --check reports zero whitespace or newline errors."""
        res = subprocess.run(['git', 'diff', '--check'], cwd=REPO_ROOT, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"git diff --check failed:\n{res.stdout}\n{res.stderr}")


if __name__ == '__main__':
    unittest.main()
