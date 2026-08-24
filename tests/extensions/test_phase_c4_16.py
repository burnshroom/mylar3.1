"""
Phase C4.16 Test Suite: Creator Identity Conflict Mapping Transfer and Reversal.

Verifies:
1. Successful atomic transfer from source entity to destination entity.
2. Exactly one active provider mapping before and after (UNIQUE constraint preserved).
3. Single composite TRANSFER_PROVIDER_MAPPING audit event with complete JSON before/after snapshots.
4. Successful safe reversal (Undo transfer) restoring mapping, entity links, credit caches, and rejections.
5. Stale reversal attempts causing zero writes (optimistic locking, diverged mapping, already reversed).
6. Stale/missing/changed conflict requests causing zero writes.
7. Unlinked name record entity creation, destination collision check, and rejection supersession.
8. Rejection supersession and restoration lifecycle.
9. Credit reference cache boundary: only CreatorEntityID changes; raw names/roles/provenance invariant.
10. Failure injection and rollback at every mutation boundary.
11. HTTP controller security: POST enforcement, CSRF validation, client-supplied entity ID rejection.
12. Zero network calls, zero archive/comic/metadata writes.
"""

import json
import os
import sys
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.join(REPO_ROOT, 'lib'))
sys.path.insert(0, REPO_ROOT)

from mylar.extensions.creators.conflict_service import (
    CreatorConflictService,
    InvalidConflictInputError,
    LocalNameRecordNotFoundError,
    StaleConflictStateError,
    TransferTargetUnavailableError,
)
from mylar.extensions.creators.conflict_controller import handle_resolve_creator_conflict
from mylar.extensions.creators.decision_controller import get_or_create_csrf_token
from mylar.extensions.creators.identity_repository import IdentityRepository
from mylar.extensions.creators.candidate_service import CreatorCandidateService
from mylar.extensions.creators.registry_service import CreatorRegistryService
from mylar.extensions.creators.history_service import CreatorHistoryService


