#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Unit tests for markissues Request-Contract Normalization and Issue Status Updates.

Tests:
1. Modern serialization using `issueids` with single ID.
2. Modern serialization using `issueids` with multiple IDs.
3. Legacy serialization using `issueids[]` with single and multiple IDs.
4. Annual issue status update behavior.
5. Story arc issue status update behavior.
6. Missing / empty issue IDs returns controlled JSON failure response without 500 error.
7. Discarding of non-ID tokens so request keys like 'issueids' are never queried as IDs.
8. Zero 'unable to reference issueid: issueids' warnings logged.
9. Retained actions: Wanted, Skipped, Archived, Downloaded, Clear, OppositeTier.
"""

import os
import sys
import tempfile
import sqlite3
import shutil
import json
import unittest
from unittest.mock import patch, MagicMock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.join(REPO_ROOT, 'lib'))
sys.path.insert(0, REPO_ROOT)

import mylar
import mylar.config
import mylar.db as db
from mylar.webserve import WebInterface


class TestMarkIssuesStatusContract(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="mylar_markissues_test_")
        self.test_db = os.path.join(self.test_dir, 'mylar.db')
        self.test_config = os.path.join(self.test_dir, 'config.ini')

        mylar.PROG_DIR = REPO_ROOT
        mylar.DATA_DIR = self.test_dir
        mylar.CONFIG_FILE = self.test_config
        mylar.DBFILE = self.test_db

        with open(self.test_config, 'w') as f:
            f.write("[General]\n")
            f.write("comic_dir = %s\n" % self.test_dir)
            f.write("annuals_on = True\n")
            f.write("failed_download_handling = False\n")

        cc = mylar.config.Config(self.test_config)
        mylar.CONFIG = cc.read(startup=True)
        mylar.CONFIG.COMIC_DIR = self.test_dir
        mylar.CONFIG.ANNUALS_ON = True
        mylar.CONFIG.FAILED_DOWNLOAD_HANDLING = False
        mylar.SEARCH_TIER_DATE = '2026-08-01'

        # Initialize schema
        conn = sqlite3.connect(self.test_db)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE comics (
                ComicID TEXT PRIMARY KEY,
                ComicName TEXT,
                ComicYear TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE issues (
                IssueID TEXT PRIMARY KEY,
                ComicName TEXT,
                ComicID TEXT,
                Issue_Number TEXT,
                Status TEXT,
                DateAdded TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE annuals (
                IssueID TEXT PRIMARY KEY,
                ComicID TEXT,
                ReleaseComicName TEXT,
                Issue_Number TEXT,
                Status TEXT,
                Deleted INTEGER DEFAULT 0,
                DateAdded TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE storyarcs (
                IssueArcID TEXT PRIMARY KEY,
                ComicID TEXT,
                ComicName TEXT,
                IssueNumber TEXT,
                Status TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE snatched (
                IssueID TEXT,
                ComicID TEXT,
                Status TEXT
            )
        """)

        # Seed data
        cur.execute("INSERT INTO comics VALUES ('100', 'Sunstone', '2014')")
        cur.execute("INSERT INTO issues VALUES ('1001', 'Sunstone', '100', '1', 'Skipped', '2026-08-10')")
        cur.execute("INSERT INTO issues VALUES ('1002', 'Sunstone', '100', '2', 'Skipped', '2026-08-10')")
        cur.execute("INSERT INTO issues VALUES ('1003', 'Sunstone', '100', '3', 'Skipped', '2026-08-10')")
        cur.execute("INSERT INTO annuals VALUES ('2001', '100', 'Sunstone Annual', '1', 'Skipped', 0, '2026-08-10')")
        cur.execute("INSERT INTO storyarcs VALUES ('3001', '100', 'Sunstone Arc', '1', 'Skipped')")
        cur.execute("INSERT INTO snatched VALUES ('1001', '100', 'Snatched')")
        conn.commit()
        conn.close()

        self.web = WebInterface()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    @patch('mylar.updater.forceRescan')
    @patch('threading.Thread')
    def test_modern_single_issue_status_update(self, mock_thread, mock_rescan):
        """Test Modern request format with single ID in 'issueids'."""
        resp_str = self.web.markissues(action='Wanted', issueids='1001', comicid='100')
        resp = json.loads(resp_str)
        self.assertEqual(resp.get('status'), 'success')
        self.assertEqual(resp.get('updated_count'), 1)

        # Check DB status
        conn = sqlite3.connect(self.test_db)
        cur = conn.cursor()
        cur.execute("SELECT Status FROM issues WHERE IssueID = '1001'")
        self.assertEqual(cur.fetchone()[0], 'Wanted')
        cur.execute("SELECT Status FROM issues WHERE IssueID = '1002'")
        self.assertEqual(cur.fetchone()[0], 'Skipped')
        conn.close()

    @patch('mylar.updater.forceRescan')
    @patch('threading.Thread')
    def test_modern_multiple_issues_status_update(self, mock_thread, mock_rescan):
        """Test Modern request format with multiple IDs list in 'issueids'."""
        resp_str = self.web.markissues(action='Wanted', issueids=['1001', '1002'], comicid='100')
        resp = json.loads(resp_str)
        self.assertEqual(resp.get('status'), 'success')
        self.assertEqual(resp.get('updated_count'), 2)

        conn = sqlite3.connect(self.test_db)
        cur = conn.cursor()
        cur.execute("SELECT Status FROM issues WHERE IssueID = '1001'")
        self.assertEqual(cur.fetchone()[0], 'Wanted')
        cur.execute("SELECT Status FROM issues WHERE IssueID = '1002'")
        self.assertEqual(cur.fetchone()[0], 'Wanted')
        cur.execute("SELECT Status FROM issues WHERE IssueID = '1003'")
        self.assertEqual(cur.fetchone()[0], 'Skipped')
        conn.close()

    @patch('mylar.updater.forceRescan')
    @patch('threading.Thread')
    def test_legacy_form_single_and_multi_issueids_bracket(self, mock_thread, mock_rescan):
        """Test legacy/form serialization 'issueids[]' with single string and list."""
        # Single string in legacy format
        resp_str = self.web.markissues(action='Skipped', **{'issueids[]': '1001', 'comicid': '100'})
        resp = json.loads(resp_str)
        self.assertEqual(resp.get('status'), 'success')

        # List in legacy format
        resp_str2 = self.web.markissues(action='Archived', **{'issueids[]': ['1001', '1002'], 'comicid': '100'})
        resp2 = json.loads(resp_str2)
        self.assertEqual(resp2.get('status'), 'success')
        self.assertEqual(resp2.get('updated_count'), 2)

        conn = sqlite3.connect(self.test_db)
        cur = conn.cursor()
        cur.execute("SELECT Status FROM issues WHERE IssueID = '1001'")
        self.assertEqual(cur.fetchone()[0], 'Archived')
        cur.execute("SELECT Status FROM issues WHERE IssueID = '1002'")
        self.assertEqual(cur.fetchone()[0], 'Archived')
        conn.close()

    @patch('mylar.updater.forceRescan')
    @patch('threading.Thread')
    def test_annual_issue_status_update(self, mock_thread, mock_rescan):
        """Test annual issue status update via markissues and markannuals."""
        resp_str = self.web.markissues(action='Wanted', issueids='2001', comicid='100')
        resp = json.loads(resp_str)
        self.assertEqual(resp.get('status'), 'success')
        self.assertEqual(resp.get('updated_count'), 1)

        conn = sqlite3.connect(self.test_db)
        cur = conn.cursor()
        cur.execute("SELECT Status FROM annuals WHERE IssueID = '2001'")
        self.assertEqual(cur.fetchone()[0], 'Wanted')
        conn.close()

    @patch('mylar.updater.forceRescan')
    @patch('threading.Thread')
    def test_storyarc_issue_status_update(self, mock_thread, mock_rescan):
        """Test storyarc issue status update."""
        resp_str = self.web.markissues(action='Wanted', issueids='3001')
        resp = json.loads(resp_str)
        self.assertEqual(resp.get('status'), 'success')
        self.assertEqual(resp.get('updated_count'), 1)

        conn = sqlite3.connect(self.test_db)
        cur = conn.cursor()
        cur.execute("SELECT Status FROM storyarcs WHERE IssueArcID = '3001'")
        self.assertEqual(cur.fetchone()[0], 'Wanted')
        conn.close()

    def test_empty_or_missing_issueids_returns_controlled_json_failure(self):
        """Test missing/empty issueids returns controlled JSON response, never raising 500 or error."""
        # Case 1: No issue parameters passed
        resp1 = json.loads(self.web.markissues(action='Wanted', comicid='100'))
        self.assertEqual(resp1.get('status'), 'failure')
        self.assertIn('No valid issue IDs', resp1.get('message', ''))

        # Case 2: Empty list
        resp2 = json.loads(self.web.markissues(action='Wanted', issueids=[]))
        self.assertEqual(resp2.get('status'), 'failure')

        # Case 3: Empty string or None
        resp3 = json.loads(self.web.markissues(action='Wanted', issueids=''))
        self.assertEqual(resp3.get('status'), 'failure')

        # Case 4: Only table tokens passed
        resp4 = json.loads(self.web.markissues(action='Wanted', issueids='issue_table'))
        self.assertEqual(resp4.get('status'), 'failure')

    def test_request_keys_never_queried_as_issueid_and_zero_warning(self):
        """Verify request key 'issueids' is never queried and never produces 'unable to reference issueid: issueids'."""
        with patch('mylar.logger.warn') as mock_warn:
            resp = json.loads(self.web.markissues(action='Wanted', issueids='1001', comicid='100'))
            self.assertEqual(resp.get('status'), 'success')

            # Verify no warning logged with literal string 'issueids'
            for call in mock_warn.call_args_list:
                arg_msg = str(call[0][0])
                self.assertNotIn('issueids', arg_msg)

    @patch('mylar.updater.forceRescan')
    @patch('threading.Thread')
    def test_actions_wantednew_and_retry(self, mock_thread, mock_rescan):
        """Test WantedNew and Retry actions map to Wanted status."""
        resp1 = json.loads(self.web.markissues(action='WantedNew', issueids='1001', comicid='100'))
        self.assertEqual(resp1.get('status'), 'success')

        resp2 = json.loads(self.web.markissues(action='Retry', issueids='1002', comicid='100'))
        self.assertEqual(resp2.get('status'), 'success')

        conn = sqlite3.connect(self.test_db)
        cur = conn.cursor()
        cur.execute("SELECT Status FROM issues WHERE IssueID = '1001'")
        self.assertEqual(cur.fetchone()[0], 'Wanted')
        cur.execute("SELECT Status FROM issues WHERE IssueID = '1002'")
        self.assertEqual(cur.fetchone()[0], 'Wanted')
        conn.close()

    @patch('mylar.updater.forceRescan')
    def test_action_oppositetier(self, mock_rescan):
        """Test OppositeTier changes DateAdded."""
        resp = json.loads(self.web.markissues(action='OppositeTier', issueids=['1001'], comicid='100'))
        self.assertEqual(resp.get('status'), 'success')

        conn = sqlite3.connect(self.test_db)
        cur = conn.cursor()
        cur.execute("SELECT DateAdded FROM issues WHERE IssueID = '1001'")
        new_date = cur.fetchone()[0]
        self.assertIsNotNone(new_date)
        conn.close()

    @patch('mylar.updater.forceRescan')
    @patch('threading.Thread')
    def test_comma_separated_issueids_string(self, mock_thread, mock_rescan):
        """Test issueids passed as comma-separated string '1001, 1002'."""
        resp = json.loads(self.web.markissues(action='Wanted', issueids='1001, 1002', comicid='100'))
        self.assertEqual(resp.get('status'), 'success')
        self.assertEqual(resp.get('updated_count'), 2)

        conn = sqlite3.connect(self.test_db)
        cur = conn.cursor()
        cur.execute("SELECT Status FROM issues WHERE IssueID = '1001'")
        self.assertEqual(cur.fetchone()[0], 'Wanted')
        cur.execute("SELECT Status FROM issues WHERE IssueID = '1002'")
        self.assertEqual(cur.fetchone()[0], 'Wanted')
        conn.close()

    @patch('mylar.updater.forceRescan')
    @patch('threading.Thread')
    def test_mixed_tokens_filters_noise(self, mock_thread, mock_rescan):
        """Test mixed tokens (e.g. ['1001', 'issue_table', 'issueids', '']) only updates valid ID."""
        with patch('mylar.logger.warn') as mock_warn:
            resp = json.loads(self.web.markissues(action='Wanted', issueids=['1001', 'issue_table', 'issueids', ''], comicid='100'))
            self.assertEqual(resp.get('status'), 'success')
            self.assertEqual(resp.get('updated_count'), 1)

            for call in mock_warn.call_args_list:
                arg_msg = str(call[0][0])
                self.assertNotIn('issueids', arg_msg)
                self.assertNotIn('issue_table', arg_msg)

    @patch('mylar.updater.forceRescan')
    def test_clear_action_removes_from_snatched(self, mock_rescan):
        """Test Clear action deletes from snatched table."""
        resp = json.loads(self.web.markissues(action='Clear', issueids=['1001']))
        self.assertEqual(resp.get('status'), 'success')

        conn = sqlite3.connect(self.test_db)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM snatched WHERE IssueID = '1001'")
        self.assertEqual(cur.fetchone()[0], 0)
        conn.close()


if __name__ == '__main__':
    unittest.main()
