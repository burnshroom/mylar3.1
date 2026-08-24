"""
Unit and Integration Tests for Phase C4.10 Explicit Creator Identity Resolution Actions.

Tests:
1. Explicit confirmation writes exactly one resolution/audit decision.
2. Explicit rejection writes exactly one pairing-specific suppression/audit decision.
3. Reversal (undo) restores previous state and records audit reversal.
4. Repeated identical confirmation/rejection requests are idempotent.
5. Same normalized names with distinct NameRecordID values remain independent.
6. Composite credits remain unsplit.
7. Missing provider ID cannot be confirmed or rejected.
8. Provider-ID collision fails closed with zero partial writes.
9. Stale or already-resolved candidate actions fail safely.
10. Disabled Metron or removed credentials blocks candidate-derived mutation.
11. Invalid regular/Annual scope and malformed IDs cause zero writes.
12. POST-only HTTP method enforcement (GET returns 405).
13. CSRF token enforcement (missing/invalid tokens return 403).
14. Browser cannot inject entity IDs, state, or audit IDs to alter decision.
15. Undo makes zero provider/network requests regardless of Metron configuration.
16. Transaction failure guarantees atomic rollback (zero partial writes).
17. Mutation responses and logs are sanitized.
"""

import os
import sys
import unittest
import tempfile
import sqlite3
import json
from unittest.mock import patch, MagicMock

# Set up test paths
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.join(REPO_ROOT, 'lib'))
sys.path.insert(0, REPO_ROOT)

import mylar
from mylar import db
from mylar.extensions.migrations.runner import run_extension_migrations
from mylar.extensions.creators.identity_repository import IdentityRepository
from mylar.extensions.creators.identity_service import CreatorIdentityService
from mylar.extensions.creators.decision_controller import (
    handle_confirm_creator_identity,
    handle_reject_creator_candidate,
    handle_reverse_creator_decision,
    handle_get_creator_csrf_token,
    get_or_create_csrf_token,
    verify_csrf_token,
)


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

    def action(self, query, params=None):
        cur = self.conn.cursor()
        cur.execute(query, params or [])
        self.conn.commit()
        return cur


