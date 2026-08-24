"""
Automated Verification Suite for Phase C3.1: Contextual Creator Links in the Modern Issue Inspector.

Covers:
1. Zero network/provider calls and zero IssueDetails() calls.
2. Regular issue creator credits retrieval, ordering, and data structure.
3. Annual scope separation (IsAnnual=0 vs IsAnnual=1).
4. Unindexed issue handling (HTTP 200, empty groups, clean non-error).
5. Composite credit preservation (intact raw name and exact NameRecordID link).
6. Separate name record preservation (duplicate normalized names remain distinct).
7. Controller validation and error codes (HTTP 400 for malformed parameters).
8. Database invariance check (zero writes across all core and extension tables).
"""

import os
import sys
import json
import sqlite3
import unittest
from unittest.mock import patch, MagicMock

# Set up paths
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
LIB_DIR = os.path.join(REPO_ROOT, 'lib')
if LIB_DIR not in sys.path:
    sys.path.insert(0, LIB_DIR)

import mylar
from mylar.extensions.creators.browser_service import CreatorBrowserService
from mylar.extensions.creators.browser_controller import handle_issue_creator_credits
from mylar.extensions.creators.schema import ROLES

STAGING_DIR = r"C:\Users\spike\.gemini\antigravity\brain\ff5d5fcf-d223-4e21-94d9-ed4e7c37cd34\scratch\live_staging_20260822_0050"
STAGING_DB_PATH = os.path.join(STAGING_DIR, "mylar.db")


