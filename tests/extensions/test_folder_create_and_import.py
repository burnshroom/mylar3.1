"""
Comprehensive regression test suite for Series Import Folder-Creation and Stale Placeholder Recovery.
"""

import os
import sys
import tempfile
import sqlite3
import unittest
import queue
import pathlib
from unittest.mock import patch

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import mylar
from mylar import helpers, filers, importer, db
from mylar.extensions.storyarcs import cbl_service, service


class DummyConfig:
    DESTINATION_DIR = "/comics"
    FOLDER_FORMAT = "$Series ($Year)"
    FORMAT_BOOKTYPE = False
    SETDEFAULTVOLUME = False
    REPLACE_SPACES = False
    REPLACE_CHAR = "_"
    CREATE_FOLDERS = False
    AUTOWANT_ALL = True


class TestFolderCreateAndImport(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        mylar.DATA_DIR = self.test_dir
        mylar.CONFIG = DummyConfig()
        mylar.CONFIG.DESTINATION_DIR = os.path.join(self.test_dir, 'Comics').replace('\\', '/')
        os.makedirs(mylar.CONFIG.DESTINATION_DIR, exist_ok=True)
        mylar.OS_DETECT = 'Linux'
        mylar.GLOBAL_MESSAGES = []
        mylar.ADD_LIST = queue.Queue()
        mylar.ISSUE_WATCH_LIST = queue.Queue()
        mylar.REFRESH_QUEUE = queue.Queue()

        self.db_path = os.path.join(self.test_dir, 'mylar.db')
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute('''CREATE TABLE IF NOT EXISTS comics (
            ComicID TEXT PRIMARY KEY, ComicName TEXT, ComicSortName TEXT, ComicName_Filesafe TEXT,
            DynamicComicName TEXT, ComicYear TEXT, ComicImage TEXT, FirstImageSize INTEGER,
            ComicImageURL TEXT, ComicImageALTURL TEXT, Total TEXT, ComicVersion TEXT,
            ComicLocation TEXT, ComicPublisher TEXT, Description TEXT, DescriptionEdit TEXT,
            PublisherImprint TEXT, DetailURL TEXT, AlternateSearch TEXT, ComicPublished TEXT,
            Type TEXT, Corrected_Type TEXT, Collects TEXT, DateAdded TEXT, Status TEXT,
            cv_removed INTEGER DEFAULT 0, LatestIssue TEXT, LatestDate TEXT, LatestIssueID TEXT,
            LastUpdated TEXT, ForceContinuing INTEGER DEFAULT 0, NewPublish INTEGER DEFAULT 0,
            Corrected_SeriesYear TEXT
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS issues (
            IssueID TEXT PRIMARY KEY, ComicID TEXT, ComicName TEXT, IssueName TEXT,
            Issue_Number TEXT, Int_IssueNumber INTEGER, Status TEXT, Location TEXT,
            IssueDate TEXT, ReleaseDate TEXT
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS annuals (
            IssueID TEXT PRIMARY KEY, ComicID TEXT, ComicName TEXT, ReleaseComicName TEXT,
            IssueName TEXT, Issue_Number TEXT, Int_IssueNumber INTEGER, Status TEXT,
            Location TEXT, IssueDate TEXT, ReleaseDate TEXT, Deleted INTEGER DEFAULT 0
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS storyarcs (
            IssueArcID TEXT PRIMARY KEY, StoryArcID TEXT, StoryArc TEXT, ReadingOrder INTEGER, ComicID TEXT,
            IssueID TEXT, ComicName TEXT, Issue_Number TEXT, IssueNumber TEXT,
            IssueName TEXT, SeriesYear TEXT, IssueYEAR TEXT, Status TEXT, Manual TEXT
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS storyarc_manifests (
            StoryArcID TEXT PRIMARY KEY, StoryArcName TEXT, SourceType TEXT,
            SourceName TEXT, RepoURL TEXT, RepoPath TEXT, RepoCommit TEXT,
            SHA256 TEXT UNIQUE, ImportTime TEXT, TotalIssues INTEGER, RawXMLPath TEXT
        )''')
        conn.commit()
        conn.close()

    def tearDown(self):
        import shutil
        if hasattr(self, 'test_dir') and os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def _norm(self, path_str):
        return pathlib.Path(path_str).as_posix()

    # 1. filesafe tests
    def test_01_helpers_filesafe_returns_string(self):
        """Verify helpers.filesafe returns sanitized non-None string across edge cases."""
        self.assertEqual(helpers.filesafe('Batman'), 'Batman')
        self.assertEqual(helpers.filesafe('The New Titans'), 'The New Titans')
        self.assertEqual(helpers.filesafe('Batman: Year One'), 'Batman Year One')
        self.assertEqual(helpers.filesafe('Spider-Man/Deadpool*'), 'Spider-Man-Deadpool-')
        self.assertEqual(helpers.filesafe('Crisis on Infinite Earths \u2014 Deluxe'), 'Crisis on Infinite Earths  -  Deluxe')
        self.assertEqual(helpers.filesafe(None), '')
        self.assertEqual(helpers.filesafe(''), '')
        self.assertEqual(helpers.filesafe(b'Batman'), 'Batman')

    # 2. folder_create tests
    def test_02_folder_create_batman_comicvine_796(self):
        """Verify deterministic folder name for Batman (1940) [ID 796]."""
        comic_values = {
            'ComicName': 'Batman',
            'ComicPublisher': 'DC Comics',
            'PublisherImprint': None,
            'ComicYear': '1940',
            'ComicVersion': None,
            'Type': 'Print',
            'Corrected_Type': None
        }
        handler = filers.FileHandlers(comic=comic_values)
        res = handler.folder_create()
        self.assertIsNotNone(res)
        expected = f"{mylar.CONFIG.DESTINATION_DIR}/Batman (1940)"
        self.assertEqual(self._norm(res['comlocation']), self._norm(expected))

    def test_03_folder_create_the_new_titans_comicvine_4014(self):
        """Verify deterministic folder name for The New Titans (1988) [ID 4014]."""
        comic_values = {
            'ComicName': 'The New Titans',
            'ComicPublisher': 'DC Comics',
            'PublisherImprint': None,
            'ComicYear': '1988',
            'ComicVersion': None,
            'Type': 'Print',
            'Corrected_Type': None
        }
        handler = filers.FileHandlers(comic=comic_values)
        res = handler.folder_create()
        self.assertIsNotNone(res)
        expected = f"{mylar.CONFIG.DESTINATION_DIR}/The New Titans (1988)"
        self.assertEqual(self._norm(res['comlocation']), self._norm(expected))

    def test_04_folder_create_comicversion_none_and_string_none(self):
        """Verify ComicVersion None, 'None', and '' safely omit $VolumeN token."""
        mylar.CONFIG.FOLDER_FORMAT = '$Series ($Year) $VolumeN'
        for vol_val in [None, 'None', '', 'none', 'Null']:
            comic_values = {
                'ComicName': 'X-Men',
                'ComicPublisher': 'Marvel',
                'PublisherImprint': None,
                'ComicYear': '1991',
                'ComicVersion': vol_val,
                'Type': None,
                'Corrected_Type': None
            }
            handler = filers.FileHandlers(comic=comic_values)
            res = handler.folder_create()
            self.assertIsNotNone(res)
            expected = f"{mylar.CONFIG.DESTINATION_DIR}/X-Men (1991)"
            self.assertEqual(self._norm(res['comlocation']), self._norm(expected))

    def test_05_folder_create_valid_version_formatting(self):
        """Verify valid ComicVersion populates $VolumeN correctly."""
        mylar.CONFIG.FOLDER_FORMAT = '$Series $VolumeN ($Year)'
        comic_values = {
            'ComicName': 'Batman',
            'ComicPublisher': 'DC Comics',
            'PublisherImprint': None,
            'ComicYear': '2016',
            'ComicVersion': 'v3',
            'Type': None,
            'Corrected_Type': None
        }
        handler = filers.FileHandlers(comic=comic_values)
        res = handler.folder_create()
        self.assertIsNotNone(res)
        expected = f"{mylar.CONFIG.DESTINATION_DIR}/Batman V3 (2016)"
        self.assertEqual(self._norm(res['comlocation']), self._norm(expected))

    def test_06_folder_create_trailing_period_sanitization(self):
        """Verify series names with trailing periods are safely trimmed without dereferencing None."""
        for title, sanitized in [('S.H.I.E.L.D.', 'S.H.I.E.L.D'), ('Batman...', 'Batman'), ('Flash.', 'Flash')]:
            comic_values = {
                'ComicName': title,
                'ComicPublisher': 'DC Comics',
                'PublisherImprint': None,
                'ComicYear': '2020',
                'ComicVersion': None,
                'Type': None,
                'Corrected_Type': None
            }
            handler = filers.FileHandlers(comic=comic_values)
            res = handler.folder_create()
            self.assertIsNotNone(res)
            expected = f"{mylar.CONFIG.DESTINATION_DIR}/{sanitized} (2020)"
            self.assertEqual(self._norm(res['comlocation']), self._norm(expected))

    def test_07_folder_create_empty_or_invalid_comicname_fails_gracefully(self):
        """Verify genuinely empty or invalid ComicName fails gracefully with None instead of uncaught exception."""
        for invalid_name in [None, '', '   ', '...']:
            comic_values = {
                'ComicName': invalid_name,
                'ComicPublisher': 'DC Comics',
                'PublisherImprint': None,
                'ComicYear': '2020',
                'ComicVersion': None,
                'Type': None,
                'Corrected_Type': None
            }
            handler = filers.FileHandlers(comic=comic_values)
            res = handler.folder_create()
            self.assertIsNone(res)

    def test_08_folder_create_publisher_none_and_custom_format(self):
        """Verify folder_create handles Publisher=None and hierarchical folder formats."""
        mylar.CONFIG.FOLDER_FORMAT = '$Publisher/$Series ($Year)'
        comic_values = {
            'ComicName': 'Spawn',
            'ComicPublisher': 'Image!',  # Boom! style exclamation
            'PublisherImprint': None,
            'ComicYear': '1992',
            'ComicVersion': None,
            'Type': None,
            'Corrected_Type': None
        }
        handler = filers.FileHandlers(comic=comic_values)
        res = handler.folder_create()
        self.assertIsNotNone(res)
        expected = f"{mylar.CONFIG.DESTINATION_DIR}/Image/Spawn (1992)"
        self.assertEqual(self._norm(res['comlocation']), self._norm(expected))

    # 3. Import Worker & Exception Resilience tests
    def test_09_addvialist_survives_worker_exception(self):
        """Verify mass-add worker loop catches exceptions and continues processing subsequent queue items."""
        processed = []
        def mock_add_comic(cid, **kwargs):
            if cid == '999_fail':
                raise ValueError("Simulated provider failure")
            processed.append(cid)

        test_q = queue.Queue()
        test_q.put({'comicid': '999_fail', 'comicname': 'Broken', 'seriesyear': '2020'})
        test_q.put({'comicid': '4014', 'comicname': 'The New Titans', 'seriesyear': '1988'})
        test_q.put('exit')

        with patch('time.sleep', return_value=None):
            with patch('mylar.importer.addComictoDB', side_effect=mock_add_comic):
                importer.addvialist(test_q, queue.Queue())

        self.assertIn('4014', processed)

    # 4. Stale Placeholder Recovery tests
    def test_10_stale_placeholder_detection_and_recovery(self):
        """Verify execute_entry_action distinguishes active vs stale placeholder and queues recovery."""
        my_db = db.DBConnection()
        # Seed an abandoned Loading placeholder
        my_db.action(
            "INSERT INTO comics (ComicID, ComicName, Status, ComicLocation) VALUES ('4014', 'Comic ID: 4014', 'Loading', NULL)"
        )
        my_db.action(
            "INSERT INTO storyarcs (IssueArcID, StoryArcID, StoryArc, ReadingOrder, ComicID, IssueID, IssueNumber, Status, Manual) VALUES ('arc_titans_1', 'arc_titans', 'Judas Contract', 1, '4014', '60', '60', 'Unmonitored', NULL)"
        )

        with patch('mylar.importer.importer_thread') as mock_importer_thread:
            res = cbl_service.execute_entry_action('arc_titans', 'arc_titans_1', 'add_series', myDB=my_db)
            self.assertEqual(res['status'], 'success')
            self.assertIn('Recovered stale placeholder', res['message'])
            mock_importer_thread.assert_called()

    def test_11_completed_record_not_overwritten_by_add_series(self):
        """Verify completed comic record is reconciled and not re-queued as a new import."""
        my_db = db.DBConnection()
        # Seed a completed comic record
        com_loc = f"{mylar.CONFIG.DESTINATION_DIR}/The New Titans (1988)"
        my_db.action(
            "INSERT INTO comics (ComicID, ComicName, Status, ComicLocation, Total) VALUES ('4014', 'The New Titans', 'Active', ?, '82')",
            [com_loc]
        )
        my_db.action(
            "INSERT INTO issues (IssueID, ComicID, ComicName, Issue_Number, Int_IssueNumber, Status) VALUES ('60', '4014', 'The New Titans', '60', 60, 'Skipped')"
        )
        my_db.action(
            "INSERT INTO storyarcs (IssueArcID, StoryArcID, StoryArc, ReadingOrder, ComicID, IssueID, IssueNumber, Status, Manual) VALUES ('arc_titans_1', 'arc_titans', 'Judas Contract', 1, '4014', '60', '60', 'Unmonitored', NULL)"
        )

        with patch('mylar.importer.importer_thread') as mock_importer_thread:
            res = cbl_service.execute_entry_action('arc_titans', 'arc_titans_1', 'add_series', myDB=my_db)
            self.assertEqual(res['status'], 'success')
            self.assertIn('monitored', res['message'])
            mock_importer_thread.assert_not_called()

        # Verify arc status updated
        arc_row = my_db.selectone("SELECT Status FROM storyarcs WHERE StoryArcID='arc_titans'").fetchone()
        self.assertEqual(arc_row['Status'], 'Skipped')

    def test_12_storyarc_detail_reflects_reconciled_and_retry_states(self):
        """Verify get_storyarc_detail marks stale placeholders as unmonitored (can add) and completed as monitored."""
        my_db = db.DBConnection()
        # 1. Stale placeholder in comics
        my_db.action("INSERT INTO comics (ComicID, ComicName, Status, ComicLocation) VALUES ('796', 'Comic ID: 796', 'Loading', NULL)")
        my_db.action("INSERT INTO storyarcs (IssueArcID, StoryArcID, StoryArc, ReadingOrder, ComicID, IssueNumber, Status, Manual) VALUES ('arc_bat_1', 'arc_bat', 'Batman Arc', 1, '796', '1', 'Unmonitored', NULL)")

        detail = service.get_storyarc_detail('arc_bat', myDB=my_db)
        self.assertEqual(len(detail['readlist']), 1)
        entry = detail['readlist'][0]
        # Should recognize placeholder as not monitored so user can click Add Series / Retry
        self.assertTrue(entry['CanAddSeries'])
        self.assertFalse(entry['IsMonitored'])

        # 2. Complete the record
        my_db.action("UPDATE comics SET ComicName='Batman', Status='Active', ComicLocation='/comics/Batman (1940)' WHERE ComicID='796'")
        detail2 = service.get_storyarc_detail('arc_bat', myDB=my_db)
        entry2 = detail2['readlist'][0]
        self.assertFalse(entry2['CanAddSeries'])
        self.assertTrue(entry2['IsMonitored'])
        self.assertEqual(entry2['ResolutionState'], 'Series Monitored (Issue Pending)')


if __name__ == '__main__':
    unittest.main()
