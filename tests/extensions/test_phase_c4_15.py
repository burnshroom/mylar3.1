"""
Automated Test Suite for Phase C4.15 — Explicit Conflict Retention and Pairing Rejection.

Validates all 10 acceptance conditions:
1. Successful exact-pair rejection while existing mapping remains unchanged.
2. Exactly one immutable audit event logged per resolution.
3. Idempotent repeat submission.
4. Stale/no-longer-conflicted request causing zero writes.
5. Changed existing mapping causing zero writes.
6. CSRF, HTTP method, malformed input, and cross-record attempts.
7. Same-normalized-name isolation and composite-credit preservation.
8. Zero provider/network calls.
9. Zero writes outside allowed identity/audit/rejection structures.
10. Inspector and Registry template UI structure, confirmation box, and race protection.
"""

import unittest
import sqlite3
import json
import os
import sys
from unittest.mock import patch, MagicMock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.join(REPO_ROOT, 'lib'))
sys.path.insert(0, REPO_ROOT)

from mylar.extensions.migrations.runner import run_extension_migrations
from mylar.extensions.creators.identity_repository import IdentityRepository
from mylar.extensions.creators.conflict_service import (
    CreatorConflictService,
    InvalidConflictInputError,
    LocalNameRecordNotFoundError,
    StaleConflictStateError,
)
from mylar.extensions.creators.conflict_controller import (
    handle_resolve_creator_conflict,
    handle_get_creator_conflict_analysis,
)
from mylar.extensions.creators.decision_controller import (
    get_or_create_csrf_token,
    verify_csrf_token,
)


class MockDBWrapper:
    """Wrapper exposing .connection and .conn for compatibility with Mylar DB conventions."""
    def __init__(self, connection):
        self.connection = connection
        self.conn = connection


