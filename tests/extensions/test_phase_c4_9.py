"""
Unit and Integration Tests for Phase C4.9: Read-Only Creator Identity Candidate Review.

Tests deterministic, read-only candidate discovery connecting local indexed credits with
normalized provider issue snapshots, verifying all 16 required test conditions including
caching gates, rejection suppression, scope isolation, error handling, and DB immutability.
"""

import os
import sys
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Ensure project root and lib are in sys.path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.join(BASE_DIR, 'lib'))
sys.path.insert(0, BASE_DIR)

import mylar
from mylar.extensions.migrations.runner import run_extension_migrations
from mylar.extensions.creators.identity_repository import IdentityRepository
from mylar.extensions.creators.identity_service import CreatorIdentityService
from mylar.extensions.creators.candidate_service import (
    CreatorCandidateService,
    CandidateDiscoveryError,
    InvalidCandidateInputError,
    MalformedProviderSnapshotError,
    LocalIssueNotFoundError,
    is_composite_credit,
)
from mylar.extensions.providers.metron.comparison_service import (
    MetronComparisonService,
    MetronProviderDisabledError,
    MetronInvalidRequestError,
    MetronLocalIssueNotFoundError,
)
from mylar.extensions.providers.metron.cache import MetronIssueCache


class MockDBWrapper:
    """Wrapper exposing .connection and .conn for compatibility with Mylar DB conventions."""
    def __init__(self, connection):
        self.connection = connection
        self.conn = connection

    def select(self, query, params=None):
        cur = self.conn.cursor()
        cur.execute(query, params or [])
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def selectone(self, query, params=None):
        cur = self.conn.cursor()
        cur.execute(query, params or [])
        return cur


