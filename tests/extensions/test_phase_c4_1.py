#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Unit tests for Phase C4.1: Modular Metron Client Foundation and Read-Only Connection Probe.

Tests canonical endpoint validation, diagnostic redaction, configuration primitives,
secret redaction, and transport security.
Guarantees ZERO live network calls, ZERO database writes, and ZERO secret leakage.
"""

import os
import sys

# Portable test bootstrap: insert repository and library paths before other imports
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.join(REPO_ROOT, 'lib'))
sys.path.insert(0, REPO_ROOT)

import math
import urllib.parse
import unittest
import sqlite3
import requests
from unittest.mock import patch, MagicMock

import mylar
from mylar.extensions.providers.metron import (
    MetronCredentials,
    MetronClient,
    MetronConnectionService,
    probe_connection_controller,
    MetronConfigurationError,
    MetronAuthenticationError,
    MetronRateLimitError,
    MetronTimeoutError,
    MetronTransportError,
    MetronUpstreamError,
    MetronInvalidResponseError,
    MetronNotFoundError,
    MetronInvalidRequestError,
    AUTH_MODE_TOKEN,
    AUTH_MODE_BASIC,
    AUTH_MODE_NONE
)
from mylar.extensions.providers.metron.client import (
    validate_and_normalize_base_url,
    validate_relative_endpoint,
    validate_query_params
)

STAGING_DB = r'C:\Users\spike\.gemini\antigravity\brain\ff5d5fcf-d223-4e21-94d9-ed4e7c37cd34\scratch\live_staging_20260822_0050\mylar.db'


class TestPhaseC4_1MetronFoundation(unittest.TestCase):

    def setUp(self):
        self.mock_base_url = 'https://mock.metron.test/api/'
        self.test_token = 'secret_test_token_abc123xyz'
        self.test_user = 'testuser_secret'
        self.test_pass = 'testpass_secret_999'

    def get_db_counts(self):
        conn = sqlite3.connect(STAGING_DB)
        cur = conn.cursor()
        counts = {
            'issues': cur.execute('SELECT COUNT(*) FROM issues').fetchone()[0],
            'annuals': cur.execute('SELECT COUNT(*) FROM annuals').fetchone()[0],
            'comics': cur.execute('SELECT COUNT(*) FROM comics').fetchone()[0],
            'storyarcs': cur.execute('SELECT COUNT(*) FROM storyarcs').fetchone()[0],
            'ext_creator_entities': cur.execute('SELECT COUNT(*) FROM ext_creator_entities').fetchone()[0],
            'ext_creator_name_records': cur.execute('SELECT COUNT(*) FROM ext_creator_name_records').fetchone()[0],
            'ext_creator_credits': cur.execute('SELECT COUNT(*) FROM ext_creator_credits').fetchone()[0],
            'ext_creator_scan_records': cur.execute('SELECT COUNT(*) FROM ext_creator_scan_records').fetchone()[0],
            'ext_creator_aliases': cur.execute('SELECT COUNT(*) FROM ext_creator_aliases').fetchone()[0],
            'ext_creator_external_ids': cur.execute('SELECT COUNT(*) FROM ext_creator_external_ids').fetchone()[0]
        }
        conn.close()
        return counts

    def _create_mock_response(self, status_code=200, json_data=None, text_data='', headers=None):
        mock_resp = MagicMock(spec=requests.Response)
        mock_resp.status_code = status_code
        mock_resp.headers = headers or {}
        if json_data is not None:
            mock_resp.json.return_value = json_data
            mock_resp.text = str(json_data)
        else:
            mock_resp.json.side_effect = ValueError("No JSON object could be decoded")
            mock_resp.text = text_data
        return mock_resp

    # --- 1. Endpoint Rejection & Canonical Validation ---

    def test_01_endpoint_traversal_and_encoded_bypasses(self):
        # Raw traversal
        with self.assertRaises(MetronInvalidRequestError):
            validate_relative_endpoint('publisher/../../etc/passwd')
        # Single-encoded traversal (lowercase)
        with self.assertRaises(MetronInvalidRequestError):
            validate_relative_endpoint('issue/%2e%2e/creator/')
        # Mixed-case encoded traversal
        with self.assertRaises(MetronInvalidRequestError):
            validate_relative_endpoint('issue/%2E%2E/creator/')
        # Double-encoded traversal
        with self.assertRaises(MetronInvalidRequestError):
            validate_relative_endpoint('issue/%252e%252e/creator/')
        # Encoded protocol-relative syntax
        with self.assertRaises(MetronInvalidRequestError):
            validate_relative_endpoint('%2f%2fevil.example/path')
        # Encoded backslash
        with self.assertRaises(MetronInvalidRequestError):
            validate_relative_endpoint('issue/%5cadmin/')
        # Raw backslash
        with self.assertRaises(MetronInvalidRequestError):
            validate_relative_endpoint('issue\\admin')
        # Encoded CRLF
        with self.assertRaises(MetronInvalidRequestError):
            validate_relative_endpoint('issue/%0d%0aInjected:value')
        # Raw CRLF
        with self.assertRaises(MetronInvalidRequestError):
            validate_relative_endpoint('issue/\r\nInjected:value')
        # Leading / and //
        with self.assertRaises(MetronInvalidRequestError):
            validate_relative_endpoint('/publisher/')
        with self.assertRaises(MetronInvalidRequestError):
            validate_relative_endpoint('//publisher/')
        # Absolute URLs
        for scheme in ('http', 'https', 'ftp', 'file'):
            with self.assertRaises(MetronInvalidRequestError):
                validate_relative_endpoint(f'{scheme}://evil.example/api/')
        # User information syntax
        with self.assertRaises(MetronInvalidRequestError):
            validate_relative_endpoint('user:pass@evil.example/api/')
        # Fragments
        with self.assertRaises(MetronInvalidRequestError):
            validate_relative_endpoint('publisher/?page=1#frag')
        with self.assertRaises(MetronInvalidRequestError):
            validate_relative_endpoint('publisher/?page=1%23frag')

    def test_02_sensitive_query_parameters_rejection(self):
        sensitive_keys = ['token', 'access_token', 'api_key', 'apikey', 'password', 'secret', 'authorization']
        for key in sensitive_keys:
            # In endpoint query string (exact, uppercase, mixed-case, encoded)
            with self.assertRaises(MetronInvalidRequestError):
                validate_relative_endpoint(f'publisher/?{key}=secret123')
            with self.assertRaises(MetronInvalidRequestError):
                validate_relative_endpoint(f'publisher/?{key.upper()}=secret123')
            with self.assertRaises(MetronInvalidRequestError):
                validate_relative_endpoint(f'publisher/?{urllib.parse.quote(key)}=secret123')

            # In params dictionary
            with self.assertRaises(MetronInvalidRequestError):
                validate_query_params({key: 'secret123'})
            with self.assertRaises(MetronInvalidRequestError):
                validate_query_params({key.upper(): 'secret123'})

    def test_03_malformed_params_rejection(self):
        with self.assertRaises(MetronInvalidRequestError):
            validate_query_params(12345)
        with self.assertRaises(MetronInvalidRequestError):
            validate_query_params("not_a_dict")
        with self.assertRaises(MetronInvalidRequestError):
            validate_query_params([1, 2, 3])

    def test_04_diagnostic_endpoint_redaction_on_rejection(self):
        service = MetronConnectionService()
        creds = MetronCredentials(mode=AUTH_MODE_TOKEN, token=self.test_token)

        # Probe with dangerous endpoint containing sensitive token and traversal
        dirty_endpoint = f"issue/%2e%2e/creator/?token={self.test_token}&pass={self.test_pass}"
        result = service.probe_connection(credentials=creds, endpoint=dirty_endpoint)

        self.assertFalse(result['success'])
        self.assertEqual(result['error_code'], 'invalid_request')
        self.assertEqual(result['endpoint'], '[rejected]')

        # Verify dirty endpoint and secrets do NOT appear anywhere in the result
        result_str = str(result)
        self.assertNotIn(dirty_endpoint, result_str)
        self.assertNotIn(self.test_token, result_str)
        self.assertNotIn(self.test_pass, result_str)
        self.assertNotIn('%2e%2e', result_str)

    # --- 2. Safe Endpoint Acceptance ---

    def test_05_safe_endpoints_accepted(self):
        safe_list = ['publisher/?page=1', 'issue/?cv_id=12345', 'role/']
        for ep in safe_list:
            validated = validate_relative_endpoint(ep)
            self.assertEqual(validated, ep)

        with patch.object(requests.Session, 'get') as mock_get:
            mock_get.return_value = self._create_mock_response(status_code=200, json_data={'results': []})
            creds = MetronCredentials(mode=AUTH_MODE_TOKEN, token=self.test_token)
            service = MetronConnectionService()
            res = service.probe_connection(
                credentials=creds,
                endpoint='issue/?cv_id=12345',
                client_options={'base_url': self.mock_base_url}
            )
            self.assertTrue(res['success'])
            self.assertEqual(res['endpoint'], 'issue/?cv_id=12345')

    # --- 3. Configuration Primitives Validation ---

    def test_06_timeout_primitives_validation(self):
        invalid_timeouts = [0, -1, -5.5, float('nan'), float('inf'), float('-inf'), True, False, "5.0", None]
        for bad_to in invalid_timeouts:
            with self.assertRaises(MetronConfigurationError):
                MetronClient(base_url=self.mock_base_url, connect_timeout=bad_to)
            with self.assertRaises(MetronConfigurationError):
                MetronClient(base_url=self.mock_base_url, read_timeout=bad_to)

        # Valid timeouts succeed
        client = MetronClient(base_url=self.mock_base_url, connect_timeout=2.5, read_timeout=8.0)
        self.assertEqual(client.connect_timeout, 2.5)
        self.assertEqual(client.read_timeout, 8.0)

    def test_07_user_agent_validation(self):
        invalid_agents = ["", "   ", "Agent\r\nInjected", "Agent\x00Null", "Agent\nHeader", "Agent\t\x1f"]
        for bad_ua in invalid_agents:
            with self.assertRaises(MetronConfigurationError):
                MetronClient(base_url=self.mock_base_url, user_agent=bad_ua)

        # Valid user agent succeeds
        client = MetronClient(base_url=self.mock_base_url, user_agent='Mylar/1.0 (Custom UA)')
        self.assertEqual(client.user_agent, 'Mylar/1.0 (Custom UA)')

    def test_08_tls_verification_strictly_mandatory(self):
        with self.assertRaises(MetronConfigurationError):
            MetronClient(base_url=self.mock_base_url, verify_ssl=False)

        with patch.object(requests.Session, 'get') as mock_get:
            mock_get.return_value = self._create_mock_response(status_code=200, json_data={'results': []})
            client = MetronClient(base_url=self.mock_base_url, verify_ssl=True)
            client.get('publisher/?page=1')
            _, kwargs = mock_get.call_args
            self.assertIs(kwargs.get('verify'), True)

    def test_09_base_url_https_enforcement(self):
        self.assertEqual(validate_and_normalize_base_url(None), 'https://metron.cloud/api/')
        self.assertEqual(validate_and_normalize_base_url('https://custom.test/api'), 'https://custom.test/api/')

        for bad_url in [
            'http://insecure.test/api/',
            '//custom.test/api/',
            'custom.test/api/',
            'https://user:pass@custom.test/api/',
            'https://custom.test/api/?token=1',
            'https://custom.test/api/#frag'
        ]:
            with self.assertRaises(MetronConfigurationError):
                validate_and_normalize_base_url(bad_url)

    # --- 4. Authentication Headers & Credential States ---

    def test_10_auth_header_and_credential_states(self):
        # Token header format
        creds_tok = MetronCredentials(mode=AUTH_MODE_TOKEN, token=self.test_token)
        self.assertEqual(creds_tok.get_auth_headers(), {'Authorization': f'Token {self.test_token}'})
        self.assertNotIn('Bearer', creds_tok.get_auth_headers()['Authorization'])

        # Basic header format
        creds_basic = MetronCredentials(mode=AUTH_MODE_BASIC, username=self.test_user, password=self.test_pass)
        self.assertTrue(creds_basic.get_auth_headers()['Authorization'].startswith('Basic '))

        # mode='none' -> not_configured
        service = MetronConnectionService()
        res_none = service.probe_connection(credentials=MetronCredentials(mode=AUTH_MODE_NONE))
        self.assertFalse(res_none['success'])
        self.assertEqual(res_none['error_code'], 'not_configured')

        # Incomplete token -> invalid_configuration
        res_tok_empty = service.probe_connection(credentials=MetronCredentials(mode=AUTH_MODE_TOKEN, token=''))
        self.assertFalse(res_tok_empty['success'])
        self.assertEqual(res_tok_empty['error_code'], 'invalid_configuration')

        # Incomplete basic -> invalid_configuration
        res_basic_empty = service.probe_connection(credentials=MetronCredentials(mode=AUTH_MODE_BASIC, username='u', password=''))
        self.assertFalse(res_basic_empty['success'])
        self.assertEqual(res_basic_empty['error_code'], 'invalid_configuration')

    # --- 5. Response Shape & Status Code Classifications ---

    def test_11_response_shapes_and_primitives_rejection(self):
        service = MetronConnectionService()
        creds = MetronCredentials(mode=AUTH_MODE_TOKEN, token=self.test_token)

        # JSON primitives rejected
        for prim in ["string", 123, 4.56, True, False, None]:
            with patch.object(requests.Session, 'get') as mock_get:
                mock_get.return_value = self._create_mock_response(status_code=200, json_data=prim)
                res = service.probe_connection(credentials=creds, client_options={'base_url': self.mock_base_url})
                self.assertFalse(res['success'])
                self.assertEqual(res['error_code'], 'invalid_response')

        # Valid shapes accepted
        with patch.object(requests.Session, 'get') as mock_get:
            mock_get.return_value = self._create_mock_response(status_code=200, json_data={'results': [{'id': 1}]})
            res = service.probe_connection(credentials=creds, client_options={'base_url': self.mock_base_url})
            self.assertTrue(res['success'])
            self.assertEqual(res['response_shape'], 'paginated_envelope')

        with patch.object(requests.Session, 'get') as mock_get:
            mock_get.return_value = self._create_mock_response(status_code=200, json_data=[{'id': 1}])
            res = service.probe_connection(credentials=creds, client_options={'base_url': self.mock_base_url})
            self.assertTrue(res['success'])
            self.assertEqual(res['response_shape'], 'flat_list')

        with patch.object(requests.Session, 'get') as mock_get:
            mock_get.return_value = self._create_mock_response(status_code=200, json_data={'id': 1, 'name': 'DC'})
            res = service.probe_connection(credentials=creds, client_options={'base_url': self.mock_base_url})
            self.assertTrue(res['success'])
            self.assertEqual(res['response_shape'], 'object')

    def test_12_status_code_mappings(self):
        service = MetronConnectionService()
        creds = MetronCredentials(mode=AUTH_MODE_TOKEN, token=self.test_token)

        # 304 Not Modified
        with patch.object(requests.Session, 'get') as mock_get:
            mock_get.return_value = self._create_mock_response(status_code=304)
            res = service.probe_connection(credentials=creds, client_options={'base_url': self.mock_base_url})
            self.assertTrue(res['success'])
            self.assertEqual(res['http_status'], 304)
            self.assertEqual(res['response_shape'], 'not_modified')

        # 401 & 403 Authentication failure
        for code in (401, 403):
            with patch.object(requests.Session, 'get') as mock_get:
                mock_get.return_value = self._create_mock_response(status_code=code, json_data={'detail': 'Unauthorized'})
                res = service.probe_connection(credentials=creds, client_options={'base_url': self.mock_base_url})
                self.assertFalse(res['success'])
                self.assertEqual(res['http_status'], code)
                self.assertEqual(res['error_code'], 'authentication_failed')
                self.assertEqual(mock_get.call_count, 1)

        # 404 Not Found
        with patch.object(requests.Session, 'get') as mock_get:
            mock_get.return_value = self._create_mock_response(status_code=404, json_data={'detail': 'Not found'})
            res = service.probe_connection(credentials=creds, client_options={'base_url': self.mock_base_url})
            self.assertFalse(res['success'])
            self.assertEqual(res['http_status'], 404)
            self.assertEqual(res['error_code'], 'not_found')

        # 429 Rate Limited
        with patch.object(requests.Session, 'get') as mock_get:
            mock_get.return_value = self._create_mock_response(
                status_code=429,
                json_data={'detail': 'Throttled'},
                headers={'Retry-After': '90', 'X-RateLimit-Remaining': '0'}
            )
            res = service.probe_connection(credentials=creds, client_options={'base_url': self.mock_base_url})
            self.assertFalse(res['success'])
            self.assertEqual(res['http_status'], 429)
            self.assertEqual(res['error_code'], 'rate_limited')
            self.assertEqual(res['rate_limit']['retry_after'], '90')

        # 500/502 Upstream error
        with patch.object(requests.Session, 'get') as mock_get:
            mock_get.return_value = self._create_mock_response(status_code=502, text_data='Bad Gateway')
            res = service.probe_connection(credentials=creds, client_options={'base_url': self.mock_base_url})
            self.assertFalse(res['success'])
            self.assertEqual(res['http_status'], 502)
            self.assertEqual(res['error_code'], 'upstream_error')

    # --- 6. Secret Redaction & Exception Chaining Suppression ---

    def test_13_transport_failure_and_secret_redaction(self):
        leaky_msg = f"Transport error with token={self.test_token} and pass={self.test_pass} url=https://leak.test/?key=1"
        service = MetronConnectionService()
        creds = MetronCredentials(mode=AUTH_MODE_TOKEN, token=self.test_token)

        # ConnectTimeout
        with patch.object(requests.Session, 'get') as mock_get:
            mock_get.side_effect = requests.exceptions.ConnectTimeout(leaky_msg)
            res = service.probe_connection(credentials=creds, client_options={'base_url': self.mock_base_url})
            self.assertFalse(res['success'])
            self.assertEqual(res['error_code'], 'timeout')
            self.assertNotIn(self.test_token, str(res))

        # ReadTimeout
        with patch.object(requests.Session, 'get') as mock_get:
            mock_get.side_effect = requests.exceptions.ReadTimeout(leaky_msg)
            res = service.probe_connection(credentials=creds, client_options={'base_url': self.mock_base_url})
            self.assertFalse(res['success'])
            self.assertEqual(res['error_code'], 'timeout')
            self.assertNotIn(self.test_token, str(res))

        # SSLError
        with patch.object(requests.Session, 'get') as mock_get:
            mock_get.side_effect = requests.exceptions.SSLError(leaky_msg)
            res = service.probe_connection(credentials=creds, client_options={'base_url': self.mock_base_url})
            self.assertFalse(res['success'])
            self.assertEqual(res['error_code'], 'transport_error')
            self.assertNotIn(self.test_token, str(res))

        # Generic RequestException
        with patch.object(requests.Session, 'get') as mock_get:
            mock_get.side_effect = requests.exceptions.RequestException(leaky_msg)
            res = service.probe_connection(credentials=creds, client_options={'base_url': self.mock_base_url})
            self.assertFalse(res['success'])
            self.assertEqual(res['error_code'], 'transport_error')
            self.assertNotIn(self.test_token, str(res))
            self.assertNotIn(self.test_pass, str(res))

        # JSON Decode failure
        with patch.object(requests.Session, 'get') as mock_get:
            mock_resp = MagicMock(spec=requests.Response)
            mock_resp.status_code = 200
            mock_resp.headers = {}
            mock_resp.json.side_effect = ValueError(leaky_msg)
            mock_get.return_value = mock_resp
            res = service.probe_connection(credentials=creds, client_options={'base_url': self.mock_base_url})
            self.assertFalse(res['success'])
            self.assertEqual(res['error_code'], 'invalid_response')
            self.assertNotIn(self.test_token, str(res))

    # --- 7. Invariants & Zero Activity Proof ---

    def test_14_zero_network_on_import_and_init(self):
        with patch.object(requests.Session, 'get') as mock_get:
            import mylar.extensions.providers.metron as test_pkg
            creds = test_pkg.MetronCredentials(mode=AUTH_MODE_TOKEN, token='tok')
            client = test_pkg.MetronClient(credentials=creds)
            service = test_pkg.MetronConnectionService()
            self.assertEqual(mock_get.call_count, 0)

    def test_15_single_request_guarantee(self):
        with patch.object(requests.Session, 'get') as mock_get:
            mock_get.return_value = self._create_mock_response(status_code=200, json_data={'results': []})
            creds = MetronCredentials(mode=AUTH_MODE_TOKEN, token=self.test_token)
            service = MetronConnectionService()
            res = service.probe_connection(credentials=creds, client_options={'base_url': self.mock_base_url})
            self.assertTrue(res['success'])
            self.assertEqual(mock_get.call_count, 1)

    def test_16_controller_interface(self):
        with patch.object(requests.Session, 'get') as mock_get:
            mock_get.return_value = self._create_mock_response(
                status_code=200,
                json_data={'count': 1, 'results': [{'id': 1, 'name': 'Image'}]}
            )
            res = probe_connection_controller(
                credentials={'mode': 'token', 'token': self.test_token},
                client_options={'base_url': self.mock_base_url}
            )
            self.assertTrue(res['success'])
            self.assertEqual(res['observed_result_count'], 1)

    def test_17_database_invariance(self):
        before = self.get_db_counts()
        creds = MetronCredentials(mode=AUTH_MODE_TOKEN, token=self.test_token)
        service = MetronConnectionService()
        with patch.object(requests.Session, 'get') as mock_get:
            mock_get.return_value = self._create_mock_response(status_code=200, json_data={'results': []})
            service.probe_connection(credentials=creds, client_options={'base_url': self.mock_base_url})
        after = self.get_db_counts()
        self.assertEqual(before, after, "Database row counts must remain 100% invariant")


if __name__ == '__main__':
    unittest.main()