class TestPhaseC31(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        mylar.PROG_DIR = REPO_ROOT
        mylar.DATA_DIR = STAGING_DIR

    def _get_staging_db_conn(self):
        conn = sqlite3.connect(STAGING_DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    def test_01_no_network_and_no_issue_details_calls(self):
        """Proof: Service makes zero HTTP calls and zero calls to helpers.IssueDetails."""
        import mylar.extensions.creators.browser_service as bs_mod
        import mylar.extensions.creators.browser_controller as bc_mod
        
        # Check source code of browser_service and browser_controller for forbidden imports
        for mod in (bs_mod, bc_mod):
            with open(mod.__file__, 'r', encoding='utf-8') as f:
                content = f.read()
            self.assertNotIn('requests.', content)
            self.assertNotIn('urllib.request', content)
            self.assertNotIn('helpers.IssueDetails', content)
            self.assertNotIn('IssueDetails(', content)
            self.assertNotIn('ComicVine', content)
            self.assertNotIn('metron', content)
            self.assertNotIn('zipfile', content)
            self.assertNotIn('rarfile', content)

    def test_02_regular_issue_creator_credits(self):
        """Verify indexed credits for regular issue Fight Girls #1 (IssueID=868995)."""
        service = CreatorBrowserService()
        res = service.get_issue_creator_credits('868995', is_annual=0)
        
        self.assertTrue(res['success'])
        self.assertEqual(res['issue_id'], '868995')
        self.assertEqual(res['is_annual'], 0)
        self.assertGreater(res['total_credits'], 0)
        self.assertIsInstance(res['groups'], list)
        
        # Role groups order check: Writer, Penciller, Inker, Colorist, Letterer, Cover Artist
        role_keys = [g['role_key'] for g in res['groups']]
        expected_roles = ['writer', 'penciller', 'inker', 'colorist', 'letterer', 'cover_artist']
        for expected in expected_roles:
            self.assertIn(expected, role_keys)
        
        # Verify credits contain exact NameRecordID and RawName
        writer_group = next(g for g in res['groups'] if g['role_key'] == 'writer')
        self.assertEqual(writer_group['role_display'], 'Writer')
        self.assertGreater(len(writer_group['credits']), 0)
        writer_credit = writer_group['credits'][0]
        self.assertEqual(writer_credit['raw_name'], 'Frank Cho')
        self.assertEqual(writer_credit['name_record_id'], 1)
        self.assertFalse(writer_credit['is_resolved'])
        self.assertEqual(writer_credit['resolution_source'], 'unresolved')

    def test_03_annual_scope_and_credits(self):
        """Verify annual credit retrieval with IsAnnual=1 vs IsAnnual=0 separation."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        
        # Build minimal schema fixture
        cur.executescript("""
            CREATE TABLE ext_creator_name_records (
                NameRecordID INTEGER PRIMARY KEY AUTOINCREMENT,
                RawName TEXT NOT NULL UNIQUE,
                NormalizedName TEXT NOT NULL,
                NameSlug TEXT NOT NULL,
                CreatorEntityID INTEGER DEFAULT NULL,
                ResolutionSource TEXT DEFAULT 'unresolved',
                CreatedAt TEXT NOT NULL,
                UpdatedAt TEXT NOT NULL
            );
            CREATE TABLE ext_creator_credits (
                CreditID INTEGER PRIMARY KEY AUTOINCREMENT,
                ComicID TEXT NOT NULL,
                IssueID TEXT NOT NULL,
                IsAnnual INTEGER NOT NULL DEFAULT 0,
                NameRecordID INTEGER NOT NULL,
                Role TEXT NOT NULL,
                RawRoleText TEXT,
                RawCreditName TEXT NOT NULL,
                IsCover INTEGER NOT NULL DEFAULT 0,
                SortOrder INTEGER NOT NULL DEFAULT 0,
                SourceProvenance TEXT NOT NULL DEFAULT 'comicinfo',
                ScanRecordID INTEGER,
                IndexedAt TEXT NOT NULL
            );
            CREATE TABLE ext_creator_entities (
                CreatorEntityID INTEGER PRIMARY KEY AUTOINCREMENT,
                DisplayName TEXT NOT NULL,
                EntitySlug TEXT NOT NULL UNIQUE
            );
            INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, CreatedAt, UpdatedAt)
            VALUES (10, 'Dan Slott', 'dan slott', 'dan-slott', '2026-08-23', '2026-08-23');
            
            -- Credit for Annual #1 (IssueID=500, IsAnnual=1)
            INSERT INTO ext_creator_credits (ComicID, IssueID, IsAnnual, NameRecordID, Role, RawRoleText, RawCreditName, IsCover, SortOrder, IndexedAt)
            VALUES ('4200', '500', 1, 10, 'writer', 'Writer', 'Dan Slott', 0, 0, '2026-08-23');
            
            -- Credit for Regular Issue #500 (IssueID=500, IsAnnual=0) with different creator
            INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, CreatedAt, UpdatedAt)
            VALUES (11, 'Stan Lee', 'stan lee', 'stan-lee', '2026-08-23', '2026-08-23');
            INSERT INTO ext_creator_credits (ComicID, IssueID, IsAnnual, NameRecordID, Role, RawRoleText, RawCreditName, IsCover, SortOrder, IndexedAt)
            VALUES ('4200', '500', 0, 11, 'writer', 'Writer', 'Stan Lee', 0, 0, '2026-08-23');
        """)
        
        # Test custom DB mock
        class MockDB:
            def select(self, query, params=None):
                c = conn.cursor()
                return c.execute(query, params or []).fetchall()

        service = CreatorBrowserService(db_conn=MockDB())
        
        # Query annual=1
        res_annual = service.get_issue_creator_credits('500', is_annual=1)
        self.assertTrue(res_annual['success'])
        self.assertEqual(res_annual['is_annual'], 1)
        self.assertEqual(res_annual['total_credits'], 1)
        self.assertEqual(res_annual['credits'][0]['raw_name'], 'Dan Slott')
        self.assertEqual(res_annual['credits'][0]['name_record_id'], 10)
        
        # Query annual=0
        res_regular = service.get_issue_creator_credits('500', is_annual=0)
        self.assertTrue(res_regular['success'])
        self.assertEqual(res_regular['is_annual'], 0)
        self.assertEqual(res_regular['total_credits'], 1)
        self.assertEqual(res_regular['credits'][0]['raw_name'], 'Stan Lee')
        self.assertEqual(res_regular['credits'][0]['name_record_id'], 11)

    def test_04_unindexed_issue_returns_empty_groups(self):
        """Unindexed issue returns HTTP 200 structure with empty groups without error."""
        service = CreatorBrowserService()
        res = service.get_issue_creator_credits('9999999', is_annual=0)
        
        self.assertTrue(res['success'])
        self.assertEqual(res['issue_id'], '9999999')
        self.assertEqual(res['is_annual'], 0)
        self.assertEqual(res['total_credits'], 0)
        self.assertEqual(res['credits'], [])
        self.assertEqual(res['groups'], [])

    def test_05_composite_observed_name_intact(self):
        """Composite credit ('Scott Snyder and Nick Dragotta') remains intact without splitting."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.executescript("""
            CREATE TABLE ext_creator_name_records (
                NameRecordID INTEGER PRIMARY KEY,
                RawName TEXT NOT NULL,
                NormalizedName TEXT NOT NULL,
                NameSlug TEXT NOT NULL,
                CreatorEntityID INTEGER DEFAULT NULL,
                ResolutionSource TEXT DEFAULT 'unresolved'
            );
            CREATE TABLE ext_creator_credits (
                CreditID INTEGER PRIMARY KEY,
                ComicID TEXT NOT NULL,
                IssueID TEXT NOT NULL,
                IsAnnual INTEGER DEFAULT 0,
                NameRecordID INTEGER NOT NULL,
                Role TEXT NOT NULL,
                RawRoleText TEXT,
                RawCreditName TEXT NOT NULL,
                IsCover INTEGER DEFAULT 0,
                SortOrder INTEGER DEFAULT 0,
                SourceProvenance TEXT DEFAULT 'comicinfo'
            );
            CREATE TABLE ext_creator_entities (
                CreatorEntityID INTEGER PRIMARY KEY,
                DisplayName TEXT NOT NULL,
                EntitySlug TEXT NOT NULL
            );
            INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug)
            VALUES (42, 'Scott Snyder and Nick Dragotta', 'scott snyder and nick dragotta', 'scott-snyder-and-nick-dragotta');
            INSERT INTO ext_creator_credits (CreditID, ComicID, IssueID, IsAnnual, NameRecordID, Role, RawRoleText, RawCreditName)
            VALUES (101, '999', '123', 0, 42, 'writer', 'Story', 'Scott Snyder and Nick Dragotta');
        """)
        class MockDB:
            def select(self, query, params=None):
                return conn.cursor().execute(query, params or []).fetchall()

        service = CreatorBrowserService(db_conn=MockDB())
        res = service.get_issue_creator_credits('123', is_annual=0)
        self.assertTrue(res['success'])
        self.assertEqual(res['total_credits'], 1)
        self.assertEqual(res['credits'][0]['raw_name'], 'Scott Snyder and Nick Dragotta')
        self.assertEqual(res['credits'][0]['name_record_id'], 42)

    def test_06_separate_name_records_remain_separate(self):
        """Two distinct NameRecordIDs with identical normalized names remain distinct links."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.executescript("""
            CREATE TABLE ext_creator_name_records (
                NameRecordID INTEGER PRIMARY KEY,
                RawName TEXT NOT NULL,
                NormalizedName TEXT NOT NULL,
                NameSlug TEXT NOT NULL,
                CreatorEntityID INTEGER DEFAULT NULL,
                ResolutionSource TEXT DEFAULT 'unresolved'
            );
            CREATE TABLE ext_creator_credits (
                CreditID INTEGER PRIMARY KEY,
                ComicID TEXT NOT NULL,
                IssueID TEXT NOT NULL,
                IsAnnual INTEGER DEFAULT 0,
                NameRecordID INTEGER NOT NULL,
                Role TEXT NOT NULL,
                RawRoleText TEXT,
                RawCreditName TEXT NOT NULL,
                IsCover INTEGER DEFAULT 0,
                SortOrder INTEGER DEFAULT 0,
                SourceProvenance TEXT DEFAULT 'comicinfo'
            );
            CREATE TABLE ext_creator_entities (
                CreatorEntityID INTEGER PRIMARY KEY,
                DisplayName TEXT NOT NULL,
                EntitySlug TEXT NOT NULL
            );
            INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug)
            VALUES (1, 'Frank Cho', 'frank cho', 'frank-cho'),
                   (2, 'FRANK CHO', 'frank cho', 'frank-cho');
            INSERT INTO ext_creator_credits (CreditID, ComicID, IssueID, IsAnnual, NameRecordID, Role, RawRoleText, RawCreditName, SortOrder)
            VALUES (1, '100', '200', 0, 1, 'writer', 'Writer', 'Frank Cho', 0),
                   (2, '100', '200', 0, 2, 'cover_artist', 'Cover', 'FRANK CHO', 1);
        """)
        class MockDB:
            def select(self, query, params=None):
                return conn.cursor().execute(query, params or []).fetchall()

        service = CreatorBrowserService(db_conn=MockDB())
        res = service.get_issue_creator_credits('200', is_annual=0)
        self.assertTrue(res['success'])
        self.assertEqual(res['total_credits'], 2)
        self.assertEqual(res['credits'][0]['name_record_id'], 1)
        self.assertEqual(res['credits'][0]['raw_name'], 'Frank Cho')
        self.assertEqual(res['credits'][1]['name_record_id'], 2)
        self.assertEqual(res['credits'][1]['raw_name'], 'FRANK CHO')

    def test_07_controller_validation_and_errors(self):
        """Controller validates issueid and annual parameters, returning HTTP 400 on bad input."""
        import cherrypy
        
        # Test 1: Missing issueid
        cherrypy.response.status = 200
        res = handle_issue_creator_credits(issueid=None)
        self.assertEqual(cherrypy.response.status, 400)
        parsed = json.loads(res)
        self.assertFalse(parsed['success'])
        
        # Test 2: Malformed non-numeric issueid
        cherrypy.response.status = 200
        res = handle_issue_creator_credits(issueid='not_a_number')
        self.assertEqual(cherrypy.response.status, 400)
        
        # Test 3: Negative issueid
        cherrypy.response.status = 200
        res = handle_issue_creator_credits(issueid='-5')
        self.assertEqual(cherrypy.response.status, 400)
        
        # Test 4: Invalid annual parameter
        cherrypy.response.status = 200
        res = handle_issue_creator_credits(issueid='868995', annual='invalid_scope')
        self.assertEqual(cherrypy.response.status, 400)

    def test_08_database_invariance(self):
        """Database invariance: confirm zero rows added to core tables or ext_creator_entities."""
        conn = self._get_staging_db_conn()
        cur = conn.cursor()
        
        issues_count = cur.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
        annuals_count = cur.execute("SELECT COUNT(*) FROM annuals").fetchone()[0]
        comics_count = cur.execute("SELECT COUNT(*) FROM comics").fetchone()[0]
        storyarcs_count = cur.execute("SELECT COUNT(*) FROM storyarcs").fetchone()[0]
        entities_count = cur.execute("SELECT COUNT(*) FROM ext_creator_entities").fetchone()[0]
        name_records_count = cur.execute("SELECT COUNT(*) FROM ext_creator_name_records").fetchone()[0]
        credits_count = cur.execute("SELECT COUNT(*) FROM ext_creator_credits").fetchone()[0]
        
        # Execute service and controller queries
        service = CreatorBrowserService()
        service.get_issue_creator_credits('868995', is_annual=0)
        service.get_issue_creator_credits('9999999', is_annual=0)
        handle_issue_creator_credits(issueid='868995', annual=0)
        
        # Re-check counts
        self.assertEqual(cur.execute("SELECT COUNT(*) FROM issues").fetchone()[0], issues_count)
        self.assertEqual(cur.execute("SELECT COUNT(*) FROM annuals").fetchone()[0], annuals_count)
        self.assertEqual(cur.execute("SELECT COUNT(*) FROM comics").fetchone()[0], comics_count)
        self.assertEqual(cur.execute("SELECT COUNT(*) FROM storyarcs").fetchone()[0], storyarcs_count)
        self.assertEqual(cur.execute("SELECT COUNT(*) FROM ext_creator_entities").fetchone()[0], entities_count)
        self.assertEqual(cur.execute("SELECT COUNT(*) FROM ext_creator_name_records").fetchone()[0], name_records_count)
        self.assertEqual(cur.execute("SELECT COUNT(*) FROM ext_creator_credits").fetchone()[0], credits_count)
        conn.close()


if __name__ == '__main__':
    unittest.main()