class TestPhaseC49CandidateReview(unittest.TestCase):
    """Test suite for Creator Identity Candidate Review."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix='.db')
        self.conn = sqlite3.connect(self.db_path)
        self.cursor = self.conn.cursor()
        self.db_wrapper = MockDBWrapper(self.conn)
        self.repo = IdentityRepository(db_conn=self.db_wrapper)
        self.identity_service = CreatorIdentityService(db_conn=self.db_wrapper, repository=self.repo)
        self.candidate_service = CreatorCandidateService(db_conn=self.db_wrapper, repository=self.repo)

        # Create core tables needed for tests (issues, annuals, comics)
        self.cursor.execute(
            """
            CREATE TABLE issues (
                IssueID TEXT PRIMARY KEY,
                ComicID TEXT,
                Issue_Number TEXT,
                Int_IssueNumber REAL,
                IssueName TEXT,
                IssueDate TEXT,
                ReleaseDate TEXT,
                Location TEXT,
                Status TEXT
            )
            """
        )
        self.cursor.execute(
            """
            CREATE TABLE annuals (
                IssueID TEXT PRIMARY KEY,
                ComicID TEXT,
                Issue_Number TEXT,
                Int_IssueNumber REAL,
                IssueName TEXT,
                IssueDate TEXT,
                ReleaseDate TEXT,
                Location TEXT,
                Status TEXT
            )
            """
        )
        self.cursor.execute(
            """
            CREATE TABLE comics (
                ComicID TEXT PRIMARY KEY,
                ComicName TEXT,
                ComicYear TEXT,
                Corrected_SeriesYear TEXT,
                ComicPublisher TEXT,
                ComicLocation TEXT
            )
            """
        )

        # Run all extension migrations
        run_extension_migrations(self.cursor)
        self.conn.commit()

        # Seed sample comic and issues
        self._insert_comic("5535", "Amazing X-Men", "1995")
        self._insert_issue("105543", "5535", "2", "Sacrificial Lambs")
        self._insert_annual("406949", "5535", "1", "Annual #1")

        # Setup Metron Config
        class MockConfig:
            METRON_ENABLED = True
            METRON_AUTH_MODE = "basic"
            METRON_USERNAME = "testuser"
            METRON_PASSWORD = "testpassword"
            METRON_API_TOKEN = None

        mylar.CONFIG = MockConfig()

    def tearDown(self):
        self.cursor.close()
        self.conn.close()
        try:
            os.close(self.db_fd)
        except OSError:
            pass
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass

    def _insert_comic(self, comic_id, name, year):
        self.cursor.execute(
            "INSERT INTO comics (ComicID, ComicName, ComicYear, ComicPublisher) VALUES (?, ?, ?, 'Marvel')",
            (comic_id, name, year)
        )
        self.conn.commit()

    def _insert_issue(self, issue_id, comic_id, issue_num, issue_name):
        self.cursor.execute(
            "INSERT INTO issues (IssueID, ComicID, Issue_Number, Int_IssueNumber, IssueName, Status) VALUES (?, ?, ?, ?, ?, 'Archived')",
            (issue_id, comic_id, issue_num, float(issue_num), issue_name)
        )
        self.conn.commit()

    def _insert_annual(self, issue_id, comic_id, issue_num, issue_name):
        self.cursor.execute(
            "INSERT INTO annuals (IssueID, ComicID, Issue_Number, Int_IssueNumber, IssueName, Status) VALUES (?, ?, ?, ?, ?, 'Archived')",
            (issue_id, comic_id, issue_num, float(issue_num), issue_name)
        )
        self.conn.commit()

    def _insert_name_record(self, raw_name, normalized_name=None, entity_id=None, source='unresolved'):
        norm = normalized_name or raw_name.strip().lower()
        slug = norm.replace(' ', '-')
        self.cursor.execute(
            """
            INSERT INTO ext_creator_name_records 
            (RawName, NormalizedName, NameSlug, CreatorEntityID, ResolutionSource, CreatedAt, UpdatedAt)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (raw_name, norm, slug, entity_id, source)
        )
        self.conn.commit()
        return self.cursor.lastrowid

    def _insert_credit(self, name_record_id, issue_id="105543", is_annual=0, comic_id="5535", role="writer", raw_credit_name="Test", sort_order=0, entity_id=None):
        self.cursor.execute(
            """
            INSERT INTO ext_creator_credits
            (NameRecordID, CreatorEntityID, IssueID, ComicID, IsAnnual, Role, RawRoleText, RawCreditName, IsCover, SortOrder, SourceProvenance, SourceRevision, CreatedAt)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'comicinfo', 'rev1', CURRENT_TIMESTAMP)
            """,
            (name_record_id, entity_id, str(issue_id), str(comic_id), int(is_annual), str(role), str(role), str(raw_credit_name), int(sort_order))
        )
        self.conn.commit()
        return self.cursor.lastrowid

    # -------------------------------------------------------------------------
    # 1. Regular issue with a name and role candidate
    # -------------------------------------------------------------------------
    def test_01_regular_issue_with_name_and_role_candidate(self):
        nr_id = self._insert_name_record("Fabian Nicieza")
        self._insert_credit(nr_id, issue_id="105543", is_annual=0, role="writer", raw_credit_name="Fabian Nicieza")

        snapshot = {
            'credits': [
                {
                    'raw_creator_name': 'Fabian Nicieza',
                    'canonical_role': 'writer',
                    'raw_role_text': 'Writer',
                    'metron_creator_id': 101,
                }
            ]
        }

        res = self.candidate_service.build_candidates("105543", 0, "metron", snapshot)
        self.assertTrue(res['success'])
        self.assertEqual(len(res['identity_candidates']), 1)
        cand = res['identity_candidates'][0]
        self.assertEqual(cand['name_record_id'], nr_id)
        self.assertEqual(cand['raw_local_name'], 'Fabian Nicieza')
        self.assertEqual(cand['local_role'], 'writer')
        self.assertEqual(cand['provider'], 'metron')
        self.assertEqual(cand['provider_creator_id'], '101')
        self.assertEqual(cand['provider_display_name'], 'Fabian Nicieza')
        self.assertEqual(cand['provider_role'], 'writer')
        self.assertEqual(cand['candidate_state'], 'candidate')
        self.assertIn('exact_display_name_match', cand['evidence'])
        self.assertIn('role_match', cand['evidence'])
        self.assertFalse(cand['is_confirmed'])
        self.assertFalse(cand['is_rejected'])
        self.assertFalse(cand['is_conflicted'])

    # -------------------------------------------------------------------------
    # 2. Annual with a candidate
    # -------------------------------------------------------------------------
    def test_02_annual_with_candidate(self):
        nr_id = self._insert_name_record("Andy Kubert")
        self._insert_credit(nr_id, issue_id="406949", is_annual=1, role="penciller", raw_credit_name="Andy Kubert")

        snapshot = {
            'credits': [
                {
                    'raw_creator_name': 'Andy Kubert',
                    'canonical_role': 'penciller',
                    'raw_role_text': 'Pencils',
                    'metron_creator_id': 500,
                }
            ]
        }

        res = self.candidate_service.build_candidates("406949", 1, "metron", snapshot)
        self.assertTrue(res['success'])
        self.assertEqual(res['is_annual'], 1)
        self.assertEqual(len(res['identity_candidates']), 1)
        cand = res['identity_candidates'][0]
        self.assertEqual(cand['candidate_state'], 'candidate')
        self.assertEqual(cand['provider_creator_id'], '500')

    # -------------------------------------------------------------------------
    # 3. Provider credit with a valid Metron creator ID
    # -------------------------------------------------------------------------
    def test_03_provider_credit_with_valid_metron_creator_id(self):
        nr_id = self._insert_name_record("Scott Lobdell")
        self._insert_credit(nr_id, issue_id="105543", role="writer", raw_credit_name="Scott Lobdell")

        snapshot = {
            'credits': [{'raw_creator_name': 'Scott Lobdell', 'canonical_role': 'writer', 'metron_creator_id': 345}]
        }

        res = self.candidate_service.build_candidates("105543", 0, "metron", snapshot)
        cand = res['identity_candidates'][0]
        self.assertEqual(cand['provider_creator_id'], '345')
        self.assertEqual(cand['candidate_state'], 'candidate')
        self.assertNotIn('provider_id_missing', cand['evidence'])

    # -------------------------------------------------------------------------
    # 4. Provider credit without a creator ID producing insufficient_provider_identity
    # -------------------------------------------------------------------------
    def test_04_provider_credit_without_creator_id_produces_insufficient_provider_identity(self):
        nr_id = self._insert_name_record("Mystery Creator")
        self._insert_credit(nr_id, issue_id="105543", role="other", raw_credit_name="Mystery Creator")

        snapshot = {
            'credits': [{'raw_creator_name': 'Mystery Creator', 'canonical_role': 'other', 'metron_creator_id': None}]
        }

        res = self.candidate_service.build_candidates("105543", 0, "metron", snapshot)
        cand = res['identity_candidates'][0]
        self.assertEqual(cand['candidate_state'], 'insufficient_provider_identity')
        self.assertIsNone(cand['provider_creator_id'])
        self.assertIn('provider_id_missing', cand['evidence'])
        self.assertIn('Provider credit lacks an authoritative creator identifier.', cand['warnings'])
        self.assertFalse(cand['allow_confirmation'])

    # -------------------------------------------------------------------------
    # 5. Two separate NameRecordID values with same normalized name remain distinct
    # -------------------------------------------------------------------------
    def test_05_two_separate_name_records_with_matching_normalized_names_remain_distinct(self):
        nr1 = self._insert_name_record("John Smith", "john smith")
        nr2 = self._insert_name_record("John  Smith", "john smith")  # Same normalized name
        self._insert_credit(nr1, issue_id="105543", role="writer", raw_credit_name="John Smith", sort_order=1)
        self._insert_credit(nr2, issue_id="105543", role="editor", raw_credit_name="John  Smith", sort_order=2)

        snapshot = {
            'credits': [{'raw_creator_name': 'John Smith', 'canonical_role': 'writer', 'metron_creator_id': 999}]
        }

        res = self.candidate_service.build_candidates("105543", 0, "metron", snapshot)
        cands = res['identity_candidates']
        self.assertEqual(len(cands), 2)
        self.assertEqual(cands[0]['name_record_id'], nr1)
        self.assertEqual(cands[1]['name_record_id'], nr2)
        self.assertNotEqual(cands[0]['name_record_id'], cands[1]['name_record_id'])

    # -------------------------------------------------------------------------
    # 6. Composite local credit remaining unsplit
    # -------------------------------------------------------------------------
    def test_06_composite_local_credit_remains_unsplit(self):
        nr = self._insert_name_record("Scott Snyder & Greg Capullo")
        self._insert_credit(nr, issue_id="105543", role="writer", raw_credit_name="Scott Snyder & Greg Capullo")

        snapshot = {
            'credits': [{'raw_creator_name': 'Scott Snyder & Greg Capullo', 'canonical_role': 'writer', 'metron_creator_id': 888}]
        }

        res = self.candidate_service.build_candidates("105543", 0, "metron", snapshot)
        cand = res['identity_candidates'][0]
        self.assertEqual(cand['raw_local_name'], "Scott Snyder & Greg Capullo")
        self.assertEqual(cand['candidate_state'], 'insufficient_provider_identity')
        self.assertIn('composite_raw_credit', cand['evidence'])
        self.assertFalse(cand['allow_confirmation'])

    # -------------------------------------------------------------------------
    # 7. Existing confirmed external-ID mapping reported as confirmed without creating another entity
    # -------------------------------------------------------------------------
    def test_07_existing_confirmed_mapping_reported_confirmed_without_creating_entity(self):
        nr = self._insert_name_record("Fabian Nicieza")
        conf = self.identity_service.confirm_provider_identity(nr, "metron", "101", provider_display_name="Fabian Nicieza")
        entity_id = conf['creator_entity_id']
        self._insert_credit(nr, issue_id="105543", role="writer", raw_credit_name="Fabian Nicieza", entity_id=entity_id)

        snapshot = {
            'credits': [{'raw_creator_name': 'Fabian Nicieza', 'canonical_role': 'writer', 'metron_creator_id': 101}]
        }

        res = self.candidate_service.build_candidates("105543", 0, "metron", snapshot)
        cand = res['identity_candidates'][0]
        self.assertEqual(cand['candidate_state'], 'confirmed')
        self.assertTrue(cand['is_confirmed'])
        self.assertIn('already_confirmed', cand['evidence'])

        # Verify no additional entity created
        self.cursor.execute("SELECT COUNT(*) FROM ext_creator_entities")
        self.assertEqual(self.cursor.fetchone()[0], 1)

    # -------------------------------------------------------------------------
    # 8. Existing rejected candidate remaining rejected or suppressed
    # -------------------------------------------------------------------------
    def test_08_existing_rejected_candidate_reported_rejected_with_no_mutation(self):
        nr = self._insert_name_record("Andy Kubert")
        self._insert_credit(nr, issue_id="105543", role="penciller", raw_credit_name="Andy Kubert")

        # Actively reject Metron 500
        self.identity_service.reject_candidate(nr, "metron", "500", reason="Mistake in provider")

        snapshot = {
            'credits': [{'raw_creator_name': 'Andy Kubert', 'canonical_role': 'penciller', 'metron_creator_id': 500}]
        }

        res = self.candidate_service.build_candidates("105543", 0, "metron", snapshot)
        cand = res['identity_candidates'][0]
        self.assertEqual(cand['candidate_state'], 'rejected')
        self.assertTrue(cand['is_rejected'])
        self.assertIn('active_rejection', cand['evidence'])
        self.assertFalse(cand['allow_confirmation'])

    # -------------------------------------------------------------------------
    # 9. Conflicting provider-ID evidence reported as conflicted
    # -------------------------------------------------------------------------
    def test_09_conflicting_provider_id_evidence_reported_as_conflicted(self):
        # nr1 is Claremont (Entity 1)
        nr1 = self._insert_name_record("Chris Claremont")
        conf1 = self.identity_service.confirm_provider_identity(nr1, "metron", "10", provider_display_name="Chris Claremont")
        entity_1 = conf1['creator_entity_id']

        # nr2 is Jim Lee (Entity 2, Metron 20)
        nr2 = self._insert_name_record("Jim Lee")
        conf2 = self.identity_service.confirm_provider_identity(nr2, "metron", "20", provider_display_name="Jim Lee")

        # Local credit is for Claremont (linked to Entity 1)
        self._insert_credit(nr1, issue_id="105543", role="writer", raw_credit_name="Chris Claremont", entity_id=entity_1)

        # Provider snapshot erroneously claims Claremont is Metron 20 (which is Jim Lee)
        snapshot = {
            'credits': [{'raw_creator_name': 'Chris Claremont', 'canonical_role': 'writer', 'metron_creator_id': 20}]
        }

        res = self.candidate_service.build_candidates("105543", 0, "metron", snapshot)
        cand = res['identity_candidates'][0]
        self.assertEqual(cand['candidate_state'], 'conflicted')
        self.assertTrue(cand['is_conflicted'])
        self.assertIn('external_id_collision', cand['evidence'])
        self.assertFalse(cand['allow_confirmation'])

    # -------------------------------------------------------------------------
    # 10. Same-name but different-role evidence remaining only a candidate
    # -------------------------------------------------------------------------
    def test_10_same_name_different_role_remains_only_candidate(self):
        nr = self._insert_name_record("Matt Ryan")
        self._insert_credit(nr, issue_id="105543", role="inker", raw_credit_name="Matt Ryan")

        snapshot = {
            'credits': [{'raw_creator_name': 'Matt Ryan', 'canonical_role': 'penciller', 'metron_creator_id': 888}]
        }

        res = self.candidate_service.build_candidates("105543", 0, "metron", snapshot)
        cand = res['identity_candidates'][0]
        self.assertEqual(cand['candidate_state'], 'candidate')
        self.assertIn('role_difference', cand['evidence'])
        self.assertNotIn('role_match', cand['evidence'])
        self.assertFalse(cand['is_confirmed'])

    # -------------------------------------------------------------------------
    # 11. Cached provider snapshot combined with freshly read local resolution state
    # -------------------------------------------------------------------------
    def test_11_cached_provider_snapshot_combined_with_freshly_read_local_resolution_state(self):
        nr = self._insert_name_record("Fabian Nicieza")
        self._insert_credit(nr, issue_id="105543", role="writer", raw_credit_name="Fabian Nicieza")

        cache = MetronIssueCache()
        snapshot = {
            'credits': [{'raw_creator_name': 'Fabian Nicieza', 'canonical_role': 'writer', 'metron_creator_id': 101}],
            'normalization_warnings': []
        }
        cache.set(105543, snapshot)

        comp_svc = MetronComparisonService(db_conn=self.db_wrapper, cache=cache)
        
        # 1. First run: unlinked state
        res1 = comp_svc.compare_issue_with_metron("105543", 0)
        self.assertTrue(res1['cached'])
        cand1 = res1['identity_candidates'][0]
        self.assertEqual(cand1['candidate_state'], 'candidate')
        self.assertFalse(cand1['is_confirmed'])

        # 2. Confirm in DB locally
        self.identity_service.confirm_provider_identity(nr, "metron", "101", provider_display_name="Fabian Nicieza")

        # 3. Second run with SAME cached provider snapshot: newly freshly reads confirmed local state!
        res2 = comp_svc.compare_issue_with_metron("105543", 0)
        self.assertTrue(res2['cached'])
        cand2 = res2['identity_candidates'][0]
        self.assertEqual(cand2['candidate_state'], 'confirmed')
        self.assertTrue(cand2['is_confirmed'])

    # -------------------------------------------------------------------------
    # 12. Metron disabled after cache population raises disabled error
    # -------------------------------------------------------------------------
    def test_12_metron_disabled_after_cache_population_raises_disabled_error(self):
        cache = MetronIssueCache()
        cache.set(105543, {'credits': []})

        comp_svc = MetronComparisonService(db_conn=self.db_wrapper, cache=cache)
        mylar.CONFIG.METRON_ENABLED = False

        with self.assertRaises(MetronProviderDisabledError):
            comp_svc.compare_issue_with_metron("105543", 0)

    # -------------------------------------------------------------------------
    # 13. Credentials removed after cache population raises invalid request error
    # -------------------------------------------------------------------------
    def test_13_credentials_removed_after_cache_population_raises_invalid_request_error(self):
        cache = MetronIssueCache()
        cache.set(105543, {'credits': []})

        comp_svc = MetronComparisonService(db_conn=self.db_wrapper, cache=cache)
        mylar.CONFIG.METRON_ENABLED = True
        mylar.CONFIG.METRON_USERNAME = ""  # Cleared credentials

        with self.assertRaises(MetronInvalidRequestError):
            comp_svc.compare_issue_with_metron("105543", 0)

    # -------------------------------------------------------------------------
    # 14. Provider timeout or error leaves local credits intact
    # -------------------------------------------------------------------------
    def test_14_provider_timeout_or_error_leaves_local_credits_intact(self):
        nr = self._insert_name_record("Bob Harras")
        self._insert_credit(nr, issue_id="105543", role="editor", raw_credit_name="Bob Harras")

        # Query local credits before
        self.cursor.execute("SELECT COUNT(*) FROM ext_creator_credits WHERE IssueID = '105543'")
        before_count = self.cursor.fetchone()[0]

        # Simulate provider failure
        mock_issue_svc = MagicMock()
        mock_issue_svc.fetch_issue_by_comicvine_id.side_effect = Exception("Metron API Timeout")
        comp_svc = MetronComparisonService(db_conn=self.db_wrapper, issue_service=mock_issue_svc)

        with self.assertRaises(Exception):
            comp_svc.compare_issue_with_metron("105543", 0)

        # Verify local credits are 100% intact
        self.cursor.execute("SELECT COUNT(*) FROM ext_creator_credits WHERE IssueID = '105543'")
        after_count = self.cursor.fetchone()[0]
        self.assertEqual(before_count, after_count)

    # -------------------------------------------------------------------------
    # 15. Invalid issue ID and invalid annual scope fail explicitly
    # -------------------------------------------------------------------------
    def test_15_invalid_issue_id_and_invalid_annual_scope_fail_explicitly(self):
        snap = {'credits': []}
        with self.assertRaises(InvalidCandidateInputError):
            self.candidate_service.build_candidates("invalid", 0, "metron", snap)
        with self.assertRaises(InvalidCandidateInputError):
            self.candidate_service.build_candidates("105543", "invalid_scope", "metron", snap)
        with self.assertRaises(LocalIssueNotFoundError):
            self.candidate_service.build_candidates("9999999", 0, "metron", snap)

    # -------------------------------------------------------------------------
    # 16. Zero SQL writes during all candidate review requests
    # -------------------------------------------------------------------------
    def test_16_zero_sql_writes_during_all_candidate_review_requests(self):
        nr = self._insert_name_record("Fabian Nicieza")
        self._insert_credit(nr, issue_id="105543", role="writer", raw_credit_name="Fabian Nicieza")

        self.cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in self.cursor.fetchall()]
        before_counts = {t: self.cursor.execute(f"SELECT COUNT(*) FROM `{t}`").fetchone()[0] for t in tables}

        snapshot = {
            'credits': [
                {'raw_creator_name': 'Fabian Nicieza', 'canonical_role': 'writer', 'metron_creator_id': 101}
            ]
        }
        self.candidate_service.build_candidates("105543", 0, "metron", snapshot)

        after_counts = {t: self.cursor.execute(f"SELECT COUNT(*) FROM `{t}`").fetchone()[0] for t in tables}
        self.assertEqual(before_counts, after_counts)


if __name__ == '__main__':
    unittest.main()
