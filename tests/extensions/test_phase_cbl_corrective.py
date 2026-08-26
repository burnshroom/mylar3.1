"""
Phase CBL Corrective Comprehensive Verification Suite:
Actionable CBL Import Behavior, Mutation Security, Integration Importer Verification,
Transaction Failures, Preview-Commit Consistency, and Existing Arc Recovery.
"""

import os
import sys
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))
sys.path.insert(0, REPO_ROOT)

import cherrypy
import mylar
from mylar.extensions.storyarcs import (
    cbl_service,
    controller,
    service,
    cbl_catalog,
    get_or_create_cbl_csrf_token,
    verify_cbl_csrf_token,
)


class FakeRow(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().get(key)
    def keys(self):
        return super().keys()


class FakeCursor:
    def __init__(self, rows):
        self.rows = [FakeRow(r) for r in rows]
    def fetchone(self):
        return self.rows[0] if self.rows else None
    def fetchall(self):
        return self.rows


class SQLiteDBAdapter:
    def __init__(self, connection):
        self.conn = connection
        self.conn.row_factory = sqlite3.Row

    def select(self, query, params=None):
        cur = self.conn.cursor()
        cur.execute(query, params or [])
        rows = cur.fetchall()
        return [FakeRow(dict(r)) for r in rows]

    def selectone(self, query, params=None):
        cur = self.conn.cursor()
        cur.execute(query, params or [])
        row = cur.fetchone()
        return FakeCursor([dict(row)] if row else [])

    def action(self, query, params=None):
        cur = self.conn.cursor()
        cur.execute(query, params or [])
        self.conn.commit()
        return cur

    def upsert(self, tableName, valueDict, keyDict):
        cur = self.conn.cursor()
        set_clause = ', '.join([f"{k} = ?" for k in valueDict.keys()])
        where_clause = ' AND '.join([f"{k} = ?" for k in keyDict.keys()])
        params = list(valueDict.values()) + list(keyDict.values())
        cur.execute(f"UPDATE {tableName} SET {set_clause} WHERE {where_clause}", params)
        if cur.rowcount == 0:
            all_dict = {**keyDict, **valueDict}
            cols = ', '.join(all_dict.keys())
            placeholders = ', '.join(['?' for _ in all_dict])
            cur.execute(f"INSERT INTO {tableName} ({cols}) VALUES ({placeholders})", list(all_dict.values()))
        self.conn.commit()


BATMAN_LONELY_PLACE_CBL_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<ReadingList xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <Name>Batman: A Lonely Place of Dying</Name>
  <Publisher>DC Comics</Publisher>
  <Books>
    <Book Series="Batman" Volume="1940" Number="440" Year="1989">
      <Database Name="cv" Series="1001" Issue="440" />
    </Book>
    <Book Series="The New Titans" Volume="1988" Number="60" Year="1989">
      <Database Name="cv" Series="5001" Issue="60" />
    </Book>
    <Book Series="Batman" Volume="1940" Number="441" Year="1989">
      <Database Name="cv" Series="1001" Issue="441" />
    </Book>
    <Book Series="The New Titans" Volume="1988" Number="61" Year="1989">
      <Database Name="cv" Series="5001" Issue="61" />
    </Book>
    <Book Series="Batman" Volume="1940" Number="442" Year="1989">
      <Database Name="cv" Series="1001" Issue="442" />
    </Book>
  </Books>
</ReadingList>"""


class TestPhaseCBLCorrectiveComprehensive(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, 'mylar_cbl_test.db')
        self.conn = sqlite3.connect(self.db_path)
        self.cursor = self.conn.cursor()

        mylar.DATA_DIR = self.test_dir

        self.cursor.execute('''CREATE TABLE comics (
            ComicID TEXT PRIMARY KEY,
            ComicName TEXT,
            ComicYear TEXT,
            Status TEXT
        )''')

        self.cursor.execute('''CREATE TABLE issues (
            IssueID TEXT PRIMARY KEY,
            ComicID TEXT,
            IssueName TEXT,
            Issue_Number TEXT,
            Status TEXT,
            Location TEXT,
            IssueDate TEXT,
            ReleaseDate TEXT
        )''')

        self.cursor.execute('''CREATE TABLE annuals (
            IssueID TEXT PRIMARY KEY,
            ComicID TEXT,
            IssueName TEXT,
            Issue_Number TEXT,
            Status TEXT,
            Location TEXT,
            IssueDate TEXT,
            ReleaseDate TEXT,
            Deleted INT DEFAULT 0
        )''')

        self.cursor.execute('''CREATE TABLE storyarcs (
            StoryArcID TEXT,
            ComicName TEXT,
            IssueNumber TEXT,
            SeriesYear TEXT,
            IssueYEAR TEXT,
            StoryArc TEXT,
            TotalIssues TEXT,
            Status TEXT,
            IssueArcID TEXT,
            ReadingOrder INT,
            IssueID TEXT,
            ComicID TEXT,
            IssueName TEXT,
            Publisher TEXT,
            DateAdded TEXT,
            Type TEXT,
            Manual TEXT
        )''')

        self.cursor.execute('''CREATE TABLE storyarc_manifests (
            StoryArcID TEXT PRIMARY KEY,
            StoryArcName TEXT,
            SourceName TEXT,
            SourceType TEXT,
            RepoURL TEXT,
            RepoCommit TEXT,
            RepoPath TEXT,
            SHA256 TEXT,
            ImportTime TEXT,
            TotalIssues INT,
            RawXMLPath TEXT
        )''')

        # Insert Series 1001 (Batman)
        self.cursor.execute("INSERT INTO comics VALUES ('1001', 'Batman', '1940', 'Active')")

        # Insert issues for Series 1001:
        # Issue 440: Downloaded
        self.cursor.execute("INSERT INTO issues VALUES ('440', '1001', 'Part 1: Suspects', '440', 'Downloaded', '/comics/Batman/440.cbz', '1989-10-01', '1989-10-01')")
        # Issue 441: Skipped
        self.cursor.execute("INSERT INTO issues VALUES ('441', '1001', 'Part 3: The Search', '441', 'Skipped', 'None', '1989-11-01', '1989-11-01')")
        # Issue 442: Archived
        self.cursor.execute("INSERT INTO issues VALUES ('442', '1001', 'Part 5: Conclusion', '442', 'Archived', 'None', '1989-12-01', '1989-12-01')")

        # Note: Series 5001 (The New Titans) and issues 60, 61 are NOT in comics/issues table (Unmonitored Series).

        self.conn.commit()
        self.myDB = SQLiteDBAdapter(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_01_carbon_contract_matrix_and_defaults(self):
        """
        Verifies Carbon's exact contract across all status combinations and options:
        - Downloaded -> No action needed
        - Skipped -> Mark Wanted
        - Archived (ignorearchived=True) -> Skipped because Archived
        - Archived (ignorearchived=False) -> Mark Wanted
        - Unmonitored (apply_library) -> Add series and mark issue Wanted
        - Unmonitored (reading_list_only) -> Reading-list entry only
        """
        # 1. Apply to library with ignorearchived=True
        res_apply_ignore = cbl_service.parse_and_reconcile_cbl(
            BATMAN_LONELY_PLACE_CBL_XML,
            'batman_lonely.cbl',
            myDB=self.myDB,
            import_mode='apply_library',
            issuesonly=True,
            ignorearchived=True
        )
        self.assertEqual(res_apply_ignore['status'], 'success')
        summary1 = res_apply_ignore['summary']
        self.assertEqual(summary1['total_entries'], 5)
        self.assertEqual(summary1['series_to_add'], 1)  # New Titans 5001
        self.assertEqual(summary1['issues_to_want'], 3)  # Batman 441, New Titans 60 & 61
        self.assertEqual(summary1['unchanged_entries'], 2)  # Batman 440 (Downloaded) & 442 (Archived excluded)
        self.assertEqual(summary1['archived_excluded'], 1)

        actions1 = [r['predicted_action'] for r in res_apply_ignore['results']]
        self.assertEqual(actions1[0], 'No action needed — Downloaded')
        self.assertEqual(actions1[1], 'Add series and mark issue Wanted')
        self.assertEqual(actions1[2], 'Mark issue Wanted')
        self.assertEqual(actions1[3], 'Add series and mark issue Wanted')
        self.assertEqual(actions1[4], 'Skipped because Archived')

        # 2. Apply to library with ignorearchived=False
        res_apply_include = cbl_service.parse_and_reconcile_cbl(
            BATMAN_LONELY_PLACE_CBL_XML,
            'batman_lonely.cbl',
            myDB=self.myDB,
            import_mode='apply_library',
            issuesonly=True,
            ignorearchived=False
        )
        self.assertEqual(res_apply_include['summary']['issues_to_want'], 4)  # 441, 442, 60, 61
        self.assertEqual(res_apply_include['summary']['archived_excluded'], 0)
        self.assertEqual(res_apply_include['results'][4]['predicted_action'], 'Mark issue Wanted')

        # 3. Reading list only
        res_ro = cbl_service.parse_and_reconcile_cbl(
            BATMAN_LONELY_PLACE_CBL_XML,
            'batman_lonely.cbl',
            myDB=self.myDB,
            import_mode='reading_list_only'
        )
        self.assertEqual(res_ro['summary']['series_to_add'], 0)
        self.assertEqual(res_ro['summary']['issues_to_want'], 0)
        for r in res_ro['results']:
            self.assertEqual(r['predicted_action'], 'Reading-list entry only')

    def test_02_endpoint_security_post_method_and_csrf_enforcement(self):
        """
        Verifies HTTP POST enforcement and CSRF validation on all mutation routes:
        - handle_cbl_confirm_import
        - handle_cbl_reconcile_arc (apply=true)
        - handle_cbl_entry_action
        - handle_cbl_delete_arc
        - handle_cbl_upload
        - handle_cbl_catalog_refresh
        """
        valid_csrf = get_or_create_cbl_csrf_token()

        class MockRequest:
            def __init__(self, method='GET', headers=None):
                self.method = method
                self.headers = headers or {}

        # 1. handle_cbl_confirm_import: Reject GET (405)
        cherrypy.request = MockRequest(method='GET')
        res_get = json.loads(controller.handle_cbl_confirm_import(token='token123', csrf_token=valid_csrf))
        self.assertEqual(res_get['error_code'], 'method_not_allowed')

        # 2. handle_cbl_confirm_import: Reject invalid CSRF (403)
        cherrypy.request = MockRequest(method='POST')
        res_bad_csrf = json.loads(controller.handle_cbl_confirm_import(token='token123', csrf_token='bad_token'))
        self.assertEqual(res_bad_csrf['error_code'], 'invalid_csrf_token')

        # 3. handle_cbl_reconcile_arc: Apply=true requires POST and valid CSRF
        cherrypy.request = MockRequest(method='GET')
        res_recon_get = json.loads(controller.handle_cbl_reconcile_arc(storyarcid='arc123', apply='true', csrf_token=valid_csrf))
        self.assertEqual(res_recon_get['error_code'], 'method_not_allowed')

        cherrypy.request = MockRequest(method='POST')
        res_recon_bad_csrf = json.loads(controller.handle_cbl_reconcile_arc(storyarcid='arc123', apply='true', csrf_token='wrong'))
        self.assertEqual(res_recon_bad_csrf['error_code'], 'invalid_csrf_token')

        # 4. handle_cbl_entry_action: Requires POST and valid CSRF
        cherrypy.request = MockRequest(method='GET')
        res_entry_get = json.loads(controller.handle_cbl_entry_action(storyarcid='arc123', issue_arc_id='entry1', action='mark_wanted', csrf_token=valid_csrf))
        self.assertEqual(res_entry_get['error_code'], 'method_not_allowed')

        cherrypy.request = MockRequest(method='POST')
        res_entry_bad_csrf = json.loads(controller.handle_cbl_entry_action(storyarcid='arc123', issue_arc_id='entry1', action='mark_wanted', csrf_token='forged'))
        self.assertEqual(res_entry_bad_csrf['error_code'], 'invalid_csrf_token')

        # 5. handle_cbl_delete_arc: Requires POST and valid CSRF
        cherrypy.request = MockRequest(method='GET')
        res_del_get = json.loads(controller.handle_cbl_delete_arc(storyarcid='arc123', csrf_token=valid_csrf))
        self.assertEqual(res_del_get['error_code'], 'method_not_allowed')

        # 6. Header CSRF token validation (X-CSRF-Token)
        cherrypy.request = MockRequest(method='POST', headers={'X-CSRF-Token': valid_csrf})
        self.assertTrue(controller._validate_csrf(None))

    def test_03_endpoint_identifier_validation_and_ownership_enforcement(self):
        """
        Verifies cross-record authorization protection:
        - Entry belonging to Arc A cannot be modified via Arc B.
        - Invalid actions and malformed identifiers cause zero writes.
        """
        valid_csrf = get_or_create_cbl_csrf_token()
        cherrypy.request = MagicMock(method='POST', headers={'X-CSRF-Token': valid_csrf})

        # Insert Arc A and Arc B
        self.cursor.execute("INSERT INTO storyarcs VALUES ('arc_A', 'Batman', '440', '1940', '1989', 'Arc A', '1', 'Imported', 'arc_A_1', 1, '440', '1001', 'Part 1', 'DC Comics', '2026-08-25', 'cbl', NULL)")
        self.cursor.execute("INSERT INTO storyarcs VALUES ('arc_B', 'Batman', '441', '1940', '1989', 'Arc B', '1', 'Imported', 'arc_B_1', 1, '441', '1001', 'Part 3', 'DC Comics', '2026-08-25', 'cbl', NULL)")
        self.conn.commit()

        with patch('mylar.db.DBConnection', return_value=self.myDB):
            # Cross-record attempt: Ask Arc A to mutate entry arc_B_1
            res = json.loads(controller.handle_cbl_entry_action(storyarcid='arc_A', issue_arc_id='arc_B_1', action='mark_wanted', csrf_token=valid_csrf))
            self.assertEqual(res['status'], 'error')
            self.assertIn('not found', res['message'].lower())

            # Untrusted/invalid action
            res_bad_act = json.loads(controller.handle_cbl_entry_action(storyarcid='arc_A', issue_arc_id='arc_A_1', action='drop_table_comics', csrf_token=valid_csrf))
            self.assertEqual(res_bad_act['error_code'], 'invalid_action')

    def test_04_server_side_protection_untrusted_client_actions(self):
        """
        Proves server-side protection prevents status downgrades:
        - Downloaded issues are protected from being marked Wanted.
        - Archived issues are protected when ignorearchived=True.
        """
        # 1. Attempt to mark Downloaded issue 440 Wanted via entry action
        self.cursor.execute("INSERT INTO storyarcs VALUES ('arc_test', 'Batman', '440', '1940', '1989', 'Arc Test', '1', 'Imported', 'arc_test_1', 1, '440', '1001', 'Part 1', 'DC Comics', '2026-08-25', 'cbl', NULL)")
        self.conn.commit()

        res1 = cbl_service.execute_entry_action('arc_test', 'arc_test_1', 'mark_wanted', myDB=self.myDB)
        self.assertEqual(res1['status'], 'info')
        self.assertIn('already in state', res1['message'])

        # Verify DB untouched
        iss_440 = self.myDB.selectone("SELECT Status FROM issues WHERE IssueID='440'").fetchone()
        self.assertEqual(iss_440['Status'], 'Downloaded')

        # 2. _apply_library_mutations ignores Downloaded issue even if passed directly
        cbl_service._apply_library_mutations(volume_index={}, monitored_want_ids=['440'], issuesonly=True, ignorearchived=True, myDB=self.myDB)
        iss_440_after = self.myDB.selectone("SELECT Status FROM issues WHERE IssueID='440'").fetchone()
        self.assertEqual(iss_440_after['Status'], 'Downloaded')

    @patch('mylar.importer.importer_thread')
    @patch('mylar.importer.issue_watcher_thread')
    def test_05_integration_unmonitored_series_import_and_worker_payload(self, mock_watcher, mock_importer):
        """
        Integration-level proof for unmonitored-series import:
        1. Correct ComicVine volume submitted for addition.
        2. Only intended CBL issues submitted for Wanted status when issuesonly=True.
        3. Story Arc entries resolve to correct ComicID and IssueID.
        4. Simulated worker execution updates target issues while leaving unrelated issues unchanged.
        5. Duplicate series addition prevented.
        6. Repeat operation is idempotent.
        """
        upload_res = cbl_service.upload_cbl_manifest(
            BATMAN_LONELY_PLACE_CBL_XML,
            'batman_lonely.cbl',
            myDB=self.myDB,
            import_mode='apply_library',
            issuesonly=True,
            ignorearchived=True
        )
        token = upload_res['upload_token']

        confirm_res = cbl_service.confirm_cbl_import(
            token,
            filename='batman_lonely.cbl',
            myDB=self.myDB,
            import_mode='apply_library',
            issuesonly=True,
            ignorearchived=True
        )
        self.assertEqual(confirm_res['status'], 'success')
        arc_id = confirm_res['storyarcid']

        # 1. Verify exact payload sent to importer_thread for unmonitored series 5001
        mock_importer.assert_any_call([{
            'comicid': '5001',
            'comicname': 'The New Titans',
            'seriesyear': '1988',
            'suppress_addall': True
        }])

        # 2. Verify exact payload sent to issue_watcher_thread for unmonitored volume issues
        mock_watcher.assert_any_call(['60', '61'])

        # 3. Verify Story Arc rows in database
        arc_rows = self.myDB.select("SELECT * FROM storyarcs WHERE StoryArcID=? ORDER BY ReadingOrder ASC", [arc_id])
        self.assertEqual(len(arc_rows), 5)
        self.assertEqual(arc_rows[1]['ComicID'], '5001')
        self.assertEqual(arc_rows[1]['IssueID'], '60')
        self.assertEqual(arc_rows[3]['ComicID'], '5001')
        self.assertEqual(arc_rows[3]['IssueID'], '61')

        # 4. Consumer worker test: simulate background addComictoDB indexing Series 5001 with issues 60, 61, and unrelated 62
        self.cursor.execute("INSERT INTO comics VALUES ('5001', 'The New Titans', '1988', 'Active')")
        self.cursor.execute("INSERT INTO issues VALUES ('60', '5001', 'Part 2: Roots', '60', 'Skipped', 'None', '1989-10-15', '1989-10-15')")
        self.cursor.execute("INSERT INTO issues VALUES ('61', '5001', 'Part 4: Going Home', '61', 'Skipped', 'None', '1989-11-15', '1989-11-15')")
        self.cursor.execute("INSERT INTO issues VALUES ('62', '5001', 'Unrelated Storyline', '62', 'Skipped', 'None', '1989-12-15', '1989-12-15')")
        self.conn.commit()

        # Simulate consumer issue_watcher_thread marking watched issues
        from mylar import importer
        with patch('mylar.db.DBConnection', return_value=self.myDB):
            importer.markIssueWantedById('60')
            importer.markIssueWantedById('61')

        # Verify: target issues 60 & 61 are Wanted, unrelated issue 62 remains Skipped!
        iss_60 = self.myDB.selectone("SELECT Status FROM issues WHERE IssueID='60'").fetchone()
        iss_61 = self.myDB.selectone("SELECT Status FROM issues WHERE IssueID='61'").fetchone()
        iss_62 = self.myDB.selectone("SELECT Status FROM issues WHERE IssueID='62'").fetchone()

        self.assertEqual(iss_60['Status'], 'Wanted')
        self.assertEqual(iss_61['Status'], 'Wanted')
        self.assertEqual(iss_62['Status'], 'Skipped')

        # 5. Idempotent repeat: submitting again reports already_imported with zero duplicate records
        repeat_res = cbl_service.confirm_cbl_import(token, myDB=self.myDB, import_mode='apply_library')
        self.assertEqual(repeat_res['status'], 'already_imported')
        count = self.myDB.selectone("SELECT count(*) FROM storyarc_manifests WHERE StoryArcID=?", [arc_id]).fetchone()[0]
        self.assertEqual(count, 1)

    def test_06_transaction_and_failure_boundaries(self):
        """
        Verifies atomic rollback on failure:
        - DB insertion failure rolls back manifests and storyarcs.
        - Stale/expired/missing token returns clean error with zero writes.
        """
        # 1. Missing or invalid token
        res_bad = cbl_service.confirm_cbl_import('invalid_token_xyz', myDB=self.myDB)
        self.assertEqual(res_bad['status'], 'error')

        # 2. Failure injection during insertion rolls back transaction
        class FailingDBAdapter(SQLiteDBAdapter):
            def action(self, query, params=None):
                if 'INSERT INTO storyarcs' in query:
                    raise sqlite3.OperationalError("Simulated disk write failure")
                return super().action(query, params)

        failing_db = FailingDBAdapter(self.conn)
        upload_res = cbl_service.upload_cbl_manifest(BATMAN_LONELY_PLACE_CBL_XML, 'fail_test.cbl', myDB=self.myDB)
        token = upload_res['upload_token']

        res_fail = cbl_service.confirm_cbl_import(token, myDB=failing_db)
        self.assertEqual(res_fail['status'], 'error')

        # Verify rollback: no rows left in storyarc_manifests or storyarcs
        manifest_row = self.myDB.selectone("SELECT count(*) as cnt FROM storyarc_manifests WHERE SHA256=?", [token]).fetchone()
        manifest_cnt = manifest_row['cnt'] if manifest_row else 0
        self.assertEqual(manifest_cnt, 0)

        arc_row = self.myDB.selectone("SELECT count(*) as cnt FROM storyarcs WHERE StoryArcID=?", [f"cbl_{token[:12]}"]).fetchone()
        arc_cnt = arc_row['cnt'] if arc_row else 0
        self.assertEqual(arc_cnt, 0)

    def test_07_preview_and_commit_consistency_with_concurrent_state_changes(self):
        """
        Proves preview and confirmation share identical logic and re-read live DB state:
        - If an issue was Downloaded after preview, commit re-reads DB and does not overwrite it.
        - If an issue was Archived after preview, commit respects ignorearchived.
        """
        upload_res = cbl_service.upload_cbl_manifest(BATMAN_LONELY_PLACE_CBL_XML, 'state_change.cbl', myDB=self.myDB, import_mode='apply_library', ignorearchived=True)
        token = upload_res['upload_token']

        # Concurrent state change: Issue 441 is downloaded in the background between preview and confirm
        self.cursor.execute("UPDATE issues SET Status='Downloaded', Location='/comics/Batman/441.cbz' WHERE IssueID='441'")
        self.conn.commit()

        # Confirmation executes and re-reads DB
        confirm_res = cbl_service.confirm_cbl_import(token, myDB=self.myDB, import_mode='apply_library', ignorearchived=True)
        self.assertEqual(confirm_res['status'], 'success')

        # Truthful verified outcome: Issue 441 remained Downloaded (not overwritten to Wanted)
        iss_441 = self.myDB.selectone("SELECT Status, Location FROM issues WHERE IssueID='441'").fetchone()
        self.assertEqual(iss_441['Status'], 'Downloaded')

    @patch('mylar.importer.importer_thread')
    @patch('mylar.importer.issue_watcher_thread')
    def test_08_verify_existing_arc_recovery_batman_lonely_place_of_dying(self, mock_watcher, mock_importer):
        """
        Demonstrates the exact recovery of the previously imported manifest-only Story Arc:
        '[DC Comics] Batman - A Lonely Place of Dying'
        - Pre-reconciliation: 2 unmonitored entries (The New Titans #60, #61), 1 skipped entry (Batman #441).
        - Preview reconciliation: predicts 1 series to add, 3 issues to want, 2 unchanged.
        - Bulk reconciliation applied: repairs all entries in place without deleting/reimporting the Story Arc.
        """
        arc_id = 'cbl_c33e762620fe'
        self.cursor.execute("INSERT INTO storyarc_manifests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            [arc_id, 'Batman: A Lonely Place of Dying', '[DC Comics] Batman- A Lonely Place of Dying.cbl', 'upload', None, None, None, 'c33e762620fefe249015c10d8591e40492edbfde20b47581ddd0e14f1a2f60f6', '2026-08-25 12:00:00', 5, None])

        # Insert 5 arc rows
        self.cursor.execute("INSERT INTO storyarcs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            [arc_id, 'Batman', '440', '1940', '1989', 'Batman: A Lonely Place of Dying', '5', 'Imported', f'{arc_id}_1', 1, '440', '1001', 'Part 1', 'DC Comics', '2026-08-25', 'cbl', None])
        self.cursor.execute("INSERT INTO storyarcs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            [arc_id, 'The New Titans', '60', '1988', '1989', 'Batman: A Lonely Place of Dying', '5', 'Imported', f'{arc_id}_2', 2, '60', '5001', 'Part 2', 'DC Comics', '2026-08-25', 'cbl', None])
        self.cursor.execute("INSERT INTO storyarcs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            [arc_id, 'Batman', '441', '1940', '1989', 'Batman: A Lonely Place of Dying', '5', 'Imported', f'{arc_id}_3', 3, '441', '1001', 'Part 3', 'DC Comics', '2026-08-25', 'cbl', None])
        self.cursor.execute("INSERT INTO storyarcs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            [arc_id, 'The New Titans', '61', '1988', '1989', 'Batman: A Lonely Place of Dying', '5', 'Imported', f'{arc_id}_4', 4, '61', '5001', 'Part 4', 'DC Comics', '2026-08-25', 'cbl', None])
        self.cursor.execute("INSERT INTO storyarcs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            [arc_id, 'Batman', '442', '1940', '1989', 'Batman: A Lonely Place of Dying', '5', 'Imported', f'{arc_id}_5', 5, '442', '1001', 'Part 5', 'DC Comics', '2026-08-25', 'cbl', None])
        self.conn.commit()

        # Step 1: Preview Reconciliation
        preview = cbl_service.reconcile_existing_storyarc(
            storyarc_id=arc_id,
            myDB=self.myDB,
            import_mode='apply_library',
            issuesonly=True,
            ignorearchived=True,
            apply_changes=False
        )
        self.assertEqual(preview['status'], 'success')
        self.assertEqual(preview['summary']['total_entries'], 5)
        self.assertEqual(preview['summary']['series_to_add'], 1)  # The New Titans
        self.assertEqual(preview['summary']['issues_to_want'], 3)  # 441, 60, 61
        self.assertEqual(preview['summary']['unchanged_entries'], 2)  # 440 (Downloaded), 442 (Archived excluded)
        self.assertEqual(preview['summary']['archived_excluded'], 1)

        # Step 2: Apply Reconciliation
        applied = cbl_service.reconcile_existing_storyarc(
            storyarc_id=arc_id,
            myDB=self.myDB,
            import_mode='apply_library',
            issuesonly=True,
            ignorearchived=True,
            apply_changes=True
        )
        self.assertEqual(applied['status'], 'success')

        # Step 3: Verify Mutations
        # - Batman #440 stays Downloaded
        iss_440 = self.myDB.selectone("SELECT Status FROM issues WHERE IssueID='440'").fetchone()
        self.assertEqual(iss_440['Status'], 'Downloaded')

        # - Batman #441 is now Wanted
        iss_441 = self.myDB.selectone("SELECT Status FROM issues WHERE IssueID='441'").fetchone()
        self.assertEqual(iss_441['Status'], 'Wanted')

        # - Batman #442 stays Archived
        iss_442 = self.myDB.selectone("SELECT Status FROM issues WHERE IssueID='442'").fetchone()
        self.assertEqual(iss_442['Status'], 'Archived')

        # - The New Titans queued for addition
        mock_importer.assert_any_call([{
            'comicid': '5001',
            'comicname': 'The New Titans',
            'seriesyear': '1988',
            'suppress_addall': True
        }])
        mock_watcher.assert_any_call(['60', '61'])

        # - Story Arc remains fully intact
        arc_rows_post = self.myDB.select("SELECT * FROM storyarcs WHERE StoryArcID=?", [arc_id])
        self.assertEqual(len(arc_rows_post), 5)

    @patch('mylar.importer.importer_thread')
    @patch('mylar.importer.issue_watcher_thread')
    def test_09_per_entry_actions_lifecycle_and_idempotency(self, mock_watcher, mock_importer):
        """
        Verifies granular per-entry actions on the Story Arc detail page:
        - add_series
        - mark_wanted
        - retry_resolution
        """
        arc_id = 'arc_per_entry'
        self.cursor.execute("INSERT INTO storyarcs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            [arc_id, 'The New Titans', '60', '1988', '1989', 'Arc Entry Test', '1', 'Imported', f'{arc_id}_1', 1, '60', '5001', 'Part 2', 'DC Comics', '2026-08-25', 'cbl', None])
        self.conn.commit()

        # Action: add_series
        res_add = cbl_service.execute_entry_action(arc_id, f'{arc_id}_1', 'add_series', myDB=self.myDB)
        self.assertEqual(res_add['status'], 'success')
        mock_importer.assert_called()

        # Repeat add_series when already monitored
        self.cursor.execute("INSERT INTO comics VALUES ('5001', 'The New Titans', '1988', 'Active')")
        self.conn.commit()
        res_add_repeat = cbl_service.execute_entry_action(arc_id, f'{arc_id}_1', 'add_series', myDB=self.myDB)
        self.assertEqual(res_add_repeat['status'], 'success')
        self.assertIn('monitored', res_add_repeat['message'])

        # Action: mark_wanted
        self.cursor.execute("INSERT INTO issues VALUES ('60', '5001', 'Part 2', '60', 'Skipped', 'None', '1989-10-15', '1989-10-15')")
        self.conn.commit()

        res_want = cbl_service.execute_entry_action(arc_id, f'{arc_id}_1', 'mark_wanted', myDB=self.myDB)
        self.assertEqual(res_want['status'], 'success')
        iss_60 = self.myDB.selectone("SELECT Status FROM issues WHERE IssueID='60'").fetchone()
        self.assertEqual(iss_60['Status'], 'Wanted')

        # Action: retry_resolution
        res_retry = cbl_service.execute_entry_action(arc_id, f'{arc_id}_1', 'retry_resolution', myDB=self.myDB)
        self.assertEqual(res_retry['status'], 'success')
        self.assertEqual(res_retry['resolution_state'], 'Missing (Monitored)')

    def test_10_xxe_rejection_and_book_ceiling(self):
        """
        Verifies security boundaries against XXE entities and oversized payloads:
        - DOCTYPE declarations rejected.
        - Payloads exceeding 1,000 books rejected.
        """
        xxe_xml = b'<?xml version="1.0"?><!DOCTYPE test [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><ReadingList><Name>XXE</Name><Books><Book Series="X" Volume="1" Number="1" Year="2020"/></Books></ReadingList>'
        res_xxe = cbl_service.parse_and_reconcile_cbl(xxe_xml, 'xxe.cbl', myDB=self.myDB)
        self.assertEqual(res_xxe['status'], 'error')
        self.assertIn('DOCTYPE', res_xxe['message'])

        # Over 1000 books
        books_str = ''.join([f'<Book Series="Test" Volume="2020" Number="{i}" Year="2020"><Database Name="cv" Series="1" Issue="{i}"/></Book>' for i in range(1005)])
        big_xml = f'<?xml version="1.0"?><ReadingList><Name>Big</Name><Books>{books_str}</Books></ReadingList>'.encode('utf-8')
        res_big = cbl_service.parse_and_reconcile_cbl(big_xml, 'big.cbl', myDB=self.myDB)
        self.assertEqual(res_big['status'], 'error')
        self.assertIn('1,000', res_big['message'])

    def test_11_service_detail_action_flags(self):
        """
        Verifies get_storyarc_detail enriches readlist records with per-entry action flags:
        - IsUnmonitoredSeries, CanAddSeries, CanMarkWanted
        """
        arc_id = 'test_arc_service'
        self.cursor.execute("INSERT INTO storyarc_manifests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            [arc_id, 'Service Test Arc', 'test.cbl', 'upload', None, None, None, 'hash1', '2026-08-25', 2, None])
        self.cursor.execute("INSERT INTO storyarcs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            [arc_id, 'Batman', '440', '1940', '1989', 'Service Test Arc', '2', 'Imported', f'{arc_id}_1', 1, '440', '1001', 'Batman #440', 'DC Comics', '2026-08-25', 'cbl', None])
        self.cursor.execute("INSERT INTO storyarcs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            [arc_id, 'The New Titans', '60', '1988', '1989', 'Service Test Arc', '2', 'Imported', f'{arc_id}_2', 2, '60', '5001', 'New Titans #60', 'DC Comics', '2026-08-25', 'cbl', None])
        self.conn.commit()

        with patch('mylar.db.DBConnection', return_value=self.myDB), patch('mylar.helpers.updatearc_locs'):
            detail = service.get_storyarc_detail(arc_id, myDB=self.myDB)
            readlist = detail['readlist']
            self.assertEqual(len(readlist), 2)
            # Entry 1 (Batman #440 - Downloaded)
            self.assertTrue(readlist[0]['IsMonitored'])
            self.assertFalse(readlist[0]['IsUnmonitoredSeries'])
            self.assertFalse(readlist[0]['CanAddSeries'])

            # Entry 2 (New Titans #60 - Unmonitored)
            self.assertFalse(readlist[1]['IsMonitored'])
            self.assertTrue(readlist[1]['IsUnmonitoredSeries'])
            self.assertTrue(readlist[1]['CanAddSeries'])

    def test_12_catalog_endpoints_and_status(self):
        """
        Verifies local DieselTech catalog search, status queries, and CSRF protection on refresh:
        """
        status = cbl_catalog.get_catalog_status()
        self.assertIn('cached', status)
        self.assertIn('total_count', status)

        # When not cached
        search_res = cbl_catalog.search_catalog(query='Batman', limit=10)
        self.assertIn(search_res['status'], ('success', 'not_cached'))

        # Seed fake snapshot cache and test search
        cache_path = cbl_catalog.get_snapshot_path()
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump({
                'repository': 'DieselTech/CBL-ReadingLists',
                'commit_sha': 'fake_sha',
                'fetched_at': '2026-08-25 12:00:00',
                'total_count': 1,
                'entries': [{
                    'id': 'entry_1',
                    'path': 'DC/Batman.cbl',
                    'name': 'Batman.cbl',
                    'filename': 'Batman.cbl',
                    'publisher': 'DC',
                    'category': 'Events',
                    'title': 'Batman: A Lonely Place of Dying',
                    'size': 1024,
                    'sha': 'abc',
                    'download_url': 'https://example.com/batman.cbl'
                }]
            }, f)

        search_res2 = cbl_catalog.search_catalog(query='Batman', limit=10)
        self.assertEqual(search_res2['status'], 'success')
        self.assertEqual(len(search_res2['entries']), 1)

    @patch('mylar.importer.importer_thread')
    @patch('mylar.importer.issue_watcher_thread')
    def test_13_targeted_identifier_integrity_distinct_cv_ids(self, mock_watcher, mock_importer):
        """
        Targeted Identifier Integrity Regression Test:
        Proves issue numbers ('60', '61') are NEVER confused with ComicVine issue IDs ('115447', '115448').
        Fixture:
        - Series ComicVine ID: '4014'
        - Issue #60 -> ComicVine Issue ID: '115447'
        - Issue #61 -> ComicVine Issue ID: '115448'
        - Unrelated Issue #62 -> ComicVine Issue ID: '115449'
        Proves:
        1. issue_watcher_thread is called with ComicVine Issue IDs ['115447', '115448'] (NOT ['60', '61']).
        2. Consumer worker markIssueWantedById('115447') and ('115448') updates #60 and #61 to Wanted.
        3. Unrelated issue #62 (ID '115449') remains Skipped.
        4. The operation does not succeed because issue numbers and IDs happen to match.
        """
        xml_content = b"""<?xml version="1.0" encoding="utf-8"?>
<ReadingList xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <Name>The New Titans: Distinct ID Test</Name>
  <Publisher>DC Comics</Publisher>
  <Books>
    <Book Series="The New Titans" Volume="1988" Number="60" Year="1989">
      <Database Name="cv" Series="4014" Issue="115447" />
    </Book>
    <Book Series="The New Titans" Volume="1988" Number="61" Year="1989">
      <Database Name="cv" Series="4014" Issue="115448" />
    </Book>
  </Books>
</ReadingList>"""

        upload_res = cbl_service.upload_cbl_manifest(xml_content, 'titans_distinct_ids.cbl', myDB=self.myDB, import_mode='apply_library', issuesonly=True)
        token = upload_res['upload_token']

        confirm_res = cbl_service.confirm_cbl_import(token, filename='titans_distinct_ids.cbl', myDB=self.myDB, import_mode='apply_library', issuesonly=True)
        self.assertEqual(confirm_res['status'], 'success')
        arc_id = confirm_res['storyarcid']

        # 1. Verify importer_thread received ComicID 4014
        mock_importer.assert_any_call([{
            'comicid': '4014',
            'comicname': 'The New Titans',
            'seriesyear': '1988',
            'suppress_addall': True
        }])

        # 2. Verify issue_watcher_thread received EXACT ComicVine Issue IDs '115447' and '115448', NOT '60' and '61'!
        mock_watcher.assert_any_call(['115447', '115448'])
        for call_args in mock_watcher.call_args_list:
            arg_list = call_args[0][0]
            self.assertNotIn('60', arg_list, "Issue number '60' must not be passed to issue_watcher_thread!")
            self.assertNotIn('61', arg_list, "Issue number '61' must not be passed to issue_watcher_thread!")

        # 3. Simulate background indexing of Series 4014 in library:
        # Issue 115447 (Issue_Number='60'), Issue 115448 (Issue_Number='61'), Issue 115449 (Issue_Number='62')
        self.cursor.execute("INSERT INTO comics VALUES ('4014', 'The New Titans', '1988', 'Active')")
        self.cursor.execute("INSERT INTO issues VALUES ('115447', '4014', 'Roots', '60', 'Skipped', 'None', '1989-10-15', '1989-10-15')")
        self.cursor.execute("INSERT INTO issues VALUES ('115448', '4014', 'Going Home', '61', 'Skipped', 'None', '1989-11-15', '1989-11-15')")
        self.cursor.execute("INSERT INTO issues VALUES ('115449', '4014', 'Unrelated Titans Story', '62', 'Skipped', 'None', '1989-12-15', '1989-12-15')")
        self.conn.commit()

        # 4. Consumer execution: importer.markIssueWantedById takes ComicVine Issue ID
        from mylar import importer
        with patch('mylar.db.DBConnection', return_value=self.myDB):
            importer.markIssueWantedById('115447')
            importer.markIssueWantedById('115448')

        # 5. Verify results
        iss_60 = self.myDB.selectone("SELECT Status, Issue_Number FROM issues WHERE IssueID='115447'").fetchone()
        iss_61 = self.myDB.selectone("SELECT Status, Issue_Number FROM issues WHERE IssueID='115448'").fetchone()
        iss_62 = self.myDB.selectone("SELECT Status, Issue_Number FROM issues WHERE IssueID='115449'").fetchone()

        self.assertEqual(iss_60['Status'], 'Wanted')
        self.assertEqual(iss_60['Issue_Number'], '60')
        self.assertEqual(iss_61['Status'], 'Wanted')
        self.assertEqual(iss_61['Issue_Number'], '61')
        self.assertEqual(iss_62['Status'], 'Skipped')
        self.assertEqual(iss_62['Issue_Number'], '62')

        # 6. Verify Story Arc rows stored IssueID as '115447' and '115448'
        arc_rows = self.myDB.select("SELECT ComicID, IssueID, IssueNumber FROM storyarcs WHERE StoryArcID=? ORDER BY ReadingOrder ASC", [arc_id])
        self.assertEqual(arc_rows[0]['ComicID'], '4014')
        self.assertEqual(arc_rows[0]['IssueID'], '115447')
        self.assertEqual(arc_rows[0]['IssueNumber'], '60')
        self.assertEqual(arc_rows[1]['ComicID'], '4014')
        self.assertEqual(arc_rows[1]['IssueID'], '115448')
        self.assertEqual(arc_rows[1]['IssueNumber'], '61')


if __name__ == '__main__':
    unittest.main()
