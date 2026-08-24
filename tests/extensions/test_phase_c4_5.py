#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Unit tests for Phase C4.5: Read-Only Metron Issue Comparison Runtime.

Tests in-memory snapshot cache, TTL expiration, LRU eviction, local issue scope verification,
zero crossover between regular issues and annuals, provider configuration gates,
concordance comparison result structures, HTTP method enforcement, and database invariance.
"""

import os
import sys

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
    CANONICAL_METRON_BASE_URL,
    MetronIssueCache,
    get_metron_cache,
    MetronComparisonService,
    MetronLocalIssueNotFoundError,
    MetronProviderDisabledError,
    validate_annual_scope,
    handle_test_metron,
    handle_metron_compare_credits,
    CREDIT_COMPARISON_DISCLAIMER,
    MetronClient,
    MetronIssueService,
    validate_comicvine_issue_id
)

STAGING_CONFIG = r'C:\Users\spike\.gemini\antigravity\brain\ff5d5fcf-d223-4e21-94d9-ed4e7c37cd34\scratch\live_staging_20260822_0050\config.ini'
STAGING_DB = r'C:\Users\spike\.gemini\antigravity\brain\ff5d5fcf-d223-4e21-94d9-ed4e7c37cd34\scratch\live_staging_20260822_0050\mylar.db'


class TestPhaseC4_5MetronComparisonRuntime(unittest.TestCase):

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

        # Default test setup: enable Metron with token
        mylar.CONFIG.METRON_ENABLED = True
        mylar.CONFIG.METRON_AUTH_MODE = 'token'
        mylar.CONFIG.METRON_API_TOKEN = 'test_token_12345'

        # Fresh isolated cache for tests
        self.test_cache = MetronIssueCache(default_ttl=3600, max_entries=500)
        get_metron_cache().clear()

    def tearDown(self):
        mylar.CONFIG.METRON_ENABLED = self.orig_enabled
        mylar.CONFIG.METRON_AUTH_MODE = self.orig_auth_mode
        mylar.CONFIG.METRON_API_TOKEN = self.orig_token
        mylar.CONFIG.METRON_USERNAME = self.orig_user
        mylar.CONFIG.METRON_PASSWORD = self.orig_pass
        get_metron_cache().clear()

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

    def _sample_metron_issue(self, cv_id=105543):
        return {
            'id': 1234,
            'cv_id': cv_id,
            'issue_name': 'X-Men #45',
            'number': '45',
            'cover_date': '1995-06-01',
            'store_date': '1995-05-15',
            'credits': [
                {'creator': 'Fabian Nicieza', 'role': [{'name': 'Writer'}]},
                {'creator': 'Andy Kubert', 'role': [{'name': 'Penciller'}]},
                {'creator': 'Andy Kubert', 'role': [{'name': 'Cover'}]},
                {'creator': 'Matt Ryan', 'role': [{'name': 'Inker'}]},
                {'creator': 'Kevin Somers', 'role': [{'name': 'Colorist'}]},
                {'creator': 'Comicraft', 'role': [{'name': 'Letterer'}]},
                {'creator': 'Bob Harras', 'role': [{'name': 'Editor'}]},
                {'creator': 'Metron Exclusive Writer', 'role': [{'name': 'Writer'}]}
            ],
            'publisher': {'name': 'Marvel'},
            'series': {'name': 'X-Men', 'volume': 2, 'year_began': 1991}
        }

    # 1. Importing or constructing the service performs zero network requests
    def test_01_import_and_init_zero_network(self):
        with patch.object(requests.Session, 'get') as mock_get:
            cache = MetronIssueCache()
            service = MetronComparisonService(cache=cache)
            self.assertEqual(mock_get.call_count, 0)
            self.assertEqual(cache.size, 0)

    # 2. Disabled Metron returns a sanitized response and performs zero requests
    def test_02_disabled_metron_returns_sanitized_400_zero_network(self):
        mylar.CONFIG.METRON_ENABLED = False
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'GET'
            with patch.object(requests.Session, 'get') as mock_get:
                res_str = handle_metron_compare_credits(issueid='105543', annual=0)
                res = json.loads(res_str)

                self.assertFalse(res['success'])
                self.assertEqual(res['status_code'], 400)
                self.assertEqual(res['error_code'], 'provider_disabled')
                self.assertIn('disabled in Settings', res['message'])
                self.assertEqual(mock_get.call_count, 0)

    # 3. Missing credentials return a sanitized response and perform zero requests
    def test_03_missing_credentials_returns_sanitized_400_zero_network(self):
        mylar.CONFIG.METRON_API_TOKEN = None
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'GET'
            with patch.object(requests.Session, 'get') as mock_get:
                res_str = handle_metron_compare_credits(issueid='105543', annual=0)
                res = json.loads(res_str)

                self.assertFalse(res['success'])
                self.assertEqual(res['status_code'], 400)
                self.assertEqual(res['error_code'], 'missing_credentials')
                self.assertEqual(mock_get.call_count, 0)

    # 4. Invalid issueid values return HTTP 400 and perform zero requests
    def test_04_invalid_issueid_returns_400_zero_network(self):
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'GET'
            with patch.object(requests.Session, 'get') as mock_get:
                for bad_id in [-5, 0, '0', '-105543', '105543 ', ' 105543', '105543.5', 'abc', True, False, None]:
                    res_str = handle_metron_compare_credits(issueid=bad_id, annual=0)
                    res = json.loads(res_str)
                    self.assertFalse(res['success'], f"Failed on bad_id: {bad_id}")
                    self.assertEqual(res['status_code'], 400)
                    self.assertEqual(res['error_code'], 'invalid_request')
                self.assertEqual(mock_get.call_count, 0)

    # 5. Invalid Annual scope returns HTTP 400 and performs zero requests
    def test_05_invalid_annual_scope_returns_400_zero_network(self):
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'GET'
            with patch.object(requests.Session, 'get') as mock_get:
                for bad_annual in [2, -1, '2', 'annual', 'true', 'false', True, False, None, ' 0 ']:
                    res_str = handle_metron_compare_credits(issueid='105543', annual=bad_annual)
                    res = json.loads(res_str)
                    self.assertFalse(res['success'], f"Failed on bad_annual: {bad_annual}")
                    self.assertEqual(res['status_code'], 400)
                    self.assertEqual(res['error_code'], 'invalid_request')
                self.assertEqual(mock_get.call_count, 0)

    # 6. Nonexistent local issue returns HTTP 404 and performs zero requests
    def test_06_nonexistent_local_issue_returns_404_zero_network(self):
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'GET'
            with patch.object(requests.Session, 'get') as mock_get:
                res_str = handle_metron_compare_credits(issueid='999999999', annual=0)
                res = json.loads(res_str)
                self.assertFalse(res['success'])
                self.assertEqual(res['status_code'], 404)
                self.assertEqual(res['error_code'], 'local_issue_not_found')
                self.assertIn('not found in the local issues table', res['message'])
                self.assertEqual(mock_get.call_count, 0)

    # 7. Regular issue cannot resolve through the Annual table
    def test_07_regular_issue_cannot_resolve_through_annual_table(self):
        # 105543 exists in issues, but NOT in annuals
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'GET'
            with patch.object(requests.Session, 'get') as mock_get:
                res_str = handle_metron_compare_credits(issueid='105543', annual=1)
                res = json.loads(res_str)
                self.assertFalse(res['success'])
                self.assertEqual(res['status_code'], 404)
                self.assertEqual(res['error_code'], 'local_issue_not_found')
                self.assertIn('not found in the local annuals table', res['message'])
                self.assertEqual(mock_get.call_count, 0)

    # 8. Annual cannot resolve through the regular issue table
    def test_08_annual_cannot_resolve_through_regular_issue_table(self):
        # 406949 exists in annuals, but NOT in issues
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'GET'
            with patch.object(requests.Session, 'get') as mock_get:
                res_str = handle_metron_compare_credits(issueid='406949', annual=0)
                res = json.loads(res_str)
                self.assertFalse(res['success'])
                self.assertEqual(res['status_code'], 404)
                self.assertEqual(res['error_code'], 'local_issue_not_found')
                self.assertIn('not found in the local issues table', res['message'])
                self.assertEqual(mock_get.call_count, 0)

    # 9 & 10. Successful cache miss performs exactly one fake provider request with authoritative CV ID
    def test_09_10_cache_miss_performs_one_request_with_authoritative_cv_id(self):
        sample_payload = {'results': [self._sample_metron_issue(105543)]}
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'GET'
            with patch.object(requests.Session, 'get') as mock_get:
                mock_get.return_value = self._create_mock_response(status_code=200, json_data=sample_payload)

                res_str = handle_metron_compare_credits(issueid='105543', annual=0)
                res = json.loads(res_str)

                self.assertTrue(res['success'])
                self.assertEqual(res['issue_id'], '105543')
                self.assertEqual(res['comicvine_issue_id'], 105543)
                self.assertFalse(res['cached'])
                self.assertEqual(mock_get.call_count, 1)

                # Verify exact params passed to Metron
                call_kwargs = mock_get.call_args[1]
                self.assertEqual(call_kwargs.get('params'), {'cv_id': '105543'})

    # 11. Mismatched returned cv_id is rejected and not cached
    def test_11_mismatched_returned_cv_id_rejected_and_not_cached(self):
        # Returned issue has cv_id 999999 instead of 105543
        mismatched_payload = {'results': [self._sample_metron_issue(cv_id=999999)]}
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'GET'
            with patch.object(requests.Session, 'get') as mock_get:
                mock_get.return_value = self._create_mock_response(status_code=200, json_data=mismatched_payload)

                res_str = handle_metron_compare_credits(issueid='105543', annual=0)
                res = json.loads(res_str)

                self.assertFalse(res['success'])
                self.assertEqual(res['status_code'], 502)
                self.assertEqual(res['error_code'], 'invalid_response')

                # Verify not cached
                self.assertIsNone(get_metron_cache().get(105543))

    # 12. Repeated request within TTL performs zero additional provider requests
    def test_12_repeated_request_within_ttl_zero_requests(self):
        sample_payload = {'results': [self._sample_metron_issue(105543)]}
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'GET'
            with patch.object(requests.Session, 'get') as mock_get:
                mock_get.return_value = self._create_mock_response(status_code=200, json_data=sample_payload)

                # Request 1 (cache miss)
                res1 = json.loads(handle_metron_compare_credits(issueid='105543', annual=0))
                self.assertTrue(res1['success'])
                self.assertFalse(res1['cached'])
                self.assertEqual(mock_get.call_count, 1)

                # Request 2 (cache hit)
                res2 = json.loads(handle_metron_compare_credits(issueid='105543', annual=0))
                self.assertTrue(res2['success'])
                self.assertTrue(res2['cached'])
                self.assertEqual(mock_get.call_count, 1)  # Still 1 call!

    # 13. Local credits are re-read and re-compared on a cache hit
    def test_13_local_credits_reread_on_cache_hit(self):
        sample_payload = {'results': [self._sample_metron_issue(105543)]}
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'GET'
            with patch.object(requests.Session, 'get') as mock_get:
                mock_get.return_value = self._create_mock_response(status_code=200, json_data=sample_payload)

                # Populate cache
                handle_metron_compare_credits(issueid='105543', annual=0)
                self.assertEqual(mock_get.call_count, 1)

                # On cache hit, mock local credits returning a different set
                with patch('mylar.extensions.creators.browser_service.CreatorBrowserService.get_issue_creator_credits') as mock_creds:
                    mock_creds.return_value = {
                        'success': True,
                        'credits': [
                            {'raw_name': 'Brand New Local Writer', 'role': 'writer', 'raw_role_text': 'Writer', 'is_cover': False}
                        ]
                    }
                    res_hit = json.loads(handle_metron_compare_credits(issueid='105543', annual=0))
                    self.assertTrue(res_hit['cached'])
                    # Comparison must reflect the freshly read local credits
                    local_only_names = [c.get('raw_creator_name') or c.get('raw_name') for c in res_hit['local_only_credits']]
                    self.assertIn('Brand New Local Writer', local_only_names)

    # 14. Expired cache entry performs one new provider request
    def test_14_expired_cache_entry_triggers_new_provider_request(self):
        current_time = [1000.0]

        def mock_time():
            return current_time[0]

        cache = MetronIssueCache(default_ttl=3600, max_entries=500, time_func=mock_time)
        service = MetronComparisonService(cache=cache)

        sample_payload = {'results': [self._sample_metron_issue(105543)]}
        with patch.object(requests.Session, 'get') as mock_get:
            mock_get.return_value = self._create_mock_response(status_code=200, json_data=sample_payload)

            # Request 1 (t=1000, miss)
            res1 = service.compare_issue_with_metron(issue_id=105543, is_annual=0)
            self.assertFalse(res1['cached'])
            self.assertEqual(mock_get.call_count, 1)

            # Request 2 (t=2000, hit)
            current_time[0] = 2000.0
            res2 = service.compare_issue_with_metron(issue_id=105543, is_annual=0)
            self.assertTrue(res2['cached'])
            self.assertEqual(mock_get.call_count, 1)

            # Request 3 (t=5000, expired > 3600s TTL -> new request)
            current_time[0] = 5000.0
            res3 = service.compare_issue_with_metron(issue_id=105543, is_annual=0)
            self.assertFalse(res3['cached'])
            self.assertEqual(mock_get.call_count, 2)

    # 15. Capacity eviction keeps the cache at or below max_entries (LRU)
    def test_15_capacity_eviction_bounded_at_max_entries(self):
        cache = MetronIssueCache(default_ttl=3600, max_entries=5)

        for i in range(1, 10):
            cache.set(i, {'id': i, 'cv_id': i, 'title': f'Issue {i}'})
            self.assertLessEqual(cache.size, 5)

        self.assertEqual(cache.size, 5)
        # Items 1-4 should have been evicted; 5-9 should remain
        self.assertIsNone(cache.get(1))
        self.assertIsNone(cache.get(2))
        self.assertIsNone(cache.get(3))
        self.assertIsNone(cache.get(4))
        self.assertIsNotNone(cache.get(5))
        self.assertIsNotNone(cache.get(9))

    # 16. Authentication, 404, 429, timeout, and ambiguous failures are not cached
    def test_16_provider_failures_are_not_cached(self):
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'GET'
            with patch.object(requests.Session, 'get') as mock_get:
                # 401 Auth error
                mock_get.return_value = self._create_mock_response(status_code=401, json_data={'detail': 'Invalid token'})
                handle_metron_compare_credits(issueid='105543', annual=0)
                self.assertIsNone(get_metron_cache().get(105543))

                # 404 Not found
                mock_get.return_value = self._create_mock_response(status_code=404, json_data={'detail': 'Not found'})
                handle_metron_compare_credits(issueid='105543', annual=0)
                self.assertIsNone(get_metron_cache().get(105543))

                # 429 Rate limit
                mock_get.return_value = self._create_mock_response(status_code=429, json_data={'detail': 'Throttled'})
                handle_metron_compare_credits(issueid='105543', annual=0)
                self.assertIsNone(get_metron_cache().get(105543))

                # 422 Ambiguous (multiple results)
                mock_get.return_value = self._create_mock_response(status_code=200, json_data={'results': [
                    self._sample_metron_issue(105543), self._sample_metron_issue(105543)
                ]})
                handle_metron_compare_credits(issueid='105543', annual=0)
                self.assertIsNone(get_metron_cache().get(105543))

    # 17, 18, 19, 20. Exact overlaps, local-only, provider-only, discrepancies, raw names, and disclaimer
    def test_17_to_20_comparison_structure_and_disclaimer(self):
        sample_payload = {'results': [self._sample_metron_issue(105543)]}
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'GET'
            with patch.object(requests.Session, 'get') as mock_get:
                mock_get.return_value = self._create_mock_response(status_code=200, json_data=sample_payload)

                res_str = handle_metron_compare_credits(issueid='105543', annual=0)
                res = json.loads(res_str)

                self.assertTrue(res['success'])
                # Overlaps (Fabian Nicieza, Andy Kubert, Matt Ryan, Bob Harras, etc.)
                overlap_names = [o['creator_name'] for o in res['exact_overlaps']]
                self.assertIn('Fabian Nicieza', overlap_names)
                self.assertIn('Andy Kubert', overlap_names)

                # Provider only ('Metron Exclusive Writer')
                prov_only_names = [p['raw_creator_name'] for p in res['provider_only_credits']]
                self.assertIn('Metron Exclusive Writer', prov_only_names)

                # Disclaimer
                self.assertEqual(res['disclaimer'], CREDIT_COMPARISON_DISCLAIMER)

    # 21. No secrets in response JSON, exception text, or query parameters
    def test_21_zero_secret_leakage(self):
        secret_tok = "HIGHLY_CONFIDENTIAL_METRON_TOKEN_99999"
        mylar.CONFIG.METRON_API_TOKEN = secret_tok

        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'GET'
            with patch.object(requests.Session, 'get') as mock_get:
                mock_get.return_value = self._create_mock_response(status_code=401, json_data={'error': 'bad token'})
                res_str = handle_metron_compare_credits(issueid='105543', annual=0)
                self.assertNotIn(secret_tok, res_str)

    # 22. POST to the comparison route returns HTTP 405
    def test_22_post_to_comparison_route_returns_405(self):
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'POST'
            res_str = handle_metron_compare_credits(issueid='105543', annual=0)
            res = json.loads(res_str)
            self.assertFalse(res['success'])
            self.assertEqual(res['status_code'], 405)
            self.assertEqual(res['error_code'], 'method_not_allowed')
            self.assertIn('Method Not Allowed', res['message'])

    # 23. Regular and Annual success cases both work
    def test_23_regular_and_annual_both_work(self):
        # Regular issue 105543
        reg_payload = {'results': [self._sample_metron_issue(105543)]}
        # Annual 406949
        ann_payload = {'results': [self._sample_metron_issue(406949)]}

        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'GET'
            with patch.object(requests.Session, 'get') as mock_get:
                # Regular
                mock_get.return_value = self._create_mock_response(status_code=200, json_data=reg_payload)
                res_reg = json.loads(handle_metron_compare_credits(issueid='105543', annual=0))
                self.assertTrue(res_reg['success'])
                self.assertEqual(res_reg['is_annual'], 0)

                # Annual
                mock_get.return_value = self._create_mock_response(status_code=200, json_data=ann_payload)
                res_ann = json.loads(handle_metron_compare_credits(issueid='406949', annual=1))
                self.assertTrue(res_ann['success'])
                self.assertEqual(res_ann['is_annual'], 1)

    # 24. Existing /testMetron behavior remains unchanged
    def test_24_existing_test_metron_unaffected(self):
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'POST'
            with patch.object(requests.Session, 'get') as mock_get:
                mock_get.return_value = self._create_mock_response(status_code=200, json_data={'results': []})
                res_str = handle_test_metron(auth_mode='token', api_token='valid_tok')
                res = json.loads(res_str)
                self.assertTrue(res['success'])
                self.assertEqual(res['status_code'], 200)

    # 25. Database row counts remain 100% invariant
    def test_25_database_invariance(self):
        before = self.get_db_counts()
        sample_payload = {'results': [self._sample_metron_issue(105543)]}
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'GET'
            with patch.object(requests.Session, 'get') as mock_get:
                mock_get.return_value = self._create_mock_response(status_code=200, json_data=sample_payload)
                handle_metron_compare_credits(issueid='105543', annual=0)
                handle_metron_compare_credits(issueid='406949', annual=1)
        after = self.get_db_counts()
        self.assertEqual(before, after, "Database row counts must remain 100% invariant across all operations")

    # 26. Disabled after cache population returns provider_disabled and performs zero requests
    def test_26_cache_bypass_prevented_when_disabled_after_population(self):
        sample_payload = {'results': [self._sample_metron_issue(105543)]}
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'GET'
            with patch.object(requests.Session, 'get') as mock_get:
                mock_get.return_value = self._create_mock_response(status_code=200, json_data=sample_payload)

                # 1. Successful request populates cache
                res1 = json.loads(handle_metron_compare_credits(issueid='105543', annual=0))
                self.assertTrue(res1['success'])
                self.assertFalse(res1['cached'])
                self.assertEqual(mock_get.call_count, 1)

                # 2. Disable Metron in configuration
                mylar.CONFIG.METRON_ENABLED = False

                # 3. Request comparison again - must fail closed
                res2 = json.loads(handle_metron_compare_credits(issueid='105543', annual=0))
                self.assertFalse(res2['success'])
                self.assertEqual(res2['status_code'], 400)
                self.assertEqual(res2['error_code'], 'provider_disabled')
                self.assertIn('disabled in Settings', res2['message'])
                self.assertNotIn('exact_overlaps', res2)
                # Zero additional provider requests
                self.assertEqual(mock_get.call_count, 1)

    # 27. Credentials removed after cache population returns missing_credentials and performs zero requests
    def test_27_cache_bypass_prevented_when_credentials_removed_after_population(self):
        sample_payload = {'results': [self._sample_metron_issue(105543)]}
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'GET'
            with patch.object(requests.Session, 'get') as mock_get:
                mock_get.return_value = self._create_mock_response(status_code=200, json_data=sample_payload)

                # 1. Successful request populates cache
                res1 = json.loads(handle_metron_compare_credits(issueid='105543', annual=0))
                self.assertTrue(res1['success'])
                self.assertEqual(mock_get.call_count, 1)

                # 2. Invalidate / remove saved token
                mylar.CONFIG.METRON_API_TOKEN = None

                # 3. Request comparison again - must fail closed
                res2 = json.loads(handle_metron_compare_credits(issueid='105543', annual=0))
                self.assertFalse(res2['success'])
                self.assertEqual(res2['status_code'], 400)
                self.assertEqual(res2['error_code'], 'missing_credentials')
                self.assertNotIn('exact_overlaps', res2)
                # Zero additional provider requests
                self.assertEqual(mock_get.call_count, 1)

    # 28. Cache reuse after valid configuration is restored before TTL expiry
    def test_28_cache_reuse_after_valid_configuration_restored(self):
        sample_payload = {'results': [self._sample_metron_issue(105543)]}
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'GET'
            with patch.object(requests.Session, 'get') as mock_get:
                mock_get.return_value = self._create_mock_response(status_code=200, json_data=sample_payload)

                # 1. Successful request populates cache
                res1 = json.loads(handle_metron_compare_credits(issueid='105543', annual=0))
                self.assertTrue(res1['success'])
                self.assertEqual(mock_get.call_count, 1)

                # 2. Temporarily disable Metron
                mylar.CONFIG.METRON_ENABLED = False
                res_disabled = json.loads(handle_metron_compare_credits(issueid='105543', annual=0))
                self.assertFalse(res_disabled['success'])

                # 3. Re-enable Metron with valid token before TTL expires
                mylar.CONFIG.METRON_ENABLED = True
                mylar.CONFIG.METRON_API_TOKEN = 'test_token_12345'

                res_restored = json.loads(handle_metron_compare_credits(issueid='105543', annual=0))
                self.assertTrue(res_restored['success'])
                self.assertTrue(res_restored['cached'])
                self.assertIn('exact_overlaps', res_restored)
                # Zero additional provider requests across disable/restore cycle
                self.assertEqual(mock_get.call_count, 1)


if __name__ == '__main__':
    unittest.main()
