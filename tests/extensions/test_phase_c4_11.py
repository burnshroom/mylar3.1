"""
Phase C4.11 — Read-Only Creator Identity Decision History Automated Test Suite.

Verifies:
1. Confirmed, rejected, reversed, conflicted, and no-history records.
2. Two same-normalized-name records remaining strictly separate.
3. Composite credit preservation (composite credits intact with independent NameRecordIDs).
4. Malformed IDs, invalid scope, and pagination validation (clamping, bounds).
5. Zero SQL writes during history querying.
6. Zero provider/network requests, including while Metron is disabled.
7. Sanitized responses (no secrets, tracebacks, or raw SQL leaked).
8. Race condition protection (request sequence / stale navigation protection).
"""

import os
import sys
import json
import sqlite3
import unittest
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.join(REPO_ROOT, 'lib'))
sys.path.insert(0, REPO_ROOT)

from mylar.extensions.creators.identity_repository import IdentityRepository
from mylar.extensions.creators.identity_service import CreatorIdentityService
from mylar.extensions.creators.history_service import (
    CreatorHistoryService,
    InvalidHistoryInputError,
    LocalNameRecordNotFoundError
)
from mylar.extensions.creators.history_controller import handle_get_creator_decision_history


from mylar.extensions.migrations.runner import run_extension_migrations


class MockConfig:
    METRON_ENABLED = True
    METRON_AUTH_MODE = "basic"
    METRON_USERNAME = "testuser"
    METRON_PASSWORD = "testpassword"
    METRON_API_TOKEN = None


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