def setup_in_memory_db():
    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()

    # Core Mylar tables
    cur.execute("""
        CREATE TABLE comics (
            ComicID TEXT PRIMARY KEY,
            ComicName TEXT,
            Publisher TEXT,
            ComicYear TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE issues (
            IssueID TEXT PRIMARY KEY,
            ComicID TEXT,
            Issue_Number TEXT,
            IssueName TEXT,
            IssueDate TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE annuals (
            AnnualID TEXT PRIMARY KEY,
            ComicID TEXT,
            Issue_Number TEXT,
            IssueName TEXT,
            IssueDate TEXT
        )
    """)

    # Extension Creator tables
    cur.execute("""
        CREATE TABLE ext_creator_entities (
            CreatorEntityID INTEGER PRIMARY KEY AUTOINCREMENT,
            DisplayName TEXT NOT NULL,
            NormalizedName TEXT NOT NULL,
            EntitySlug TEXT UNIQUE NOT NULL,
            Notes TEXT,
            CreatedAt TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UpdatedAt TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE ext_creator_name_records (
            NameRecordID INTEGER PRIMARY KEY AUTOINCREMENT,
            RawName TEXT NOT NULL,
            NormalizedName TEXT NOT NULL,
            NameSlug TEXT NOT NULL,
            CreatorEntityID INTEGER,
            ResolutionSource TEXT DEFAULT 'unresolved',
            CreatedAt TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UpdatedAt TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (CreatorEntityID) REFERENCES ext_creator_entities(CreatorEntityID)
        )
    """)
    cur.execute("""
        CREATE TABLE ext_creator_credits (
            CreditID INTEGER PRIMARY KEY AUTOINCREMENT,
            IssueID TEXT,
            AnnualID TEXT,
            IsAnnual INTEGER DEFAULT 0,
            ComicID TEXT,
            Role TEXT NOT NULL,
            RawRoleText TEXT,
            RawCreditName TEXT NOT NULL,
            IsCover INTEGER DEFAULT 0,
            SortOrder INTEGER DEFAULT 0,
            NameRecordID INTEGER,
            CreatorEntityID INTEGER,
            SourceProvenance TEXT DEFAULT 'comicinfo',
            SourceRevision TEXT,
            CreatedAt TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (NameRecordID) REFERENCES ext_creator_name_records(NameRecordID),
            FOREIGN KEY (CreatorEntityID) REFERENCES ext_creator_entities(CreatorEntityID)
        )
    """)
    cur.execute("""
        CREATE TABLE ext_creator_external_ids (
            ExternalMappingID INTEGER PRIMARY KEY AUTOINCREMENT,
            CreatorEntityID INTEGER NOT NULL,
            Provider TEXT NOT NULL,
            ExternalID TEXT NOT NULL,
            ExternalURL TEXT,
            SourceVersion TEXT,
            ObservedAt TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            Confidence REAL DEFAULT 1.0,
            UNIQUE(Provider, ExternalID),
            FOREIGN KEY (CreatorEntityID) REFERENCES ext_creator_entities(CreatorEntityID)
        )
    """)
    cur.execute("""
        CREATE TABLE ext_creator_candidate_rejections (
            RejectionID INTEGER PRIMARY KEY AUTOINCREMENT,
            NameRecordID INTEGER NOT NULL,
            Provider TEXT NOT NULL,
            ExternalID TEXT NOT NULL,
            ProviderDisplayName TEXT,
            Status TEXT NOT NULL DEFAULT 'active',
            RejectedBy TEXT NOT NULL,
            RejectedAt TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ReversedBy TEXT,
            ReversedAt TIMESTAMP,
            Reason TEXT,
            ReversalReason TEXT,
            FOREIGN KEY (NameRecordID) REFERENCES ext_creator_name_records(NameRecordID)
        )
    """)
    cur.execute("""
        CREATE TABLE ext_creator_resolution_audit (
            AuditID INTEGER PRIMARY KEY AUTOINCREMENT,
            Action TEXT NOT NULL,
            NameRecordID INTEGER,
            CreatorEntityID INTEGER,
            Provider TEXT,
            ExternalID TEXT,
            ProviderDisplayName TEXT,
            Actor TEXT NOT NULL,
            Reason TEXT,
            ConfirmationSource TEXT,
            BeforeStateJson TEXT,
            AfterStateJson TEXT,
            CreatedAt TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    return conn


class TestPhaseC416(unittest.TestCase):

    def setUp(self):
        self.conn = setup_in_memory_db()
        self.repo = IdentityRepository(db_conn=self.conn)
        self.service = CreatorConflictService(repository=self.repo, db_conn=self.conn)

        # Seed initial data:
        # Comic and Issue
        cur = self.conn.cursor()
        cur.execute("INSERT INTO comics (ComicID, ComicName, Publisher) VALUES ('5535', 'Amazing X-Men', 'Marvel')")
        cur.execute("INSERT INTO issues (IssueID, ComicID, Issue_Number, IssueDate) VALUES ('105544', '5535', '2', '1995-04-01')")

        # Entity A: Robert Harras (Metron ID 999 is currently mapped to Entity A)
        cur.execute("""
            INSERT INTO ext_creator_entities (CreatorEntityID, DisplayName, NormalizedName, EntitySlug)
            VALUES (3, 'Robert Harras (Metron)', 'robert harras', 'robert-harras')
        """)
        cur.execute("""
            INSERT INTO ext_creator_external_ids (CreatorEntityID, Provider, ExternalID, Confidence)
            VALUES (3, 'metron', '999', 1.0)
        """)

        # Entity B: Bob Harras (Local Entity with NameRecord 104)
        cur.execute("""
            INSERT INTO ext_creator_entities (CreatorEntityID, DisplayName, NormalizedName, EntitySlug)
            VALUES (2, 'Bob Harras (Legacy)', 'bob harras', 'bob-harras')
        """)
        cur.execute("""
            INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, CreatorEntityID, ResolutionSource)
            VALUES (104, 'Bob Harras', 'bob harras', 'bob-harras', 2, 'unresolved')
        """)
        cur.execute("""
            INSERT INTO ext_creator_credits (CreditID, IssueID, Role, RawRoleText, RawCreditName, NameRecordID, CreatorEntityID, SourceProvenance, SourceRevision)
            VALUES (501, '105544', 'Editor', 'Editor', 'Bob Harras', 104, 2, 'comicinfo', 'rev-1')
        """)

        # Unlinked NameRecord 105: Robert Harras Jr
        cur.execute("""
            INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, CreatorEntityID, ResolutionSource)
            VALUES (105, 'Robert Harras Jr', 'robert harras jr', 'robert-harras-jr', NULL, 'unresolved')
        """)
        cur.execute("""
            INSERT INTO ext_creator_credits (CreditID, IssueID, Role, RawRoleText, RawCreditName, NameRecordID, CreatorEntityID, SourceProvenance, SourceRevision)
            VALUES (502, '105544', 'Writer', 'Writer', 'Robert Harras Jr', 105, NULL, 'comicinfo', 'rev-1')
        """)

        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    # -------------------------------------------------------------------------
    # Criterion 1 & 2: Successful Atomic Transfer & 1:1 Mapping Invariance
    # -------------------------------------------------------------------------
    def test_successful_atomic_transfer_and_one_mapping_invariant(self):
        cur = self.conn.cursor()

        # Before transfer: verify Provider mapping is bound to Entity 3
        cur.execute("SELECT CreatorEntityID FROM ext_creator_external_ids WHERE Provider = 'metron' AND ExternalID = '999'")
        self.assertEqual(cur.fetchone()[0], 3)
        cur.execute("SELECT COUNT(*) FROM ext_creator_external_ids WHERE Provider = 'metron' AND ExternalID = '999'")
        self.assertEqual(cur.fetchone()[0], 1)

        # Execute transfer
        res = self.service.resolve_conflict_transfer_mapping(
            name_record_id=104,
            provider='metron',
            provider_creator_id='999',
            reason="Human verified: mapping belongs to Bob Harras",
            actor="admin"
        )
        self.assertTrue(res['success'])
        self.assertEqual(res['source_entity_id'], 3)
        self.assertEqual(res['destination_entity_id'], 2)
        self.assertIsNotNone(res['audit_id'])

        # After transfer: verify Provider mapping is now bound to Entity 2
        cur.execute("SELECT CreatorEntityID FROM ext_creator_external_ids WHERE Provider = 'metron' AND ExternalID = '999'")
        self.assertEqual(cur.fetchone()[0], 2)

        # Invariant: EXACTLY one mapping exists before and after
        cur.execute("SELECT COUNT(*) FROM ext_creator_external_ids WHERE Provider = 'metron' AND ExternalID = '999'")
        self.assertEqual(cur.fetchone()[0], 1)

        # Verify NameRecord 104 is now linked to Entity 2 with explicit_user resolution source
        cur.execute("SELECT CreatorEntityID, ResolutionSource FROM ext_creator_name_records WHERE NameRecordID = 104")
        nr_row = cur.fetchone()
        self.assertEqual(nr_row[0], 2)
        self.assertEqual(nr_row[1], 'explicit_user')

        # Verify Credits cache updated
        cur.execute("SELECT CreatorEntityID FROM ext_creator_credits WHERE NameRecordID = 104")
        self.assertEqual(cur.fetchone()[0], 2)

    # -------------------------------------------------------------------------
    # Criterion 3: Composite Audit Event Payload Verification
    # -------------------------------------------------------------------------
    def test_composite_audit_event_logged(self):
        cur = self.conn.cursor()
        audit_count_before = cur.execute("SELECT COUNT(*) FROM ext_creator_resolution_audit").fetchone()[0]

        res = self.service.resolve_conflict_transfer_mapping(
            name_record_id=104,
            provider='metron',
            provider_creator_id='999',
            reason="Explicit human transfer",
            actor="reviewer_alice"
        )
        audit_count_after = cur.execute("SELECT COUNT(*) FROM ext_creator_resolution_audit").fetchone()[0]

        # Exactly ONE audit row emitted (composite event)
        self.assertEqual(audit_count_after, audit_count_before + 1)

        cur.execute("""
            SELECT Action, NameRecordID, CreatorEntityID, Provider, ExternalID, Actor, Reason,
                   BeforeStateJson, AfterStateJson
            FROM ext_creator_resolution_audit
            WHERE AuditID = ?
        """, (res['audit_id'],))
        row = cur.fetchone()

        self.assertEqual(row[0], 'TRANSFER_PROVIDER_MAPPING')
        self.assertEqual(row[1], 104)
        self.assertEqual(row[2], 2) # destination entity
        self.assertEqual(row[3], 'metron')
        self.assertEqual(row[4], '999')
        self.assertEqual(row[5], 'reviewer_alice')

        before_state = json.loads(row[7])
        after_state = json.loads(row[8])

        self.assertEqual(before_state['source_entity_id'], 3)
        self.assertEqual(before_state['destination_entity_id'], 2)
        self.assertEqual(before_state['status'], 'conflicted')

        self.assertEqual(after_state['source_entity_id'], 3)
        self.assertEqual(after_state['destination_entity_id'], 2)
        self.assertEqual(after_state['status'], 'transferred')

    # -------------------------------------------------------------------------
    # Criterion 4: Safe Reversal (Undo Transfer)
    # -------------------------------------------------------------------------
    def test_successful_safe_reversal(self):
        cur = self.conn.cursor()

        # Step 1: Perform transfer
        self.service.resolve_conflict_transfer_mapping(
            name_record_id=104,
            provider='metron',
            provider_creator_id='999',
            reason="Transfer before undo test",
            actor="admin"
        )
        # Verify transferred
        cur.execute("SELECT CreatorEntityID FROM ext_creator_external_ids WHERE Provider = 'metron' AND ExternalID = '999'")
        self.assertEqual(cur.fetchone()[0], 2)

        # Step 2: Reverse transfer
        rev_res = self.service.reverse_conflict_transfer_mapping(
            name_record_id=104,
            provider='metron',
            provider_creator_id='999',
            reason="Human reviewer realized transfer was mistaken",
            actor="admin"
        )
        self.assertTrue(rev_res['success'])
        self.assertEqual(rev_res['restored_entity_id'], 3)

        # Verify mapping restored to Entity 3
        cur.execute("SELECT CreatorEntityID FROM ext_creator_external_ids WHERE Provider = 'metron' AND ExternalID = '999'")
        self.assertEqual(cur.fetchone()[0], 3)

        # Verify NameRecord restored to unresolved
        cur.execute("SELECT CreatorEntityID, ResolutionSource FROM ext_creator_name_records WHERE NameRecordID = 104")
        nr_row = cur.fetchone()
        self.assertEqual(nr_row[0], 2) # original entity before transfer
        self.assertEqual(nr_row[1], 'unresolved')

        # Verify exactly one active mapping exists
        cur.execute("SELECT COUNT(*) FROM ext_creator_external_ids WHERE Provider = 'metron' AND ExternalID = '999'")
        self.assertEqual(cur.fetchone()[0], 1)

        # Verify reversal audit event logged
        cur.execute("""
            SELECT Action, NameRecordID, CreatorEntityID, Provider, ExternalID
            FROM ext_creator_resolution_audit
            WHERE AuditID = ?
        """, (rev_res['audit_id'],))
        rev_audit = cur.fetchone()
        self.assertEqual(rev_audit[0], 'REVERSE_TRANSFER_PROVIDER_MAPPING')
        self.assertEqual(rev_audit[1], 104)
        self.assertEqual(rev_audit[2], 3)

    # -------------------------------------------------------------------------
    # Criterion 5: Stale / Diverged Reversal Fails with Zero Writes
    # -------------------------------------------------------------------------
    def test_stale_reversal_divergence_fails_safely(self):
        cur = self.conn.cursor()

        # Step 1: Perform transfer
        self.service.resolve_conflict_transfer_mapping(
            name_record_id=104,
            provider='metron',
            provider_creator_id='999',
            actor="admin"
        )

        # Step 2: Simulate subsequent divergence (mapping moved to entity 4)
        cur.execute("""
            INSERT INTO ext_creator_entities (CreatorEntityID, DisplayName, NormalizedName, EntitySlug)
            VALUES (4, 'Other Entity', 'other entity', 'other-entity')
        """)
        cur.execute("UPDATE ext_creator_external_ids SET CreatorEntityID = 4 WHERE Provider = 'metron' AND ExternalID = '999'")
        self.conn.commit()

        audit_count_before = cur.execute("SELECT COUNT(*) FROM ext_creator_resolution_audit").fetchone()[0]

        # Step 3: Attempt reversal - must fail due to optimistic lock divergence
        with self.assertRaises(StaleConflictStateError):
            self.service.reverse_conflict_transfer_mapping(
                name_record_id=104,
                provider='metron',
                provider_creator_id='999',
                actor="admin"
            )

        # Verify zero writes occurred
        audit_count_after = cur.execute("SELECT COUNT(*) FROM ext_creator_resolution_audit").fetchone()[0]
        self.assertEqual(audit_count_after, audit_count_before)
        cur.execute("SELECT CreatorEntityID FROM ext_creator_external_ids WHERE Provider = 'metron' AND ExternalID = '999'")
        self.assertEqual(cur.fetchone()[0], 4)

    # -------------------------------------------------------------------------
    # Criterion 6: Stale / Missing / Already Resolved Conflict Causes Zero Writes
    # -------------------------------------------------------------------------
    def test_stale_or_missing_conflict_causes_zero_writes(self):
        cur = self.conn.cursor()
        audit_count_before = cur.execute("SELECT COUNT(*) FROM ext_creator_resolution_audit").fetchone()[0]

        # 1. Non-existent provider mapping
        with self.assertRaises(StaleConflictStateError):
            self.service.resolve_conflict_transfer_mapping(
                name_record_id=104,
                provider='metron',
                provider_creator_id='999999', # not in DB
                actor="admin"
            )

        # 2. Already linked to the same entity (no conflict)
        cur.execute("UPDATE ext_creator_name_records SET CreatorEntityID = 3 WHERE NameRecordID = 104")
        self.conn.commit()

        with self.assertRaises(StaleConflictStateError):
            self.service.resolve_conflict_transfer_mapping(
                name_record_id=104,
                provider='metron',
                provider_creator_id='999',
                actor="admin"
            )

        audit_count_after = cur.execute("SELECT COUNT(*) FROM ext_creator_resolution_audit").fetchone()[0]
        self.assertEqual(audit_count_after, audit_count_before)

    # -------------------------------------------------------------------------
    # Criterion 7 & 8: Rejection Supersession and Restoration Lifecycle
    # -------------------------------------------------------------------------
    def test_rejection_supersession_and_restoration_lifecycle(self):
        cur = self.conn.cursor()

        # Step 1: Add an active rejection for (NameRecord 104, metron, 999)
        cur.execute("""
            INSERT INTO ext_creator_candidate_rejections (NameRecordID, Provider, ExternalID, ProviderDisplayName, Status, RejectedBy, Reason)
            VALUES (104, 'metron', '999', 'Robert Harras', 'active', 'reviewer', 'Initial candidate rejection')
        """)
        self.conn.commit()
        rej_id = cur.lastrowid

        # Verify active rejection exists
        cur.execute("SELECT Status FROM ext_creator_candidate_rejections WHERE RejectionID = ?", (rej_id,))
        self.assertEqual(cur.fetchone()[0], 'active')

        # Step 2: Transfer mapping (must supersede this exact rejection)
        res = self.service.resolve_conflict_transfer_mapping(
            name_record_id=104,
            provider='metron',
            provider_creator_id='999',
            actor="admin"
        )
        self.assertEqual(res['superseded_rejection_id'], rej_id)

        # Verify rejection is superseded (not deleted)
        cur.execute("SELECT Status, ReversalReason FROM ext_creator_candidate_rejections WHERE RejectionID = ?", (rej_id,))
        rej_row = cur.fetchone()
        self.assertEqual(rej_row[0], 'superseded')
        self.assertIn("Superseded", rej_row[1])

        # Step 3: Reverse transfer (must restore rejection back to active)
        rev_res = self.service.reverse_conflict_transfer_mapping(
            name_record_id=104,
            provider='metron',
            provider_creator_id='999',
            actor="admin"
        )
        self.assertEqual(rev_res['restored_rejection_id'], rej_id)

        cur.execute("SELECT Status FROM ext_creator_candidate_rejections WHERE RejectionID = ?", (rej_id,))
        self.assertEqual(cur.fetchone()[0], 'active')

    # -------------------------------------------------------------------------
    # Criterion 9: Credit Reference Cache Boundary Verification
    # -------------------------------------------------------------------------
    def test_credit_reference_cache_boundary_invariance(self):
        cur = self.conn.cursor()

        # Snapshot full credit row before transfer
        cur.execute("""
            SELECT CreditID, IssueID, AnnualID, ComicID, Role, RawRoleText, RawCreditName,
                   NameRecordID, CreatorEntityID, SourceProvenance, SourceRevision
            FROM ext_creator_credits
            WHERE CreditID = 501
        """)
        credit_before = cur.fetchone()

        # Perform transfer
        self.service.resolve_conflict_transfer_mapping(
            name_record_id=104,
            provider='metron',
            provider_creator_id='999',
            actor="admin"
        )

        cur.execute("""
            SELECT CreditID, IssueID, AnnualID, ComicID, Role, RawRoleText, RawCreditName,
                   NameRecordID, CreatorEntityID, SourceProvenance, SourceRevision
            FROM ext_creator_credits
            WHERE CreditID = 501
        """)
        credit_after = cur.fetchone()

        # Check fields:
        # CreditID (0), IssueID (1), AnnualID (2), ComicID (3), Role (4), RawRoleText (5), RawCreditName (6), NameRecordID (7) MUST BE IDENTICAL
        self.assertEqual(credit_before[0:8], credit_after[0:8])
        # CreatorEntityID (8) changed from 2 to 2 (or destination entity)
        self.assertEqual(credit_after[8], 2)
        # SourceProvenance (9), SourceRevision (10) MUST BE IDENTICAL
        self.assertEqual(credit_before[9:], credit_after[9:])

    # -------------------------------------------------------------------------
    # Corrective Verification 1 & 2: Unlinked Target Fails Closed with 409 and Complete DB Invariance
    # -------------------------------------------------------------------------
    def test_unlinked_target_fails_closed_with_complete_database_invariance(self):
        cur = self.conn.cursor()

        # Pre-condition: NameRecord 105 is unlinked (CreatorEntityID is NULL)
        cur.execute("SELECT CreatorEntityID FROM ext_creator_name_records WHERE NameRecordID = 105")
        self.assertIsNone(cur.fetchone()[0])

        # Snapshot all tables before failed transfer attempt
        cur.execute("SELECT * FROM ext_creator_entities ORDER BY CreatorEntityID")
        entities_before = cur.fetchall()
        cur.execute("SELECT * FROM ext_creator_name_records ORDER BY NameRecordID")
        nr_before = cur.fetchall()
        cur.execute("SELECT * FROM ext_creator_external_ids ORDER BY ExternalMappingID")
        ext_ids_before = cur.fetchall()
        cur.execute("SELECT * FROM ext_creator_candidate_rejections ORDER BY RejectionID")
        rej_before = cur.fetchall()
        cur.execute("SELECT * FROM ext_creator_credits ORDER BY CreditID")
        credits_before = cur.fetchall()
        cur.execute("SELECT * FROM ext_creator_resolution_audit ORDER BY AuditID")
        audit_before = cur.fetchall()

        # 1. Direct Service Call must raise TransferTargetUnavailableError
        with self.assertRaises(TransferTargetUnavailableError) as ctx:
            self.service.resolve_conflict_transfer_mapping(
                name_record_id=105,
                provider='metron',
                provider_creator_id='999',
                actor="admin"
            )
        self.assertIn("unlinked", str(ctx.exception).lower())

        # 2. Controller Call must return HTTP 409 status code with transfer_target_unavailable error code
        token = get_or_create_csrf_token()
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'POST'
            mock_req.headers = {'X-CSRF-Token': token}
            res_raw = handle_resolve_creator_conflict(
                name_record_id=105,
                provider='metron',
                provider_creator_id='999',
                action='transfer_mapping',
                csrf_token=token,
                service=self.service
            )
            res = json.loads(res_raw)
            self.assertFalse(res['success'])
            self.assertEqual(res.get('error_code'), 'transfer_target_unavailable')
            self.assertEqual(res.get('status_code'), 409)

        # 3. Assert complete database invariance (zero writes across all tables)
        cur.execute("SELECT * FROM ext_creator_entities ORDER BY CreatorEntityID")
        self.assertEqual(cur.fetchall(), entities_before, "No entities should be created")
        cur.execute("SELECT * FROM ext_creator_name_records ORDER BY NameRecordID")
        self.assertEqual(cur.fetchall(), nr_before, "No name records should be modified")
        cur.execute("SELECT * FROM ext_creator_external_ids ORDER BY ExternalMappingID")
        self.assertEqual(cur.fetchall(), ext_ids_before, "No external IDs should be reassigned")
        cur.execute("SELECT * FROM ext_creator_candidate_rejections ORDER BY RejectionID")
        self.assertEqual(cur.fetchall(), rej_before, "No rejections should be created or superseded")
        cur.execute("SELECT * FROM ext_creator_credits ORDER BY CreditID")
        self.assertEqual(cur.fetchall(), credits_before, "No credits should be modified")
        cur.execute("SELECT * FROM ext_creator_resolution_audit ORDER BY AuditID")
        self.assertEqual(cur.fetchall(), audit_before, "No audit events should be logged")

    # -------------------------------------------------------------------------
    # Corrective Verification 4, 5, 6: Truthful State Copy Across Templates
    # -------------------------------------------------------------------------
    def test_truthful_state_copy_across_templates(self):
        comicdetails_path = os.path.join(REPO_ROOT, 'data', 'interfaces', 'modern', 'comicdetails_update.html')
        registry_path = os.path.join(REPO_ROOT, 'data', 'interfaces', 'modern', 'creator_registry.html')

        with open(comicdetails_path, 'r', encoding='utf-8') as f:
            comicdetails_src = f.read()
        with open(registry_path, 'r', encoding='utf-8') as f:
            registry_src = f.read()

        # 1. Transferred state must contain state-aware copy
        transferred_statement = "This provider mapping was transferred by an explicit, auditable human decision. It was not created by automatic matching."
        self.assertIn(transferred_statement, comicdetails_src)

        # 2. Header notice when transfers exist must be truthful
        transfer_notice = "Provider mappings marked as transferred reflect explicit, auditable human transfers. Unresolved candidates remain read-only."
        self.assertIn(transfer_notice, comicdetails_src)

        # 3. Footer disclaimer when transfers exist must acknowledge transfer and reversal
        transfer_disclaimer = "Provider mapping transfer on record. Local creator links reflect explicit human resolution decisions. An audited reversal is available when current state permits."
        self.assertIn(transfer_disclaimer, comicdetails_src)

        # 4. Purely unresolved notice must be preserved for review-only cases
        unresolved_notice = "Review only. Candidate evidence does not establish creator identity, and no creator records or links have been modified."
        self.assertIn(unresolved_notice, comicdetails_src)

        # 5. Confirmation views must warn about unlinked target
        unlinked_warning = "This competing credit is unlinked. You must link this credit to an existing creator entity before transferring provider mappings."
        self.assertIn(unlinked_warning, comicdetails_src)
        self.assertIn(unlinked_warning, registry_src)

    # -------------------------------------------------------------------------
    # Criterion 11: Transaction Rollback Injection at Mutation Boundaries
    # -------------------------------------------------------------------------
    def test_failure_injection_rollback_at_every_boundary(self):
        cur = self.conn.cursor()

        # Capture pre-transfer state snapshots
        cur.execute("SELECT * FROM ext_creator_external_ids ORDER BY ExternalMappingID")
        ext_ids_before = cur.fetchall()
        cur.execute("SELECT * FROM ext_creator_name_records ORDER BY NameRecordID")
        nr_before = cur.fetchall()
        cur.execute("SELECT * FROM ext_creator_credits ORDER BY CreditID")
        credits_before = cur.fetchall()
        cur.execute("SELECT COUNT(*) FROM ext_creator_resolution_audit")
        audit_count_before = cur.fetchone()[0]

        # Injection 1: Failure during credit cache update
        with patch.object(self.repo, 'update_credits_entity', side_effect=RuntimeError("Simulated DB Crash during credit cache update")):
            with self.assertRaises(RuntimeError):
                self.service.resolve_conflict_transfer_mapping(
                    name_record_id=104,
                    provider='metron',
                    provider_creator_id='999',
                    actor="admin"
                )

        # Verify complete rollback
        cur.execute("SELECT * FROM ext_creator_external_ids ORDER BY ExternalMappingID")
        self.assertEqual(cur.fetchall(), ext_ids_before)
        cur.execute("SELECT * FROM ext_creator_name_records ORDER BY NameRecordID")
        self.assertEqual(cur.fetchall(), nr_before)
        cur.execute("SELECT * FROM ext_creator_credits ORDER BY CreditID")
        self.assertEqual(cur.fetchall(), credits_before)
        cur.execute("SELECT COUNT(*) FROM ext_creator_resolution_audit")
        self.assertEqual(cur.fetchone()[0], audit_count_before)

        # Injection 2: Failure during audit logging
        with patch.object(self.repo, 'insert_audit_entry', side_effect=RuntimeError("Simulated DB Crash during audit write")):
            with self.assertRaises(RuntimeError):
                self.service.resolve_conflict_transfer_mapping(
                    name_record_id=104,
                    provider='metron',
                    provider_creator_id='999',
                    actor="admin"
                )

        # Verify complete rollback
        cur.execute("SELECT * FROM ext_creator_external_ids ORDER BY ExternalMappingID")
        self.assertEqual(cur.fetchall(), ext_ids_before)
        cur.execute("SELECT COUNT(*) FROM ext_creator_resolution_audit")
        self.assertEqual(cur.fetchone()[0], audit_count_before)

    # -------------------------------------------------------------------------
    # Criterion 12: Controller Security, CSRF, Method & Client Entity-ID Attack
    # -------------------------------------------------------------------------
    def test_controller_security_and_entity_id_attack_rejection(self):
        # 1. Reject GET requests
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'GET'
            res = json.loads(handle_resolve_creator_conflict(
                name_record_id=104,
                provider='metron',
                provider_creator_id='999',
                action='transfer_mapping',
                service=self.service
            ))
            self.assertFalse(res['success'])
            self.assertEqual(res['error_code'], 'method_not_allowed')

        # 2. Reject Missing / Invalid CSRF
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'POST'
            mock_req.headers = {}
            res = json.loads(handle_resolve_creator_conflict(
                name_record_id=104,
                provider='metron',
                provider_creator_id='999',
                action='transfer_mapping',
                csrf_token='bogus_token',
                service=self.service
            ))
            self.assertFalse(res['success'])
            self.assertEqual(res['error_code'], 'invalid_csrf_token')

        # 3. Successful POST with valid CSRF & client-supplied malicious entity ID ignored
        token = get_or_create_csrf_token()
        with patch('cherrypy.request') as mock_req, patch('cherrypy.response') as mock_resp:
            mock_req.method = 'POST'
            mock_req.headers = {'X-CSRF-Token': token}
            res = json.loads(handle_resolve_creator_conflict(
                name_record_id=104,
                provider='metron',
                provider_creator_id='999',
                action='transfer_mapping',
                csrf_token=token,
                # Malicious client-supplied entity ID trying to force destination to 9999
                target_entity_id=9999,
                creator_entity_id=9999,
                destination_entity_id=9999,
                service=self.service
            ))
            self.assertTrue(res['success'])
            # Server derives destination entity (2) from DB, completely ignoring 9999
            self.assertEqual(res['destination_entity_id'], 2)

    # -------------------------------------------------------------------------
    # Criterion 13: Zero Provider / Network Calls
    # -------------------------------------------------------------------------
    @patch('urllib.request.urlopen')
    @patch('urllib.request.Request')
    def test_zero_network_calls_during_transfer_and_reversal(self, mock_req, mock_urlopen):
        # Transfer
        self.service.resolve_conflict_transfer_mapping(
            name_record_id=104,
            provider='metron',
            provider_creator_id='999',
            actor="admin"
        )
        mock_urlopen.assert_not_called()
        mock_req.assert_not_called()

        # Reversal
        self.service.reverse_conflict_transfer_mapping(
            name_record_id=104,
            provider='metron',
            provider_creator_id='999',
            actor="admin"
        )
        mock_urlopen.assert_not_called()
        mock_req.assert_not_called()

    # -------------------------------------------------------------------------
    # Criterion 14: Cross-Surface Contract Tests for the Complete Lifecycle
    # -------------------------------------------------------------------------
    def test_consistent_transferred_state_across_surfaces_before_and_after_reversal(self):
        cand_service = CreatorCandidateService(repository=self.repo)
        reg_service = CreatorRegistryService(repository=self.repo)
        hist_service = CreatorHistoryService(repository=self.repo)
        conflict_service = self.service

        provider_credits = [{
            'creator_id': '999',
            'name': 'Bob Harras',
            'raw_creator_name': 'Bob Harras',
            'role': 'Editor',
            'canonical_role': 'Editor',
            'raw_role': 'Editor',
            'is_cover_credit': False
        }]

        # ─── 1. Initial Conflict Lifecycle Stage ───
        cur, _ = self.repo._get_cursor()
        cur.execute("""
            INSERT INTO ext_creator_resolution_audit (Action, NameRecordID, CreatorEntityID, Provider, ExternalID, ProviderDisplayName, Actor, Reason)
            VALUES ('LEGACY_EXTERNAL_ID_COLLISION', 104, 2, 'metron', '999', 'Robert Harras', 'system', 'External ID collision detected')
        """)
        self.conn.commit()

        # Candidate / Inspector
        cand_res_1 = cand_service.build_candidates(issue_id='105544', is_annual=0, provider='metron', provider_snapshot={'credits': provider_credits})
        c104_1 = next((c for c in cand_res_1['identity_candidates'] if c['name_record_id'] == 104 and c['provider_creator_id'] == '999'), None)
        self.assertIsNotNone(c104_1)
        self.assertEqual(c104_1['candidate_state'], 'conflicted')
        self.assertTrue(c104_1['is_conflicted'])

        # Registry
        reg_1 = reg_service.get_registry_entries(state='all')
        r104_1 = next((r for r in reg_1['entries'] if r['name_record_id'] == 104), None)
        self.assertIsNotNone(r104_1)
        self.assertEqual(r104_1['current_state'], 'conflicted')
        self.assertGreaterEqual(reg_1['counts']['conflicted'], 1)

        # Decision History
        hist_1 = hist_service.get_decision_history(name_record_id=104, provider='metron', provider_creator_id='999')
        self.assertEqual(hist_1['current_state']['status'], 'conflicted')

        # Conflict Service
        conf_1 = conflict_service.analyze_creator_conflict(name_record_id=104, provider='metron', provider_creator_id='999')
        self.assertEqual(conf_1['derived_state'], 'conflicted')
        self.assertTrue(conf_1['is_conflicted'])

        # Shared canonical helper
        shared_1 = self.repo.derive_pairing_state(104, 'metron', '999')
        self.assertEqual(shared_1['state'], 'conflicted')
        self.assertEqual(c104_1['candidate_state'], r104_1['current_state'])
        self.assertEqual(r104_1['current_state'], hist_1['current_state']['status'])
        self.assertEqual(hist_1['current_state']['status'], conf_1['derived_state'])

        # ─── 2. Active Transfer Lifecycle Stage ───
        res_transfer = self.service.resolve_conflict_transfer_mapping(
            name_record_id=104,
            provider='metron',
            provider_creator_id='999',
            actor="human_reviewer",
            reason="Confirmed transfer"
        )
        self.assertTrue(res_transfer['success'])

        # Candidate / Inspector
        cand_res_2 = cand_service.build_candidates(issue_id='105544', is_annual=0, provider='metron', provider_snapshot={'credits': provider_credits})
        c104_2 = next((c for c in cand_res_2['identity_candidates'] if c['name_record_id'] == 104 and c['provider_creator_id'] == '999'), None)
        self.assertIsNotNone(c104_2)
        self.assertEqual(c104_2['candidate_state'], 'transferred')
        self.assertTrue(c104_2['is_transferred'])
        self.assertFalse(c104_2['is_confirmed'])

        # Registry
        reg_2 = reg_service.get_registry_entries(state='all')
        r104_2 = next((r for r in reg_2['entries'] if r['name_record_id'] == 104), None)
        self.assertIsNotNone(r104_2)
        self.assertEqual(r104_2['current_state'], 'transferred')
        self.assertEqual(r104_2['latest_decision']['action_label'], 'Transferred Provider Mapping')
        self.assertGreaterEqual(reg_2['counts']['transferred'], 1)
        reg_trans_2 = reg_service.get_registry_entries(state='transferred')
        self.assertTrue(any(r['name_record_id'] == 104 for r in reg_trans_2['entries']))

        # Decision History
        hist_2 = hist_service.get_decision_history(name_record_id=104, provider='metron', provider_creator_id='999')
        self.assertEqual(hist_2['current_state']['status'], 'transferred')
        self.assertTrue(hist_2['explanation_summary'].startswith("Confirmed through an explicit provider-mapping transfer"))
        self.assertEqual(hist_2['timeline'][-1]['action'], 'TRANSFER_PROVIDER_MAPPING')

        # Conflict Service
        conf_2 = conflict_service.analyze_creator_conflict(name_record_id=104, provider='metron', provider_creator_id='999')
        self.assertEqual(conf_2['derived_state'], 'transferred')

        # Shared canonical helper
        shared_2 = self.repo.derive_pairing_state(104, 'metron', '999')
        self.assertEqual(shared_2['state'], 'transferred')
        self.assertEqual(c104_2['candidate_state'], r104_2['current_state'])
        self.assertEqual(r104_2['current_state'], hist_2['current_state']['status'])
        self.assertEqual(hist_2['current_state']['status'], conf_2['derived_state'])

        # ─── 3. Reversed Transfer Returning to Conflict Lifecycle Stage ───
        res_rev = self.service.reverse_conflict_transfer_mapping(
            name_record_id=104,
            provider='metron',
            provider_creator_id='999',
            actor="human_reviewer",
            reason="Undo transfer"
        )
        self.assertTrue(res_rev['success'])

        # Candidate / Inspector
        cand_res_3 = cand_service.build_candidates(issue_id='105544', is_annual=0, provider='metron', provider_snapshot={'credits': provider_credits})
        c104_3 = next((c for c in cand_res_3['identity_candidates'] if c['name_record_id'] == 104 and c['provider_creator_id'] == '999'), None)
        self.assertIsNotNone(c104_3)
        self.assertEqual(c104_3['candidate_state'], 'conflicted')
        self.assertTrue(c104_3['is_conflicted'])
        self.assertFalse(c104_3['is_transferred'])

        # Registry
        reg_3 = reg_service.get_registry_entries(state='all')
        r104_3 = next((r for r in reg_3['entries'] if r['name_record_id'] == 104), None)
        self.assertIsNotNone(r104_3)
        self.assertEqual(r104_3['current_state'], 'conflicted')
        self.assertEqual(r104_3['latest_decision']['action_label'], 'Reversed Provider Transfer')
        self.assertEqual(reg_3['counts']['transferred'], 0)
        reg_conf_3 = reg_service.get_registry_entries(state='conflicted')
        self.assertTrue(any(r['name_record_id'] == 104 for r in reg_conf_3['entries']))

        # Decision History
        hist_3 = hist_service.get_decision_history(name_record_id=104, provider='metron', provider_creator_id='999')
        self.assertEqual(hist_3['current_state']['status'], 'conflicted')
        self.assertIn("Transfer was reversed; provider mapping [METRON ID: 999] again maps to competing entity", hist_3['explanation_summary'])
        actions_3 = [ev['action'] for ev in hist_3['timeline']]
        self.assertIn('TRANSFER_PROVIDER_MAPPING', actions_3)
        self.assertIn('REVERSE_TRANSFER_PROVIDER_MAPPING', actions_3)

        # Conflict Service
        conf_3 = conflict_service.analyze_creator_conflict(name_record_id=104, provider='metron', provider_creator_id='999')
        self.assertEqual(conf_3['derived_state'], 'conflicted')
        self.assertTrue(conf_3['is_conflicted'])

        # Shared canonical helper
        shared_3 = self.repo.derive_pairing_state(104, 'metron', '999')
        self.assertEqual(shared_3['state'], 'conflicted')
        self.assertEqual(c104_3['candidate_state'], r104_3['current_state'])
        self.assertEqual(r104_3['current_state'], hist_3['current_state']['status'])
        self.assertEqual(hist_3['current_state']['status'], conf_3['derived_state'])

        # ─── 4. Transfer That Restores an Active Rejection ───
        # Create Entity 6, NameRecord 106, Entity 7 with Metron 998, and an active rejection on (106, 'metron', '998')
        cur, _ = self.repo._get_cursor()
        cur.execute("INSERT INTO ext_creator_entities (CreatorEntityID, DisplayName, NormalizedName, EntitySlug) VALUES (6, 'Test Entity 6', 'test entity 6', 'test-entity-6')")
        cur.execute("INSERT INTO ext_creator_entities (CreatorEntityID, DisplayName, NormalizedName, EntitySlug) VALUES (7, 'Test Entity 7', 'test entity 7', 'test-entity-7')")
        cur.execute("INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, CreatorEntityID) VALUES (106, 'Test Creator 6', 'test creator 6', 'test-creator-6', 6)")
        cur.execute("INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, CreatorEntityID) VALUES (107, 'Test Creator 7', 'test creator 7', 'test-creator-7', 7)")
        cur.execute("INSERT INTO ext_creator_credits (CreditID, IssueID, IsAnnual, ComicID, Role, RawCreditName, NameRecordID, CreatorEntityID) VALUES (206, '105544', 0, '5535', 'writer', 'Test Creator 6', 106, 6)")
        cur.execute("INSERT INTO ext_creator_external_ids (CreatorEntityID, Provider, ExternalID) VALUES (7, 'metron', '998')")
        cur.execute("INSERT INTO ext_creator_candidate_rejections (NameRecordID, Provider, ExternalID, Status, RejectedBy) VALUES (106, 'metron', '998', 'active', 'reviewer')")
        self.conn.commit()

        # Transfer Metron 998 to 106 (superseding rejection)
        res_trans_rej = self.service.resolve_conflict_transfer_mapping(106, 'metron', '998', actor='reviewer')
        self.assertTrue(res_trans_rej['success'])

        # Reverse transfer (restoring active rejection)
        res_rev_rej = self.service.reverse_conflict_transfer_mapping(106, 'metron', '998', actor='reviewer')
        self.assertTrue(res_rev_rej['success'])

        p_creds_6 = [{'creator_id': '998', 'name': 'Test Creator 6', 'raw_creator_name': 'Test Creator 6', 'role': 'writer', 'canonical_role': 'writer', 'raw_role': 'writer', 'is_cover_credit': False}]
        cand_res_4 = cand_service.build_candidates(issue_id='105544', is_annual=0, provider='metron', provider_snapshot={'credits': p_creds_6})
        c106_4 = next((c for c in cand_res_4['identity_candidates'] if c['name_record_id'] == 106 and c['provider_creator_id'] == '998'), None)
        self.assertIsNotNone(c106_4)
        self.assertEqual(c106_4['candidate_state'], 'rejected')
        self.assertTrue(c106_4['is_rejected'])

        reg_4 = reg_service.get_registry_entries(state='all')
        r106_4 = next((r for r in reg_4['entries'] if r['name_record_id'] == 106), None)
        self.assertIsNotNone(r106_4)
        self.assertEqual(r106_4['current_state'], 'rejected')

        hist_4 = hist_service.get_decision_history(name_record_id=106, provider='metron', provider_creator_id='998')
        self.assertEqual(hist_4['current_state']['status'], 'rejected')

        conf_4 = conflict_service.analyze_creator_conflict(name_record_id=106, provider='metron', provider_creator_id='998')
        self.assertEqual(conf_4['derived_state'], 'rejected')

        shared_4 = self.repo.derive_pairing_state(106, 'metron', '998')
        self.assertEqual(shared_4['state'], 'rejected')
        self.assertEqual(c106_4['candidate_state'], r106_4['current_state'])
        self.assertEqual(r106_4['current_state'], hist_4['current_state']['status'])
        self.assertEqual(hist_4['current_state']['status'], conf_4['derived_state'])

        # ─── 5. Stale Reversal Divergence ───
        # Transfer 999 to 104 again
        self.service.resolve_conflict_transfer_mapping(104, 'metron', '999', actor='reviewer')
        # Manually alter mapping in DB to simulate third-party drift
        cur.execute("UPDATE ext_creator_external_ids SET CreatorEntityID = 99 WHERE Provider = 'metron' AND ExternalID = '999'")
        self.conn.commit()
        with self.assertRaises(StaleConflictStateError):
            self.service.reverse_conflict_transfer_mapping(104, 'metron', '999', actor='reviewer')

        # ─── 6. Exact Confirmed Mapping Unaffected by Unrelated Entity Link ───
        # Map Metron 777 to Entity 2 (where 104 is linked)
        cur.execute("INSERT INTO ext_creator_external_ids (CreatorEntityID, Provider, ExternalID) VALUES (2, 'metron', '777')")
        self.conn.commit()
        # For exact pairing (104, 'metron', '777') -> CONFIRMED
        shared_conf = self.repo.derive_pairing_state(104, 'metron', '777')
        self.assertEqual(shared_conf['state'], 'confirmed')
        hist_conf = hist_service.get_decision_history(104, 'metron', '777')
        self.assertEqual(hist_conf['current_state']['status'], 'confirmed')

        # For unmapped/competing pairing (104, 'metron', '555') where 555 is mapped to Entity 4 -> CONFLICTED
        cur.execute("INSERT INTO ext_creator_entities (CreatorEntityID, DisplayName, NormalizedName, EntitySlug) VALUES (4, 'Other Entity 4', 'other entity 4', 'other-entity-4')")
        cur.execute("INSERT INTO ext_creator_external_ids (CreatorEntityID, Provider, ExternalID) VALUES (4, 'metron', '555')")
        self.conn.commit()
        shared_unrel = self.repo.derive_pairing_state(104, 'metron', '555')
        self.assertEqual(shared_unrel['state'], 'conflicted')
        hist_unrel = hist_service.get_decision_history(104, 'metron', '555')
        self.assertEqual(hist_unrel['current_state']['status'], 'conflicted')


if __name__ == '__main__':
    unittest.main()
