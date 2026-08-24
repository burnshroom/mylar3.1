"""
Unit and Integration Tests for Phase C4.8: Creator Identity Resolution Persistence Foundation.

Tests all persistence, rejection suppression, explicit confirmation, safe reversal,
atomic transactions, collision handling, and audit trail functionality using isolated
temporary SQLite databases.
"""

import os
import sys
import json
import sqlite3
import tempfile
import unittest

# Ensure project root and lib are in sys.path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.join(BASE_DIR, 'lib'))
sys.path.insert(0, BASE_DIR)

from mylar.extensions.migrations.runner import run_extension_migrations
from mylar.extensions.migrations.versions import creators, creator_identity_resolution
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


class MockDBWrapper:
    """Wrapper exposing .connection and .conn for compatibility with Mylar DB conventions."""
    def __init__(self, connection):
        self.connection = connection
        self.conn = connection


class TestPhaseC48IdentityPersistence(unittest.TestCase):
    """Test suite for Creator Identity Resolution Persistence Foundation."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix='.db')
        self.conn = sqlite3.connect(self.db_path)
        self.cursor = self.conn.cursor()
        self.db_wrapper = MockDBWrapper(self.conn)
        self.repo = IdentityRepository(db_conn=self.db_wrapper)
        self.service = CreatorIdentityService(db_conn=self.db_wrapper, repository=self.repo)

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

    def _apply_all_migrations(self):
        run_extension_migrations(self.cursor)
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

    def _insert_credit(self, name_record_id, issue_id="101", comic_id="501", role="writer", raw_credit_name="Test", entity_id=None):
        self.cursor.execute(
            """
            INSERT INTO ext_creator_credits
            (NameRecordID, CreatorEntityID, IssueID, ComicID, IsAnnual, Role, RawRoleText, RawCreditName, IsCover, SortOrder, SourceProvenance, SourceRevision, CreatedAt)
            VALUES (?, ?, ?, ?, 0, ?, ?, ?, 0, 0, 'comicinfo', 'rev1', CURRENT_TIMESTAMP)
            """,
            (name_record_id, entity_id, issue_id, comic_id, role, role, raw_credit_name)
        )
        self.conn.commit()
        return self.cursor.lastrowid

    # -------------------------------------------------------------------------
    # 1 & 2: Migration Execution & Idempotency
    # -------------------------------------------------------------------------

    def test_01_fresh_migration_creates_tables_and_indexes(self):
        """Verify that running migrations on a fresh database creates both tables and all indexes."""
        self._apply_all_migrations()

        # Check table existence
        self.cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in self.cursor.fetchall()}
        self.assertIn('ext_creator_candidate_rejections', tables)
        self.assertIn('ext_creator_resolution_audit', tables)
        self.assertIn('ext_creator_name_records', tables)
        self.assertIn('ext_creator_entities', tables)
        self.assertIn('ext_creator_external_ids', tables)

        # Check index existence
        self.cursor.execute("SELECT name FROM sqlite_master WHERE type='index'")
        indexes = {row[0] for row in self.cursor.fetchall()}
        self.assertIn('uq_ext_rejections_active', indexes)
        self.assertIn('idx_ext_rejections_lookup', indexes)
        self.assertIn('idx_ext_rejections_namerec', indexes)
        self.assertIn('idx_ext_audit_namerec', indexes)
        self.assertIn('idx_ext_audit_entity', indexes)
        self.assertIn('idx_ext_audit_action', indexes)
        self.assertIn('idx_ext_audit_created', indexes)

    def test_02_migration_is_idempotent(self):
        """Verify that running migrations repeatedly produces zero errors."""
        self._apply_all_migrations()
        # Second run
        self._apply_all_migrations()
        # Third run
        self._apply_all_migrations()

    def test_03_existing_c2_schema_upgrades_without_data_loss(self):
        """Verify that upgrading an existing C2 schema preserves all prior data."""
        # 1. Apply C2 migration only
        creators.upgrade(self.cursor)
        self.conn.commit()

        # 2. Insert test data
        nr_id = self._insert_name_record("Fabian Nicieza")
        self._insert_credit(nr_id, raw_credit_name="Fabian Nicieza")

        # 3. Apply C4.8 migration
        creator_identity_resolution.upgrade(self.cursor)
        self.conn.commit()

        # 4. Verify existing data is 100% intact
        self.cursor.execute("SELECT RawName, CreatorEntityID FROM ext_creator_name_records WHERE NameRecordID = ?", (nr_id,))
        row = self.cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "Fabian Nicieza")
        self.assertIsNone(row[1])

    # -------------------------------------------------------------------------
    # 4 & 5: Candidate Rejection & Idempotency
    # -------------------------------------------------------------------------

    def test_04_reject_candidate_activates_suppression_and_logs_audit(self):
        """Rejecting a candidate marks it active and logs an immutable audit event."""
        self._apply_all_migrations()
        nr_id = self._insert_name_record("Mike Deodato")

        res = self.service.reject_candidate(
            name_record_id=nr_id,
            provider="metron",
            provider_creator_id="1205",
            provider_display_name="Mike Deodato Jr.",
            reason="Wrong generation (father vs son)",
            actor="test_user"
        )

        self.assertIsNotNone(res)
        self.assertEqual(res['status'], 'active')
        self.assertEqual(res['provider'], 'metron')
        self.assertEqual(res['external_id'], '1205')
        self.assertTrue(self.service.is_candidate_rejected(nr_id, "metron", "1205"))

        # Verify audit log
        history = self.service.get_resolution_history(name_record_id=nr_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['action'], 'REJECT_CANDIDATE')
        self.assertEqual(history[0]['actor'], 'test_user')
        self.assertEqual(history[0]['reason'], 'Wrong generation (father vs son)')

    def test_05_repeating_active_rejection_creates_no_duplicate_rows(self):
        """Repeating the same active rejection is idempotent and maintains exactly 1 active rejection row."""
        self._apply_all_migrations()
        nr_id = self._insert_name_record("Mike Deodato")

        res1 = self.service.reject_candidate(nr_id, "metron", "1205", reason="Initial")
        res2 = self.service.reject_candidate(nr_id, "metron", "1205", reason="Duplicate")

        self.assertEqual(res1['rejection_id'], res2['rejection_id'])

        self.cursor.execute(
            "SELECT COUNT(*) FROM ext_creator_candidate_rejections WHERE NameRecordID = ? AND Provider = ? AND ExternalID = ?",
            (nr_id, "metron", "1205")
        )
        count = self.cursor.fetchone()[0]
        self.assertEqual(count, 1)

    # -------------------------------------------------------------------------
    # 6: Reversing Rejection
    # -------------------------------------------------------------------------

    def test_06_reverse_rejection_removes_suppression_and_preserves_history(self):
        """Reversing a rejection sets status='reversed', retains the row, and appends a REVERSE_REJECTION audit event."""
        self._apply_all_migrations()
        nr_id = self._insert_name_record("Andy Kubert")

        rej = self.service.reject_candidate(nr_id, "metron", "500", reason="Mistake")
        self.assertTrue(self.service.is_candidate_rejected(nr_id, "metron", "500"))

        rev = self.service.reverse_candidate_rejection(rej['rejection_id'], actor="admin", reason="User reconsidered")
        self.assertEqual(rev['status'], 'reversed')
        self.assertFalse(self.service.is_candidate_rejected(nr_id, "metron", "500"))

        # Check row remains in database
        self.cursor.execute("SELECT Status, ReversedBy, ReversalReason FROM ext_creator_candidate_rejections WHERE RejectionID = ?", (rej['rejection_id'],))
        row = self.cursor.fetchone()
        self.assertEqual(row[0], 'reversed')
        self.assertEqual(row[1], 'admin')
        self.assertEqual(row[2], 'User reconsidered')

        # Check audit trail has 2 entries (REJECT, REVERSE_REJECTION)
        history = self.service.get_resolution_history(name_record_id=nr_id)
        self.assertEqual(len(history), 2)
        actions = [h['action'] for h in history]
        self.assertIn('REJECT_CANDIDATE', actions)
        self.assertIn('REVERSE_REJECTION', actions)

    # -------------------------------------------------------------------------
    # 7 & 8 & 9: Explicit Confirmation & Entity Independence
    # -------------------------------------------------------------------------

    def test_07_explicit_confirmation_creates_entity_and_external_id(self):
        """Explicit confirmation creates a new creator entity, links the name record, adds external ID, and updates credits."""
        self._apply_all_migrations()
        nr_id = self._insert_name_record("Fabian Nicieza")
        cred_id = self._insert_credit(nr_id, raw_credit_name="Fabian Nicieza")

        res = self.service.confirm_provider_identity(
            name_record_id=nr_id,
            provider="metron",
            provider_creator_id="101",
            provider_display_name="Fabian Nicieza",
            actor="curator",
            confirmation_source="explicit_user"
        )

        self.assertTrue(res['success'])
        entity_id = res['creator_entity_id']
        self.assertIsNotNone(entity_id)

        # Check Name Record is linked
        name_rec = self.repo.get_name_record(nr_id)
        self.assertEqual(name_rec['creator_entity_id'], entity_id)
        self.assertEqual(name_rec['resolution_source'], 'explicit_user')

        # Check Credits are updated
        self.cursor.execute("SELECT CreatorEntityID FROM ext_creator_credits WHERE CreditID = ?", (cred_id,))
        self.assertEqual(self.cursor.fetchone()[0], entity_id)

        # Check External ID mapping exists
        ext_map = self.repo.get_external_id_mapping("metron", "101")
        self.assertIsNotNone(ext_map)
        self.assertEqual(ext_map['creator_entity_id'], entity_id)

        # Check Audit Log
        history = self.service.get_resolution_history(name_record_id=nr_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['action'], 'CONFIRM_PROVIDER_IDENTITY')

    def test_08_name_equality_alone_never_confirms_or_merges_identities(self):
        """Case variations ('Frank Cho' and 'FRANK CHO') remain distinct unlinked records without automatic merge."""
        self._apply_all_migrations()
        nr1 = self._insert_name_record("Frank Cho")
        nr2 = self._insert_name_record("FRANK CHO")

        rec1 = self.repo.get_name_record(nr1)
        rec2 = self.repo.get_name_record(nr2)
        self.assertIsNone(rec1['creator_entity_id'])
        self.assertIsNone(rec2['creator_entity_id'])
        self.assertNotEqual(rec1['name_record_id'], rec2['name_record_id'])

    def test_09_two_identical_names_remain_distinct_until_individually_confirmed(self):
        """Confirming one name record does not automatically link a separate name record with the same display name."""
        self._apply_all_migrations()
        nr1 = self._insert_name_record("Jack Miller")
        nr2 = self._insert_name_record("Jack Miller (1950s)")

        self.service.confirm_provider_identity(
            name_record_id=nr1,
            provider="metron",
            provider_creator_id="801",
            provider_display_name="Jack Miller",
            actor="curator"
        )

        rec1 = self.repo.get_name_record(nr1)
        rec2 = self.repo.get_name_record(nr2)
        self.assertIsNotNone(rec1['creator_entity_id'])
        self.assertIsNone(rec2['creator_entity_id'])

    # -------------------------------------------------------------------------
    # 10 & 11 & 12: Collision Detection & Rejection Guarding
    # -------------------------------------------------------------------------

    def test_10_provider_collision_raises_error_and_rolls_back(self):
        """Attempting to confirm a provider ID to a name record already linked to a different entity raises ConflictingNameRecordLinkError."""
        self._apply_all_migrations()
        nr1 = self._insert_name_record("Chris Claremont")
        nr2 = self._insert_name_record("Jim Starlin")

        # Confirm Claremont to Metron 10
        self.service.confirm_provider_identity(nr1, "metron", "10", provider_display_name="Chris Claremont")
        # Confirm Starlin to Metron 20
        self.service.confirm_provider_identity(nr2, "metron", "20", provider_display_name="Jim Starlin")

        # Attempt to confirm Claremont (already Entity A) to Metron 20 (already mapped to Entity B)
        with self.assertRaises(ConflictingNameRecordLinkError):
            self.service.confirm_provider_identity(nr1, "metron", "20")

    def test_11_active_rejection_blocks_confirmation(self):
        """Attempting to confirm an actively rejected candidate raises ActiveCandidateRejectionError."""
        self._apply_all_migrations()
        nr_id = self._insert_name_record("John Byrne")

        self.service.reject_candidate(nr_id, "metron", "300", reason="Bad match")

        with self.assertRaises(ActiveCandidateRejectionError):
            self.service.confirm_provider_identity(nr_id, "metron", "300")

    def test_12_confirmation_succeeds_after_rejection_reversal(self):
        """Confirmation succeeds cleanly once the active rejection is reversed."""
        self._apply_all_migrations()
        nr_id = self._insert_name_record("John Byrne")

        rej = self.service.reject_candidate(nr_id, "metron", "300", reason="Mistake")
        self.service.reverse_candidate_rejection(rej['rejection_id'], actor="admin")

        res = self.service.confirm_provider_identity(nr_id, "metron", "300", provider_display_name="John Byrne")
        self.assertTrue(res['success'])
        self.assertIsNotNone(res['creator_entity_id'])

    # -------------------------------------------------------------------------
    # 13 & 14: Confirmation Reversal & Shared Entity Preservation
    # -------------------------------------------------------------------------

    def test_13_confirmation_reversal_preserves_raw_name_record(self):
        """Reversing confirmation unlinks CreatorEntityID, restores UNLINKED state, and preserves the raw name record."""
        self._apply_all_migrations()
        nr_id = self._insert_name_record("Matt Ryan")
        cred_id = self._insert_credit(nr_id, raw_credit_name="Matt Ryan")

        conf = self.service.confirm_provider_identity(nr_id, "metron", "700", provider_display_name="Matt Ryan")
        entity_id = conf['creator_entity_id']

        rev = self.service.reverse_provider_confirmation(nr_id, "metron", "700", actor="admin", reason="Incorrect credit")
        self.assertTrue(rev['success'])

        # Name record still exists, but CreatorEntityID is None
        name_rec = self.repo.get_name_record(nr_id)
        self.assertIsNotNone(name_rec)
        self.assertEqual(name_rec['raw_name'], "Matt Ryan")
        self.assertIsNone(name_rec['creator_entity_id'])
        self.assertEqual(name_rec['resolution_source'], 'unresolved')

        # Credit CreatorEntityID is cleared
        self.cursor.execute("SELECT CreatorEntityID FROM ext_creator_credits WHERE CreditID = ?", (cred_id,))
        self.assertIsNone(self.cursor.fetchone()[0])

        # Entity was orphaned and safely cleaned up
        self.assertIsNone(self.repo.get_entity_by_id(entity_id))

        # Check Audit Log contains both CONFIRM and REVERSE_CONFIRMATION
        history = self.service.get_resolution_history(name_record_id=nr_id)
        self.assertEqual(len(history), 2)
        actions = [h['action'] for h in history]
        self.assertIn('CONFIRM_PROVIDER_IDENTITY', actions)
        self.assertIn('REVERSE_CONFIRMATION', actions)

    def test_14_reversal_does_not_remove_shared_entity_still_in_use(self):
        """Reversing confirmation for one name record does NOT delete the shared entity if another name record still references it."""
        self._apply_all_migrations()
        nr1 = self._insert_name_record("Stan Lee")
        nr2 = self._insert_name_record("Stanley Lieber")

        # Confirm Stan Lee -> Metron 1 (creates Entity A)
        conf1 = self.service.confirm_provider_identity(nr1, "metron", "1", provider_display_name="Stan Lee")
        entity_id = conf1['creator_entity_id']

        # Confirm Stanley Lieber -> Metron 1 (reuses Entity A)
        conf2 = self.service.confirm_provider_identity(nr2, "metron", "1", provider_display_name="Stan Lee")
        self.assertEqual(conf2['creator_entity_id'], entity_id)

        # Reverse confirmation for Stanley Lieber only
        rev = self.service.reverse_provider_confirmation(nr2, "metron", "1", actor="admin")
        self.assertTrue(rev['success'])
        self.assertFalse(rev['cleaned_up_entity'])
        self.assertFalse(rev['cleaned_up_external_id'])

        # Entity A and External ID mapping are STILL IN TACT for Stan Lee
        self.assertIsNotNone(self.repo.get_entity_by_id(entity_id))
        self.assertIsNotNone(self.repo.get_external_id_mapping("metron", "1"))

        # Name record 1 remains linked, name record 2 is unlinked
        self.assertEqual(self.repo.get_name_record(nr1)['creator_entity_id'], entity_id)
        self.assertIsNone(self.repo.get_name_record(nr2)['creator_entity_id'])

    # -------------------------------------------------------------------------
    # 15: Composite Names
    # -------------------------------------------------------------------------

    def test_15_composite_names_remain_intact_and_are_never_split(self):
        """Composite credits containing conjunctions remain a single intact raw name record."""
        self._apply_all_migrations()
        composite_name = "Scott Snyder and Nick Dragotta"
        nr_id = self._insert_name_record(composite_name)

        rec = self.repo.get_name_record(nr_id)
        self.assertEqual(rec['raw_name'], composite_name)

        # Confirming as a collaborative studio/entity operates on the whole record without splitting
        conf = self.service.confirm_provider_identity(nr_id, "metron", "9999", provider_display_name="Snyder / Dragotta")
        self.assertTrue(conf['success'])

        rec_after = self.repo.get_name_record(nr_id)
        self.assertEqual(rec_after['raw_name'], composite_name)

    # -------------------------------------------------------------------------
    # 16: Atomic Rollback on Mid-Transaction Failure
    # -------------------------------------------------------------------------

    def test_16_forced_failure_rolls_back_all_changes(self):
        """Mid-transaction failure rolls back entity creation, link update, and audit log atomically."""
        self._apply_all_migrations()
        nr_id = self._insert_name_record("Todd McFarlane")

        # Patch repo.link_name_record to simulate an unexpected mid-transaction SQLite error
        original_link = self.repo.link_name_record

        def mock_failing_link(*args, **kwargs):
            raise sqlite3.OperationalError("Simulated database failure during link")

        self.repo.link_name_record = mock_failing_link

        try:
            with self.assertRaises(sqlite3.OperationalError):
                self.service.confirm_provider_identity(nr_id, "metron", "400")
        finally:
            self.repo.link_name_record = original_link

        # Verify rollback: 0 entities created, 0 external IDs created, 0 audit rows
        self.cursor.execute("SELECT COUNT(*) FROM ext_creator_entities")
        self.assertEqual(self.cursor.fetchone()[0], 0)
        self.cursor.execute("SELECT COUNT(*) FROM ext_creator_external_ids")
        self.assertEqual(self.cursor.fetchone()[0], 0)
        self.cursor.execute("SELECT COUNT(*) FROM ext_creator_resolution_audit")
        self.assertEqual(self.cursor.fetchone()[0], 0)
        self.assertIsNone(self.repo.get_name_record(nr_id)['creator_entity_id'])

    # -------------------------------------------------------------------------
    # 17: Append-Only Audit History
    # -------------------------------------------------------------------------

    def test_17_audit_history_is_append_only(self):
        """Audit history accurately accumulates sequential operations in descending chronological order."""
        self._apply_all_migrations()
        nr_id = self._insert_name_record("Walt Simonson")

        self.service.reject_candidate(nr_id, "metron", "100", reason="Check 1", actor="user1")
        self.service.reverse_candidate_rejection(name_record_id=nr_id, provider="metron", provider_creator_id="100", actor="user2")
        self.service.confirm_provider_identity(nr_id, "metron", "100", actor="user3")
        self.service.reverse_provider_confirmation(nr_id, "metron", "100", actor="user4")

        history = self.service.get_resolution_history(name_record_id=nr_id)
        self.assertEqual(len(history), 4)
        actions = [h['action'] for h in history]
        self.assertEqual(actions, [
            'REVERSE_CONFIRMATION',
            'CONFIRM_PROVIDER_IDENTITY',
            'REVERSE_REJECTION',
            'REJECT_CANDIDATE',
        ])
        actors = [h['actor'] for h in history]
        self.assertEqual(actors, ['user4', 'user3', 'user2', 'user1'])


if __name__ == '__main__':
    unittest.main()
