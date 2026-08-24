"""
Phase C4.17 Test Suite: Creator Identity System End-to-End Acceptance Audit.

Comprehensive acceptance and hardening audit verifying:
1. Lifecycle State Matrix (complete exact-pair lifecycles, independence, cross-surface consistency)
2. Transaction & Recovery (failure injection at every write boundary, rollback, application restart persistence)
3. Database Integrity (SQLite integrity check, foreign keys, UNIQUE constraints, pre-C4 migration idempotency, table deltas, unrelated table invariance)
4. HTTP & Security (method enforcement, CSRF requirements, input bounds, rejection of client entity claims, sanitized errors, zero credential leakage)
5. Network & Provider Gates (zero network calls, disabled/missing credential gates, local audit readability without credentials)
"""

import json
import os
import sys
import sqlite3
import tempfile
import unittest
import socket
import urllib.request
from unittest.mock import patch, MagicMock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.join(REPO_ROOT, 'lib'))
sys.path.insert(0, REPO_ROOT)

import mylar
from mylar import db, logger
from mylar.extensions.creators.identity_repository import IdentityRepository
from mylar.extensions.creators.identity_service import (
    CreatorIdentityService,
    IdentityResolutionError,
    InvalidIdentityInputError,
    NameRecordNotFoundError,
    ActiveCandidateRejectionError,
    ProviderIDCollisionError,
    ConflictingNameRecordLinkError,
    UnsafeReversalError,
    TargetNotFoundError,
)
from mylar.extensions.creators.candidate_service import (
    CreatorCandidateService,
    is_composite_credit,
    InvalidCandidateInputError,
    LocalIssueNotFoundError,
)
from mylar.extensions.creators.conflict_service import (
    CreatorConflictService,
    InvalidConflictInputError,
    LocalNameRecordNotFoundError as ConflictNameRecordNotFoundError,
    StaleConflictStateError,
    TransferTargetUnavailableError,
)
from mylar.extensions.creators.registry_service import (
    CreatorRegistryService,
    InvalidRegistryInputError,
)
from mylar.extensions.creators.history_service import (
    CreatorHistoryService,
    InvalidHistoryInputError,
    LocalNameRecordNotFoundError as HistoryNameRecordNotFoundError,
)
from mylar.extensions.creators.browser_service import CreatorBrowserService
from mylar.extensions.creators.decision_controller import (
    get_or_create_csrf_token,
    handle_confirm_creator_identity,
    handle_reject_creator_candidate,
    handle_reverse_creator_decision,
    handle_get_creator_csrf_token,
)
from mylar.extensions.creators.conflict_controller import (
    handle_get_creator_conflict_analysis,
    handle_resolve_creator_conflict,
)
from mylar.extensions.creators.registry_controller import (
    handle_creator_registry,
    handle_get_creator_registry_json,
)
from mylar.extensions.creators.history_controller import (
    handle_get_creator_decision_history,
)
from mylar.extensions.creators.browser_controller import (
    handle_creator_catalog,
    handle_creator_detail,
    handle_issue_creator_credits,
)
from mylar.extensions.creators.controller import CreatorController
from mylar.extensions.migrations.runner import run_extension_migrations


class MockDBWrapper:
    """Wrapper exposing .connection and .conn for compatibility with Mylar DB conventions."""
    def __init__(self, connection):
        self.connection = connection
        self.conn = connection

    def select(self, query, params=None):
        cur = self.conn.cursor()
        cur.execute(query, params or [])
        return cur.fetchall()

    def selectone(self, query, params=None):
        cur = self.conn.cursor()
        cur.execute(query, params or [])
        return cur.fetchone()

    def action(self, query, params=None):
        cur = self.conn.cursor()
        cur.execute(query, params or [])
        self.conn.commit()
        return cur