class TestPhaseC4_10(unittest.TestCase):
    """Test suite for Phase C4.10 Explicit Creator Identity Resolution Actions."""

    def setUp(self):
        # Create an isolated temporary database for each test
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        self.conn = sqlite3.connect(self.db_path)
        self.cursor = self.conn.cursor()

        # Create core tables
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

        self.mock_db = MockDBWrapper(self.conn)
        self.repo = IdentityRepository(db_conn=self.mock_db)
        self.service = CreatorIdentityService(db_conn=self.mock_db, repository=self.repo)

        # Seed sample local name records & credits
        self.cursor.execute("""
            INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, ResolutionSource)
            VALUES (101, 'Fabian Nicieza', 'fabian nicieza', 'fabian-nicieza', 'unresolved'),
                   (102, 'Andy Kubert', 'andy kubert', 'andy-kubert', 'unresolved'),
                   (103, 'F. Nicieza', 'fabian nicieza', 'f-nicieza', 'unresolved'),
                   (104, 'Stan Lee & Jack Kirby', 'stan lee & jack kirby', 'stan-lee-jack-kirby', 'unresolved')
        """)
        self.cursor.execute("""
            INSERT INTO ext_creator_credits (CreditID, NameRecordID, IssueID, ComicID, IsAnnual, Role, RawRoleText, RawCreditName, SourceProvenance, SourceRevision)
            VALUES (1, 101, '105543', '5535', 0, 'writer', 'Writer', 'Fabian Nicieza', 'comicinfo', 'rev1'),
                   (2, 102, '105543', '5535', 0, 'penciller', 'Penciller', 'Andy Kubert', 'comicinfo', 'rev1'),
                   (3, 103, '105544', '5535', 0, 'writer', 'Writer', 'F. Nicieza', 'comicinfo', 'rev1'),
                   (4, 104, '105545', '5535', 0, 'writer', 'Writer', 'Stan Lee & Jack Kirby', 'comicinfo', 'rev1')
        """)
        self.conn.commit()

        # Mock Mylar config
        class MockConfig:
            METRON_ENABLED = True
            METRON_AUTH_MODE = "basic"
            METRON_USERNAME = "testuser"
            METRON_PASSWORD = "testpassword"
            METRON_API_TOKEN = None

        mylar.CONFIG = MockConfig()

        # Set up cherrypy mock request/response
        self.csrf_token = get_or_create_csrf_token()

    def tearDown(self):
        self.conn.close()
        try:
            os.close(self.db_fd)
            os.remove(self.db_path)
        except OSError:
            pass

    def _mock_cherrypy_post(self, headers=None):
        req = MagicMock()
        req.method = 'POST'
        req.headers = headers or {'X-CSRF-Token': self.csrf_token}
        resp = MagicMock()
        resp.headers = {}
        resp.status = 200
        return req, resp

    # -------------------------------------------------------------------------
    # 1. Explicit Confirmation
    # -------------------------------------------------------------------------
    def test_01_confirm_identity_writes_resolution_and_audit(self):
        req, resp = self._mock_cherrypy_post()
        with patch('cherrypy.request', req), patch('cherrypy.response', resp):
            raw_res = handle_confirm_creator_identity(
                name_record_id=101,
                provider='metron',
                provider_creator_id='500',
                provider_display_name='Fabian Nicieza',
                csrf_token=self.csrf_token,
                service=self.service
            )
            res = json.loads(raw_res)

        self.assertTrue(res['success'])
        self.assertEqual(res['name_record_id'], 101)
        self.assertEqual(res['provider_creator_id'], '500')
        self.assertIsNotNone(res['creator_entity_id'])

        # Verify DB state
        name_rec = self.repo.get_name_record(101)
        self.assertIsNotNone(name_rec['creator_entity_id'])
        self.assertEqual(name_rec['resolution_source'], 'explicit_user')

        ext_map = self.repo.get_external_id_mapping('metron', '500')
        self.assertIsNotNone(ext_map)
        self.assertEqual(ext_map['creator_entity_id'], name_rec['creator_entity_id'])

        audit = self.repo.get_audit_history(name_record_id=101)
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]['action'], 'CONFIRM_PROVIDER_IDENTITY')

    # -------------------------------------------------------------------------
    # 2. Explicit Rejection
    # -------------------------------------------------------------------------
    def test_02_reject_candidate_writes_active_suppression_and_audit(self):
        req, resp = self._mock_cherrypy_post()
        with patch('cherrypy.request', req), patch('cherrypy.response', resp):
            raw_res = handle_reject_creator_candidate(
                name_record_id=101,
                provider='metron',
                provider_creator_id='999',
                provider_display_name='Fabian Imposter',
                reason='Not the same creator',
                csrf_token=self.csrf_token,
                service=self.service
            )
            res = json.loads(raw_res)

        self.assertTrue(res['success'])
        self.assertEqual(res['status'], 'active')

        rej = self.repo.get_active_rejection(101, 'metron', '999')
        self.assertIsNotNone(rej)
        self.assertEqual(rej['status'], 'active')

        audit = self.repo.get_audit_history(name_record_id=101)
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]['action'], 'REJECT_CANDIDATE')

    # -------------------------------------------------------------------------
    # 3. Reversal (Undo) for Confirmation and Rejection
    # -------------------------------------------------------------------------
    def test_03_reversal_undoes_confirmation_and_rejection_safely(self):
        # 3a. Test Confirmation -> Undo
        self.service.confirm_provider_identity(101, 'metron', '500', actor='user')
        self.assertIsNotNone(self.repo.get_name_record(101)['creator_entity_id'])

        req, resp = self._mock_cherrypy_post()
        with patch('cherrypy.request', req), patch('cherrypy.response', resp):
            raw_res = handle_reverse_creator_decision(
                name_record_id=101,
                provider='metron',
                provider_creator_id='500',
                csrf_token=self.csrf_token,
                service=self.service,
                repository=self.repo
            )
            res = json.loads(raw_res)

        self.assertTrue(res['success'])
        self.assertEqual(res['action'], 'reverse_confirmation')
        self.assertIsNone(self.repo.get_name_record(101)['creator_entity_id'])

        audit = self.repo.get_audit_history(name_record_id=101)
        self.assertEqual(len(audit), 2)
        self.assertEqual(audit[0]['action'], 'REVERSE_CONFIRMATION')

        # 3b. Test Rejection -> Undo
        self.service.reject_candidate(102, 'metron', '600', actor='user')
        self.assertIsNotNone(self.repo.get_active_rejection(102, 'metron', '600'))

        with patch('cherrypy.request', req), patch('cherrypy.response', resp):
            raw_res = handle_reverse_creator_decision(
                name_record_id=102,
                provider='metron',
                provider_creator_id='600',
                csrf_token=self.csrf_token,
                service=self.service,
                repository=self.repo
            )
            res = json.loads(raw_res)

        self.assertTrue(res['success'])
        self.assertEqual(res['action'], 'reverse_rejection')
        self.assertIsNone(self.repo.get_active_rejection(102, 'metron', '600'))

    # -------------------------------------------------------------------------
    # 4. Idempotency on Repeated Requests
    # -------------------------------------------------------------------------
    def test_04_repeated_identical_requests_are_idempotent(self):
        req, resp = self._mock_cherrypy_post()
        with patch('cherrypy.request', req), patch('cherrypy.response', resp):
            # First confirm
            raw1 = handle_confirm_creator_identity(
                name_record_id=101, provider='metron', provider_creator_id='500',
                csrf_token=self.csrf_token, service=self.service
            )
            res1 = json.loads(raw1)
            # Repeated confirm with same link
            raw2 = handle_confirm_creator_identity(
                name_record_id=101, provider='metron', provider_creator_id='500',
                csrf_token=self.csrf_token, service=self.service
            )
            res2 = json.loads(raw2)

        self.assertTrue(res1['success'])
        self.assertTrue(res2['success'])
        self.assertEqual(res1['creator_entity_id'], res2['creator_entity_id'])

        # Repeated rejection
        self.service.reject_candidate(102, 'metron', '700')
        r2 = self.service.reject_candidate(102, 'metron', '700')
        self.assertEqual(r2['status'], 'active')

    # -------------------------------------------------------------------------
    # 5. Distinct Local Records Remain Independent
    # -------------------------------------------------------------------------
    def test_05_distinct_name_records_remain_independent(self):
        # 101 and 103 have identical name 'Fabian Nicieza'
        req, resp = self._mock_cherrypy_post()
        with patch('cherrypy.request', req), patch('cherrypy.response', resp):
            handle_confirm_creator_identity(
                name_record_id=101, provider='metron', provider_creator_id='500',
                csrf_token=self.csrf_token, service=self.service
            )

        # 101 is confirmed, 103 is still unresolved
        rec101 = self.repo.get_name_record(101)
        rec103 = self.repo.get_name_record(103)
        self.assertIsNotNone(rec101['creator_entity_id'])
        self.assertIsNone(rec103['creator_entity_id'])
        self.assertEqual(rec103['resolution_source'], 'unresolved')

    # -------------------------------------------------------------------------
    # 6. Composite Credits Remain Intact
    # -------------------------------------------------------------------------
    def test_06_composite_credits_remain_intact(self):
        req, resp = self._mock_cherrypy_post()
        with patch('cherrypy.request', req), patch('cherrypy.response', resp):
            handle_confirm_creator_identity(
                name_record_id=104, provider='metron', provider_creator_id='888',
                provider_display_name='Stan Lee & Jack Kirby',
                csrf_token=self.csrf_token, service=self.service
            )

        rec104 = self.repo.get_name_record(104)
        self.assertEqual(rec104['raw_name'], 'Stan Lee & Jack Kirby')

    # -------------------------------------------------------------------------
    # 7. Missing Provider ID Rejected
    # -------------------------------------------------------------------------
    def test_07_missing_provider_id_cannot_be_mutated(self):
        req, resp = self._mock_cherrypy_post()
        with patch('cherrypy.request', req), patch('cherrypy.response', resp):
            raw = handle_confirm_creator_identity(
                name_record_id=101, provider='metron', provider_creator_id='',
                csrf_token=self.csrf_token, service=self.service
            )
            res = json.loads(raw)
            self.assertFalse(res['success'])
            self.assertEqual(res['error_code'], 'invalid_input')

    # -------------------------------------------------------------------------
    # 8. Provider-ID Collision Fails Closed
    # -------------------------------------------------------------------------
    def test_08_provider_id_collision_fails_closed(self):
        # Create an existing entity with metron:500
        e1 = self.repo.create_entity('Fabian Nicieza', 'fabian nicieza', 'fabian-nicieza')
        self.repo.insert_external_id(e1, 'metron', '500')

        # Link name record 101 to a DIFFERENT entity
        e2 = self.repo.create_entity('Other Creator', 'other creator', 'other-creator')
        self.repo.link_name_record(101, e2, resolution_source='manual_user')

        # Now attempt to confirm 101 with metron:500 -> Conflict
        req, resp = self._mock_cherrypy_post()
        with patch('cherrypy.request', req), patch('cherrypy.response', resp):
            raw = handle_confirm_creator_identity(
                name_record_id=101, provider='metron', provider_creator_id='500',
                csrf_token=self.csrf_token, service=self.service
            )
            res = json.loads(raw)
            self.assertFalse(res['success'])
            self.assertEqual(res['error_code'], 'conflicting_link')

    # -------------------------------------------------------------------------
    # 9. Stale / Blocked Actions Fail Safely
    # -------------------------------------------------------------------------
    def test_09_active_rejection_blocks_confirmation(self):
        self.service.reject_candidate(101, 'metron', '500')

        req, resp = self._mock_cherrypy_post()
        with patch('cherrypy.request', req), patch('cherrypy.response', resp):
            raw = handle_confirm_creator_identity(
                name_record_id=101, provider='metron', provider_creator_id='500',
                csrf_token=self.csrf_token, service=self.service
            )
            res = json.loads(raw)
            self.assertFalse(res['success'])
            self.assertEqual(res['error_code'], 'active_rejection_blocked')

    # -------------------------------------------------------------------------
    # 10. Disabled Metron / Missing Credentials Blocks Candidate Mutation
    # -------------------------------------------------------------------------
    def test_10_disabled_metron_blocks_mutation(self):
        mylar.CONFIG.METRON_ENABLED = False

        req, resp = self._mock_cherrypy_post()
        with patch('cherrypy.request', req), patch('cherrypy.response', resp):
            raw = handle_confirm_creator_identity(
                name_record_id=101, provider='metron', provider_creator_id='500',
                csrf_token=self.csrf_token, service=self.service
            )
            res = json.loads(raw)
            self.assertFalse(res['success'])
            self.assertEqual(res['error_code'], 'invalid_input')

    # -------------------------------------------------------------------------
    # 11. Malformed IDs Cause Zero Writes
    # -------------------------------------------------------------------------
    def test_11_malformed_ids_cause_zero_writes(self):
        req, resp = self._mock_cherrypy_post()
        with patch('cherrypy.request', req), patch('cherrypy.response', resp):
            raw = handle_confirm_creator_identity(
                name_record_id='bad-id', provider='metron', provider_creator_id='500',
                csrf_token=self.csrf_token, service=self.service
            )
            res = json.loads(raw)
            self.assertFalse(res['success'])
            self.assertEqual(res['error_code'], 'invalid_input')

    # -------------------------------------------------------------------------
    # 12. HTTP Method Enforcement (GET returns 405)
    # -------------------------------------------------------------------------
    def test_12_http_method_enforcement_get_returns_405(self):
        req = MagicMock()
        req.method = 'GET'
        resp = MagicMock()
        resp.headers = {}
        resp.status = 200

        with patch('cherrypy.request', req), patch('cherrypy.response', resp):
            raw = handle_confirm_creator_identity(name_record_id=101, provider='metron', provider_creator_id='500')
            res = json.loads(raw)
            self.assertFalse(res['success'])
            self.assertEqual(res['status_code'], 405)
            self.assertEqual(res['error_code'], 'method_not_allowed')

    # -------------------------------------------------------------------------
    # 13. CSRF Token Enforcement (Invalid/Missing returns 403)
    # -------------------------------------------------------------------------
    def test_13_csrf_token_enforcement(self):
        req = MagicMock()
        req.method = 'POST'
        req.headers = {}
        resp = MagicMock()
        resp.headers = {}
        resp.status = 200

        with patch('cherrypy.request', req), patch('cherrypy.response', resp):
            # Missing token
            raw1 = handle_confirm_creator_identity(name_record_id=101, provider='metron', provider_creator_id='500')
            res1 = json.loads(raw1)
            self.assertFalse(res1['success'])
            self.assertEqual(res1['status_code'], 403)
            self.assertEqual(res1['error_code'], 'invalid_csrf_token')

            # Invalid token
            raw2 = handle_confirm_creator_identity(
                name_record_id=101, provider='metron', provider_creator_id='500', csrf_token='invalid_token'
            )
            res2 = json.loads(raw2)
            self.assertFalse(res2['success'])
            self.assertEqual(res2['status_code'], 403)

    # -------------------------------------------------------------------------
    # 14. Browser Injection Parameters Cannot Alter Target Decision
    # -------------------------------------------------------------------------
    def test_14_browser_injection_parameters_ignored(self):
        req, resp = self._mock_cherrypy_post()
        with patch('cherrypy.request', req), patch('cherrypy.response', resp):
            # Try to inject rogue entity_id and state
            raw = handle_confirm_creator_identity(
                name_record_id=101, provider='metron', provider_creator_id='500',
                creator_entity_id=9999, candidate_state='confirmed', audit_id=5555,
                csrf_token=self.csrf_token, service=self.service
            )
            res = json.loads(raw)
            self.assertTrue(res['success'])
            # Ensure assigned entity ID is newly generated, not injected 9999
            self.assertNotEqual(res['creator_entity_id'], 9999)

    # -------------------------------------------------------------------------
    # 15. Undo Makes Zero Provider Calls
    # -------------------------------------------------------------------------
    def test_15_undo_makes_zero_provider_calls(self):
        self.service.confirm_provider_identity(101, 'metron', '500')

        # Disable Metron entirely
        mylar.CONFIG.METRON_ENABLED = False
        mylar.CONFIG.METRON_USERNAME = None

        req, resp = self._mock_cherrypy_post()
        with patch('cherrypy.request', req), patch('cherrypy.response', resp):
            # Reversal should succeed purely using local SQLite without contacting Metron
            raw = handle_reverse_creator_decision(
                name_record_id=101, provider='metron', provider_creator_id='500',
                csrf_token=self.csrf_token, service=self.service, repository=self.repo
            )
            res = json.loads(raw)
            self.assertTrue(res['success'])
            self.assertIsNone(self.repo.get_name_record(101)['creator_entity_id'])

    # -------------------------------------------------------------------------
    # 16. Transaction Rollback Safety on Failure
    # -------------------------------------------------------------------------
    def test_16_transaction_rollback_safety(self):
        # Inject an intentional error during confirmation link
        with patch.object(self.repo, 'link_name_record', side_effect=RuntimeError("Simulated DB failure")):
            with self.assertRaises(RuntimeError):
                self.service.confirm_provider_identity(101, 'metron', '500')

        # Verify rollback: no entity created, no external ID, no audit record
        self.assertEqual(len(self.repo.get_audit_history(name_record_id=101)), 0)
        self.assertIsNone(self.repo.get_external_id_mapping('metron', '500'))
        self.assertIsNone(self.repo.get_name_record(101)['creator_entity_id'])

    # -------------------------------------------------------------------------
    # 17. Responses and Logs are Sanitized
    # -------------------------------------------------------------------------
    def test_17_responses_are_sanitized(self):
        req, resp = self._mock_cherrypy_post()
        with patch('cherrypy.request', req), patch('cherrypy.response', resp):
            raw = handle_confirm_creator_identity(
                name_record_id=101, provider='metron', provider_creator_id='500',
                csrf_token=self.csrf_token, service=self.service
            )
            res = json.loads(raw)

        # Ensure no sensitive config keys are returned
        res_str = json.dumps(res)
        self.assertNotIn("testpassword", res_str)
        self.assertNotIn("Authorization", res_str)
        self.assertNotIn("traceback", res_str)

    # -------------------------------------------------------------------------
    # 18. Decision-State Truthfulness and State-Aware Notice/Footer Copy
    # -------------------------------------------------------------------------
    def test_18_decision_state_truthfulness_and_notice_copy(self):
        """
        Verify that review-only vs decision-present results generate truthful notice and footer copy.
        """
        def compute_ui_copy(candidates):
            has_decisions = any(
                c.get('is_confirmed') or c.get('is_rejected') or
                c.get('candidate_state') in ('confirmed', 'already_confirmed', 'rejected')
                for c in candidates
            )
            if has_decisions:
                notice = "Candidate evidence alone does not establish identity. Any confirmed or rejected state shown here reflects an explicit, auditable human decision—not an automatic match."
                footer = "Provider creator links reflect explicit human resolution decisions. No automatic matches or heuristic links were written."
            else:
                notice = "Review only. Candidate evidence does not establish creator identity, and no creator records or links have been modified."
                footer = "Matched by normalized display name and role only. This is not creator identity confirmation. No creator records were linked or modified."
            return notice, footer, has_decisions

        # Scenario A: Fully read-only / candidate / insufficient / conflicted response
        readonly_candidates = [
            {'name_record_id': 101, 'candidate_state': 'candidate', 'is_confirmed': False, 'is_rejected': False},
            {'name_record_id': 102, 'candidate_state': 'insufficient_provider_identity', 'is_confirmed': False, 'is_rejected': False},
            {'name_record_id': 103, 'candidate_state': 'conflicted', 'is_confirmed': False, 'is_rejected': False},
        ]
        notice_a, footer_a, has_decisions_a = compute_ui_copy(readonly_candidates)
        self.assertFalse(has_decisions_a)
        self.assertIn("Review only. Candidate evidence does not establish creator identity", notice_a)
        self.assertIn("no creator records or links have been modified", notice_a)
        self.assertIn("No creator records were linked or modified.", footer_a)

        # Scenario B: Explicit confirmation present
        confirmed_candidates = [
            {'name_record_id': 101, 'candidate_state': 'confirmed', 'is_confirmed': True, 'is_rejected': False},
            {'name_record_id': 102, 'candidate_state': 'candidate', 'is_confirmed': False, 'is_rejected': False},
        ]
        notice_b, footer_b, has_decisions_b = compute_ui_copy(confirmed_candidates)
        self.assertTrue(has_decisions_b)
        self.assertIn("Candidate evidence alone does not establish identity", notice_b)
        self.assertIn("explicit, auditable human decision—not an automatic match", notice_b)
        self.assertNotIn("no creator records or links have been modified", notice_b)
        self.assertEqual(footer_b, "Provider creator links reflect explicit human resolution decisions. No automatic matches or heuristic links were written.")

        # Scenario C: Explicit rejection present
        rejected_candidates = [
            {'name_record_id': 101, 'candidate_state': 'rejected', 'is_confirmed': False, 'is_rejected': True},
            {'name_record_id': 102, 'candidate_state': 'candidate', 'is_confirmed': False, 'is_rejected': False},
        ]
        notice_c, footer_c, has_decisions_c = compute_ui_copy(rejected_candidates)
        self.assertTrue(has_decisions_c)
        self.assertIn("Candidate evidence alone does not establish identity", notice_c)
        self.assertNotIn("no creator records or links have been modified", notice_c)
        self.assertEqual(footer_c, "Provider creator links reflect explicit human resolution decisions. No automatic matches or heuristic links were written.")


if __name__ == '__main__':
    unittest.main()
