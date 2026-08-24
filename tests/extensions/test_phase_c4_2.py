#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Unit tests for Phase C4.2: Single-Issue Metron Comparison Core (Read-Only and Fixture-Driven).

Tests authoritative ComicVine IssueID lookup, strict returned-ID verification,
conservative role mapping, multi-valued role preservation, duplicate credit handling,
defensive local credit normalization, response-envelope error coverage, observational comparison,
and database invariance.
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
    MetronIssueService,
    MetronAmbiguousResultError,
    MetronNotFoundError,
    MetronInvalidRequestError,
    MetronInvalidResponseError,
    validate_comicvine_issue_id,
    normalize_metron_issue,
    normalize_metron_credit,
    normalize_role,
    compare_with_local_credits,
    CREDIT_COMPARISON_DISCLAIMER,
    CANONICAL_ROLES,
    ROLE_MAPPING,
    AUTH_MODE_TOKEN
)
from mylar.extensions.providers.metron.issue_service import _verify_returned_comicvine_id

STAGING_DB = r'C:\Users\spike\.gemini\antigravity\brain\ff5d5fcf-d223-4e21-94d9-ed4e7c37cd34\scratch\live_staging_20260822_0050\mylar.db'


# Realistic test fixtures based on approved audit scenarios
FIGHT_GIRLS_1_FIXTURE = {
    "id": 101,
    "cv_id": 868995,
    "gcd_id": 2001,
    "number": "1",
    "title": "Fight Girls #1",
    "cover_date": "2021-07-07",
    "store_date": "2021-07-07",
    "page": 32,
    "desc": "Ten women enter the Ancient Olympics...",
    "series": {
        "id": 501,
        "cv_id": 136932,
        "name": "Fight Girls",
        "year_began": 2021,
        "volume": 1,
        "publisher": {"id": 10, "name": "AWA Studios"},
        "imprint": {"id": 2, "name": "Upshot"}
    },
    "credits": [
        {
            "id": 1,
            "creator": {"id": 1001, "name": "Frank Cho", "cv_id": 2011, "gcd_id": 3011},
            "role": {"id": 1, "name": "Writer"}
        },
        {
            "id": 2,
            "creator": {"id": 1001, "name": "Frank Cho", "cv_id": 2011, "gcd_id": 3011},
            "role": {"id": 2, "name": "Penciller"}
        },
        {
            "id": 3,
            "creator": {"id": 1002, "name": "Sabine Rich", "cv_id": 2012, "gcd_id": 3012},
            "role": {"id": 4, "name": "Colorist"}
        },
        {
            "id": 4,
            "creator": {"id": 1003, "name": "Sal Cipriano", "cv_id": 2013, "gcd_id": 3013},
            "role": {"id": 5, "name": "Letterer"}
        },
        {
            "id": 5,
            "creator": {"id": 1001, "name": "Frank Cho", "cv_id": 2011, "gcd_id": 3011},
            "role": {"id": 7, "name": "Cover Artist"}
        }
    ]
}

BATMAN_52_FIXTURE = {
    "id": 202,
    "cv_id": 531238,
    "gcd_id": 2002,
    "number": "52",
    "title": "The List",
    "cover_date": "2016-07-01",
    "store_date": "2016-05-25",
    "page": 32,
    "desc": "Batman finale issue.",
    "series": {
        "id": 601,
        "cv_id": 42721,
        "name": "Batman",
        "year_began": 2011,
        "volume": 2,
        "publisher": "DC Comics"
    },
    "credits": [
        {
            "id": 10,
            "creator": {"id": 3001, "name": "James Tynion IV", "cv_id": 4001},
            "role": "Writer"
        },
        {
            "id": 11,
            "creator": {"id": 3002, "name": "Riley Rossmo", "cv_id": 4002},
            "role": "Penciller"
        }
    ]
}