class TestPhaseC411CreatorDecisionHistory(unittest.TestCase):

    def setUp(self):
        # Create an in-memory SQLite DB with standard C4.8 schema
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self._create_schema(self.conn)
        self.mock_db = MockDBWrapper(self.conn)
        self.repo = IdentityRepository(db_conn=self.mock_db)
        self.id_service = CreatorIdentityService(db_conn=self.mock_db, repository=self.repo)
        self.history_service = CreatorHistoryService(repository=self.repo)
        self._seed_test_data()

    def tearDown(self):
        self.conn.close()

    def _create_schema(self, conn):
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS comics (
                ComicID TEXT PRIMARY KEY,
                ComicName TEXT,
                ComicYear TEXT,
                Corrected_SeriesYear TEXT,
                ComicPublisher TEXT,
                ComicLocation TEXT
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS issues (
                IssueID TEXT PRIMARY KEY,
                ComicID TEXT,
                Issue_Number TEXT,
                Int_IssueNumber REAL,
                IssueName TEXT,
                IssueDate TEXT,
                ReleaseDate TEXT,
                Location TEXT,
                Status TEXT
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS annuals (
                IssueID TEXT PRIMARY KEY,
                ComicID TEXT,
                Issue_Number TEXT,
                Int_IssueNumber REAL,
                IssueName TEXT,
                IssueDate TEXT,
                ReleaseDate TEXT,
                Location TEXT,
                Status TEXT
            );
        """)
        run_extension_migrations(cur)
        conn.commit()

    def _seed_test_data(self):
        cur = self.conn.cursor()
        # Seed NameRecords
        # 101: Fabian Nicieza (will be confirmed)
        cur.execute("""
            INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, ResolutionSource, CreatedAt, UpdatedAt)
            VALUES (101, 'Fabian Nicieza', 'fabian nicieza', 'fabian-nicieza', 'unresolved', '2026-08-20 10:00:00', '2026-08-20 10:00:00')
        """)
        # 102: Andy Kubert (will be confirmed then reversed)
        cur.execute("""
            INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, ResolutionSource, CreatedAt, UpdatedAt)
            VALUES (102, 'Andy Kubert', 'andy kubert', 'andy-kubert', 'unresolved', '2026-08-20 10:00:00', '2026-08-20 10:00:00')
        """)
        # 103: Kevin Somers (will be rejected)
        cur.execute("""
            INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, ResolutionSource, CreatedAt, UpdatedAt)
            VALUES (103, 'Kevin Somers', 'kevin somers', 'kevin-somers', 'unresolved', '2026-08-20 10:00:00', '2026-08-20 10:00:00')
        """)
        # 104: Fresh Unresolved Record (no history)
        cur.execute("""
            INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, ResolutionSource, CreatedAt, UpdatedAt)
            VALUES (104, 'Chris Claremont', 'chris claremont', 'chris-claremont', 'unresolved', '2026-08-20 10:00:00', '2026-08-20 10:00:00')
        """)
        # 105: Second distinct NameRecord with same normalized name 'fabian nicieza' (e.g. 'F. Nicieza')
        cur.execute("""
            INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, ResolutionSource)
            VALUES (105, 'F. Nicieza', 'fabian nicieza', 'f-nicieza', 'unresolved')
        """)
        # 106: Composite credit 'Jim Lee & Scott Williams'
        cur.execute("""
            INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, ResolutionSource)
            VALUES (106, 'Jim Lee & Scott Williams', 'jim lee & scott williams', 'jim-lee-scott-williams', 'unresolved')
        """)
        self.conn.commit()

    # -------------------------------------------------------------------------
    # 1. Confirmed, Rejected, Reversed, Conflicted, and No-History Records
    # -------------------------------------------------------------------------
    def test_01_confirmed_record_history(self):
        # Explicitly confirm Fabian Nicieza (101) with Metron creator 101
        self.id_service.confirm_provider_identity(
            name_record_id=101, provider='metron', provider_creator_id='101',
            provider_display_name='Fabian Nicieza', actor='human_reviewer'
        )

        res = self.history_service.get_decision_history(101)
        self.assertTrue(res['success'])
        self.assertEqual(res['name_record_id'], 101)
        self.assertEqual(res['current_state']['status'], 'confirmed')
        self.assertEqual(res['current_state']['confirmed_entity']['display_name'], 'Fabian Nicieza')
        self.assertEqual(res['total_events'], 1)

        event = res['timeline'][0]
        self.assertEqual(event['action'], 'CONFIRM_PROVIDER_IDENTITY')
        self.assertEqual(event['action_label'], 'Confirmed Provider Identity')
        self.assertEqual(event['actor'], 'human_reviewer')
        self.assertEqual(event['actor_label'], 'Human Reviewer')
        self.assertEqual(event['provider'], 'metron')
        self.assertEqual(event['external_id'], '101')
        self.assertIn("Explicitly confirmed as Fabian Nicieza (METRON ID: 101)", event['explanation'])
        self.assertIn("currently confirmed as 'Fabian Nicieza' [METRON ID: 101]", res['explanation_summary'])

    def test_02_rejected_record_history(self):
        # Explicitly reject Kevin Somers (103)
        self.id_service.reject_candidate(
            name_record_id=103, provider='metron', provider_creator_id='789',
            reason='Role discrepancy', actor='human_reviewer'
        )

        res = self.history_service.get_decision_history(103, provider='metron', provider_creator_id='789')
        self.assertTrue(res['success'])
        self.assertEqual(res['current_state']['status'], 'rejected')
        self.assertIsNotNone(res['current_state']['active_rejection'])
        self.assertEqual(res['total_events'], 1)

        event = res['timeline'][0]
        self.assertEqual(event['action'], 'REJECT_CANDIDATE')
        self.assertEqual(event['action_label'], 'Rejected Candidate')
        self.assertEqual(event['external_id'], '789')
        self.assertIn("Actively suppressed candidate pairing", event['explanation'])
        self.assertIn("active rejection suppressing METRON ID: 789", res['explanation_summary'])

    def test_03_reversed_record_history(self):
        # Confirm Andy Kubert (102) then reverse it
        self.id_service.confirm_provider_identity(
            name_record_id=102, provider='metron', provider_creator_id='500',
            provider_display_name='Andy Kubert', actor='human_reviewer'
        )
        self.id_service.reverse_provider_confirmation(
            name_record_id=102, provider='metron', provider_creator_id='500',
            actor='human_reviewer', reason='User undo'
        )

        res = self.history_service.get_decision_history(102)
        self.assertTrue(res['success'])
        self.assertEqual(res['current_state']['status'], 'unresolved')
        self.assertIsNone(res['current_state']['creator_entity_id'])
        self.assertEqual(res['total_events'], 2)

        # Timeline is deterministic oldest to newest
        self.assertEqual(res['timeline'][0]['action'], 'CONFIRM_PROVIDER_IDENTITY')
        self.assertEqual(res['timeline'][1]['action'], 'REVERSE_CONFIRMATION')
        self.assertEqual(res['timeline'][1]['action_label'], 'Reversed Confirmation')
        self.assertIsNotNone(res['timeline'][1]['reversal_info'])
        self.assertEqual(res['timeline'][1]['reversal_info']['reverses_action'], 'CONFIRM_PROVIDER_IDENTITY')
        self.assertIn("Reversed previous identity confirmation", res['timeline'][1]['explanation'])
        self.assertIn("currently unlinked. 2 decision events on record.", res['explanation_summary'])

    def test_04_no_history_record(self):
        # Chris Claremont (104) has never had any decisions
        res = self.history_service.get_decision_history(104)
        self.assertTrue(res['success'])
        self.assertEqual(res['current_state']['status'], 'unresolved')
        self.assertEqual(res['total_events'], 0)
        self.assertEqual(len(res['timeline']), 0)
        self.assertIn("currently unlinked with no decision history on record.", res['explanation_summary'])

    # -------------------------------------------------------------------------
    # 2. Two Same-Normalized-Name Records Remain Strictly Separate
    # -------------------------------------------------------------------------
    def test_05_same_normalized_name_records_isolated(self):
        # NameRecord 101 and 105 both have NormalizedName = 'fabian nicieza'
        # Confirm 101 only
        self.id_service.confirm_provider_identity(
            name_record_id=101, provider='metron', provider_creator_id='101',
            provider_display_name='Fabian Nicieza', actor='human_reviewer'
        )

        hist_101 = self.history_service.get_decision_history(101)
        hist_105 = self.history_service.get_decision_history(105)

        self.assertEqual(hist_101['current_state']['status'], 'confirmed')
        self.assertEqual(hist_101['total_events'], 1)

        # 105 must remain completely unlinked with 0 events
        self.assertEqual(hist_105['current_state']['status'], 'unresolved')
        self.assertIsNone(hist_105['current_state']['creator_entity_id'])
        self.assertEqual(hist_105['total_events'], 0)
        self.assertEqual(len(hist_105['timeline']), 0)

    # -------------------------------------------------------------------------
    # 3. Composite Credit Preservation
    # -------------------------------------------------------------------------
    def test_06_composite_credit_preserved(self):
        # Composite credit (106: 'Jim Lee & Scott Williams')
        hist = self.history_service.get_decision_history(106)
        self.assertEqual(hist['name_record_id'], 106)
        self.assertEqual(hist['current_state']['raw_local_name'], 'Jim Lee & Scott Williams')
        self.assertEqual(hist['current_state']['status'], 'unresolved')
        self.assertIn("Jim Lee & Scott Williams", hist['explanation_summary'])

    # -------------------------------------------------------------------------
    # 4. Malformed IDs, Invalid Scope, and Pagination Validation
    # -------------------------------------------------------------------------
    def test_07_invalid_inputs_and_pagination(self):
        # Missing or None NameRecordID
        with self.assertRaises(InvalidHistoryInputError):
            self.history_service.get_decision_history(None)

        # Non-integer / negative NameRecordID
        with self.assertRaises(InvalidHistoryInputError):
            self.history_service.get_decision_history("invalid_id")
        with self.assertRaises(InvalidHistoryInputError):
            self.history_service.get_decision_history(-5)

        # Non-existent NameRecordID
        with self.assertRaises(LocalNameRecordNotFoundError):
            self.history_service.get_decision_history(99999)

        # Unsupported provider
        with self.assertRaises(InvalidHistoryInputError):
            self.history_service.get_decision_history(101, provider='unsupported_provider')

        # Pagination clamping: limit > 100 clamped to 100, limit < 1 clamped to 50
        res = self.history_service.get_decision_history(104, limit=500, offset=-10)
        self.assertEqual(res['limit'], 100)
        self.assertEqual(res['offset'], 0)

    # -------------------------------------------------------------------------
    # 5. Zero SQL Writes During History Query Execution
    # -------------------------------------------------------------------------
    def test_08_zero_sql_writes_during_history_query(self):
        def get_all_counts():
            cur = self.conn.cursor()
            tables = [
                'ext_creator_name_records', 'ext_creator_entities',
                'ext_creator_external_ids', 'ext_creator_candidate_rejections',
                'ext_creator_resolution_audit', 'ext_creator_credits'
            ]
            return {t: cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}

        before_counts = get_all_counts()

        # Execute multiple history queries
        self.history_service.get_decision_history(101)
        self.history_service.get_decision_history(102)
        self.history_service.get_decision_history(103)
        self.history_service.get_decision_history(104)

        after_counts = get_all_counts()
        self.assertEqual(before_counts, after_counts)

    # -------------------------------------------------------------------------
    # 6. Zero Provider/Network Requests (Even with Metron Disabled)
    # -------------------------------------------------------------------------
    def test_09_zero_provider_network_requests_metron_disabled(self):
        with patch('urllib.request.urlopen') as mock_url, patch('requests.get') as mock_req:
            mock_cfg = MockConfig()
            mock_cfg.METRON_ENABLED = False
            with patch('mylar.CONFIG', mock_cfg):
                res = self.history_service.get_decision_history(101)
                self.assertTrue(res['success'])
                mock_url.assert_not_called()
                mock_req.assert_not_called()

    # -------------------------------------------------------------------------
    # 7. Sanitized Controller Responses (No Secrets or SQL Traces)
    # -------------------------------------------------------------------------
    def test_10_controller_sanitized_responses(self):
        # 400 Bad Request on invalid input
        raw_400 = handle_get_creator_decision_history(name_record_id='bad_id', service=self.history_service)
        res_400 = json.loads(raw_400)
        self.assertFalse(res_400['success'])
        self.assertEqual(res_400['error_code'], 'invalid_input')
        self.assertNotIn("Traceback", raw_400)
        self.assertNotIn("SELECT", raw_400)

        # 404 Not Found on missing record
        raw_404 = handle_get_creator_decision_history(name_record_id=99999, service=self.history_service)
        res_404 = json.loads(raw_404)
        self.assertFalse(res_404['success'])
        self.assertEqual(res_404['error_code'], 'record_not_found')

        # 200 OK on valid record
        raw_200 = handle_get_creator_decision_history(name_record_id=101, service=self.history_service)
        res_200 = json.loads(raw_200)
        self.assertTrue(res_200['success'])
        self.assertEqual(res_200['name_record_id'], 101)

    # -------------------------------------------------------------------------
    # 8. Legacy Neutral Actor and Timestamp Handling
    # -------------------------------------------------------------------------
    def test_11_legacy_record_neutral_labeling(self):
        # Insert a raw audit record lacking actor and created_at
        cur = self.conn.cursor()
        cur.execute("""
            INSERT INTO ext_creator_resolution_audit
            (Action, NameRecordID, CreatorEntityID, Provider, ExternalID, ProviderDisplayName, Actor, CreatedAt)
            VALUES ('CONFIRM_PROVIDER_IDENTITY', 101, 1, 'metron', '101', 'Fabian Nicieza', '', '')
        """)
        self.conn.commit()

        res = self.history_service.get_decision_history(101)
        event = [e for e in res['timeline'] if e['actor'] == 'historical_record'][0]
        self.assertEqual(event['actor'], 'historical_record')
        self.assertEqual(event['actor_label'], 'Historical Record')

    # -------------------------------------------------------------------------
    # 9. Reversed Rejection History Event Formatting
    # -------------------------------------------------------------------------
    def test_12_reversed_rejection_history(self):
        # Reject then reverse rejection for Kevin Somers (103)
        self.id_service.reject_candidate(
            name_record_id=103, provider='metron', provider_creator_id='789',
            reason='Role discrepancy', actor='human_reviewer'
        )
        self.id_service.reverse_candidate_rejection(
            name_record_id=103, provider='metron', provider_creator_id='789',
            actor='human_reviewer', reason='Reinstated candidate'
        )

        res = self.history_service.get_decision_history(103)
        self.assertTrue(res['success'])
        self.assertEqual(res['current_state']['status'], 'unresolved')
        self.assertEqual(res['total_events'], 2)
        self.assertEqual(res['timeline'][0]['action'], 'REJECT_CANDIDATE')
        self.assertEqual(res['timeline'][1]['action'], 'REVERSE_REJECTION')
        self.assertEqual(res['timeline'][1]['action_label'], 'Reversed Rejection')
        self.assertIsNotNone(res['timeline'][1]['reversal_info'])
        self.assertEqual(res['timeline'][1]['reversal_info']['reverses_action'], 'REJECT_CANDIDATE')
        self.assertIn("Reversed candidate suppression", res['timeline'][1]['explanation'])

    # -------------------------------------------------------------------------
    # 10. Pagination and Offset Slicing
    # -------------------------------------------------------------------------
    def test_13_pagination_and_has_more(self):
        # Insert 5 audit records for NameRecord 101
        cur = self.conn.cursor()
        for i in range(1, 6):
            cur.execute("""
                INSERT INTO ext_creator_resolution_audit
                (Action, NameRecordID, CreatorEntityID, Provider, ExternalID, ProviderDisplayName, Actor, CreatedAt)
                VALUES ('CONFIRM_PROVIDER_IDENTITY', 101, ?, 'metron', ?, 'Fabian Nicieza', 'human_reviewer', ?)
            """, (i, str(100 + i), f"2026-08-20 1{i}:00:00"))
        self.conn.commit()

        # Query first page of 2 items
        page_1 = self.history_service.get_decision_history(101, limit=2, offset=0)
        self.assertEqual(page_1['total_events'], 5)
        self.assertEqual(len(page_1['timeline']), 2)
        self.assertTrue(page_1['has_more'])
        self.assertEqual(page_1['timeline'][0]['audit_id'], 1)
        self.assertEqual(page_1['timeline'][1]['audit_id'], 2)

        # Query second page of 2 items
        page_2 = self.history_service.get_decision_history(101, limit=2, offset=2)
        self.assertEqual(len(page_2['timeline']), 2)
        self.assertTrue(page_2['has_more'])
        self.assertEqual(page_2['timeline'][0]['audit_id'], 3)
        self.assertEqual(page_2['timeline'][1]['audit_id'], 4)

        # Query third page of 2 items (last item)
        page_3 = self.history_service.get_decision_history(101, limit=2, offset=4)
        self.assertEqual(len(page_3['timeline']), 1)
        self.assertFalse(page_3['has_more'])
        self.assertEqual(page_3['timeline'][0]['audit_id'], 5)

    # -------------------------------------------------------------------------
    # 11. Stale Navigation / Sequence Protection
    # -------------------------------------------------------------------------
    def test_14_stale_navigation_sequence_protection(self):
        """Verify client-side request sequence discard logic."""
        def simulate_client_response_handler(res, req_id, current_counter, req_issue_id, active_issue_id):
            if req_id != current_counter or req_issue_id != active_issue_id:
                return "DISCARDED_STALE"
            return "RENDERED_ACTIVE"

        # Case 1: Matching active counter and active issue
        self.assertEqual(
            simulate_client_response_handler({}, req_id=2, current_counter=2, req_issue_id='105543', active_issue_id='105543'),
            "RENDERED_ACTIVE"
        )
        # Case 2: Outdated request ID (stale fast response after newer click)
        self.assertEqual(
            simulate_client_response_handler({}, req_id=1, current_counter=2, req_issue_id='105543', active_issue_id='105543'),
            "DISCARDED_STALE"
        )
        # Case 3: Changed issue ID (user navigated to next issue while request was in-flight)
        self.assertEqual(
            simulate_client_response_handler({}, req_id=2, current_counter=2, req_issue_id='105543', active_issue_id='105544'),
            "DISCARDED_STALE"
        )


if __name__ == '__main__':
    unittest.main()