def create_pre_c4_database(conn):
    """Create legacy pre-C4 database schema with core Mylar tables."""
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS comics (
            ComicID TEXT PRIMARY KEY,
            ComicName TEXT,
            Publisher TEXT,
            ComicYear TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS issues (
            IssueID TEXT PRIMARY KEY,
            ComicID TEXT,
            Issue_Number TEXT,
            IssueName TEXT,
            IssueDate TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS annuals (
            AnnualID TEXT PRIMARY KEY,
            ComicID TEXT,
            Issue_Number TEXT,
            IssueName TEXT,
            IssueDate TEXT
        )
    """)
    conn.commit()


def setup_complete_test_database(conn):
    """Set up complete database with pre-C4 tables and all extension migrations applied."""
    create_pre_c4_database(conn)
    cur = conn.cursor()
    run_extension_migrations(cur)
    conn.commit()


def seed_standard_audit_dataset(conn):
    """Seed a representative test dataset across core and creator extension tables."""
    cur = conn.cursor()

    # Core tables
    cur.execute("INSERT OR REPLACE INTO comics (ComicID, ComicName, Publisher, ComicYear) VALUES ('5535', 'Uncanny X-Men', 'Marvel', '1963')")
    cur.execute("INSERT OR REPLACE INTO comics (ComicID, ComicName, Publisher, ComicYear) VALUES ('8888', 'Batman', 'DC Comics', '1940')")
    cur.execute("INSERT OR REPLACE INTO issues (IssueID, ComicID, Issue_Number, IssueName, IssueDate) VALUES ('105544', '5535', '281', 'Fresh Inks', '1991-10-01')")
    cur.execute("INSERT OR REPLACE INTO issues (IssueID, ComicID, Issue_Number, IssueName, IssueDate) VALUES ('105545', '5535', '282', 'Bishop Arrives', '1991-11-01')")
    cur.execute("INSERT OR REPLACE INTO annuals (AnnualID, ComicID, Issue_Number, IssueName, IssueDate) VALUES ('2001', '5535', '1', 'Annual One', '1992-01-01')")

    # Creator Entities
    # Entity 1: Stan Lee (metron: 101)
    cur.execute("INSERT INTO ext_creator_entities (CreatorEntityID, DisplayName, NormalizedName, EntitySlug) VALUES (1, 'Stan Lee', 'stan lee', 'stan-lee')")
    cur.execute("INSERT INTO ext_creator_external_ids (CreatorEntityID, Provider, ExternalID, Confidence) VALUES (1, 'metron', '101', 1.0)")

    # Entity 2: Bob Harras (Metron ID 999 mapped initially)
    cur.execute("INSERT INTO ext_creator_entities (CreatorEntityID, DisplayName, NormalizedName, EntitySlug) VALUES (2, 'Bob Harras', 'bob harras', 'bob-harras')")
    cur.execute("INSERT INTO ext_creator_external_ids (CreatorEntityID, Provider, ExternalID, Confidence) VALUES (2, 'metron', '999', 1.0)")

    # Entity 3: Robert Harras (Competing entity for conflict tests)
    cur.execute("INSERT INTO ext_creator_entities (CreatorEntityID, DisplayName, NormalizedName, EntitySlug) VALUES (3, 'Robert Harras', 'robert harras', 'robert-harras')")

    # Name Records
    # Record 101: Stan Lee (linked to Entity 1, confirmed)
    cur.execute("INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, CreatorEntityID, ResolutionSource) VALUES (101, 'Stan Lee', 'stan lee', 'stan-lee', 1, 'explicit_user')")
    # Record 102: Jack Kirby (unlinked)
    cur.execute("INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, CreatorEntityID, ResolutionSource) VALUES (102, 'Jack Kirby', 'jack kirby', 'jack-kirby', NULL, 'unresolved')")
    # Record 103: Jim Lee (unlinked)
    cur.execute("INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, CreatorEntityID, ResolutionSource) VALUES (103, 'Jim Lee', 'jim lee', 'jim-lee', NULL, 'unresolved')")
    # Record 104: Bob Harras (linked to Entity 2)
    cur.execute("INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, CreatorEntityID, ResolutionSource) VALUES (104, 'Bob Harras', 'bob harras', 'bob-harras', 2, 'explicit_user')")
    # Record 105: Robert Harras (linked to Entity 3)
    cur.execute("INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, CreatorEntityID, ResolutionSource) VALUES (105, 'Robert Harras', 'robert harras', 'robert-harras', 3, 'unresolved')")
    # Record 106: Stan Lee (Independent second name record with exact same normalized name)
    cur.execute("INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, CreatorEntityID, ResolutionSource) VALUES (106, 'Stan Lee (Editor)', 'stan lee', 'stan-lee', NULL, 'unresolved')")
    # Record 107: Composite credit: 'Stan Lee / Jack Kirby'
    cur.execute("INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, CreatorEntityID, ResolutionSource) VALUES (107, 'Stan Lee / Jack Kirby', 'stan lee jack kirby', 'stan-lee-jack-kirby', NULL, 'unresolved')")

    # Issue Creator Credits
    cur.execute("""
        INSERT INTO ext_creator_credits (CreditID, NameRecordID, CreatorEntityID, IssueID, ComicID, IsAnnual, Role, RawRoleText, RawCreditName, IsCover, SortOrder, SourceProvenance, SourceRevision)
        VALUES (501, 101, 1, '105544', '5535', 0, 'editor', 'Editor', 'Stan Lee', 0, 1, 'local_index', 'rev1')
    """)
    cur.execute("""
        INSERT INTO ext_creator_credits (CreditID, NameRecordID, CreatorEntityID, IssueID, ComicID, IsAnnual, Role, RawRoleText, RawCreditName, IsCover, SortOrder, SourceProvenance, SourceRevision)
        VALUES (502, 102, NULL, '105544', '5535', 0, 'penciller', 'Pencils', 'Jack Kirby', 0, 2, 'local_index', 'rev1')
    """)
    cur.execute("""
        INSERT INTO ext_creator_credits (CreditID, NameRecordID, CreatorEntityID, IssueID, ComicID, IsAnnual, Role, RawRoleText, RawCreditName, IsCover, SortOrder, SourceProvenance, SourceRevision)
        VALUES (503, 103, NULL, '105544', '5535', 0, 'penciller', 'Pencils', 'Jim Lee', 0, 3, 'local_index', 'rev1')
    """)
    cur.execute("""
        INSERT INTO ext_creator_credits (CreditID, NameRecordID, CreatorEntityID, IssueID, ComicID, IsAnnual, Role, RawRoleText, RawCreditName, IsCover, SortOrder, SourceProvenance, SourceRevision)
        VALUES (504, 104, 2, '105544', '5535', 0, 'editor', 'Editor', 'Bob Harras', 0, 4, 'local_index', 'rev1')
    """)
    cur.execute("""
        INSERT INTO ext_creator_credits (CreditID, NameRecordID, CreatorEntityID, IssueID, ComicID, IsAnnual, Role, RawRoleText, RawCreditName, IsCover, SortOrder, SourceProvenance, SourceRevision)
        VALUES (505, 107, NULL, '105544', '5535', 0, 'writer', 'Story', 'Stan Lee / Jack Kirby', 0, 5, 'local_index', 'rev1')
    """)
    cur.execute("""
        INSERT INTO ext_creator_credits (CreditID, NameRecordID, CreatorEntityID, IssueID, ComicID, IsAnnual, Role, RawRoleText, RawCreditName, IsCover, SortOrder, SourceProvenance, SourceRevision)
        VALUES (506, 105, 3, '105544', '5535', 0, 'editor', 'Editor', 'Robert Harras', 0, 6, 'local_index', 'rev1')
    """)

    conn.commit()


# =============================================================================
# Audit Area 1: Lifecycle State Matrix
# =============================================================================

class TestC417LifecycleStateMatrix(unittest.TestCase):
    """
    Exercise complete exact-pair lifecycles and verify cross-view state equivalence:
    Inspector, Registry, History, and Conflict Analysis must reflect identical truth.
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        setup_complete_test_database(self.conn)
        seed_standard_audit_dataset(self.conn)
        self.mock_db = MockDBWrapper(self.conn)
        self.repo = IdentityRepository(db_conn=self.mock_db)
        self.identity_service = CreatorIdentityService(db_conn=self.mock_db, repository=self.repo)
        self.candidate_service = CreatorCandidateService(db_conn=self.mock_db, repository=self.repo)
        self.conflict_service = CreatorConflictService(repository=self.repo, db_conn=self.mock_db)
        self.registry_service = CreatorRegistryService(repository=self.repo, db_conn=self.mock_db)
        self.history_service = CreatorHistoryService(repository=self.repo, db_conn=self.mock_db)

    def tearDown(self):
        self.conn.close()

    def _assert_cross_view_state(self, name_record_id, provider, external_id, expected_state, provider_credits=None):
        """
        Verify that Inspector, Registry, History, and Conflict Analysis all report
        the exact same canonical lifecycle state.
        """
        # 1. Shared Canonical State Helper
        canon = self.repo.derive_pairing_state(name_record_id, provider, external_id)
        self.assertEqual(canon['state'], expected_state, f"Canonical state mismatch for #{name_record_id}: expected {expected_state}, got {canon['state']}")

        # 2. Decision History
        hist = self.history_service.get_decision_history(name_record_id=name_record_id, provider=provider, provider_creator_id=external_id)
        self.assertEqual(hist['current_state']['status'], expected_state, f"History state mismatch: expected {expected_state}, got {hist['current_state']['status']}")

        # 3. Conflict Analysis
        conf = self.conflict_service.analyze_creator_conflict(name_record_id=name_record_id, provider=provider, provider_creator_id=external_id)
        self.assertEqual(conf['derived_state'], expected_state, f"Conflict analysis state mismatch: expected {expected_state}, got {conf['derived_state']}")

        # 4. Candidate Inspector (if provider snapshot credits supplied)
        if provider_credits:
            cand_res = self.candidate_service.build_candidates(issue_id='105544', is_annual=0, provider=provider, provider_snapshot={'credits': provider_credits})
            cand = next((c for c in cand_res['identity_candidates'] if c['name_record_id'] == name_record_id and str(c['provider_creator_id']) == str(external_id)), None)
            if cand:
                expected_cand_state = 'candidate' if expected_state == 'unresolved' else expected_state
                self.assertEqual(cand['candidate_state'], expected_cand_state, f"Candidate inspector mismatch: expected {expected_cand_state}, got {cand['candidate_state']}")

    def test_01_unresolved_to_candidate_to_confirmed_to_reversed(self):
        """Lifecycle 1: unresolved -> candidate -> confirmed -> reversed -> unresolved."""
        # 1. Unresolved Initial State for Jack Kirby (Record 102)
        provider_credits = [{'creator_id': '102', 'name': 'Jack Kirby', 'raw_creator_name': 'Jack Kirby', 'role': 'penciller', 'canonical_role': 'penciller', 'raw_role': 'Pencils', 'is_cover_credit': False}]
        self._assert_cross_view_state(102, 'metron', '102', 'unresolved', provider_credits)

        # 2. Confirm Identity
        res_conf = self.identity_service.confirm_provider_identity(
            name_record_id=102,
            provider='metron',
            provider_creator_id='102',
            provider_display_name='Jack Kirby',
            actor='reviewer',
            confirmation_source='explicit_user'
        )
        self.assertIsNotNone(res_conf)
        self.assertEqual(res_conf['name_record_id'], 102)
        self._assert_cross_view_state(102, 'metron', '102', 'confirmed', provider_credits)

        # 3. Safe Reversal
        res_rev = self.identity_service.reverse_provider_confirmation(
            name_record_id=102,
            provider='metron',
            provider_creator_id='102',
            actor='reviewer',
            reason='Undo Kirby Confirmation'
        )
        self.assertTrue(res_rev['success'])
        self._assert_cross_view_state(102, 'metron', '102', 'unresolved', provider_credits)

    def test_02_candidate_to_rejected_to_rejection_reversed(self):
        """Lifecycle 2: candidate -> rejected -> rejection reversed -> candidate."""
        provider_credits = [{'creator_id': '103', 'name': 'Jim Lee', 'raw_creator_name': 'Jim Lee', 'role': 'penciller', 'canonical_role': 'penciller', 'raw_role': 'Pencils', 'is_cover_credit': False}]
        self._assert_cross_view_state(103, 'metron', '103', 'unresolved', provider_credits)

        # Reject Candidate
        res_rej = self.identity_service.reject_candidate(
            name_record_id=103,
            provider='metron',
            provider_creator_id='103',
            provider_display_name='Jim Lee',
            reason='Wrong artist for issue',
            actor='reviewer'
        )
        self.assertEqual(res_rej['status'], 'active')
        self._assert_cross_view_state(103, 'metron', '103', 'rejected', provider_credits)

        # Rejection Suppresses Candidate in Inspector
        cand_res = self.candidate_service.build_candidates(issue_id='105544', is_annual=0, provider='metron', provider_snapshot={'credits': provider_credits})
        cand = next((c for c in cand_res['identity_candidates'] if c['name_record_id'] == 103 and str(c['provider_creator_id']) == '103'), None)
        self.assertIsNotNone(cand)
        self.assertEqual(cand['candidate_state'], 'rejected')
        self.assertTrue(cand['is_rejected'])

        # Reverse Rejection
        res_rev = self.identity_service.reverse_candidate_rejection(
            name_record_id=103,
            provider='metron',
            provider_creator_id='103',
            actor='reviewer',
            reason='Undo rejection'
        )
        self.assertEqual(res_rev['status'], 'reversed')
        self._assert_cross_view_state(103, 'metron', '103', 'unresolved', provider_credits)

    def test_03_conflict_keep_existing_reject_competing(self):
        """Lifecycle 3: conflict -> keep existing / reject competing candidate."""
        # Setup collision: Metron 999 is mapped to Entity 2 (Bob Harras). NameRecord 105 (Robert Harras, Entity 3) tries to resolve Metron 999.
        provider_credits = [{'creator_id': '999', 'name': 'Bob Harras', 'raw_creator_name': 'Bob Harras', 'role': 'editor', 'canonical_role': 'editor', 'raw_role': 'Editor', 'is_cover_credit': False}]
        self._assert_cross_view_state(105, 'metron', '999', 'conflicted', provider_credits)

        # Resolve conflict by keeping existing mapping on Entity 2 and rejecting competing candidate 105
        res_keep = self.conflict_service.resolve_conflict_keep_existing_reject_competing(
            name_record_id=105,
            provider='metron',
            provider_creator_id='999',
            actor='admin_user',
            reason='Retain primary Bob Harras entity'
        )
        self.assertTrue(res_keep['success'])
        self.assertEqual(res_keep['action'], 'keep_existing_reject_competing')

        # NameRecord 105 is now in REJECTED state for Metron 999
        self._assert_cross_view_state(105, 'metron', '999', 'rejected', provider_credits)
        # Entity 2 / NameRecord 104 remains in CONFIRMED state for Metron 999
        self._assert_cross_view_state(104, 'metron', '999', 'confirmed', provider_credits)

    def test_04_conflict_transferred_transfer_reversed_conflict_restored(self):
        """Lifecycle 4: conflict -> transferred -> transfer reversed -> conflict restored."""
        provider_credits = [{'creator_id': '999', 'name': 'Robert Harras', 'raw_creator_name': 'Robert Harras', 'role': 'editor', 'canonical_role': 'editor', 'raw_role': 'Editor', 'is_cover_credit': False}]
        self._assert_cross_view_state(105, 'metron', '999', 'conflicted', provider_credits)

        # Transfer Metron 999 from Entity 2 to Entity 3 (NameRecord 105)
        res_trans = self.conflict_service.resolve_conflict_transfer_mapping(
            name_record_id=105,
            provider='metron',
            provider_creator_id='999',
            actor='admin_user',
            reason='Transfer mapping to canonical Robert Harras entity'
        )
        self.assertTrue(res_trans['success'])
        self.assertEqual(res_trans['action'], 'transfer_mapping')

        # NameRecord 105 is now TRANSFERRED
        self._assert_cross_view_state(105, 'metron', '999', 'transferred', provider_credits)
        # Exactly one active mapping in database
        cur = self.conn.cursor()
        cur.execute("SELECT CreatorEntityID FROM ext_creator_external_ids WHERE Provider = 'metron' AND ExternalID = '999'")
        mappings = cur.fetchall()
        self.assertEqual(len(mappings), 1)
        self.assertEqual(mappings[0][0], 3)

        # Reverse Transfer
        res_rev_trans = self.conflict_service.reverse_conflict_transfer_mapping(
            name_record_id=105,
            provider='metron',
            provider_creator_id='999',
            actor='admin_user',
            reason='Undo transfer'
        )
        self.assertTrue(res_rev_trans['success'])
        self.assertEqual(res_rev_trans['action'], 'reverse_transfer')

        # Conflict is restored
        self._assert_cross_view_state(105, 'metron', '999', 'conflicted', provider_credits)
        # Mapping restored to Entity 2
        cur.execute("SELECT CreatorEntityID FROM ext_creator_external_ids WHERE Provider = 'metron' AND ExternalID = '999'")
        mappings_after = cur.fetchall()
        self.assertEqual(len(mappings_after), 1)
        self.assertEqual(mappings_after[0][0], 2)

    def test_05_conflict_transfer_with_superseded_rejection_restoration(self):
        """Lifecycle 5: conflict transfer with superseded rejection -> reversal restoring rejection."""
        # Setup: NameRecord 105 has an active rejection for Metron 999
        self.identity_service.reject_candidate(105, 'metron', '999', actor='reviewer', reason='Initial rejection')
        self.assertEqual(self.repo.derive_pairing_state(105, 'metron', '999')['state'], 'rejected')

        # Transfer Metron 999 to NameRecord 105 (Entity 3) -> rejection should be superseded
        res_trans = self.conflict_service.resolve_conflict_transfer_mapping(105, 'metron', '999', actor='admin')
        self.assertTrue(res_trans['success'])
        self.assertEqual(self.repo.derive_pairing_state(105, 'metron', '999')['state'], 'transferred')

        # Reverse Transfer -> rejection must be restored to active
        res_rev = self.conflict_service.reverse_conflict_transfer_mapping(105, 'metron', '999', actor='admin')
        self.assertTrue(res_rev['success'])
        self.assertEqual(self.repo.derive_pairing_state(105, 'metron', '999')['state'], 'rejected')

    def test_06_stale_mutation_handling(self):
        """Lifecycle 6: stale confirmation, rejection, transfer, and reversal attempts fail safely."""
        # Attempt to confirm record 101 to a DIFFERENT provider ID (Metron 999 mapped to Entity 2) -> raises ConflictingNameRecordLinkError
        with self.assertRaises(ConflictingNameRecordLinkError):
            self.identity_service.confirm_provider_identity(101, 'metron', '999', actor='reviewer')

        # Attempt to confirm an actively rejected candidate -> raises ActiveCandidateRejectionError
        self.identity_service.reject_candidate(102, 'metron', '102', actor='reviewer')
        with self.assertRaises(ActiveCandidateRejectionError):
            self.identity_service.confirm_provider_identity(102, 'metron', '102', actor='reviewer')

        # Repeating rejection is idempotent (returns existing active rejection without duplicate rows)
        dup_rej = self.identity_service.reject_candidate(102, 'metron', '102', actor='reviewer')
        self.assertEqual(dup_rej['status'], 'active')

        # Attempt to reverse an unlinked / never confirmed record
        with self.assertRaises(TargetNotFoundError):
            self.identity_service.reverse_provider_confirmation(103, 'metron', '103', actor='reviewer')

        # Attempt stale transfer when target is already mapped
        with self.assertRaises(StaleConflictStateError):
            self.conflict_service.resolve_conflict_transfer_mapping(104, 'metron', '999', actor='reviewer')

    def test_07_same_normalized_name_records_remain_independent(self):
        """Lifecycle 7: same-normalized-name records remain completely independent."""
        # Record 101 ('Stan Lee') and Record 106 ('Stan Lee (Editor)') both normalize to 'stan lee'
        # Record 101 is linked to Entity 1. Record 106 is unlinked.
        cur = self.conn.cursor()
        cur.execute("SELECT CreatorEntityID FROM ext_creator_name_records WHERE NameRecordID = 106")
        self.assertIsNone(cur.fetchone()[0])

        # State of 101 is confirmed
        self.assertEqual(self.repo.derive_pairing_state(101, 'metron', '101')['state'], 'confirmed')
        # State of 106 is conflicted when evaluated against Metron 101 (since Metron 101 is mapped to Entity 1)
        self.assertEqual(self.repo.derive_pairing_state(106, 'metron', '101')['state'], 'conflicted')

        # Reversing 101 does not link or modify 106
        self.identity_service.reverse_provider_confirmation(101, 'metron', '101', actor='reviewer')
        cur.execute("SELECT CreatorEntityID FROM ext_creator_name_records WHERE NameRecordID = 106")
        self.assertIsNone(cur.fetchone()[0])
        self.assertEqual(self.repo.derive_pairing_state(106, 'metron', '101')['state'], 'unresolved')

    def test_08_composite_credit_intact_throughout(self):
        """Lifecycle 8: composite collaborative credits remain intact throughout."""
        self.assertTrue(is_composite_credit("Stan Lee / Jack Kirby"))
        self.assertTrue(is_composite_credit("Grant Morrison & Frank Quitely"))
        self.assertTrue(is_composite_credit("Brian Michael Bendis and Mark Bagley"))
        self.assertFalse(is_composite_credit("Stan Lee"))
        self.assertFalse(is_composite_credit("Jack Kirby"))

        # Check raw credit in ext_creator_credits
        cur = self.conn.cursor()
        cur.execute("SELECT RawCreditName, NameRecordID, CreatorEntityID FROM ext_creator_credits WHERE CreditID = 505")
        row = cur.fetchone()
        self.assertEqual(row[0], "Stan Lee / Jack Kirby")
        self.assertEqual(row[1], 107)
        self.assertIsNone(row[2])


# =============================================================================
# Audit Area 2: Transaction and Recovery
# =============================================================================

class TestC417TransactionAndRecovery(unittest.TestCase):
    """
    Inject failures at every write boundary and prove atomic rollback.
    Verify restart-safety and persistence across complete database reconnections.
    """

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.db_path = self.temp_db.name
        self.conn = sqlite3.connect(self.db_path)
        setup_complete_test_database(self.conn)
        seed_standard_audit_dataset(self.conn)
        self.mock_db = MockDBWrapper(self.conn)
        self.repo = IdentityRepository(db_conn=self.mock_db)
        self.identity_service = CreatorIdentityService(db_conn=self.mock_db, repository=self.repo)
        self.conflict_service = CreatorConflictService(repository=self.repo, db_conn=self.mock_db)

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def _snapshot_tables(self):
        """Take a full row snapshot of all creator extension tables."""
        cur = self.conn.cursor()
        tables = [
            'ext_creator_entities',
            'ext_creator_name_records',
            'ext_creator_external_ids',
            'ext_creator_candidate_rejections',
            'ext_creator_credits',
            'ext_creator_resolution_audit',
            'comics',
            'issues',
            'annuals',
        ]
        snapshot = {}
        for t in tables:
            cur.execute(f"SELECT * FROM {t} ORDER BY 1")
            snapshot[t] = cur.fetchall()
        return snapshot

    def test_01_confirm_identity_failure_injection_rollback(self):
        """Inject failure during confirmation (audit insertion failure) and verify complete rollback."""
        snap_before = self._snapshot_tables()

        with patch.object(self.repo, 'insert_audit_entry', side_effect=sqlite3.OperationalError("Injected audit failure")):
            with self.assertRaises(Exception):
                self.identity_service.confirm_provider_identity(
                    name_record_id=102,
                    provider='metron',
                    provider_creator_id='102',
                    provider_display_name='Jack Kirby',
                    actor='reviewer'
                )

        snap_after = self._snapshot_tables()
        self.assertEqual(snap_before, snap_after, "Database must be 100% invariant after confirm failure rollback.")

    def test_02_reject_candidate_failure_injection_rollback(self):
        """Inject failure during candidate rejection and verify complete rollback."""
        snap_before = self._snapshot_tables()

        with patch.object(self.repo, 'insert_audit_entry', side_effect=sqlite3.OperationalError("Injected audit failure")):
            with self.assertRaises(Exception):
                self.identity_service.reject_candidate(
                    name_record_id=103,
                    provider='metron',
                    provider_creator_id='103',
                    reason='Injected test',
                    actor='reviewer'
                )

        snap_after = self._snapshot_tables()
        self.assertEqual(snap_before, snap_after, "Database must be 100% invariant after reject failure rollback.")

    def test_03_transfer_mapping_failure_injection_rollback(self):
        """Inject failure during conflict transfer and verify complete rollback."""
        snap_before = self._snapshot_tables()

        with patch.object(self.repo, 'insert_audit_entry', side_effect=sqlite3.OperationalError("Injected audit failure")):
            with self.assertRaises(Exception):
                self.conflict_service.resolve_conflict_transfer_mapping(
                    name_record_id=105,
                    provider='metron',
                    provider_creator_id='999',
                    actor='admin'
                )

        snap_after = self._snapshot_tables()
        self.assertEqual(snap_before, snap_after, "Database must be 100% invariant after transfer failure rollback.")

    def test_04_reverse_transfer_failure_injection_rollback(self):
        """Inject failure during transfer reversal and verify complete rollback."""
        # 1. Perform successful transfer
        self.conflict_service.resolve_conflict_transfer_mapping(
            name_record_id=105,
            provider='metron',
            provider_creator_id='999',
            actor='admin'
        )
        snap_before_rev = self._snapshot_tables()

        # 2. Inject failure on reversal
        with patch.object(self.repo, 'insert_audit_entry', side_effect=sqlite3.OperationalError("Injected audit failure")):
            with self.assertRaises(Exception):
                self.conflict_service.reverse_conflict_transfer_mapping(
                    name_record_id=105,
                    provider='metron',
                    provider_creator_id='999',
                    actor='admin'
                )

        snap_after_rev = self._snapshot_tables()
        self.assertEqual(snap_before_rev, snap_after_rev, "Database must remain in transferred state after failed reversal.")

    def test_05_restart_persistence_and_cache_overrides(self):
        """Simulate application restarts between lifecycle transitions and verify persistent truth."""
        # Step 1: Confirm Jack Kirby
        self.identity_service.confirm_provider_identity(102, 'metron', '102', provider_display_name='Jack Kirby', actor='user')

        # Restart simulation: Close connection and re-open from disk
        self.conn.close()
        self.conn = sqlite3.connect(self.db_path)
        self.mock_db = MockDBWrapper(self.conn)
        self.repo = IdentityRepository(db_conn=self.mock_db)
        self.identity_service = CreatorIdentityService(db_conn=self.mock_db, repository=self.repo)
        self.history_service = CreatorHistoryService(repository=self.repo, db_conn=self.mock_db)

        # Verify persistent state survived restart
        state = self.repo.derive_pairing_state(102, 'metron', '102')
        self.assertEqual(state['state'], 'confirmed')
        self.assertEqual(state['local_display_name'], 'Jack Kirby')
        hist = self.history_service.get_decision_history(102, 'metron', '102')
        self.assertEqual(len(hist['timeline']), 1)
        self.assertEqual(hist['timeline'][0]['action'], 'CONFIRM_PROVIDER_IDENTITY')

        # Step 2: Transfer Metron 999 to Robert Harras (Record 105)
        self.conflict_service = CreatorConflictService(repository=self.repo, db_conn=self.mock_db)
        self.conflict_service.resolve_conflict_transfer_mapping(105, 'metron', '999', actor='admin')

        # Restart simulation
        self.conn.close()
        self.conn = sqlite3.connect(self.db_path)
        self.mock_db = MockDBWrapper(self.conn)
        self.repo = IdentityRepository(db_conn=self.mock_db)
        self.conflict_service = CreatorConflictService(repository=self.repo, db_conn=self.mock_db)

        # Verify transferred state survived restart
        state_105 = self.repo.derive_pairing_state(105, 'metron', '999')
        self.assertEqual(state_105['state'], 'transferred')

        # Step 3: Provider-derived mock data cannot override local database truth
        candidate_service = CreatorCandidateService(db_conn=self.mock_db, repository=self.repo)
        mock_external_snapshot = [
            {'creator_id': '999', 'name': 'Robert Harras', 'raw_creator_name': 'Robert Harras', 'role': 'editor', 'canonical_role': 'editor', 'raw_role': 'Editor', 'is_cover_credit': False}
        ]
        cand_res = candidate_service.build_candidates(issue_id='105544', is_annual=0, provider='metron', provider_snapshot={'credits': mock_external_snapshot})
        cand = next((c for c in cand_res['identity_candidates'] if c['name_record_id'] == 105 and c['provider_creator_id'] == '999'), None)
        self.assertIsNotNone(cand)
        # Candidate state MUST be 'transferred' from local DB truth, not unlinked or spoofed
        self.assertEqual(cand['candidate_state'], 'transferred')
        self.assertTrue(cand['is_transferred'])

    def test_06_confirm_identity_failure_on_link_name_record_rollback(self):
        """Inject failure during name record linking in confirmation and verify complete rollback."""
        snap_before = self._snapshot_tables()
        with patch.object(self.repo, 'link_name_record', side_effect=sqlite3.OperationalError("Injected link error")):
            with self.assertRaises(Exception):
                self.identity_service.confirm_provider_identity(102, 'metron', '102', actor='reviewer')
        snap_after = self._snapshot_tables()
        self.assertEqual(snap_before, snap_after)

    def test_07_confirm_identity_failure_on_update_credits_rollback(self):
        """Inject failure during credit cache update in confirmation and verify complete rollback."""
        snap_before = self._snapshot_tables()
        with patch.object(self.repo, 'update_credits_entity', side_effect=sqlite3.OperationalError("Injected credit error")):
            with self.assertRaises(Exception):
                self.identity_service.confirm_provider_identity(102, 'metron', '102', actor='reviewer')
        snap_after = self._snapshot_tables()
        self.assertEqual(snap_before, snap_after)

    def test_08_reverse_confirmation_failure_on_unlink_rollback(self):
        """Inject failure during unlinking in reversal and verify complete rollback."""
        self.identity_service.confirm_provider_identity(102, 'metron', '102', actor='reviewer')
        snap_before = self._snapshot_tables()
        with patch.object(self.repo, 'unlink_name_record', side_effect=sqlite3.OperationalError("Injected unlink error")):
            with self.assertRaises(Exception):
                self.identity_service.reverse_provider_confirmation(102, 'metron', '102', actor='reviewer')
        snap_after = self._snapshot_tables()
        self.assertEqual(snap_before, snap_after)

    def test_09_reverse_confirmation_failure_on_delete_ext_id_rollback(self):
        """Inject failure during external ID cleanup in reversal and verify complete rollback."""
        self.identity_service.confirm_provider_identity(102, 'metron', '102', actor='reviewer')
        snap_before = self._snapshot_tables()
        with patch.object(self.repo, 'delete_external_id', side_effect=sqlite3.OperationalError("Injected delete ext ID error")):
            with self.assertRaises(Exception):
                self.identity_service.reverse_provider_confirmation(102, 'metron', '102', actor='reviewer')
        snap_after = self._snapshot_tables()
        self.assertEqual(snap_before, snap_after)


# =============================================================================
# Audit Area 3: Database Integrity
# =============================================================================

class TestC417DatabaseIntegrity(unittest.TestCase):
    """
    Verify SQLite database integrity, foreign keys, unique constraints,
    pre-C4 migration idempotency, exact table deltas, and core table invariance.
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        setup_complete_test_database(self.conn)
        seed_standard_audit_dataset(self.conn)
        self.mock_db = MockDBWrapper(self.conn)
        self.repo = IdentityRepository(db_conn=self.mock_db)
        self.identity_service = CreatorIdentityService(db_conn=self.mock_db, repository=self.repo)
        self.conflict_service = CreatorConflictService(repository=self.repo, db_conn=self.mock_db)

    def tearDown(self):
        self.conn.close()

    def test_01_sqlite_integrity_and_foreign_keys(self):
        """Run PRAGMA integrity_check and foreign_key_check."""
        cur = self.conn.cursor()
        cur.execute("PRAGMA integrity_check")
        res = cur.fetchall()
        self.assertEqual(res, [('ok',)], f"SQLite integrity check failed: {res}")

        cur.execute("PRAGMA foreign_key_check")
        fk_errors = cur.fetchall()
        self.assertEqual(fk_errors, [], f"Foreign key check found violations: {fk_errors}")

    def test_02_unique_provider_external_id_constraint(self):
        """Verify UNIQUE(Provider, ExternalID) constraint strictly prevents duplicate mappings."""
        cur = self.conn.cursor()
        with self.assertRaises(sqlite3.IntegrityError):
            cur.execute("INSERT INTO ext_creator_external_ids (CreatorEntityID, Provider, ExternalID) VALUES (1, 'metron', '999')")

    def test_03_database_reconciliation(self):
        """Reconcile active mappings, name record links, rejections, credits, and audit entries."""
        cur = self.conn.cursor()

        # 1. All external mappings point to valid CreatorEntityID
        cur.execute("""
            SELECT m.ExternalMappingID, m.CreatorEntityID
            FROM ext_creator_external_ids m
            LEFT JOIN ext_creator_entities e ON m.CreatorEntityID = e.CreatorEntityID
            WHERE e.CreatorEntityID IS NULL
        """)
        self.assertEqual(cur.fetchall(), [], "Found orphaned external ID mappings.")

        # 2. All name record links point to valid CreatorEntityID or are NULL
        cur.execute("""
            SELECT nr.NameRecordID, nr.CreatorEntityID
            FROM ext_creator_name_records nr
            LEFT JOIN ext_creator_entities e ON nr.CreatorEntityID = e.CreatorEntityID
            WHERE nr.CreatorEntityID IS NOT NULL AND e.CreatorEntityID IS NULL
        """)
        self.assertEqual(cur.fetchall(), [], "Found orphaned name record entity links.")

        # 3. All rejections point to valid NameRecordID
        cur.execute("""
            SELECT r.RejectionID, r.NameRecordID
            FROM ext_creator_candidate_rejections r
            LEFT JOIN ext_creator_name_records nr ON r.NameRecordID = nr.NameRecordID
            WHERE nr.NameRecordID IS NULL
        """)
        self.assertEqual(cur.fetchall(), [], "Found orphaned candidate rejections.")

        # 4. Derived credit references match linked name records
        cur.execute("""
            SELECT c.CreditID, c.NameRecordID, c.CreatorEntityID, nr.CreatorEntityID
            FROM ext_creator_credits c
            JOIN ext_creator_name_records nr ON c.NameRecordID = nr.NameRecordID
            WHERE (c.CreatorEntityID IS NOT nr.CreatorEntityID)
        """)
        self.assertEqual(cur.fetchall(), [], "Credit CreatorEntityID does not match NameRecord CreatorEntityID.")

    def test_04_pre_c4_migration_idempotency(self):
        """Test running migrations on clean pre-C4 legacy database fixture multiple times."""
        fresh_conn = sqlite3.connect(":memory:")
        create_pre_c4_database(fresh_conn)
        fresh_cur = fresh_conn.cursor()

        # Run 1: Should create all extension tables cleanly
        run_extension_migrations(fresh_cur)
        fresh_conn.commit()

        # Populate sample data
        seed_standard_audit_dataset(fresh_conn)

        # Run 2: Re-running migrations should be idempotent with zero errors or duplicates
        run_extension_migrations(fresh_cur)
        fresh_conn.commit()

        # Run 3: Re-running migrations a third time
        run_extension_migrations(fresh_cur)
        fresh_conn.commit()

        fresh_cur.execute("SELECT COUNT(*) FROM ext_creator_entities")
        self.assertEqual(fresh_cur.fetchone()[0], 3)
        fresh_conn.close()

    def test_05_table_delta_accounting_and_unrelated_table_invariance(self):
        """Record table row deltas for explicit actions and prove unrelated tables are invariant."""
        cur = self.conn.cursor()

        def get_counts():
            tables = ['ext_creator_entities', 'ext_creator_name_records', 'ext_creator_external_ids',
                      'ext_creator_candidate_rejections', 'ext_creator_resolution_audit',
                      'comics', 'issues', 'annuals']
            counts = {}
            for t in tables:
                cur.execute(f"SELECT COUNT(*) FROM {t}")
                counts[t] = cur.fetchone()[0]
            return counts

        c0 = get_counts()

        # Action: Confirm Identity (Record 102)
        self.identity_service.confirm_provider_identity(102, 'metron', '102', provider_display_name='Jack Kirby', actor='user')
        c1 = get_counts()
        self.assertEqual(c1['ext_creator_entities'], c0['ext_creator_entities'] + 1, "Confirm adds 1 entity")
        self.assertEqual(c1['ext_creator_external_ids'], c0['ext_creator_external_ids'] + 1, "Confirm adds 1 external ID")
        self.assertEqual(c1['ext_creator_resolution_audit'], c0['ext_creator_resolution_audit'] + 1, "Confirm adds 1 audit")
        self.assertEqual(c1['comics'], c0['comics'], "Core comics table invariant")
        self.assertEqual(c1['issues'], c0['issues'], "Core issues table invariant")
        self.assertEqual(c1['annuals'], c0['annuals'], "Core annuals table invariant")

        # Action: Reject Candidate (Record 103)
        self.identity_service.reject_candidate(103, 'metron', '103', reason='Test', actor='user')
        c2 = get_counts()
        self.assertEqual(c2['ext_creator_candidate_rejections'], c1['ext_creator_candidate_rejections'] + 1, "Reject adds 1 rejection")
        self.assertEqual(c2['ext_creator_resolution_audit'], c1['ext_creator_resolution_audit'] + 1, "Reject adds 1 audit")
        self.assertEqual(c2['comics'], c0['comics'], "Core comics table invariant")
        self.assertEqual(c2['issues'], c0['issues'], "Core issues table invariant")


# =============================================================================
# Audit Area 4: HTTP and Security
# =============================================================================

class TestC417HttpAndSecurity(unittest.TestCase):
    """
    Audit all creator HTTP routes for method enforcement, CSRF protection,
    input bounds, rejection of client entity claims, and error sanitization.
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        setup_complete_test_database(self.conn)
        seed_standard_audit_dataset(self.conn)
        self.mock_db = MockDBWrapper(self.conn)
        self.repo = IdentityRepository(db_conn=self.mock_db)
        self.identity_service = CreatorIdentityService(db_conn=self.mock_db, repository=self.repo)
        self.conflict_service = CreatorConflictService(repository=self.repo, db_conn=self.mock_db)
        self.history_service = CreatorHistoryService(repository=self.repo, db_conn=self.mock_db)
        self.registry_service = CreatorRegistryService(repository=self.repo, db_conn=self.mock_db)
        self.orig_config = mylar.CONFIG
        mock_cfg = MagicMock()
        mock_cfg.METRON_ENABLED = True
        mock_cfg.METRON_API_KEY = 'test_key'
        mock_cfg.METRON_USER_AGENT = 'Mylar/1.0'
        mock_cfg.METRON_HOST = 'https://metron.cloud'
        mylar.CONFIG = mock_cfg

    def tearDown(self):
        mylar.CONFIG = self.orig_config
        self.conn.close()

    def test_01_method_enforcement(self):
        """Mutations must reject GET requests (405 Method Not Allowed)."""
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'GET'

            # 1. confirmCreatorIdentity
            res = json.loads(handle_confirm_creator_identity(name_record_id=102, provider='metron', provider_creator_id='102', service=self.identity_service))
            self.assertFalse(res['success'])
            self.assertEqual(res.get('status_code'), 405)

            # 2. rejectCreatorCandidate
            res = json.loads(handle_reject_creator_candidate(name_record_id=103, provider='metron', provider_creator_id='103', service=self.identity_service))
            self.assertFalse(res['success'])
            self.assertEqual(res.get('status_code'), 405)

            # 3. reverseCreatorDecision
            res = json.loads(handle_reverse_creator_decision(name_record_id=101, provider='metron', provider_creator_id='101', service=self.identity_service))
            self.assertFalse(res['success'])
            self.assertEqual(res.get('status_code'), 405)

            # 4. resolveCreatorConflict
            res = json.loads(handle_resolve_creator_conflict(name_record_id=105, provider='metron', provider_creator_id='999', action='keep_existing_reject_competing', service=self.conflict_service))
            self.assertFalse(res['success'])
            self.assertEqual(res.get('status_code'), 405)

    def test_02_csrf_protection_on_mutations(self):
        """Mutations without valid CSRF token must be rejected with 403 Forbidden."""
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'POST'
            mock_req.headers = {}

            # Missing token
            res = json.loads(handle_confirm_creator_identity(name_record_id=102, provider='metron', provider_creator_id='102', csrf_token=None, service=self.identity_service))
            self.assertFalse(res['success'])
            self.assertEqual(res.get('status_code'), 403)
            self.assertEqual(res.get('error_code'), 'invalid_csrf_token')

            # Invalid token
            res = json.loads(handle_confirm_creator_identity(name_record_id=102, provider='metron', provider_creator_id='102', csrf_token='invalid_csrf_token', service=self.identity_service))
            self.assertFalse(res['success'])
            self.assertEqual(res.get('status_code'), 403)

    def test_03_input_bounds_and_client_claim_rejection(self):
        """Test bounds validation, negative IDs, and rejection of client entity claims."""
        token = get_or_create_csrf_token()
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'POST'
            mock_req.headers = {'X-CSRF-Token': token}

            # Negative ID
            res = json.loads(handle_confirm_creator_identity(name_record_id=-5, provider='metron', provider_creator_id='102', csrf_token=token, service=self.identity_service))
            self.assertFalse(res['success'])
            self.assertEqual(res.get('status_code'), 400)

            # Non-integer ID
            res = json.loads(handle_confirm_creator_identity(name_record_id='<script>alert(1)</script>', provider='metron', provider_creator_id='102', csrf_token=token, service=self.identity_service))
            self.assertFalse(res['success'])
            self.assertEqual(res.get('status_code'), 400)

            # Client passing fake CreatorEntityID or claim must be ignored
            with patch('mylar.extensions.creators.decision_controller._check_metron_gate'):
                res_conf = json.loads(handle_confirm_creator_identity(
                    name_record_id=102,
                    provider='metron',
                    provider_creator_id='102',
                    creator_entity_id=99999, # Fake browser-supplied claim
                    csrf_token=token,
                    service=self.identity_service
                ))
                self.assertTrue(res_conf['success'])
                # Server creates or binds authoritative entity, not client's 99999
                cur = self.conn.cursor()
                cur.execute("SELECT CreatorEntityID FROM ext_creator_name_records WHERE NameRecordID = 102")
                actual_entity_id = cur.fetchone()[0]
                self.assertNotEqual(actual_entity_id, 99999)

    def test_04_sanitized_errors_and_zero_credential_leakage(self):
        """Errors must be sanitized and never expose credentials or database internals."""
        token = get_or_create_csrf_token()
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'POST'
            mock_req.headers = {'X-CSRF-Token': token}

            with patch('mylar.extensions.creators.decision_controller._check_metron_gate'):
                # Non-existent record
                res = json.loads(handle_confirm_creator_identity(name_record_id=88888, provider='metron', provider_creator_id='102', csrf_token=token, service=self.identity_service))
                self.assertFalse(res['success'])
                self.assertEqual(res.get('status_code'), 404)
                self.assertNotIn("Traceback", res.get('error', ''))
                self.assertNotIn("password", json.dumps(res).lower())
                self.assertNotIn("api_key", json.dumps(res).lower())

    def test_05_get_creator_decision_history_route_audit(self):
        """Audit /getCreatorDecisionHistory route for parameter bounds and error responses."""
        # 1. Valid history retrieval
        res_raw = handle_get_creator_decision_history(name_record_id=101, service=self.history_service)
        res = json.loads(res_raw)
        self.assertTrue(res['success'])
        self.assertEqual(res['name_record_id'], 101)

        # 2. Non-existent name record (404)
        res_404_raw = handle_get_creator_decision_history(name_record_id=99999, service=self.history_service)
        res_404 = json.loads(res_404_raw)
        self.assertFalse(res_404['success'])
        self.assertEqual(res_404['error_code'], 'record_not_found')

        # 3. Invalid name record ID (400)
        res_400_raw = handle_get_creator_decision_history(name_record_id='invalid_id', service=self.history_service)
        res_400 = json.loads(res_400_raw)
        self.assertFalse(res_400['success'])
        self.assertEqual(res_400['error_code'], 'invalid_input')

    def test_06_creator_registry_route_audit(self):
        """Audit /creator_registry and /getCreatorRegistry routes."""
        # 1. Valid JSON registry retrieval
        res_raw = handle_get_creator_registry_json(state='all', service=self.registry_service)
        res = json.loads(res_raw)
        self.assertTrue(res['success'])
        self.assertGreaterEqual(res['total_entries'], 1)

        # 2. Invalid state filter
        res_bad = json.loads(handle_get_creator_registry_json(state='bad_state_value', service=self.registry_service))
        self.assertFalse(res_bad['success'])
        self.assertEqual(res_bad['error_code'], 'invalid_input')

    def test_07_get_creator_conflict_analysis_route_audit(self):
        """Audit /getCreatorConflictAnalysis route."""
        # 1. Valid conflict query
        res_raw = handle_get_creator_conflict_analysis(name_record_id=105, provider='metron', provider_creator_id='999', service=self.conflict_service)
        res = json.loads(res_raw)
        self.assertTrue(res['success'])
        self.assertEqual(res['derived_state'], 'conflicted')

        # 2. Invalid provider
        res_bad = json.loads(handle_get_creator_conflict_analysis(name_record_id=105, provider='bad_provider', service=self.conflict_service))
        self.assertFalse(res_bad['success'])
        self.assertEqual(res_bad['error_code'], 'invalid_input')

    def test_08_browser_catalog_and_detail_routes_audit(self):
        """Audit /creators and /creator_detail catalog routes."""
        with patch('mylar.extensions.creators.browser_controller.CreatorBrowserService') as MockBrowserService:
            mock_svc = MockBrowserService.return_value
            mock_svc.get_creator_catalog.return_value = {'total_records': 10, 'page': 1, 'creators': []}
            mock_svc.get_creator_detail.return_value = {'found': True, 'raw_name': 'Jack Kirby', 'credits': []}

            # 1. Catalog JSON
            cat_json = handle_creator_catalog(search='Kirby', format='json')
            cat_res = json.loads(cat_json)
            self.assertEqual(cat_res['total_records'], 10)

            # 2. Detail JSON
            det_json = handle_creator_detail(name_record_id=102, format='json')
            det_res = json.loads(det_json)
            self.assertTrue(det_res['found'])

    def test_09_indexer_controller_routes_audit(self):
        """Audit CreatorController series indexing API."""
        with patch('mylar.extensions.creators.controller.CreatorIndexWorker') as MockWorker, \
             patch('mylar.extensions.creators.controller.CreatorService') as MockService:
            worker_mock = MockWorker.return_value
            worker_mock.start_series_scan.return_value = {'status': 'started', 'job_id': 'job-123'}
            worker_mock.get_status.return_value = {'status': 'idle', 'total_processed': 5}
            worker_mock.cancel.return_value = {'status': 'cancelled'}

            svc_mock = MockService.return_value
            svc_mock.get_series_creator_summary.return_value = {'comic_id': '5535', 'total_creators': 4}

            ctrl = CreatorController()
            self.assertEqual(ctrl.get_index_status('job-123')['status'], 'idle')
            self.assertEqual(ctrl.get_series_summary('5535')['total_creators'], 4)

    def test_10_reverse_decision_client_id_spoofing_attack(self):
        """
        Attack Test: ReverseCreatorDecision must derive the reversible decision exclusively
        from server-side persisted state and ignore spoofed client IDs belonging to other pairings.
        """
        token = get_or_create_csrf_token()

        # Setup: Pairing A (Record 103, Metron 103) is rejected (Rejection ID 1)
        rej_a = self.identity_service.reject_candidate(103, 'metron', '103', reason='Reject A', actor='user')
        rej_a_id = rej_a['rejection_id']

        # Setup: Pairing B (Record 102, Metron 102) is rejected (Rejection ID 2)
        rej_b = self.identity_service.reject_candidate(102, 'metron', '102', reason='Reject B', actor='user')
        rej_b_id = rej_b['rejection_id']

        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'POST'
            mock_req.headers = {'X-CSRF-Token': token}

            # Attacker sends reverse request for Pairing A (Record 103, Metron 103),
            # but supplies rejection_id = rej_b_id (belonging to Record 102).
            res_raw = handle_reverse_creator_decision(
                name_record_id=103,
                provider='metron',
                provider_creator_id='103',
                rejection_id=rej_b_id, # Spoofed ID belonging to pairing B
                csrf_token=token,
                repository=self.repo,
                service=self.identity_service
            )
            res = json.loads(res_raw)
            self.assertTrue(res['success'])
            # Pairing A's own rejection ID is returned, NOT Pairing B's
            self.assertEqual(res['rejection_id'], rej_a_id)

            # Pairing B remains strictly active and UNCHANGED
            pairing_b_state = self.repo.derive_pairing_state(102, 'metron', '102')
            self.assertEqual(pairing_b_state['state'], 'rejected')
            rej_b_current = self.repo.get_rejection_by_id(rej_b_id)
            self.assertEqual(rej_b_current['status'], 'active')

    def test_11_conflict_action_contract_complete_allowlist(self):
        """
        Verify the complete /resolveCreatorConflict action allowlist:
        - keep_existing_reject_competing
        - transfer_mapping
        - reverse_transfer
        Ensures POST enforcement, CSRF protection, and exact-pair validation on each action.
        """
        token = get_or_create_csrf_token()

        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'POST'
            mock_req.headers = {'X-CSRF-Token': token}

            # 1. Action: keep_existing_reject_competing
            res1 = json.loads(handle_resolve_creator_conflict(
                name_record_id=105,
                provider='metron',
                provider_creator_id='999',
                action='keep_existing_reject_competing',
                csrf_token=token,
                service=self.conflict_service
            ))
            self.assertTrue(res1['success'])
            self.assertEqual(res1['action'], 'keep_existing_reject_competing')

            # 2. Action: transfer_mapping
            res2 = json.loads(handle_resolve_creator_conflict(
                name_record_id=105,
                provider='metron',
                provider_creator_id='999',
                action='transfer_mapping',
                csrf_token=token,
                service=self.conflict_service
            ))
            self.assertTrue(res2['success'])
            self.assertEqual(res2['action'], 'transfer_mapping')

            # 3. Action: reverse_transfer
            res3 = json.loads(handle_resolve_creator_conflict(
                name_record_id=105,
                provider='metron',
                provider_creator_id='999',
                action='reverse_transfer',
                csrf_token=token,
                service=self.conflict_service
            ))
            self.assertTrue(res3['success'])
            self.assertEqual(res3['action'], 'reverse_transfer')

            # 4. Disallowed action rejected with 400 Bad Request
            res_bad = json.loads(handle_resolve_creator_conflict(
                name_record_id=105,
                provider='metron',
                provider_creator_id='999',
                action='unsupported_exploit_action',
                csrf_token=token,
                service=self.conflict_service
            ))
            self.assertFalse(res_bad['success'])
            self.assertEqual(res_bad['status_code'], 400)
            self.assertEqual(res_bad['error_code'], 'invalid_action')

    def test_12_index_creators_csrf_and_method_enforcement(self):
        """
        Verify /indexCreators and /indexCreatorsCancel enforce POST-only method
        and require valid CSRF tokens, rejecting unauthorized requests with 405 / 403.
        """
        token = get_or_create_csrf_token()

        with patch('mylar.extensions.creators.controller.CreatorIndexWorker') as MockWorker:
            worker_mock = MockWorker.return_value
            worker_mock.start_series_scan.return_value = {'status': 'started', 'job_id': 'job-abc'}
            worker_mock.cancel.return_value = {'status': 'cancelled', 'job_id': 'job-abc'}

            ctrl = CreatorController()

            with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
                # 1. GET requests must be rejected with 405
                mock_req.method = 'GET'
                mock_req.headers = {}
                res_get = ctrl.start_series_index('5535')
                self.assertEqual(res_get['status_code'], 405)
                self.assertEqual(res_get['error_code'], 'method_not_allowed')

                res_get_cancel = ctrl.cancel_index('job-abc')
                self.assertEqual(res_get_cancel['status_code'], 405)
                self.assertEqual(res_get_cancel['error_code'], 'method_not_allowed')

                # 2. POST without CSRF token must be rejected with 403
                mock_req.method = 'POST'
                mock_req.headers = {}
                res_no_csrf = ctrl.start_series_index('5535', csrf_token=None)
                self.assertEqual(res_no_csrf['status_code'], 403)
                self.assertEqual(res_no_csrf['error_code'], 'invalid_csrf_token')

                res_no_csrf_cancel = ctrl.cancel_index('job-abc', csrf_token=None)
                self.assertEqual(res_no_csrf_cancel['status_code'], 403)
                self.assertEqual(res_no_csrf_cancel['error_code'], 'invalid_csrf_token')

                # 3. POST with invalid CSRF token must be rejected with 403
                res_bad_csrf = ctrl.start_series_index('5535', csrf_token='invalid_token')
                self.assertEqual(res_bad_csrf['status_code'], 403)
                self.assertEqual(res_bad_csrf['error_code'], 'invalid_csrf_token')

                # 4. POST with valid CSRF token succeeds
                mock_req.headers = {'X-CSRF-Token': token}
                res_ok = ctrl.start_series_index('5535', csrf_token=token)
                self.assertEqual(res_ok['status'], 'started')

                res_cancel_ok = ctrl.cancel_index('job-abc', csrf_token=token)
                self.assertEqual(res_cancel_ok['status'], 'cancelled')


# =============================================================================
# Audit Area 5: Network and Provider Gates
# =============================================================================

class TestC417NetworkAndProviderGates(unittest.TestCase):
    """
    Prove zero network calls during all identity operations.
    Prove credential gating closes provider comparison while local data remains 100% accessible.
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        setup_complete_test_database(self.conn)
        seed_standard_audit_dataset(self.conn)
        self.mock_db = MockDBWrapper(self.conn)
        self.repo = IdentityRepository(db_conn=self.mock_db)
        self.identity_service = CreatorIdentityService(db_conn=self.mock_db, repository=self.repo)
        self.conflict_service = CreatorConflictService(repository=self.repo, db_conn=self.mock_db)
        self.registry_service = CreatorRegistryService(repository=self.repo, db_conn=self.mock_db)
        self.history_service = CreatorHistoryService(repository=self.repo, db_conn=self.mock_db)
        self.candidate_service = CreatorCandidateService(db_conn=self.mock_db, repository=self.repo)
        self.orig_config = mylar.CONFIG
        mock_cfg = MagicMock()
        mock_cfg.METRON_ENABLED = True
        mock_cfg.METRON_API_KEY = 'test_key'
        mylar.CONFIG = mock_cfg

    def tearDown(self):
        mylar.CONFIG = self.orig_config
        self.conn.close()

    def test_01_zero_network_calls_during_all_c4_operations(self):
        """Ensure no network connections are attempted during any identity resolution lifecycle."""
        with patch('socket.socket.connect', side_effect=AssertionError("Network socket connection attempted during C4 operation!")):
            with patch('urllib.request.urlopen', side_effect=AssertionError("urllib request attempted during C4 operation!")):
                # 1. Candidate Discovery
                p_creds = [{'creator_id': '102', 'name': 'Jack Kirby', 'raw_creator_name': 'Jack Kirby', 'role': 'penciller', 'canonical_role': 'penciller', 'raw_role': 'Pencils', 'is_cover_credit': False}]
                self.candidate_service.build_candidates('105544', 0, 'metron', {'credits': p_creds})

                # 2. Confirmation
                self.identity_service.confirm_provider_identity(102, 'metron', '102', actor='user')

                # 3. Decision History
                self.history_service.get_decision_history(102, 'metron', '102')

                # 4. Registry
                self.registry_service.get_registry_entries(state='all')

                # 5. Conflict Analysis
                self.conflict_service.analyze_creator_conflict(105, 'metron', '999')

                # 6. Conflict Transfer
                self.conflict_service.resolve_conflict_transfer_mapping(105, 'metron', '999', actor='admin')

                # 7. Reverse Transfer
                self.conflict_service.reverse_conflict_transfer_mapping(105, 'metron', '999', actor='admin')

                # 8. Reversal
                self.identity_service.reverse_provider_confirmation(102, 'metron', '102', actor='user')

    def test_02_credential_gating_and_offline_data_readability(self):
        """When Metron credentials are disabled, local registry and history remain 100% accessible."""
        # Simulate disabled Metron config
        mylar.CONFIG.METRON_ENABLED = False

        # Local persistent registry and history MUST remain readable and functional
        reg = self.registry_service.get_registry_entries(state='all')
        self.assertGreaterEqual(reg['total_entries'], 1)

        hist = self.history_service.get_decision_history(101, 'metron', '101')
        self.assertEqual(hist['current_state']['status'], 'confirmed')


if __name__ == '__main__':
    unittest.main()
