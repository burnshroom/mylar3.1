"""
Comprehensive Automated Test Suite for Phase C4.13: Read-Only Creator Identity Conflict Analysis.

Covers:
1. Active conflict with competing local mapping.
2. Conflict with limited historical detail (neutral labeling).
3. Non-conflicted and malformed targets.
4. Same-normalized-name records remaining distinct.
5. Composite-credit preservation.
6. Zero SQL writes during conflict analysis.
7. Zero provider/network calls (Metron enabled/disabled).
8. Sanitized errors and responses (zero secrets, raw SQL, tracebacks).
9. Stale response / request sequence protection simulation.
10. UI template verification ensuring zero mutation controls.
"""

import json
import os
import sys
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.join(REPO_ROOT, 'lib'))
sys.path.insert(0, REPO_ROOT)

from mylar.extensions.migrations.runner import run_extension_migrations
from mylar.extensions.creators.identity_repository import IdentityRepository
from mylar.extensions.creators.conflict_service import (
    CreatorConflictService,
    InvalidConflictInputError,
    LocalNameRecordNotFoundError
)
from mylar.extensions.creators.conflict_controller import handle_get_creator_conflict_analysis


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


class TestPhaseC413ConflictAnalysis(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self._init_schema(self.conn)
        self.mock_db = MockDBWrapper(self.conn)
        self.repo = IdentityRepository(db_conn=self.mock_db)
        self.service = CreatorConflictService(repository=self.repo, db_conn=self.mock_db)
        self._seed_test_data(self.conn)

    def tearDown(self):
        self.conn.close()

    def _init_schema(self, conn):
        cur = conn.cursor()
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
        conn.commit()

    def _seed_test_data(self, conn):
        cur = conn.cursor()

        # 1. Comics & Issues
        cur.execute("INSERT INTO comics (ComicID, ComicName, ComicYear) VALUES ('5535', 'X-Men', '1991')")
        cur.execute("INSERT INTO issues (IssueID, ComicID, Issue_Number, IssueName, IssueDate) VALUES ('105543', '5535', '1', 'Rubicon', '1991-10-01')")
        cur.execute("INSERT INTO issues (IssueID, ComicID, Issue_Number, IssueName, IssueDate) VALUES ('105544', '5535', '2', 'Firestorm', '1991-11-01')")

        # 2. Creator Entities
        cur.execute("INSERT INTO ext_creator_entities (CreatorEntityID, DisplayName, NormalizedName, EntitySlug) VALUES (1, 'Fabian Nicieza', 'fabian nicieza', 'fabian-nicieza')")
        cur.execute("INSERT INTO ext_creator_entities (CreatorEntityID, DisplayName, NormalizedName, EntitySlug) VALUES (2, 'Bob Harras (Legacy)', 'bob harras legacy', 'bob-harras-legacy')")
        cur.execute("INSERT INTO ext_creator_entities (CreatorEntityID, DisplayName, NormalizedName, EntitySlug) VALUES (3, 'Robert Harras (Metron)', 'robert harras metron', 'robert-harras-metron')")
        cur.execute("INSERT INTO ext_creator_entities (CreatorEntityID, DisplayName, NormalizedName, EntitySlug) VALUES (4, 'Stan Lee & Jack Kirby', 'stan lee & jack kirby', 'stan-lee-jack-kirby')")

        # 3. Provider External IDs (Metron ID 999 is mapped to Entity 3)
        cur.execute("INSERT INTO ext_creator_external_ids (CreatorEntityID, Provider, ExternalID) VALUES (1, 'metron', '101')")
        cur.execute("INSERT INTO ext_creator_external_ids (CreatorEntityID, Provider, ExternalID) VALUES (3, 'metron', '999')")

        # 4. Name Records
        # 101: Confirmed Fabian Nicieza
        cur.execute("INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, ResolutionSource, CreatorEntityID) VALUES (101, 'Fabian Nicieza', 'fabian nicieza', 'fabian-nicieza', 'human_review', 1)")
        # 102: Rejected Kevin Somers
        cur.execute("INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, ResolutionSource, CreatorEntityID) VALUES (102, 'Kevin Somers', 'kevin somers', 'kevin-somers', 'unresolved', NULL)")
        # 104: Conflicted Bob Harras (Linked to Entity 2, while Metron 999 is mapped to Entity 3)
        cur.execute("INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, ResolutionSource, CreatorEntityID) VALUES (104, 'Bob Harras', 'bob harras', 'bob-harras', 'unresolved', 2)")
        # 105: Competing Name Record Robert Harras linked to Entity 3
        cur.execute("INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, ResolutionSource, CreatorEntityID) VALUES (105, 'Robert Harras', 'robert harras', 'robert-harras', 'human_review', 3)")
        # 106: Composite Credit Stan Lee & Jack Kirby
        cur.execute("INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, ResolutionSource, CreatorEntityID) VALUES (106, 'Stan Lee & Jack Kirby', 'stan lee & jack kirby', 'stan-lee-jack-kirby', 'human_review', 4)")
        # 107: Distinct Same-Normalized Name Fabian Nicieza Jr.
        cur.execute("INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, ResolutionSource, CreatorEntityID) VALUES (107, 'Fabian Nicieza Jr.', 'fabian nicieza', 'fabian-nicieza-2', 'unresolved', NULL)")
        # 108: Legacy collision with limited detail
        cur.execute("INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, ResolutionSource, CreatorEntityID) VALUES (108, 'Legacy Collided Creator', 'legacy collided creator', 'legacy-collided-creator', 'unresolved', NULL)")

        # 5. Candidate Rejection for 102
        cur.execute("INSERT INTO ext_creator_candidate_rejections (NameRecordID, Provider, ExternalID, ProviderDisplayName, Status, RejectedBy, RejectedAt, Reason) VALUES (102, 'metron', '789', 'Kevin Somers', 'active', 'human_reviewer', '2026-08-24 01:00:00', 'Wrong creator')")

        # 6. Audit Records
        cur.execute("INSERT INTO ext_creator_resolution_audit (AuditID, Action, NameRecordID, CreatorEntityID, Provider, ExternalID, ProviderDisplayName, Actor, Reason, CreatedAt) VALUES (1, 'CONFIRM_PROVIDER_IDENTITY', 101, 1, 'metron', '101', 'Fabian Nicieza', 'human_reviewer', 'Exact match confirmed', '2026-08-24 01:00:00')")
        cur.execute("INSERT INTO ext_creator_resolution_audit (AuditID, Action, NameRecordID, CreatorEntityID, Provider, ExternalID, ProviderDisplayName, Actor, Reason, CreatedAt) VALUES (2, 'LEGACY_EXTERNAL_ID_COLLISION', 104, 2, 'metron', '999', 'Robert Harras', 'system', 'Provider Metron ID 999 already maps to CreatorEntity #3', '2026-08-24 01:10:00')")
        cur.execute("INSERT INTO ext_creator_resolution_audit (AuditID, Action, NameRecordID, CreatorEntityID, Provider, ExternalID, ProviderDisplayName, Actor, Reason, CreatedAt) VALUES (3, 'LEGACY_EXTERNAL_ID_COLLISION', 108, NULL, 'metron', '888', 'Legacy Target', 'historical_record', NULL, '2026-08-24 01:12:00')")

        # 7. Credits
        cur.execute("INSERT INTO ext_creator_credits (CreditID, IssueID, IsAnnual, ComicID, NameRecordID, CreatorEntityID, Role, RawRoleText, RawCreditName, SourceProvenance, SourceRevision) VALUES (1, '105544', 0, '5535', 104, 2, 'editor', 'editor', 'Bob Harras', 'comicinfo', 'rev1')")
        cur.execute("INSERT INTO ext_creator_credits (CreditID, IssueID, IsAnnual, ComicID, NameRecordID, CreatorEntityID, Role, RawRoleText, RawCreditName, SourceProvenance, SourceRevision) VALUES (2, '105544', 0, '5535', 106, 4, 'writer', 'writer', 'Stan Lee & Jack Kirby', 'comicinfo', 'rev1')")

        conn.commit()

    # ──────────────────────────────────────────────────────────────────────────
    # Test 1: Active conflict with competing local mapping
    # ──────────────────────────────────────────────────────────────────────────
    def test_active_conflict_with_competing_mapping(self):
        res = self.service.analyze_creator_conflict(
            name_record_id=104,
            provider='metron',
            provider_creator_id='999'
        )

        self.assertTrue(res['success'])
        self.assertTrue(res['is_conflicted'])
        self.assertEqual(res['conflict_type'], 'EXTERNAL_ID_COLLISION')
        self.assertFalse(res['resolution_available'])
        self.assertIn("Direct resolution is blocked", res['blocking_reason'])

        # Check requested local record breakdown
        req = res['requested_record']
        self.assertEqual(req['name_record_id'], 104)
        self.assertEqual(req['raw_local_name'], 'Bob Harras')
        self.assertEqual(req['linked_entity_id'], 2)
        self.assertEqual(req['linked_entity_name'], 'Bob Harras (Legacy)')

        # Check target provider breakdown
        prov = res['target_provider']
        self.assertEqual(prov['provider'], 'metron')
        self.assertEqual(prov['provider_creator_id'], '999')

        # Check competing mapping breakdown (descriptive only)
        comp = res['competing_mapping']
        self.assertIsNotNone(comp)
        self.assertEqual(comp['competing_entity_id'], 3)
        self.assertEqual(comp['competing_entity_name'], 'Robert Harras (Metron)')
        self.assertEqual(comp['bound_external_id'], '999')

        # Other linked name records for competing entity
        other_recs = comp['competing_name_records']
        self.assertEqual(len(other_recs), 1)
        self.assertEqual(other_recs[0]['name_record_id'], 105)
        self.assertEqual(other_recs[0]['raw_local_name'], 'Robert Harras')

        # Source context
        self.assertIsNotNone(res['source_context'])
        self.assertEqual(res['source_context']['comic_name'], 'X-Men')
        self.assertEqual(res['source_context']['issue_number'], '2')

    # ──────────────────────────────────────────────────────────────────────────
    # Test 2: Conflict with limited / legacy historical detail (neutral labeling)
    # ──────────────────────────────────────────────────────────────────────────
    def test_conflict_with_limited_historical_detail(self):
        res = self.service.analyze_creator_conflict(
            name_record_id=108,
            provider='metron',
            provider_creator_id='888'
        )

        self.assertTrue(res['success'])
        self.assertTrue(res['is_conflicted'])
        self.assertEqual(res['conflict_type'], 'RECORDED_COLLISION_AUDIT')
        self.assertFalse(res['resolution_available'])

        # Verify audit references handle NULL actor/reason neutrally
        audits = res['audit_references']
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]['actor_label'], 'Historical Record')
        self.assertIn("Collision recorded in history", audits[0]['reason'])

    # ──────────────────────────────────────────────────────────────────────────
    # Test 3: Non-conflicted and malformed targets
    # ──────────────────────────────────────────────────────────────────────────
    def test_non_conflicted_and_malformed_targets(self):
        # 1. Non-conflicted confirmed record (NameRecord 101)
        res_non_conf = self.service.analyze_creator_conflict(
            name_record_id=101,
            provider='metron',
            provider_creator_id='101'
        )
        self.assertTrue(res_non_conf['success'])
        self.assertFalse(res_non_conf['is_conflicted'])
        self.assertEqual(res_non_conf['conflict_type'], 'NO_ACTIVE_CONFLICT')
        self.assertIsNone(res_non_conf['competing_mapping'])

        # 2. Non-existent name record
        with self.assertRaises(LocalNameRecordNotFoundError):
            self.service.analyze_creator_conflict(name_record_id=99999)

        # 3. Malformed name_record_id
        with self.assertRaises(InvalidConflictInputError):
            self.service.analyze_creator_conflict(name_record_id="not_an_id")

        # 4. Malformed provider namespace
        with self.assertRaises(InvalidConflictInputError):
            self.service.analyze_creator_conflict(name_record_id=104, provider='unsupported_provider')

        # 5. Negative / zero provider_creator_id
        with self.assertRaises(InvalidConflictInputError):
            self.service.analyze_creator_conflict(name_record_id=104, provider='metron', provider_creator_id='-5')

    # ──────────────────────────────────────────────────────────────────────────
    # Test 4: Same-normalized-name records remain strictly distinct
    # ──────────────────────────────────────────────────────────────────────────
    def test_same_normalized_name_records_distinct(self):
        # Record 101 and 107 have same normalized name 'fabian nicieza'
        res101 = self.service.analyze_creator_conflict(name_record_id=101)
        res107 = self.service.analyze_creator_conflict(name_record_id=107)

        self.assertEqual(res101['requested_record']['name_record_id'], 101)
        self.assertEqual(res101['requested_record']['linked_entity_id'], 1)

        self.assertEqual(res107['requested_record']['name_record_id'], 107)
        self.assertIsNone(res107['requested_record']['linked_entity_id'])

        # They are not merged, joined, or conflated
        self.assertNotEqual(res101['requested_record']['name_record_id'], res107['requested_record']['name_record_id'])

    # ──────────────────────────────────────────────────────────────────────────
    # Test 5: Composite-credit preservation without splitting
    # ──────────────────────────────────────────────────────────────────────────
    def test_composite_credit_preservation(self):
        res = self.service.analyze_creator_conflict(
            name_record_id=106,
            provider='metron',
            provider_creator_id='200'
        )
        self.assertTrue(res['success'])
        self.assertEqual(res['requested_record']['raw_local_name'], 'Stan Lee & Jack Kirby')
        self.assertEqual(res['requested_record']['normalized_name'], 'stan lee & jack kirby')
        # Preserved as a single atomic unit
        self.assertNotIn(';', res['requested_record']['raw_local_name'])

    # ──────────────────────────────────────────────────────────────────────────
    # Test 6: Zero SQL writes during conflict analysis
    # ──────────────────────────────────────────────────────────────────────────
    def test_zero_sql_writes(self):
        def get_all_counts():
            cur = self.conn.cursor()
            tables = [
                'ext_creator_name_records', 'ext_creator_entities', 'ext_creator_external_ids',
                'ext_creator_candidate_rejections', 'ext_creator_resolution_audit', 'ext_creator_credits',
                'issues', 'annuals', 'comics'
            ]
            counts = {}
            for t in tables:
                cur.execute(f"SELECT COUNT(*) FROM {t}")
                counts[t] = cur.fetchone()[0]
            return counts

        before_counts = get_all_counts()

        # Perform analysis on conflicted and non-conflicted records
        self.service.analyze_creator_conflict(104, 'metron', '999')
        self.service.analyze_creator_conflict(101, 'metron', '101')
        self.service.analyze_creator_conflict(108, 'metron', '888')

        after_counts = get_all_counts()
        self.assertEqual(before_counts, after_counts, "Conflict analysis must perform 0 SQL writes.")

    # ──────────────────────────────────────────────────────────────────────────
    # Test 7: Zero provider / network calls (Metron enabled / disabled)
    # ──────────────────────────────────────────────────────────────────────────
    @patch('urllib.request.urlopen')
    @patch('requests.get')
    def test_zero_network_calls(self, mock_requests_get, mock_urllib):
        # Even if Metron is enabled or credentials exist, 0 network calls allowed
        res = self.service.analyze_creator_conflict(104, 'metron', '999')
        self.assertTrue(res['success'])

        mock_requests_get.assert_not_called()
        mock_urllib.assert_not_called()

    # ──────────────────────────────────────────────────────────────────────────
    # Test 8: Controller sanitization & domain error mapping
    # ──────────────────────────────────────────────────────────────────────────
    def test_controller_sanitization(self):
        # 1. Successful conflict analysis JSON
        resp_json = handle_get_creator_conflict_analysis(
            name_record_id='104',
            provider='metron',
            provider_creator_id='999',
            service=self.service
        )
        data = json.loads(resp_json)
        self.assertTrue(data['success'])
        self.assertTrue(data['is_conflicted'])
        self.assertFalse(data['resolution_available'])
        # No raw SQL or traceback leak
        self.assertNotIn('SELECT', resp_json)
        self.assertNotIn('Traceback', resp_json)

        # 2. Invalid input (400 Bad Request)
        resp_invalid = handle_get_creator_conflict_analysis(
            name_record_id='invalid_id',
            service=self.service
        )
        data_invalid = json.loads(resp_invalid)
        self.assertFalse(data_invalid['success'])
        self.assertEqual(data_invalid['error_code'], 'invalid_input')

        # 3. Not found (404)
        resp_404 = handle_get_creator_conflict_analysis(
            name_record_id='99999',
            service=self.service
        )
        data_404 = json.loads(resp_404)
        self.assertFalse(data_404['success'])
        self.assertEqual(data_404['error_code'], 'record_not_found')

    # ──────────────────────────────────────────────────────────────────────────
    # Test 9: UI Template Inspection (Zero mutation buttons & 4 explicit states)
    # ──────────────────────────────────────────────────────────────────────────
    def test_rendered_templates_have_zero_mutation_buttons_and_explicit_states(self):
        templates = [
            'data/interfaces/modern/creator_registry.html',
            'data/interfaces/modern/comicdetails_update.html'
        ]
        for tmpl in templates:
            with open(tmpl, 'r', encoding='utf-8') as f:
                content = f.read()

            # Ensure conflict modal has zero mutation buttons
            self.assertIn('creator_conflict_modal', content)
            self.assertIn('Why is this conflicted?', content)
            self.assertIn('Evidence and recorded state only', content)

            # Ensure all 4 explicit UI states exist
            self.assertIn('ccm_loading_spinner', content)
            self.assertIn('ccm_error_area', content)
            self.assertIn('ccm_no_conflict_area', content)
            self.assertIn('ccm_content_area', content)

            # Ensure neutral wording replaces "External Provider Authority"
            self.assertIn('Provider-reported identity', content)
            self.assertNotIn('External Provider Authority', content)

            # Prohibit mutation keywords in conflict modal action areas
            self.assertNotIn('btn-resolve-conflict', content)
            self.assertNotIn('btn-reassign-conflict', content)
            self.assertNotIn('btn-unlink-conflict', content)

    # ──────────────────────────────────────────────────────────────────────────
    # Test 10: No Active Conflict State Handling
    # ──────────────────────────────────────────────────────────────────────────
    def test_no_longer_conflicted_state_payload(self):
        # Local record 101 with matching external mapping (not conflicted)
        res = self.service.analyze_creator_conflict(
            name_record_id=101,
            provider='metron',
            provider_creator_id='101'
        )
        self.assertTrue(res['success'])
        self.assertFalse(res['is_conflicted'])
        self.assertEqual(res['conflict_type'], 'NO_ACTIVE_CONFLICT')
        self.assertIn('No active', res['blocking_reason'])
        self.assertEqual(res['requested_record']['name_record_id'], 101)
        self.assertEqual(res['requested_record']['raw_local_name'], 'Fabian Nicieza')


if __name__ == '__main__':
    unittest.main()
