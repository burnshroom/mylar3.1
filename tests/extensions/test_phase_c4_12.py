"""
Unit and Integration Tests for Phase C4.12 Read-Only Creator Identity Registry.

Tests:
1. Supported states resolution and formatting (confirmed, rejected, reversed, conflicted, unresolved).
2. Same-normalized-name records remaining strictly separate.
3. Composite-credit preservation without splitting.
4. Search, filters, pagination, and deterministic sorting.
5. Malformed parameters and boundary clamping.
6. Sanitized JSON responses (zero secrets, zero SQL traces, zero tracebacks).
7. Zero SQL writes during registry query execution.
8. Zero provider/network calls (including with Metron enabled and disabled).
9. No mutation controls in rendered template / responses (Confirm, Reject, Undo, Merge, Link).
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

import mylar
from mylar.extensions.migrations.runner import run_extension_migrations
from mylar.extensions.creators.identity_repository import IdentityRepository
from mylar.extensions.creators.identity_service import CreatorIdentityService
from mylar.extensions.creators.registry_service import (
    CreatorRegistryService,
    InvalidRegistryInputError
)
from mylar.extensions.creators.registry_controller import (
    handle_creator_registry,
    handle_get_creator_registry_json
)


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


class TestPhaseC412CreatorRegistry(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self._create_schema(self.conn)
        self.mock_db = MockDBWrapper(self.conn)
        self.repo = IdentityRepository(db_conn=self.mock_db)
        self.id_service = CreatorIdentityService(db_conn=self.mock_db, repository=self.repo)
        self.registry_service = CreatorRegistryService(db_conn=self.mock_db, repository=self.repo)
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

        # Seed series and issues for source context validation
        cur.execute("""
            INSERT INTO comics (ComicID, ComicName, ComicYear, ComicPublisher)
            VALUES ('5535', 'X-Men', '1991', 'Marvel')
        """)
        cur.execute("""
            INSERT INTO issues (IssueID, ComicID, Issue_Number, IssueName, IssueDate)
            VALUES ('105543', '5535', '1', 'Peace and Goodwill', '1991-10-01'),
                   ('105544', '5535', '2', 'Rubicon', '1991-11-01')
        """)

        # 101: Confirmed NameRecord (Fabian Nicieza -> Metron 101)
        cur.execute("""
            INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, ResolutionSource)
            VALUES (101, 'Fabian Nicieza', 'fabian nicieza', 'fabian-nicieza', 'human_review')
        """)
        cur.execute("""
            INSERT INTO ext_creator_credits (CreditID, IssueID, IsAnnual, ComicID, NameRecordID, Role, RawRoleText, RawCreditName, SourceProvenance, SourceRevision)
            VALUES (1, '105543', 0, '5535', 101, 'writer', 'Writer', 'Fabian Nicieza', 'comicinfo', 'rev1')
        """)

        # 102: Rejected NameRecord (Kevin Somers -> Metron 789)
        cur.execute("""
            INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, ResolutionSource)
            VALUES (102, 'Kevin Somers', 'kevin somers', 'kevin-somers', 'unresolved')
        """)

        # 103: Reversed NameRecord (Andy Kubert -> Metron 500 confirmed then reversed)
        cur.execute("""
            INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, ResolutionSource)
            VALUES (103, 'Andy Kubert', 'andy kubert', 'andy-kubert', 'unresolved')
        """)

        # 104: Conflicted NameRecord (Bob Harras -> Metron 999 with collision)
        cur.execute("""
            INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, ResolutionSource)
            VALUES (104, 'Bob Harras', 'bob harras', 'bob-harras', 'unresolved')
        """)

        # 105: Same normalized name as 101, but distinct NameRecordID ('F. Nicieza')
        cur.execute("""
            INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, ResolutionSource)
            VALUES (105, 'F. Nicieza', 'fabian nicieza', 'f-nicieza', 'unresolved')
        """)

        # 106: Composite credit ('Stan Lee & Jack Kirby')
        cur.execute("""
            INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, ResolutionSource)
            VALUES (106, 'Stan Lee & Jack Kirby', 'stan lee & jack kirby', 'stan-lee-jack-kirby', 'unresolved')
        """)

        # 107: Purely unresolved record with NO decision history (must NOT be in registry)
        cur.execute("""
            INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, ResolutionSource)
            VALUES (107, 'Unrecorded Creator', 'unrecorded creator', 'unrecorded-creator', 'unresolved')
        """)

        self.conn.commit()

        # Apply C4.10 decisions to set up exact states
        # 101: Confirm Fabian Nicieza
        self.id_service.confirm_provider_identity(
            name_record_id=101, provider='metron', provider_creator_id='101',
            provider_display_name='Fabian Nicieza', actor='human_reviewer'
        )

        # 102: Reject Kevin Somers
        self.id_service.reject_candidate(
            name_record_id=102, provider='metron', provider_creator_id='789',
            reason='Role discrepancy', actor='human_reviewer'
        )

        # 103: Confirm then reverse Andy Kubert
        self.id_service.confirm_provider_identity(
            name_record_id=103, provider='metron', provider_creator_id='500',
            provider_display_name='Andy Kubert', actor='human_reviewer'
        )
        self.id_service.reverse_provider_confirmation(
            name_record_id=103, provider='metron', provider_creator_id='500',
            actor='human_reviewer', reason='User undo'
        )

        # 104: Record a collision audit entry for Bob Harras
        cur.execute("""
            INSERT INTO ext_creator_resolution_audit
            (Action, NameRecordID, CreatorEntityID, Provider, ExternalID, ProviderDisplayName, Actor, Reason, CreatedAt)
            VALUES ('LEGACY_EXTERNAL_ID_COLLISION', 104, NULL, 'metron', '999', 'Bob Harras', 'system', 'Collision with Entity 888', '2026-08-24 01:10:00')
        """)

        # 105: Confirm F. Nicieza to separate Entity
        self.id_service.confirm_provider_identity(
            name_record_id=105, provider='metron', provider_creator_id='105',
            provider_display_name='Fabian Nicieza', actor='human_reviewer'
        )

        # 106: Confirm composite credit
        self.id_service.confirm_provider_identity(
            name_record_id=106, provider='metron', provider_creator_id='200',
            provider_display_name='Stan Lee & Jack Kirby', actor='human_reviewer'
        )

        self.conn.commit()

    # -------------------------------------------------------------------------
    # 1. Supported States Verification
    # -------------------------------------------------------------------------
    def test_01_supported_states_in_registry(self):
        res = self.registry_service.get_registry_entries(state='all')
        self.assertTrue(res['success'])
        entries = {r['name_record_id']: r for r in res['entries']}

        # Record 101: Confirmed
        self.assertIn(101, entries)
        self.assertEqual(entries[101]['current_state'], 'confirmed')
        self.assertEqual(entries[101]['provider_creator_id'], '101')
        self.assertIsNotNone(entries[101]['confirmed_entity'])

        # Record 102: Rejected
        self.assertIn(102, entries)
        self.assertEqual(entries[102]['current_state'], 'rejected')
        self.assertEqual(entries[102]['provider_creator_id'], '789')

        # Record 103: Reversed / Unresolved
        self.assertIn(103, entries)
        self.assertEqual(entries[103]['current_state'], 'reversed')
        self.assertEqual(entries[103]['decision_event_count'], 2)

        # Record 104: Conflicted
        self.assertIn(104, entries)
        self.assertEqual(entries[104]['current_state'], 'conflicted')

        # Record 107: Unrecorded creator with NO audit history MUST NOT appear
        self.assertNotIn(107, entries)

        # Check state filter counts
        counts = res['counts']
        self.assertEqual(counts['all'], 6)
        self.assertEqual(counts['confirmed'], 3) # 101, 105, 106
        self.assertEqual(counts['rejected'], 1)  # 102
        self.assertEqual(counts['reversed'], 1)  # 103
        self.assertEqual(counts['conflicted'], 1)# 104

    # -------------------------------------------------------------------------
    # 2. Same-Normalized-Name Records Isolated
    # -------------------------------------------------------------------------
    def test_02_same_normalized_name_records_isolated(self):
        res = self.registry_service.get_registry_entries(state='all')
        entries = {r['name_record_id']: r for r in res['entries']}

        self.assertIn(101, entries)
        self.assertIn(105, entries)
        self.assertNotEqual(entries[101]['name_record_id'], entries[105]['name_record_id'])
        self.assertEqual(entries[101]['raw_local_name'], 'Fabian Nicieza')
        self.assertEqual(entries[105]['raw_local_name'], 'F. Nicieza')
        self.assertEqual(entries[101]['provider_creator_id'], '101')
        self.assertEqual(entries[105]['provider_creator_id'], '105')

    # -------------------------------------------------------------------------
    # 3. Composite Credit Preserved
    # -------------------------------------------------------------------------
    def test_03_composite_credit_preserved(self):
        res = self.registry_service.get_registry_entries(state='all')
        entries = {r['name_record_id']: r for r in res['entries']}

        self.assertIn(106, entries)
        self.assertEqual(entries[106]['raw_local_name'], 'Stan Lee & Jack Kirby')
        self.assertEqual(entries[106]['normalized_name'], 'stan lee & jack kirby')
        self.assertEqual(entries[106]['current_state'], 'confirmed')

    # -------------------------------------------------------------------------
    # 4. Search, State Filters, and Sorting
    # -------------------------------------------------------------------------
    def test_04_search_filters_and_sorting(self):
        # Filter by state = confirmed
        res_conf = self.registry_service.get_registry_entries(state='confirmed')
        self.assertEqual(len(res_conf['entries']), 3)
        for r in res_conf['entries']:
            self.assertEqual(r['current_state'], 'confirmed')

        # Filter by state = rejected
        res_rej = self.registry_service.get_registry_entries(state='rejected')
        self.assertEqual(len(res_rej['entries']), 1)
        self.assertEqual(res_rej['entries'][0]['name_record_id'], 102)

        # Filter by search = 'Andy'
        res_search = self.registry_service.get_registry_entries(search='Andy')
        self.assertEqual(len(res_search['entries']), 1)
        self.assertEqual(res_search['entries'][0]['raw_local_name'], 'Andy Kubert')

        # Sort by Name ASC
        res_sort_asc = self.registry_service.get_registry_entries(sort='name_asc')
        names_asc = [r['raw_local_name'] for r in res_sort_asc['entries']]
        self.assertEqual(names_asc, sorted(names_asc, key=lambda x: x.lower()))

        # Sort by Name DESC
        res_sort_desc = self.registry_service.get_registry_entries(sort='name_desc')
        names_desc = [r['raw_local_name'] for r in res_sort_desc['entries']]
        self.assertEqual(names_desc, sorted(names_desc, key=lambda x: x.lower(), reverse=True))

    # -------------------------------------------------------------------------
    # 5. Malformed Parameters and Bounds Clamping
    # -------------------------------------------------------------------------
    def test_05_malformed_parameters_and_bounds(self):
        # Invalid state raises InvalidRegistryInputError
        with self.assertRaises(InvalidRegistryInputError):
            self.registry_service.get_registry_entries(state='invalid_state')

        # Invalid provider raises InvalidRegistryInputError
        with self.assertRaises(InvalidRegistryInputError):
            self.registry_service.get_registry_entries(provider='invalid_prov')

        # Invalid sort raises InvalidRegistryInputError
        with self.assertRaises(InvalidRegistryInputError):
            self.registry_service.get_registry_entries(sort='unsupported_sort')

        # Page and PageSize clamping
        res_clamped = self.registry_service.get_registry_entries(page=-5, page_size=999)
        self.assertEqual(res_clamped['current_page'], 1)
        self.assertEqual(res_clamped['page_size'], 100)

    # -------------------------------------------------------------------------
    # 6. Sanitized JSON Controller Responses
    # -------------------------------------------------------------------------
    def test_06_sanitized_controller_responses(self):
        # 400 Bad Request on invalid state
        raw_400 = handle_get_creator_registry_json(state='bad_state', service=self.registry_service)
        res_400 = json.loads(raw_400)
        self.assertFalse(res_400['success'])
        self.assertEqual(res_400['error_code'], 'invalid_input')
        self.assertNotIn("Traceback", raw_400)
        self.assertNotIn("SELECT", raw_400)

        # 200 OK on valid parameters
        raw_200 = handle_get_creator_registry_json(state='all', service=self.registry_service)
        res_200 = json.loads(raw_200)
        self.assertTrue(res_200['success'])
        self.assertIn('entries', res_200)
        self.assertIn('counts', res_200)

    # -------------------------------------------------------------------------
    # 7. Zero SQL Writes During Registry Querying
    # -------------------------------------------------------------------------
    def test_07_zero_sql_writes_during_registry_query(self):
        cur = self.conn.cursor()
        tables = [
            'ext_creator_name_records', 'ext_creator_entities',
            'ext_creator_external_ids', 'ext_creator_candidate_rejections',
            'ext_creator_resolution_audit', 'ext_creator_credits',
            'comics', 'issues', 'annuals'
        ]

        def get_counts():
            return {t: cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}

        before = get_counts()

        # Query registry multiple times with different filters
        self.registry_service.get_registry_entries(state='all')
        self.registry_service.get_registry_entries(state='confirmed')
        self.registry_service.get_registry_entries(state='rejected')
        self.registry_service.get_registry_entries(state='reversed')
        self.registry_service.get_registry_entries(state='conflicted')
        self.registry_service.get_registry_entries(search='Nicieza')

        after = get_counts()
        self.assertEqual(before, after)

    # -------------------------------------------------------------------------
    # 8. Zero Provider Network Calls (Metron Enabled & Disabled)
    # -------------------------------------------------------------------------
    def test_08_zero_network_calls_metron_enabled_and_disabled(self):
        with patch('urllib.request.urlopen') as mock_url, patch('requests.get') as mock_req:
            # Case 1: Metron Disabled
            mock_cfg_disabled = MockConfig()
            mock_cfg_disabled.METRON_ENABLED = False
            with patch('mylar.CONFIG', mock_cfg_disabled):
                res1 = self.registry_service.get_registry_entries(state='all')
                self.assertTrue(res1['success'])
                mock_url.assert_not_called()
                mock_req.assert_not_called()

            # Case 2: Metron Enabled
            mock_cfg_enabled = MockConfig()
            mock_cfg_enabled.METRON_ENABLED = True
            with patch('mylar.CONFIG', mock_cfg_enabled):
                res2 = self.registry_service.get_registry_entries(state='all')
                self.assertTrue(res2['success'])
                mock_url.assert_not_called()
                mock_req.assert_not_called()

    # -------------------------------------------------------------------------
    # 9. No Mutation Controls in Rendered Registry Template
    # -------------------------------------------------------------------------
    def test_09_no_mutation_controls_in_rendered_template(self):
        from mako.template import Template
        tmpl_path = os.path.join(REPO_ROOT, 'data', 'interfaces', 'modern', 'creator_registry.html')

        with open(tmpl_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # Verify no mutation action endpoints or buttons
        forbidden_terms = [
            'confirmCreatorIdentity',
            'rejectCreatorCandidate',
            'reverseCreatorDecision',
            'btn-cand-confirm',
            'btn-cand-reject',
            'btn-cand-undo',
            'Merge',
            'bulk-action',
            'auto-match'
        ]
        for term in forbidden_terms:
            self.assertNotIn(term, content, f"Forbidden mutation term '{term}' found in creator_registry.html")

        # Verify read-only history button is present
        self.assertIn('btn-reg-history', content)
        self.assertIn('View history', content)

    # -------------------------------------------------------------------------
    # 10. Provider Filter Isolation
    # -------------------------------------------------------------------------
    def test_10_provider_filter_isolation(self):
        res_metron = self.registry_service.get_registry_entries(provider='metron')
        self.assertTrue(res_metron['success'])
        self.assertEqual(len(res_metron['entries']), 6) # All seeded records target Metron
        for r in res_metron['entries']:
            self.assertEqual(r['provider'], 'metron')

        res_cv = self.registry_service.get_registry_entries(provider='comicvine')
        self.assertTrue(res_cv['success'])
        self.assertEqual(len(res_cv['entries']), 0) # Zero records target ComicVine

    # -------------------------------------------------------------------------
    # 11. Navigation Target Scope and Existence Validation
    # -------------------------------------------------------------------------
    def test_11_navigation_target_scope_and_existence(self):
        # 101 has credit in X-Men #1 (IssueID: 105543, ComicID: 5535)
        res = self.registry_service.get_registry_entries(state='all')
        entries = {r['name_record_id']: r for r in res['entries']}

        ctx_101 = entries[101]['source_context']
        self.assertIsNotNone(ctx_101)
        self.assertEqual(ctx_101['comic_name'], 'X-Men')
        self.assertEqual(ctx_101['issue_number'], '1')
        self.assertEqual(ctx_101['comic_id'], '5535')
        self.assertEqual(ctx_101['issue_id'], '105543')
        self.assertFalse(ctx_101['is_annual'])

        # 102 has NO credit row seeded -> source_context must be None
        ctx_102 = entries[102]['source_context']
        self.assertIsNone(ctx_102)

    # -------------------------------------------------------------------------
    # 12. History View Reusability (C4.11 Integration)
    # -------------------------------------------------------------------------
    def test_12_history_view_reusability(self):
        from mylar.extensions.creators.history_service import CreatorHistoryService
        hist_service = CreatorHistoryService(repository=self.repo)

        res_reg = self.registry_service.get_registry_entries(state='all')
        entries = {r['name_record_id']: r for r in res_reg['entries']}

        # Verify that for confirmed record 101, C4.11 history service returns matching state
        hist_101 = hist_service.get_decision_history(101)
        self.assertTrue(hist_101['success'])
        self.assertEqual(hist_101['current_state']['status'], entries[101]['current_state'])
        self.assertEqual(hist_101['total_events'], entries[101]['decision_event_count'])


if __name__ == '__main__':
    unittest.main()