class TestPhaseC4_2SingleIssueComparison(unittest.TestCase):

    def setUp(self):
        self.mock_base_url = 'https://mock.metron.test/api/'
        self.test_token = 'secret_test_token_abc123xyz'
        self.creds = MetronCredentials(mode=AUTH_MODE_TOKEN, token=self.test_token)

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

    # 1. Approved canonical mappings produce the expected canonical role
    def test_01_approved_canonical_role_mappings(self):
        expected_approved = {
            'Writer': ('writer', False),
            'Script': ('writer', False),
            'Plot': ('writer', False),
            'Penciller': ('penciller', False),
            'Pencils': ('penciller', False),
            'Inker': ('inker', False),
            'Inks': ('inker', False),
            'Colorist': ('colorist', False),
            'Colors': ('colorist', False),
            'Letterer': ('letterer', False),
            'Letters': ('letterer', False),
            'Editor': ('editor', False),
            'Editing': ('editor', False),
            'Assistant Editor': ('editor', False),
            'Cover': ('cover_artist', True),
            'Cover Artist': ('cover_artist', True),
            'Variant Cover': ('cover_artist', True)
        }

        for raw_role, (expected_canonical, expected_cover) in expected_approved.items():
            canonical, is_cover = normalize_role(raw_role)
            self.assertEqual(canonical, expected_canonical, f"Mapping failed for '{raw_role}'")
            self.assertEqual(is_cover, expected_cover, f"Cover flag failed for '{raw_role}'")

    # 2. Unconfirmed roles strictly map to 'other' with raw role preserved
    def test_02_unconfirmed_roles_map_to_other(self):
        unconfirmed_roles = [
            'Written By', 'Author', 'Penciler', 'Artist', 'Art', 'Drawn By',
            'Inking', 'Colourist', 'Lettering', 'Series Editor', 'Consulting Editor',
            'Cover Designer', 'Cover Painter', 'Variant Cover Artist',
            'Translator', 'Production', 'Designer'
        ]

        for raw_role in unconfirmed_roles:
            canonical, is_cover = normalize_role(raw_role)
            self.assertEqual(canonical, 'other', f"Unconfirmed role '{raw_role}' was improperly mapped to '{canonical}'")
            self.assertFalse(is_cover, f"Unconfirmed role '{raw_role}' improperly set is_cover=True")

            credit_dict = {"creator": "Test Creator", "role": raw_role}
            norm_cred, warning = normalize_metron_credit(credit_dict, order=0)
            self.assertEqual(norm_cred['raw_role_text'], raw_role)
            self.assertEqual(norm_cred['canonical_role'], 'other')
            self.assertFalse(norm_cred['is_cover_credit'])

    # 3. Conservative cover classification (no substring heuristic)
    def test_03_conservative_cover_classification(self):
        # 'Cover Designer' without explicit flag -> canonical='other', is_cover=False
        norm_no_flag, _ = normalize_metron_credit({"creator": "Artist A", "role": "Cover Designer"}, order=0)
        self.assertEqual(norm_no_flag['canonical_role'], 'other')
        self.assertFalse(norm_no_flag['is_cover_credit'])

        # 'Cover Designer' with explicit provider cover flag -> canonical='other', is_cover=True
        norm_with_flag, _ = normalize_metron_credit({"creator": "Artist A", "role": "Cover Designer", "is_cover": True}, order=0)
        self.assertEqual(norm_with_flag['canonical_role'], 'other')
        self.assertTrue(norm_with_flag['is_cover_credit'])

    # 4. Strict returned ComicVine ID verification (rejecting non-canonical / malformed / padded values)
    def test_04_strict_returned_comicvine_id_verification(self):
        requested_id = 868995

        # All invalid / non-canonical returned cv_id values must raise MetronInvalidResponseError
        invalid_returned_cv_ids = [
            " 868995 ",
            "868995 ",
            " 868995",
            "+868995",
            "-868995",
            "868995.0",
            "٨٦٨٩٩٥",
            "abc",
            True,
            False,
            0,
            -1,
            -868995,
            None,
            999999,  # Mismatched integer
            "999999" # Mismatched string
        ]

        for bad_val in invalid_returned_cv_ids:
            bad_fixture = dict(FIGHT_GIRLS_1_FIXTURE)
            bad_fixture['cv_id'] = bad_val
            with self.assertRaises(MetronInvalidResponseError, msg=f"Failed to reject invalid cv_id: {bad_val!r}"):
                _verify_returned_comicvine_id(bad_fixture, requested_id)

        # Missing cv_id key in dictionary
        missing_key_fixture = {k: v for k, v in FIGHT_GIRLS_1_FIXTURE.items() if k != 'cv_id'}
        with self.assertRaises(MetronInvalidResponseError):
            _verify_returned_comicvine_id(missing_key_fixture, requested_id)

        # Valid integer and exact ASCII digit string succeed
        valid_int_fixture = dict(FIGHT_GIRLS_1_FIXTURE)
        valid_int_fixture['cv_id'] = 868995
        _verify_returned_comicvine_id(valid_int_fixture, requested_id)

        valid_str_fixture = dict(FIGHT_GIRLS_1_FIXTURE)
        valid_str_fixture['cv_id'] = "868995"
        _verify_returned_comicvine_id(valid_str_fixture, requested_id)

    # 5. Response envelope regression & error coverage
    def test_05_response_envelope_error_coverage(self):
        service = MetronIssueService(credentials=self.creds, client_options={'base_url': self.mock_base_url})

        # 1. results is not a list (string)
        with patch.object(requests.Session, 'get') as mock_get:
            mock_get.return_value = self._create_mock_response(status_code=200, json_data={'count': 1, 'results': 'not-a-list'})
            with self.assertRaises(MetronInvalidResponseError):
                service.fetch_issue_by_comicvine_id(868995)

        # 2. results is None
        with patch.object(requests.Session, 'get') as mock_get:
            mock_get.return_value = self._create_mock_response(status_code=200, json_data={'count': 1, 'results': None})
            with self.assertRaises(MetronInvalidResponseError):
                service.fetch_issue_by_comicvine_id(868995)

        # 3. Unexpected scalar payload (string, integer, boolean)
        for scalar in ["unexpected_string", 12345, True]:
            with patch.object(requests.Session, 'get') as mock_get:
                mock_get.return_value = self._create_mock_response(status_code=200, json_data=scalar)
                with self.assertRaises(MetronInvalidResponseError):
                    service.fetch_issue_by_comicvine_id(868995)

        # 4. Empty object -> MetronNotFoundError
        with patch.object(requests.Session, 'get') as mock_get:
            mock_get.return_value = self._create_mock_response(status_code=200, json_data={})
            with self.assertRaises(MetronNotFoundError):
                service.fetch_issue_by_comicvine_id(868995)

        # 5. Empty flat list -> MetronNotFoundError
        with patch.object(requests.Session, 'get') as mock_get:
            mock_get.return_value = self._create_mock_response(status_code=200, json_data=[])
            with self.assertRaises(MetronNotFoundError):
                service.fetch_issue_by_comicvine_id(868995)

        # 6. Empty paginated results -> MetronNotFoundError
        with patch.object(requests.Session, 'get') as mock_get:
            mock_get.return_value = self._create_mock_response(status_code=200, json_data={'count': 0, 'results': []})
            with self.assertRaises(MetronNotFoundError):
                service.fetch_issue_by_comicvine_id(868995)

        # 7. Multiple results in paginated envelope -> MetronAmbiguousResultError
        with patch.object(requests.Session, 'get') as mock_get:
            mock_get.return_value = self._create_mock_response(
                status_code=200,
                json_data={'count': 2, 'results': [FIGHT_GIRLS_1_FIXTURE, BATMAN_52_FIXTURE]}
            )
            with self.assertRaises(MetronAmbiguousResultError):
                service.fetch_issue_by_comicvine_id(868995)

        # 8. Multiple results in flat list -> MetronAmbiguousResultError
        with patch.object(requests.Session, 'get') as mock_get:
            mock_get.return_value = self._create_mock_response(
                status_code=200,
                json_data=[FIGHT_GIRLS_1_FIXTURE, BATMAN_52_FIXTURE]
            )
            with self.assertRaises(MetronAmbiguousResultError):
                service.fetch_issue_by_comicvine_id(868995)

    # 6. Actual response-shape metadata derived from inspected payload
    def test_06_inspected_payload_shape_and_counts(self):
        service = MetronIssueService(credentials=self.creds, client_options={'base_url': self.mock_base_url})

        # Paginated envelope
        with patch.object(requests.Session, 'get') as mock_get:
            mock_get.return_value = self._create_mock_response(
                status_code=200,
                json_data={'count': 100, 'results': [FIGHT_GIRLS_1_FIXTURE]}
            )
            snapshot = service.fetch_issue_by_comicvine_id(868995)
            self.assertEqual(snapshot['response_shape'], 'paginated')
            self.assertEqual(snapshot['observed_result_count'], 1)

        # Flat list
        with patch.object(requests.Session, 'get') as mock_get:
            mock_get.return_value = self._create_mock_response(
                status_code=200,
                json_data=[FIGHT_GIRLS_1_FIXTURE]
            )
            snapshot = service.fetch_issue_by_comicvine_id(868995)
            self.assertEqual(snapshot['response_shape'], 'list')
            self.assertEqual(snapshot['observed_result_count'], 1)

        # Single object
        with patch.object(requests.Session, 'get') as mock_get:
            mock_get.return_value = self._create_mock_response(
                status_code=200,
                json_data=BATMAN_52_FIXTURE
            )
            snapshot = service.fetch_issue_by_comicvine_id(531238)
            self.assertEqual(snapshot['response_shape'], 'object')
            self.assertEqual(snapshot['observed_result_count'], 1)

    # 7. Multi-valued role retention without loss
    def test_07_multi_valued_role_retention(self):
        multi_role_credit = {
            "creator": {"id": 1001, "name": "Frank Cho", "cv_id": 2011},
            "role": [{"name": "Writer"}, {"name": "Artist"}]
        }
        norm_cred, warning = normalize_metron_credit(multi_role_credit, order=2)

        self.assertEqual(norm_cred['raw_role_text'], 'Writer; Artist')
        self.assertEqual(norm_cred['canonical_role'], 'other')
        self.assertFalse(norm_cred['is_cover_credit'])
        self.assertEqual(norm_cred['raw_creator_name'], 'Frank Cho')
        self.assertEqual(norm_cred['comicvine_creator_id'], 2011)
        self.assertIsNotNone(warning)
        self.assertIn("contains multiple provider roles", warning)

    # 8. Duplicate credit preservation in provider order
    def test_08_duplicate_credits_preserved(self):
        provider_snapshot = {
            'credits': [
                {'raw_creator_name': 'Frank Cho', 'canonical_role': 'writer', 'raw_role_text': 'Writer', 'order': 0},
                {'raw_creator_name': 'Frank Cho', 'canonical_role': 'writer', 'raw_role_text': 'Plot', 'order': 1}
            ]
        }
        local_credits = [
            {'raw_creator_name': 'Frank Cho', 'canonical_role': 'writer'}
        ]

        result = compare_with_local_credits(provider_snapshot, local_credits)

        # Exactly 1 overlap created (matching provider order 0)
        self.assertEqual(len(result['exact_overlaps']), 1)
        self.assertEqual(result['exact_overlaps'][0]['provider_credit']['order'], 0)

        # Second credit remains provider-only (order 1)
        self.assertEqual(len(result['provider_only_credits']), 1)
        self.assertEqual(result['provider_only_credits'][0]['order'], 1)

        # Local credits are fully consumed
        self.assertEqual(len(result['local_only_credits']), 0)

    # 9. Defensive local-credit input handling (strings, numbers, None, malformed dicts)
    def test_09_defensive_local_credit_handling(self):
        provider_snapshot = normalize_metron_issue(FIGHT_GIRLS_1_FIXTURE, query_comicvine_issue_id=868995)
        bad_local_inputs = [
            "Frank Cho",
            12345,
            None,
            {"invalid_key": True},
            {"name": "Frank Cho", "role": None}
        ]

        # Must execute without raising KeyError or crashing
        result = compare_with_local_credits(provider_snapshot, bad_local_inputs)
        self.assertEqual(len(result['local_credits']), len(bad_local_inputs))
        self.assertIn(CREDIT_COMPARISON_DISCLAIMER, result['disclaimer'])

    # 10. Invalid ComicVine IssueID validation
    def test_10_invalid_comicvine_issue_ids(self):
        invalid_ids = [
            True, False, 0, -1, -500, 868995.0, 3.14,
            " 868995 ", "868995 ", " 868995",
            "+868995", "-868995", "868995.0",
            "abc", "cv868995", "", None, {}, []
        ]
        for bad_id in invalid_ids:
            with self.assertRaises(MetronInvalidRequestError):
                validate_comicvine_issue_id(bad_id)

    # 11. Case-sensitive distinction (Frank Cho vs FRANK CHO)
    def test_11_case_variant_retained_as_distinct(self):
        provider_snapshot = normalize_metron_issue(FIGHT_GIRLS_1_FIXTURE, query_comicvine_issue_id=868995)
        local_credits = [
            {'raw_creator_name': 'FRANK CHO', 'canonical_role': 'writer', 'raw_role_text': 'Writer'}
        ]
        result = compare_with_local_credits(provider_snapshot, local_credits)

        self.assertEqual(len(result['exact_overlaps']), 0)
        self.assertEqual(len(result['local_only_credits']), 1)
        self.assertEqual(result['local_only_credits'][0]['raw_creator_name'], 'FRANK CHO')

    # 12. Composite creator name retained intact without splitting
    def test_12_composite_creator_name_retained_intact(self):
        composite_issue = dict(BATMAN_52_FIXTURE)
        composite_issue['credits'] = [
            {"id": 99, "creator": "Scott Snyder and Nick Dragotta", "role": "Writer"}
        ]
        snapshot = normalize_metron_issue(composite_issue, query_comicvine_issue_id=531238)
        self.assertEqual(snapshot['credits'][0]['raw_creator_name'], 'Scott Snyder and Nick Dragotta')

        local_credits = [
            {'raw_creator_name': 'Scott Snyder', 'canonical_role': 'writer'},
            {'raw_creator_name': 'Nick Dragotta', 'canonical_role': 'writer'}
        ]
        result = compare_with_local_credits(snapshot, local_credits)
        self.assertEqual(len(result['exact_overlaps']), 0)
        self.assertEqual(len(result['local_only_credits']), 2)
        self.assertEqual(len(result['provider_only_credits']), 1)

    # 13. Provider-only credit (e.g. Greg Land) and Local-only credit
    def test_13_provider_and_local_only_credits(self):
        issue_with_greg = dict(BATMAN_52_FIXTURE)
        issue_with_greg['credits'] = [{"creator": "Greg Land", "role": "Penciller"}]
        snapshot = normalize_metron_issue(issue_with_greg, query_comicvine_issue_id=531238)
        local_credits = [{'raw_creator_name': 'Riley Rossmo', 'canonical_role': 'penciller'}]

        result = compare_with_local_credits(snapshot, local_credits)
        self.assertEqual(len(result['exact_overlaps']), 0)
        self.assertEqual(len(result['provider_only_credits']), 1)
        self.assertEqual(result['provider_only_credits'][0]['raw_creator_name'], 'Greg Land')
        self.assertEqual(len(result['local_only_credits']), 1)
        self.assertEqual(result['local_only_credits'][0]['raw_creator_name'], 'Riley Rossmo')

    # 14. Role discrepancies detection in comparison
    def test_14_role_discrepancies_detection(self):
        snapshot = normalize_metron_issue(BATMAN_52_FIXTURE, query_comicvine_issue_id=531238)
        local_credits = [
            {'raw_creator_name': 'James Tynion IV', 'canonical_role': 'editor', 'raw_role_text': 'Editor'}
        ]
        result = compare_with_local_credits(snapshot, local_credits)

        self.assertEqual(len(result['exact_overlaps']), 0)
        self.assertEqual(len(result['role_discrepancies']), 1)
        disc = result['role_discrepancies'][0]
        self.assertEqual(disc['creator_name'], 'James Tynion IV')
        self.assertEqual(disc['local_canonical_role'], 'editor')
        self.assertEqual(disc['provider_canonical_role'], 'writer')

    # 15. Single request guarantee
    def test_15_single_request_guarantee(self):
        with patch.object(requests.Session, 'get') as mock_get:
            mock_get.return_value = self._create_mock_response(status_code=200, json_data=BATMAN_52_FIXTURE)
            service = MetronIssueService(credentials=self.creds, client_options={'base_url': self.mock_base_url})
            service.fetch_issue_by_comicvine_id(531238)
            self.assertEqual(mock_get.call_count, 1)

    # 16. Zero network on import and construction
    def test_16_zero_network_on_import_and_construction(self):
        with patch.object(requests.Session, 'get') as mock_get:
            import mylar.extensions.providers.metron as test_pkg
            service = test_pkg.MetronIssueService(credentials=self.creds)
            self.assertEqual(mock_get.call_count, 0)

    # 17. Database row counts remain 100% invariant
    def test_17_database_invariance(self):
        before = self.get_db_counts()
        with patch.object(requests.Session, 'get') as mock_get:
            mock_get.return_value = self._create_mock_response(status_code=200, json_data=FIGHT_GIRLS_1_FIXTURE)
            service = MetronIssueService(credentials=self.creds, client_options={'base_url': self.mock_base_url})
            service.compare_issue_credits(868995, [{'name': 'Frank Cho', 'role': 'writer'}])
        after = self.get_db_counts()
        self.assertEqual(before, after, "Database row counts must remain 100% invariant")


if __name__ == '__main__':
    unittest.main()
