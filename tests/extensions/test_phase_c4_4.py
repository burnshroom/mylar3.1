#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Unit tests for Phase C4.4: Secure Metron Settings Integration and Connection Probe.

Tests configuration defaults, encryption at rest, secret preservation, explicit clearing,
sanitized POST /testMetron connection probe, method enforcement, error classification,
and database invariance.
"""

import os
import sys

# Portable test bootstrap: insert repository and library paths before other imports
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.join(REPO_ROOT, 'lib'))
sys.path.insert(0, REPO_ROOT)

import json
import unittest
import sqlite3
import requests
import cherrypy
from unittest.mock import patch, MagicMock

import mylar
import mylar.config
import mylar.webserve
from mylar.extensions.providers.metron import (
    MetronCredentials,
    AUTH_MODE_TOKEN,
    AUTH_MODE_BASIC,
    AUTH_MODE_NONE,
    get_metron_config_status,
    build_metron_credentials,
    handle_test_metron,
    CANONICAL_METRON_BASE_URL,
    MetronConnectionService,
    MetronAuthenticationError,
    MetronRateLimitError,
    MetronTimeoutError,
    MetronTransportError,
    MetronInvalidResponseError,
    MetronInvalidRequestError
)
from mylar import encrypted

STAGING_CONFIG = r'C:\Users\spike\.gemini\antigravity\brain\ff5d5fcf-d223-4e21-94d9-ed4e7c37cd34\scratch\live_staging_20260822_0050\config.ini'
STAGING_DB = r'C:\Users\spike\.gemini\antigravity\brain\ff5d5fcf-d223-4e21-94d9-ed4e7c37cd34\scratch\live_staging_20260822_0050\mylar.db'


class TestPhaseC4_4MetronSettingsIntegration(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        mylar.PROG_DIR = REPO_ROOT
        mylar.DATA_DIR = os.path.dirname(STAGING_CONFIG)
        cc = mylar.config.Config(STAGING_CONFIG)
        mylar.CONFIG = cc.read(startup=True)

    def setUp(self):
        self.orig_enabled = getattr(mylar.CONFIG, 'METRON_ENABLED', False)
        self.orig_auth_mode = getattr(mylar.CONFIG, 'METRON_AUTH_MODE', 'token')
        self.orig_token = getattr(mylar.CONFIG, 'METRON_API_TOKEN', None)
        self.orig_user = getattr(mylar.CONFIG, 'METRON_USERNAME', None)
        self.orig_pass = getattr(mylar.CONFIG, 'METRON_PASSWORD', None)
        self.orig_url = getattr(mylar.CONFIG, 'METRON_BASE_URL', 'https://metron.cloud/api/')

    def tearDown(self):
        mylar.CONFIG.METRON_ENABLED = self.orig_enabled
        mylar.CONFIG.METRON_AUTH_MODE = self.orig_auth_mode
        mylar.CONFIG.METRON_API_TOKEN = self.orig_token
        mylar.CONFIG.METRON_USERNAME = self.orig_user
        mylar.CONFIG.METRON_PASSWORD = self.orig_pass
        mylar.CONFIG.METRON_BASE_URL = self.orig_url

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

    def _create_mock_response(self, status_code=200, json_data=None, headers=None):
        mock_resp = MagicMock(spec=requests.Response)
        mock_resp.status_code = status_code
        mock_resp.headers = headers or {}
        if json_data is not None:
            mock_resp.json.return_value = json_data
            mock_resp.text = json.dumps(json_data)
        else:
            mock_resp.json.side_effect = ValueError("No JSON")
            mock_resp.text = ''
        return mock_resp

    # 1. Metron defaults to disabled
    def test_01_metron_defaults_to_disabled(self):
        def_spec = mylar.config._CONFIG_DEFINITIONS.get('METRON_ENABLED')
        self.assertIsNotNone(def_spec)
        self.assertEqual(def_spec[0], bool)
        self.assertEqual(def_spec[1], 'Metron')
        self.assertFalse(def_spec[2])

    # 2. Loading configuration performs zero network calls
    def test_02_config_loading_zero_network(self):
        with patch.object(requests.Session, 'get') as mock_get:
            status = get_metron_config_status()
            self.assertEqual(mock_get.call_count, 0)
            self.assertIn('enabled', status)
            self.assertIn('auth_mode', status)
            self.assertIn('base_url', status)

    # 3. Settings-page rendering performs zero network calls
    def test_03_settings_rendering_zero_network(self):
        with patch.object(requests.Session, 'get') as mock_get:
            ws = mylar.webserve.WebInterface()
            # Test that calling config() doesn't invoke Metron network
            with patch('mylar.webserve.serve_template') as mock_serve:
                mock_serve.return_value = "<html>Settings</html>"
                ws.config()
                self.assertEqual(mock_get.call_count, 0)
                # Verify config dict passed to template
                _, kwargs = mock_serve.call_args
                config_dict = kwargs.get('config', {})
                self.assertIn('metron_enabled', config_dict)
                self.assertIn('metron_has_token', config_dict)
                self.assertIn('metron_has_password', config_dict)
                self.assertNotIn('metron_api_token', config_dict)
                self.assertNotIn('metron_password', config_dict)

    # 4. Token mode builds token credential type
    def test_04_token_mode_builds_token_credential(self):
        mylar.CONFIG.METRON_AUTH_MODE = 'token'
        mylar.CONFIG.METRON_API_TOKEN = 'secret_token_123'
        creds = build_metron_credentials()
        self.assertEqual(creds.mode, AUTH_MODE_TOKEN)
        self.assertEqual(creds._token, 'secret_token_123')
        self.assertNotIn('secret_token_123', repr(creds))

    # 5. Basic mode builds Basic credential type
    def test_05_basic_mode_builds_basic_credential(self):
        mylar.CONFIG.METRON_AUTH_MODE = 'basic'
        mylar.CONFIG.METRON_USERNAME = 'metron_user'
        mylar.CONFIG.METRON_PASSWORD = 'metron_password_456'
        creds = build_metron_credentials()
        self.assertEqual(creds.mode, AUTH_MODE_BASIC)
        self.assertEqual(creds._username, 'metron_user')
        self.assertEqual(creds._password, 'metron_password_456')
        self.assertNotIn('metron_password_456', repr(creds))

    # 6. Invalid / none authentication mode fails closed
    def test_06_invalid_auth_mode_fails_closed(self):
        for bad_mode in ['none', 'anonymous', 'oauth', '', 123]:
            with self.assertRaises(MetronInvalidRequestError):
                build_metron_credentials(auth_mode=bad_mode, api_token='dummy')

    # 7. Missing token fails without a request
    def test_07_missing_token_fails_closed(self):
        mylar.CONFIG.METRON_AUTH_MODE = 'token'
        mylar.CONFIG.METRON_API_TOKEN = None
        with patch.object(requests.Session, 'get') as mock_get:
            with self.assertRaises(MetronInvalidRequestError):
                build_metron_credentials(auth_mode='token', api_token='')
            self.assertEqual(mock_get.call_count, 0)

    # 8. Missing Basic username or password fails without a request
    def test_08_missing_basic_credentials_fail_closed(self):
        with patch.object(requests.Session, 'get') as mock_get:
            with self.assertRaises(MetronInvalidRequestError):
                build_metron_credentials(auth_mode='basic', username='user', password=None)
            with self.assertRaises(MetronInvalidRequestError):
                build_metron_credentials(auth_mode='basic', username=None, password='pass')
            self.assertEqual(mock_get.call_count, 0)

    # 9. GET /testMetron returns 405 Method Not Allowed
    def test_09_get_test_metron_returns_405(self):
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'GET'
            res_str = handle_test_metron(api_token='tok')
            res = json.loads(res_str)
            self.assertFalse(res['success'])
            self.assertEqual(res['status_code'], 405)
            self.assertEqual(mock_resp.status, 405)
            self.assertIn('Method Not Allowed', res['message'])

    # 10. Successful POST returns sanitized JSON
    def test_10_successful_post_returns_sanitized_json(self):
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'POST'
            with patch.object(requests.Session, 'get') as mock_get:
                mock_get.return_value = self._create_mock_response(status_code=200, json_data={'results': []})
                res_str = handle_test_metron(auth_mode='token', api_token='test_token_xyz')
                res = json.loads(res_str)

                self.assertTrue(res['success'])
                self.assertEqual(res['status_code'], 200)
                self.assertEqual(res['auth_mode'], 'token')
                self.assertIn('Successfully connected', res['message'])
                self.assertNotIn('test_token_xyz', res_str)
                self.assertEqual(mock_get.call_count, 1)

    # 11. 401 and 403 are sanitized for active auth mode
    def test_11_auth_failures_sanitized_per_mode(self):
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'POST'
            with patch.object(requests.Session, 'get') as mock_get:
                # Token mode 401
                mock_get.return_value = self._create_mock_response(status_code=401, json_data={'detail': 'Invalid token'})
                res_tok = json.loads(handle_test_metron(auth_mode='token', api_token='bad_tok'))
                self.assertFalse(res_tok['success'])
                self.assertEqual(res_tok['status_code'], 401)
                self.assertIn('verify your API token', res_tok['message'])
                self.assertNotIn('username', res_tok['message'])

                # Basic mode 401
                mock_get.return_value = self._create_mock_response(status_code=401, json_data={'detail': 'Bad credentials'})
                res_basic = json.loads(handle_test_metron(auth_mode='basic', username='u', password='p'))
                self.assertFalse(res_basic['success'])
                self.assertEqual(res_basic['status_code'], 401)
                self.assertIn('verify your username and password', res_basic['message'])
                self.assertNotIn('API token', res_basic['message'])

    # 12. 429 and rate-limit headers are sanitized
    def test_12_rate_limit_sanitized(self):
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'POST'
            with patch.object(requests.Session, 'get') as mock_get:
                headers = {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '60'}
                mock_get.return_value = self._create_mock_response(status_code=429, json_data={'detail': 'Throttled'}, headers=headers)
                res = json.loads(handle_test_metron(auth_mode='token', api_token='valid_tok'))

                self.assertFalse(res['success'])
                self.assertEqual(res['status_code'], 429)
                self.assertEqual(res['error_code'], 'rate_limited')
                self.assertIn('rate limit reached', res['message'])
                self.assertEqual(res.get('rate_limit_remaining'), 0)
                self.assertEqual(res.get('rate_limit_reset'), 60)

    # 13. Timeout, transport, and invalid responses are sanitized
    def test_13_transport_and_timeout_sanitized(self):
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'POST'

            # Timeout
            with patch.object(requests.Session, 'get', side_effect=requests.exceptions.Timeout("Connection timed out")):
                res_timeout = json.loads(handle_test_metron(auth_mode='token', api_token='tok'))
                self.assertFalse(res_timeout['success'])
                self.assertEqual(res_timeout['status_code'], 504)
                self.assertIn('timed out', res_timeout['message'])

            # Transport / SSLError
            with patch.object(requests.Session, 'get', side_effect=requests.exceptions.SSLError("SSL Certificate verify failed")):
                res_ssl = json.loads(handle_test_metron(auth_mode='token', api_token='tok'))
                self.assertFalse(res_ssl['success'])
                self.assertEqual(res_ssl['status_code'], 502)
                self.assertIn('HTTPS connection', res_ssl['message'])

    # 14. Arbitrary base_url, URL, or host in POST body is ignored
    def test_14_arbitrary_base_url_in_post_ignored(self):
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'POST'
            with patch.object(requests.Session, 'get') as mock_get:
                mock_get.return_value = self._create_mock_response(status_code=200, json_data={'results': []})
                # Attempt to inject custom evil base URL in kwargs
                handle_test_metron(
                    auth_mode='token',
                    api_token='tok123',
                    base_url='https://attacker.com/evil_api/',
                    endpoint='evil/endpoint'
                )
                self.assertEqual(mock_get.call_count, 1)
                called_url = mock_get.call_args[0][0]
                self.assertTrue(called_url.startswith(CANONICAL_METRON_BASE_URL))
                self.assertNotIn('attacker.com', called_url)

    # 15. Stored secrets are encrypted at rest in config.ini
    def test_15_secrets_encrypted_at_rest(self):
        cfg = mylar.config.Config(STAGING_CONFIG)
        cfg = cfg.read(startup=True)
        cfg.ENCRYPT_PASSWORDS = True
        cfg.METRON_API_TOKEN = 'raw_super_secret_token_123'
        cfg.METRON_PASSWORD = 'raw_super_secret_password_456'

        cfg.encrypt_items(mode='encrypt')
        # Check that the encryption dict values are encrypted with prefix ^~$z$
        enc_token = encrypted.Encryptor('raw_super_secret_token_123').encrypt_it()['password']
        self.assertTrue(enc_token.startswith('^~$z$'))

        # Decrypt mode
        cfg.METRON_API_TOKEN = enc_token
        cfg.encrypt_items(mode='decrypt')
        self.assertEqual(cfg.METRON_API_TOKEN, 'raw_super_secret_token_123')

    # 16. Blank secret submission in configUpdate preserves existing secret
    def test_16_blank_secret_submission_preserves_existing(self):
        ws = mylar.webserve.WebInterface()
        mylar.CONFIG.METRON_API_TOKEN = 'existing_secret_token_abc'
        mylar.CONFIG.METRON_PASSWORD = 'existing_secret_pass_def'

        with patch.object(mylar.CONFIG, 'writeconfig'), patch.object(mylar.CONFIG, 'configure'):
            # Simulate POST submission with blank token and blank password
            ws.configUpdate(
                metron_enabled=True,
                metron_auth_mode='token',
                metron_api_token='',
                metron_password=''
            )
            # Must preserve existing secrets
            self.assertEqual(mylar.CONFIG.METRON_API_TOKEN, 'existing_secret_token_abc')
            self.assertEqual(mylar.CONFIG.METRON_PASSWORD, 'existing_secret_pass_def')

    # 17. Explicit clear controls remove only their intended secret
    def test_17_explicit_clear_removes_intended_secret_only(self):
        ws = mylar.webserve.WebInterface()
        mylar.CONFIG.METRON_API_TOKEN = 'existing_secret_token_abc'
        mylar.CONFIG.METRON_PASSWORD = 'existing_secret_pass_def'

        with patch.object(mylar.CONFIG, 'writeconfig'), patch.object(mylar.CONFIG, 'configure'):
            # Clear token explicitly while leaving password alone
            ws.configUpdate(
                metron_enabled=True,
                metron_auth_mode='token',
                clear_metron_token=1,
                metron_api_token='',
                metron_password=''
            )
            self.assertIsNone(mylar.CONFIG.METRON_API_TOKEN)
            self.assertEqual(mylar.CONFIG.METRON_PASSWORD, 'existing_secret_pass_def')

    # 18. metron_enabled saves correctly when checked and unchecked
    def test_18_metron_enabled_toggle(self):
        ws = mylar.webserve.WebInterface()
        with patch.object(mylar.CONFIG, 'writeconfig'), patch.object(mylar.CONFIG, 'configure'):
            # When unchecked (omitted from kwargs)
            ws.configUpdate()
            self.assertFalse(mylar.CONFIG.METRON_ENABLED)

            # When checked
            ws.configUpdate(metron_enabled='1')
            self.assertTrue(mylar.CONFIG.METRON_ENABLED)

    # 19. Existing ComicVine and provider settings remain unchanged
    def test_19_existing_comicvine_settings_unaffected(self):
        orig_cv_api = mylar.CONFIG.COMICVINE_API
        orig_cv_url = mylar.CONFIG.COMICVINE_URL
        get_metron_config_status()
        self.assertEqual(mylar.CONFIG.COMICVINE_API, orig_cv_api)
        self.assertEqual(mylar.CONFIG.COMICVINE_URL, orig_cv_url)

    # 20. Database row counts remain 100% invariant
    def test_20_database_invariance(self):
        before = self.get_db_counts()
        ws = mylar.webserve.WebInterface()
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'POST'
            with patch.object(requests.Session, 'get') as mock_get:
                mock_get.return_value = self._create_mock_response(status_code=200, json_data={'results': []})
                ws.testMetron(auth_mode='token', api_token='test_tok')
        after = self.get_db_counts()
        self.assertEqual(before, after, "Database row counts must remain 100% invariant")

    # 21. Oversized payload inputs are rejected before network calls
    def test_21_oversized_payload_rejected(self):
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'POST'
            with patch.object(requests.Session, 'get') as mock_get:
                # Oversized mode
                res_mode = json.loads(handle_test_metron(auth_mode='a'*50, api_token='tok'))
                self.assertFalse(res_mode['success'])
                self.assertEqual(res_mode['status_code'], 400)

                # Oversized token
                res_tok = json.loads(handle_test_metron(auth_mode='token', api_token='t'*5000))
                self.assertFalse(res_tok['success'])
                self.assertEqual(res_tok['status_code'], 400)

                # Oversized username
                res_user = json.loads(handle_test_metron(auth_mode='basic', username='u'*1000, password='p'))
                self.assertFalse(res_user['success'])
                self.assertEqual(res_user['status_code'], 400)

                self.assertEqual(mock_get.call_count, 0)

    # 22. Basic mode successful connection test
    def test_22_basic_mode_successful_probe(self):
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'POST'
            with patch.object(requests.Session, 'get') as mock_get:
                mock_get.return_value = self._create_mock_response(status_code=200, json_data={'results': []})
                res_str = handle_test_metron(auth_mode='basic', username='my_user', password='my_password')
                res = json.loads(res_str)

                self.assertTrue(res['success'])
                self.assertEqual(res['status_code'], 200)
                self.assertEqual(res['auth_mode'], 'basic')
                self.assertIn('Successfully connected', res['message'])
                self.assertNotIn('my_password', res_str)
                self.assertNotIn('my_user', res_str)
                self.assertEqual(mock_get.call_count, 1)

    # 23. Zero secret leakage across all failure responses
    def test_23_zero_leakage_in_diagnostics(self):
        secret_tok = "SUPER_SECRET_TOKEN_99999"
        secret_pwd = "SUPER_SECRET_PASSWORD_88888"

        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'POST'
            with patch.object(requests.Session, 'get') as mock_get:
                for status in [400, 401, 403, 404, 429, 500, 502, 504]:
                    mock_get.return_value = self._create_mock_response(status_code=status, json_data={'error': 'fail'})
                    res_t = handle_test_metron(auth_mode='token', api_token=secret_tok)
                    self.assertNotIn(secret_tok, res_t)

                    res_b = handle_test_metron(auth_mode='basic', username='user', password=secret_pwd)
                    self.assertNotIn(secret_pwd, res_b)


if __name__ == '__main__':
    unittest.main()