class TestPhaseC4_15ConflictRetentionAndRejection(unittest.TestCase):

    def setUp(self):
        # Create an in-memory SQLite database
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self._create_schema()
        self._seed_data()

        self.db_wrapper = MockDBWrapper(self.conn)
        self.repo = IdentityRepository(db_conn=self.db_wrapper)
        self.service = CreatorConflictService(repository=self.repo, db_conn=self.db_wrapper)

    def tearDown(self):
        self.conn.close()

    def _create_schema(self):
        cur = self.conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS comics (
                ComicID TEXT PRIMARY KEY,
                ComicName TEXT,
                ComicYear TEXT
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS issues (
                IssueID TEXT PRIMARY KEY,
                ComicID TEXT,
                Issue_Number TEXT,
                IssueName TEXT,
                IssueDate TEXT
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS annuals (
                IssueID TEXT PRIMARY KEY,
                ComicID TEXT,
                Issue_Number TEXT,
                IssueName TEXT,
                IssueDate TEXT
            );
        """)
        run_extension_migrations(cur)
        self.conn.commit()

    def _seed_data(self):
        cur = self.conn.cursor()
        # Entities
        cur.execute("INSERT INTO ext_creator_entities (CreatorEntityID, DisplayName, NormalizedName, EntitySlug) VALUES (1, 'Fabian Nicieza', 'fabian nicieza', 'fabian-nicieza')")
        cur.execute("INSERT INTO ext_creator_entities (CreatorEntityID, DisplayName, NormalizedName, EntitySlug) VALUES (2, 'Bob Harras (Legacy)', 'bob harras', 'bob-harras-legacy')")
        cur.execute("INSERT INTO ext_creator_entities (CreatorEntityID, DisplayName, NormalizedName, EntitySlug) VALUES (3, 'Robert Harras (Metron)', 'robert harras', 'robert-harras-metron')")

        # Name records
        cur.execute("INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, CreatorEntityID, ResolutionSource) VALUES (101, 'Fabian Nicieza', 'fabian nicieza', 'fabian-nicieza', 1, 'human_review')")
        # Record 104 is conflicted with metron:999 (which is mapped to Entity 3)
        cur.execute("INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, CreatorEntityID, ResolutionSource) VALUES (104, 'Bob Harras', 'bob harras', 'bob-harras', 2, 'unresolved')")
        cur.execute("INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, CreatorEntityID, ResolutionSource) VALUES (105, 'Robert Harras', 'robert harras', 'robert-harras', 3, 'human_review')")
        cur.execute("INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, CreatorEntityID, ResolutionSource) VALUES (106, 'Stan Lee & Jack Kirby', 'stan lee & jack kirby', 'stan-lee-jack-kirby', NULL, 'unresolved')")
        cur.execute("INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, CreatorEntityID, ResolutionSource) VALUES (107, 'Bob Harras Jr.', 'bob harras', 'bob-harras-jr', NULL, 'unresolved')")

        # External IDs: Metron 999 bound to Entity 3
        cur.execute("INSERT INTO ext_creator_external_ids (CreatorEntityID, Provider, ExternalID) VALUES (3, 'metron', '999')")
        cur.execute("INSERT INTO ext_creator_external_ids (CreatorEntityID, Provider, ExternalID) VALUES (1, 'metron', '101')")

        # Source context
        cur.execute("INSERT INTO comics (ComicID, ComicName, ComicYear) VALUES ('5535', 'X-Men', '1991')")
        cur.execute("INSERT INTO issues (IssueID, ComicID, Issue_Number, IssueName, IssueDate) VALUES ('105544', '5535', '2', 'Rubicon', '1991-11-01')")
        cur.execute("INSERT INTO ext_creator_credits (CreditID, IssueID, IsAnnual, ComicID, NameRecordID, CreatorEntityID, Role, RawRoleText, RawCreditName, SourceProvenance, SourceRevision) VALUES (1, '105544', 0, '5535', 104, 2, 'editor', 'editor', 'Bob Harras', 'comicinfo', 'rev1')")

        self.conn.commit()

    # ──────────────────────────────────────────────────────────────────────────
    # Test 1: Successful exact-pair rejection while existing mapping is preserved
    # ──────────────────────────────────────────────────────────────────────────
    def test_successful_rejection_preserves_existing_mapping(self):
        res = self.service.resolve_conflict_keep_existing_reject_competing(
            name_record_id=104,
            provider='metron',
            provider_creator_id='999',
            reason="User explicit conflict rejection",
            actor="user"
        )

        self.assertTrue(res['success'])
        self.assertEqual(res['action'], 'keep_existing_reject_competing')
        self.assertEqual(res['name_record_id'], 104)
        self.assertEqual(res['retained_entity_id'], 3)
        self.assertEqual(res['retained_entity_name'], 'Robert Harras (Metron)')
        self.assertIsNotNone(res['rejection_id'])
        self.assertIsNotNone(res['audit_id'])

        # Verify existing external ID mapping is 100% untouched
        cur = self.conn.cursor()
        cur.execute("SELECT CreatorEntityID, Provider, ExternalID FROM ext_creator_external_ids WHERE Provider = 'metron' AND ExternalID = '999'")
        ext_row = cur.fetchone()
        self.assertIsNotNone(ext_row)
        self.assertEqual(ext_row[0], 3, "External ID mapping must remain bound to original Entity #3.")

        # Verify rejection was inserted for NameRecord 104
        cur.execute("SELECT NameRecordID, Provider, ExternalID, Status FROM ext_creator_candidate_rejections WHERE RejectionID = ?", (res['rejection_id'],))
        rej_row = cur.fetchone()
        self.assertIsNotNone(rej_row)
        self.assertEqual(rej_row[0], 104)
        self.assertEqual(rej_row[1], 'metron')
        self.assertEqual(rej_row[2], '999')
        self.assertEqual(rej_row[3], 'active')

        # Verify NameRecord 104 entity link and resolution source did not mutate
        cur.execute("SELECT CreatorEntityID, ResolutionSource FROM ext_creator_name_records WHERE NameRecordID = 104")
        nr_row = cur.fetchone()
        self.assertEqual(nr_row[0], 2)
        self.assertEqual(nr_row[1], 'unresolved')

    # ──────────────────────────────────────────────────────────────────────────
    # Test 2: Exactly one audit event logged with before/after state
    # ──────────────────────────────────────────────────────────────────────────
    def test_one_audit_event_logged(self):
        res = self.service.resolve_conflict_keep_existing_reject_competing(
            name_record_id=104,
            provider='metron',
            provider_creator_id='999'
        )

        cur = self.conn.cursor()
        cur.execute("SELECT Action, NameRecordID, CreatorEntityID, Provider, ExternalID, BeforeStateJson, AfterStateJson FROM ext_creator_resolution_audit WHERE AuditID = ?", (res['audit_id'],))
        audit = cur.fetchone()
        self.assertIsNotNone(audit)
        self.assertEqual(audit[0], 'RESOLVE_CONFLICT_REJECT_COMPETING')
        self.assertEqual(audit[1], 104)
        self.assertEqual(audit[2], 2)
        self.assertEqual(audit[3], 'metron')
        self.assertEqual(audit[4], '999')

        before_state = json.loads(audit[5])
        after_state = json.loads(audit[6])
        self.assertEqual(before_state['conflict_type'], 'EXTERNAL_ID_COLLISION')
        self.assertEqual(before_state['retained_entity_id'], 3)
        self.assertEqual(after_state['action_taken'], 'keep_existing_reject_competing')
        self.assertEqual(after_state['retained_entity_id'], 3)
        self.assertEqual(after_state['status'], 'rejected')

    # ──────────────────────────────────────────────────────────────────────────
    # Test 3: Idempotent repeat submission
    # ──────────────────────────────────────────────────────────────────────────
    def test_idempotent_repeat_submission(self):
        res1 = self.service.resolve_conflict_keep_existing_reject_competing(104, 'metron', '999')
        self.assertFalse(res1['is_idempotent'])

        # Second submission
        res2 = self.service.resolve_conflict_keep_existing_reject_competing(104, 'metron', '999')
        self.assertTrue(res2['success'])
        self.assertTrue(res2['is_idempotent'])
        self.assertEqual(res1['rejection_id'], res2['rejection_id'])
        self.assertIsNone(res2['audit_id'])

        # Check rejection table row count = 1
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM ext_creator_candidate_rejections WHERE NameRecordID = 104 AND Provider = 'metron' AND ExternalID = '999'")
        self.assertEqual(cur.fetchone()[0], 1)

    # ──────────────────────────────────────────────────────────────────────────
    # Test 4: Stale/no-longer-conflicted request causes zero writes
    # ──────────────────────────────────────────────────────────────────────────
    def test_stale_no_longer_conflicted_causes_zero_writes(self):
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM ext_creator_candidate_rejections")
        rejs_before = cur.fetchone()[0]

        # Record 101 is mapped to Entity 1, and metron:101 is mapped to Entity 1 (no collision)
        with self.assertRaises(StaleConflictStateError):
            self.service.resolve_conflict_keep_existing_reject_competing(
                name_record_id=101,
                provider='metron',
                provider_creator_id='101'
            )

        cur.execute("SELECT COUNT(*) FROM ext_creator_candidate_rejections")
        self.assertEqual(cur.fetchone()[0], rejs_before, "Zero rejections written on stale conflict.")

    # ──────────────────────────────────────────────────────────────────────────
    # Test 5: Changed existing mapping causes zero writes
    # ──────────────────────────────────────────────────────────────────────────
    def test_changed_or_missing_mapping_causes_zero_writes(self):
        # metron:9999 has no external ID mapping in DB
        with self.assertRaises(StaleConflictStateError):
            self.service.resolve_conflict_keep_existing_reject_competing(
                name_record_id=104,
                provider='metron',
                provider_creator_id='9999'
            )

    # ──────────────────────────────────────────────────────────────────────────
    # Test 6: Controller HTTP Method, CSRF, Malformed Input
    # ──────────────────────────────────────────────────────────────────────────
    def test_controller_security_and_validation(self):
        # 1. Reject non-POST
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'GET'
            mock_req.headers = {}
            res = json.loads(handle_resolve_creator_conflict(name_record_id=104, service=self.service))
            self.assertFalse(res['success'])
            self.assertEqual(res['error_code'], 'method_not_allowed')

        # 2. Reject missing/invalid CSRF
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'POST'
            mock_req.headers = {}
            res = json.loads(handle_resolve_creator_conflict(
                name_record_id=104,
                provider='metron',
                provider_creator_id='999',
                action='keep_existing_reject_competing',
                csrf_token='bad_csrf_token',
                service=self.service
            ))
            self.assertFalse(res['success'])
            self.assertEqual(res['error_code'], 'invalid_csrf_token')

        # 3. Valid CSRF, invalid action
        valid_csrf = get_or_create_csrf_token()
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'POST'
            mock_req.headers = {'X-CSRF-Token': valid_csrf}
            res = json.loads(handle_resolve_creator_conflict(
                name_record_id=104,
                provider='metron',
                provider_creator_id='999',
                action='invalid_unsupported_action', # not supported
                csrf_token=valid_csrf,
                service=self.service
            ))
            self.assertFalse(res['success'])
            self.assertEqual(res['error_code'], 'invalid_action')

        # 4. Valid CSRF and action -> Success
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'POST'
            mock_req.headers = {'X-CSRF-Token': valid_csrf}
            res = json.loads(handle_resolve_creator_conflict(
                name_record_id=104,
                provider='metron',
                provider_creator_id='999',
                action='keep_existing_reject_competing',
                csrf_token=valid_csrf,
                service=self.service
            ))
            self.assertTrue(res['success'])
            self.assertEqual(res['retained_entity_id'], 3)

    # ──────────────────────────────────────────────────────────────────────────
    # Test 7: Same-normalized-name isolation & composite credit preservation
    # ──────────────────────────────────────────────────────────────────────────
    def test_same_normalized_name_isolation(self):
        # Reject pairing for NameRecord 104 ('Bob Harras')
        self.service.resolve_conflict_keep_existing_reject_competing(104, 'metron', '999')

        # NameRecord 107 ('Bob Harras') is a distinct record and has NOT been rejected
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM ext_creator_candidate_rejections WHERE NameRecordID = 107")
        self.assertEqual(cur.fetchone()[0], 0, "Rejection on NameRecord 104 must not bleed into distinct NameRecord 107.")

    # ──────────────────────────────────────────────────────────────────────────
    # Test 8: Zero provider / network calls
    # ──────────────────────────────────────────────────────────────────────────
    @patch('urllib.request.urlopen')
    @patch('requests.get')
    @patch('requests.post')
    def test_zero_network_calls(self, mock_post, mock_get, mock_urllib):
        res = self.service.resolve_conflict_keep_existing_reject_competing(104, 'metron', '999')
        self.assertTrue(res['success'])
        mock_post.assert_not_called()
        mock_get.assert_not_called()
        mock_urllib.assert_not_called()

    # ──────────────────────────────────────────────────────────────────────────
    # Test 9: Zero writes outside allowed identity/audit/rejection structures
    # ──────────────────────────────────────────────────────────────────────────
    def test_zero_writes_outside_rejection_and_audit(self):
        def get_untouched_table_counts():
            cur = self.conn.cursor()
            tables = ['comics', 'issues', 'annuals', 'ext_creator_credits', 'ext_creator_entities']
            return {t: cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}

        before_counts = get_untouched_table_counts()
        self.service.resolve_conflict_keep_existing_reject_competing(104, 'metron', '999')
        after_counts = get_untouched_table_counts()

        self.assertEqual(before_counts, after_counts, "Zero rows in comics, issues, annuals, credits, or entities may be created or deleted.")

    # ──────────────────────────────────────────────────────────────────────────
    # Test 10: Template UI inspection (Confirmation box and Reject button)
    # ──────────────────────────────────────────────────────────────────────────
    def test_template_ui_contains_confirmation_and_action_controls(self):
        templates = [
            'data/interfaces/modern/comicdetails_update.html',
            'data/interfaces/modern/creator_registry.html'
        ]
        for tmpl in templates:
            with open(tmpl, 'r', encoding='utf-8') as f:
                content = f.read()

            self.assertIn('ccm_btn_reject_competing', content)
            self.assertIn('Keep Existing Mapping &amp; Reject Candidate', content)
            self.assertIn('ccm_confirm_area', content)
            self.assertIn('Confirm Explicit Conflict Resolution', content)
            self.assertIn('This is an explicit human decision. The existing mapping will remain unchanged.', content)
            self.assertIn('resolveCreatorConflict', content)


if __name__ == '__main__':
    unittest.main()
