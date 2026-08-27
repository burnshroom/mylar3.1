#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Unit tests for Modern Issue Inspector "Search / Queue" Action and Backend Queue Endpoint.
"""

import os
import sys
import tempfile
import sqlite3
import shutil
import json
import unittest
from unittest.mock import patch, MagicMock
import queue

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.join(REPO_ROOT, 'lib'))
sys.path.insert(0, REPO_ROOT)

import mylar
import mylar.config
import mylar.db as db
from mylar.webserve import WebInterface


class TestModernInspectorQueueAction(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="mylar_inspector_queue_test_")
        self.test_db = os.path.join(self.test_dir, 'mylar.db')
        self.test_config = os.path.join(self.test_dir, 'config.ini')

        mylar.PROG_DIR = REPO_ROOT
        mylar.DATA_DIR = self.test_dir
        mylar.CONFIG_FILE = self.test_config
        mylar.DBFILE = self.test_db
        mylar.SEARCH_QUEUE = queue.Queue()

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

        # Initialize schema
        conn = sqlite3.connect(self.test_db)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE comics (
                ComicID TEXT PRIMARY KEY,
                ComicName TEXT,
                ComicYear TEXT,
                ComicPublisher TEXT,
                ComicName_Filesafe TEXT,
                AlternateSearch TEXT,
                UseFuzzy INTEGER DEFAULT 0,
                AllowPacks INTEGER DEFAULT 0,
                ComicVersion TEXT,
                TorrentID_32P TEXT,
                Type TEXT,
                Corrected_Type TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE issues (
                IssueID TEXT PRIMARY KEY,
                ComicName TEXT,
                ComicID TEXT,
                Issue_Number TEXT,
                Status TEXT,
                IssueDate TEXT,
                ReleaseDate TEXT,
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
                IssueDate TEXT,
                ReleaseDate TEXT,
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
                IssueDate TEXT,
                ReleaseDate TEXT,
                IssuePublisher TEXT,
                SeriesYear TEXT,
                Type TEXT,
                Volume TEXT,
                Status TEXT
            )
        """)
        conn.commit()
        conn.close()

        self.web = WebInterface()

    def tearDown(self):
        try:
            shutil.rmtree(self.test_dir)
        except Exception:
            pass

    def _seed_comic_and_issue(self, comic_id="101", issue_id="1001", issue_status="Skipped"):
        conn = sqlite3.connect(self.test_db)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO comics (ComicID, ComicName, ComicYear, ComicPublisher, Type)
            VALUES (?, ?, ?, ?, ?)
        """, (comic_id, "The Amazing Spider-Man", "2022", "Marvel", "Comic"))
        cur.execute("""
            INSERT INTO issues (IssueID, ComicName, ComicID, Issue_Number, Status, IssueDate, ReleaseDate)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (issue_id, "The Amazing Spider-Man", comic_id, "1", issue_status, "2022-04-01", "2022-04-06"))
        conn.commit()
        conn.close()

    def _seed_annual_issue(self, comic_id="101", issue_id="2001", issue_status="Skipped"):
        conn = sqlite3.connect(self.test_db)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO annuals (IssueID, ComicID, ReleaseComicName, Issue_Number, Status, IssueDate, ReleaseDate, Deleted)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0)
        """, (issue_id, comic_id, "The Amazing Spider-Man Annual", "1", issue_status, "2022-09-01", "2022-09-07"))
        conn.commit()
        conn.close()

    def test_01_normal_issue_manual_search_preserves_status(self):
        self._seed_comic_and_issue(comic_id="101", issue_id="1001", issue_status="Skipped")

        response_str = self.web.queueissue(
            mode="want",
            ComicID="101",
            IssueID="1001",
            ComicIssue="1",
            manualsearch="true"
        )
        res = json.loads(response_str)

        self.assertEqual(res.get("status"), "success")
        self.assertEqual(res.get("status_code"), 200)
        self.assertEqual(res.get("comicid"), "101")
        self.assertEqual(res.get("issueid"), "1001")

        # Manual search must NOT change DB status
        conn = sqlite3.connect(self.test_db)
        cur = conn.cursor()
        cur.execute("SELECT Status FROM issues WHERE IssueID=?", ("1001",))
        row = cur.fetchone()
        conn.close()
        self.assertEqual(row[0], "Skipped")

        self.assertFalse(mylar.SEARCH_QUEUE.empty())
        item = mylar.SEARCH_QUEUE.get_nowait()
        self.assertEqual(item["comicid"], "101")
        self.assertEqual(item["issueid"], "1001")
        self.assertEqual(item["manual"], True)

    def test_02_normal_issue_non_manual_queue_marks_wanted(self):
        self._seed_comic_and_issue(comic_id="101", issue_id="1001", issue_status="Skipped")

        response_str = self.web.queueissue(
            mode="want",
            ComicID="101",
            IssueID="1001",
            ComicIssue="1",
            manualsearch=None
        )
        res = json.loads(response_str)

        self.assertEqual(res.get("status"), "success")
        self.assertEqual(res.get("status_code"), 200)

        # Non-manual queue MUST change DB status to Wanted
        conn = sqlite3.connect(self.test_db)
        cur = conn.cursor()
        cur.execute("SELECT Status FROM issues WHERE IssueID=?", ("1001",))
        row = cur.fetchone()
        conn.close()
        self.assertEqual(row[0], "Wanted")

        self.assertFalse(mylar.SEARCH_QUEUE.empty())
        item = mylar.SEARCH_QUEUE.get_nowait()
        self.assertEqual(item["manual"], False)

    def test_03_annual_issue_manual_search_preserves_status(self):
        self._seed_comic_and_issue(comic_id="101", issue_id="1001", issue_status="Skipped")
        self._seed_annual_issue(comic_id="101", issue_id="2001", issue_status="Skipped")

        response_str = self.web.queueissue(
            mode="want_ann",
            ComicID="101",
            IssueID="2001",
            ComicIssue="1",
            manualsearch="true"
        )
        res = json.loads(response_str)

        self.assertEqual(res.get("status"), "success")
        self.assertEqual(res.get("status_code"), 200)
        self.assertEqual(res.get("comicid"), "101")
        self.assertEqual(res.get("issueid"), "2001")

        # Manual search must NOT change annual DB status
        conn = sqlite3.connect(self.test_db)
        cur = conn.cursor()
        cur.execute("SELECT Status FROM annuals WHERE IssueID=?", ("2001",))
        row = cur.fetchone()
        conn.close()
        self.assertEqual(row[0], "Skipped")

        self.assertFalse(mylar.SEARCH_QUEUE.empty())
        item = mylar.SEARCH_QUEUE.get_nowait()
        self.assertEqual(item["comicid"], "101")
        self.assertEqual(item["issueid"], "2001")
        self.assertEqual(item["manual"], True)

    def test_04_annual_issue_non_manual_queue_marks_wanted(self):
        self._seed_comic_and_issue(comic_id="101", issue_id="1001", issue_status="Skipped")
        self._seed_annual_issue(comic_id="101", issue_id="2001", issue_status="Skipped")

        response_str = self.web.queueissue(
            mode="want_ann",
            ComicID="101",
            IssueID="2001",
            ComicIssue="1",
            manualsearch=None
        )
        res = json.loads(response_str)

        self.assertEqual(res.get("status"), "success")
        self.assertEqual(res.get("status_code"), 200)

        # Non-manual queue MUST change annual DB status to Wanted
        conn = sqlite3.connect(self.test_db)
        cur = conn.cursor()
        cur.execute("SELECT Status FROM annuals WHERE IssueID=?", ("2001",))
        row = cur.fetchone()
        conn.close()
        self.assertEqual(row[0], "Wanted")

        self.assertFalse(mylar.SEARCH_QUEUE.empty())
        item = mylar.SEARCH_QUEUE.get_nowait()
        self.assertEqual(item["manual"], False)

    def test_05_force_mode_rejected_with_json_400(self):
        self._seed_comic_and_issue(comic_id="101", issue_id="1001", issue_status="Skipped")

        response_str = self.web.queueissue(
            mode="force",
            ComicID="101",
            IssueID="1001",
            ComicIssue="1",
            manualsearch="true"
        )
        res = json.loads(response_str)

        self.assertEqual(res.get("status"), "error")
        self.assertEqual(res.get("status_code"), 400)
        self.assertEqual(res.get("error_code"), "invalid_mode")

        conn = sqlite3.connect(self.test_db)
        cur = conn.cursor()
        cur.execute("SELECT Status FROM issues WHERE IssueID=?", ("1001",))
        row = cur.fetchone()
        conn.close()
        self.assertEqual(row[0], "Skipped")

    def test_06_missing_comic_id_returns_json_400(self):
        response_str = self.web.queueissue(
            mode="want",
            ComicID="",
            IssueID="1001"
        )
        res = json.loads(response_str)

        self.assertEqual(res.get("status"), "error")
        self.assertEqual(res.get("status_code"), 400)
        self.assertEqual(res.get("error_code"), "comic_id_missing")

    def test_07_missing_issue_id_returns_json_400(self):
        self._seed_comic_and_issue(comic_id="101", issue_id="1001")
        response_str = self.web.queueissue(
            mode="want",
            ComicID="101",
            IssueID=""
        )
        res = json.loads(response_str)

        self.assertEqual(res.get("status"), "error")
        self.assertEqual(res.get("status_code"), 400)
        self.assertEqual(res.get("error_code"), "issue_id_missing")

    def test_08_nonexistent_comic_id_returns_json_404(self):
        response_str = self.web.queueissue(
            mode="want",
            ComicID="999999",
            IssueID="1001"
        )
        res = json.loads(response_str)

        self.assertEqual(res.get("status"), "error")
        self.assertEqual(res.get("status_code"), 404)
        self.assertEqual(res.get("error_code"), "comic_not_found")

    def test_09_nonexistent_issue_id_returns_json_404(self):
        self._seed_comic_and_issue(comic_id="101", issue_id="1001")
        response_str = self.web.queueissue(
            mode="want",
            ComicID="101",
            IssueID="888888"
        )
        res = json.loads(response_str)

        self.assertEqual(res.get("status"), "error")
        self.assertEqual(res.get("status_code"), 404)
        self.assertEqual(res.get("error_code"), "issue_not_found")

    def test_10_legacy_readlist_and_pullwant_modes(self):
        conn = sqlite3.connect(self.test_db)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO storyarcs (IssueArcID, ComicID, ComicName, IssueNumber, Status, SeriesYear)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("arc-101", "101", "Secret Wars", "1", "Skipped", "2015"))
        conn.commit()
        conn.close()

        res_readlist = self.web.queueissue(
            mode="readlist",
            ComicName="Secret Wars",
            ComicIssue="1",
            IssueArcID="arc-101",
            SARC="Secret Wars"
        )
        conn = sqlite3.connect(self.test_db)
        cur = conn.cursor()
        cur.execute("SELECT Status FROM storyarcs WHERE IssueArcID=?", ("arc-101",))
        row = cur.fetchone()
        conn.close()
        self.assertEqual(row[0], "Wanted")

        res_pullwant = self.web.queueissue(
            mode="pullwant",
            ComicName="Batman",
            ComicIssue="150",
            IssueID="pull-101",
            pullinfo="2026-08-20"
        )
        self.assertIsNotNone(res_pullwant)

    def test_11_unexpected_internal_exception_returns_json_500(self):
        self._seed_comic_and_issue(comic_id="101", issue_id="1001")

        with patch.object(db.DBConnection, 'selectone', side_effect=RuntimeError("Simulated DB connection failure")):
            response_str = self.web.queueissue(
                mode="want",
                ComicID="101",
                IssueID="1001",
                ComicIssue="1"
            )
            res = json.loads(response_str)

            self.assertEqual(res.get("status"), "error")
            self.assertEqual(res.get("status_code"), 500)
            self.assertEqual(res.get("error_code"), "queue_issue_error")
            self.assertNotIn("Simulated DB connection failure", res.get("message", ""))

    def test_12_modern_template_inspector_contract(self):
        template_path = os.path.join(REPO_ROOT, "data", "interfaces", "modern", "comicdetails_update.html")
        with open(template_path, "r", encoding="utf-8") as f:
            content = f.read()

        # 1. Invalid 'force' mode is removed
        self.assertNotIn("add_wanted(iid, cid, issNum, 'force', true)", content)

        # 2. Scope-aware mode resolution
        self.assertIn("var mode = (scope === 'annuals' ? 'want_ann' : 'want');", content)

        # 3. Loading state is set before network call
        self.assertIn("$btn.addClass('pending').prop('disabled', true);", content)
        self.assertIn("Searching…", content)

        # 4. Success state updated to Search Queued without mutating status chip
        self.assertIn("!err && result && result.status === 'success'", content)
        self.assertIn("Search Queued", content)

        # 5. Prior button state restored on failure
        self.assertIn("$btn.prop('disabled', false).html(originalHtml);", content)

        # 6. add_wanted does not contain raw alert()
        add_wanted_idx = content.find("function add_wanted")
        add_wanted_block = content[add_wanted_idx:add_wanted_idx + 3000]
        self.assertNotIn("alert(", add_wanted_block)
        self.assertIn("queue_issue_error", add_wanted_block)


import cherrypy
import socket
import urllib.request
import urllib.parse
import urllib.error


class TestModernInspectorQueueHttpRoute(unittest.TestCase):
    """
    Route-level integration tests asserting real CherryPy HTTP behavior for queueissue.
    Validates HTTP status codes, Content-Type headers, JSON schema, status persistence semantics,
    and absolute absence of raw HTML, tracebacks, raw exception strings, and credentials.
    """

    @classmethod
    def setUpClass(cls):
        cls.test_dir = tempfile.mkdtemp(prefix="mylar_inspector_http_test_")
        cls.test_db = os.path.join(cls.test_dir, 'mylar.db')
        cls.test_config = os.path.join(cls.test_dir, 'config.ini')

        mylar.PROG_DIR = REPO_ROOT
        mylar.DATA_DIR = cls.test_dir
        mylar.CONFIG_FILE = cls.test_config
        mylar.DBFILE = cls.test_db
        mylar.SEARCH_QUEUE = queue.Queue()

        with open(cls.test_config, 'w') as f:
            f.write("[General]\n")
            f.write("comic_dir = %s\n" % cls.test_dir)
            f.write("annuals_on = True\n")
            f.write("failed_download_handling = False\n")

        cc = mylar.config.Config(cls.test_config)
        mylar.CONFIG = cc.read(startup=True)
        mylar.CONFIG.COMIC_DIR = cls.test_dir
        mylar.CONFIG.ANNUALS_ON = True
        mylar.CONFIG.FAILED_DOWNLOAD_HANDLING = False

        # Initialize schema
        conn = sqlite3.connect(cls.test_db)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE comics (
                ComicID TEXT PRIMARY KEY,
                ComicName TEXT,
                ComicYear TEXT,
                ComicPublisher TEXT,
                Type TEXT,
                Corrected_Type TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE issues (
                IssueID TEXT PRIMARY KEY,
                ComicName TEXT,
                ComicID TEXT,
                Issue_Number TEXT,
                Status TEXT,
                IssueDate TEXT,
                ReleaseDate TEXT,
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
                IssueDate TEXT,
                ReleaseDate TEXT,
                Deleted INTEGER DEFAULT 0,
                DateAdded TEXT
            )
        """)
        # Seed test records
        cur.execute("INSERT INTO comics (ComicID, ComicName, ComicYear, ComicPublisher, Type) VALUES ('101', 'The Amazing Spider-Man', '2022', 'Marvel', 'Comic')")
        cur.execute("INSERT INTO issues (IssueID, ComicName, ComicID, Issue_Number, Status, IssueDate, ReleaseDate) VALUES ('1001', 'The Amazing Spider-Man', '101', '1', 'Skipped', '2022-04-01', '2022-04-06')")
        cur.execute("INSERT INTO issues (IssueID, ComicName, ComicID, Issue_Number, Status, IssueDate, ReleaseDate) VALUES ('1002', 'The Amazing Spider-Man', '101', '2', 'Skipped', '2022-05-01', '2022-05-06')")
        cur.execute("INSERT INTO annuals (IssueID, ComicID, ReleaseComicName, Issue_Number, Status, IssueDate, ReleaseDate, Deleted) VALUES ('2001', '101', 'The Amazing Spider-Man Annual', '1', 'Skipped', '2022-09-01', '2022-09-07', 0)")
        cur.execute("INSERT INTO annuals (IssueID, ComicID, ReleaseComicName, Issue_Number, Status, IssueDate, ReleaseDate, Deleted) VALUES ('2002', '101', 'The Amazing Spider-Man Annual', '2', 'Skipped', '2022-10-01', '2022-10-07', 0)")
        conn.commit()
        conn.close()

        # Find free port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('127.0.0.1', 0))
        cls.server_port = sock.getsockname()[1]
        sock.close()

        cherrypy.config.update({
            'server.socket_host': '127.0.0.1',
            'server.socket_port': cls.server_port,
            'log.screen': False,
            'engine.autoreload.on': False,
            'tools.encode.on': True,
            'tools.encode.encoding': 'utf-8',
            'tools.encode.text_only': False,
        })
        cherrypy.tree.mount(WebInterface(), '/', config={'/': {'tools.encode.on': True, 'tools.encode.encoding': 'utf-8'}})
        cherrypy.engine.start()

    @classmethod
    def tearDownClass(cls):
        try:
            cherrypy.engine.stop()
            cherrypy.engine.exit()
        except Exception:
            pass
        try:
            shutil.rmtree(cls.test_dir)
        except Exception:
            pass

    def _http_get(self, params):
        url = "http://127.0.0.1:%d/queueissue?%s" % (self.server_port, urllib.parse.urlencode(params))
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, dict(resp.headers), resp.read().decode('utf-8')
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read().decode('utf-8')

    def _assert_safe_json_response(self, headers, body, expected_status, expected_error_code=None):
        content_type = headers.get('Content-Type', '')
        self.assertIn('application/json', content_type, f"Expected application/json in Content-Type header, got: {content_type}")

        self.assertNotIn('<html', body.lower())
        self.assertNotIn('<!doctype', body.lower())
        self.assertNotIn('<title>500', body.lower())
        self.assertNotIn('<pre id="traceback"', body.lower())
        self.assertNotIn('traceback (most recent call last)', body.lower())
        self.assertNotIn('unhandled exception', body.lower())

        data = json.loads(body)
        self.assertIsInstance(data, dict)
        self.assertEqual(data.get('status_code'), expected_status)

        if expected_error_code:
            self.assertEqual(data.get('status'), 'error')
            self.assertEqual(data.get('error_code'), expected_error_code)
            self.assertTrue(bool(data.get('message')), "Error message must be present and user-safe")
        else:
            self.assertEqual(data.get('status'), 'success')

        return data

    def test_http_01_normal_issue_manual_search_preserves_status(self):
        status, headers, body = self._http_get({
            'mode': 'want',
            'ComicID': '101',
            'IssueID': '1001',
            'ComicIssue': '1',
            'manualsearch': 'true'
        })
        self.assertEqual(status, 200)
        data = self._assert_safe_json_response(headers, body, 200)
        self.assertEqual(data.get('comicid'), '101')
        self.assertEqual(data.get('issueid'), '1001')

        # Status in DB must remain Skipped
        conn = sqlite3.connect(self.test_db)
        cur = conn.cursor()
        cur.execute("SELECT Status FROM issues WHERE IssueID=?", ("1001",))
        row = cur.fetchone()
        conn.close()
        self.assertEqual(row[0], "Skipped")

    def test_http_02_normal_issue_non_manual_queue_marks_wanted(self):
        status, headers, body = self._http_get({
            'mode': 'want',
            'ComicID': '101',
            'IssueID': '1002',
            'ComicIssue': '2'
        })
        self.assertEqual(status, 200)
        data = self._assert_safe_json_response(headers, body, 200)
        self.assertEqual(data.get('comicid'), '101')
        self.assertEqual(data.get('issueid'), '1002')

        # Status in DB must become Wanted
        conn = sqlite3.connect(self.test_db)
        cur = conn.cursor()
        cur.execute("SELECT Status FROM issues WHERE IssueID=?", ("1002",))
        row = cur.fetchone()
        conn.close()
        self.assertEqual(row[0], "Wanted")

    def test_http_03_annual_issue_manual_search_preserves_status(self):
        status, headers, body = self._http_get({
            'mode': 'want_ann',
            'ComicID': '101',
            'IssueID': '2001',
            'ComicIssue': '1',
            'manualsearch': 'true'
        })
        self.assertEqual(status, 200)
        data = self._assert_safe_json_response(headers, body, 200)
        self.assertEqual(data.get('comicid'), '101')
        self.assertEqual(data.get('issueid'), '2001')

        # Status in DB must remain Skipped
        conn = sqlite3.connect(self.test_db)
        cur = conn.cursor()
        cur.execute("SELECT Status FROM annuals WHERE IssueID=?", ("2001",))
        row = cur.fetchone()
        conn.close()
        self.assertEqual(row[0], "Skipped")

    def test_http_04_annual_issue_non_manual_queue_marks_wanted(self):
        status, headers, body = self._http_get({
            'mode': 'want_ann',
            'ComicID': '101',
            'IssueID': '2002',
            'ComicIssue': '2'
        })
        self.assertEqual(status, 200)
        data = self._assert_safe_json_response(headers, body, 200)
        self.assertEqual(data.get('comicid'), '101')
        self.assertEqual(data.get('issueid'), '2002')

        # Status in DB must become Wanted
        conn = sqlite3.connect(self.test_db)
        cur = conn.cursor()
        cur.execute("SELECT Status FROM annuals WHERE IssueID=?", ("2002",))
        row = cur.fetchone()
        conn.close()
        self.assertEqual(row[0], "Wanted")

    def test_http_05_force_mode_rejected_with_400_json(self):
        status, headers, body = self._http_get({
            'mode': 'force',
            'ComicID': '101',
            'IssueID': '1001'
        })
        self.assertEqual(status, 400)
        self._assert_safe_json_response(headers, body, 400, 'invalid_mode')

    def test_http_06_missing_comic_id_returns_400_json(self):
        status, headers, body = self._http_get({
            'mode': 'want',
            'ComicID': '',
            'IssueID': '1001'
        })
        self.assertEqual(status, 400)
        self._assert_safe_json_response(headers, body, 400, 'comic_id_missing')

    def test_http_07_missing_issue_id_returns_400_json(self):
        status, headers, body = self._http_get({
            'mode': 'want',
            'ComicID': '101',
            'IssueID': ''
        })
        self.assertEqual(status, 400)
        self._assert_safe_json_response(headers, body, 400, 'issue_id_missing')

    def test_http_08_nonexistent_comic_returns_404_json(self):
        status, headers, body = self._http_get({
            'mode': 'want',
            'ComicID': '999999',
            'IssueID': '1001'
        })
        self.assertEqual(status, 404)
        self._assert_safe_json_response(headers, body, 404, 'comic_not_found')

    def test_http_09_nonexistent_issue_returns_404_json(self):
        status, headers, body = self._http_get({
            'mode': 'want',
            'ComicID': '101',
            'IssueID': '999999'
        })
        self.assertEqual(status, 404)
        self._assert_safe_json_response(headers, body, 404, 'issue_not_found')

    def test_http_10_injected_internal_failure_returns_500_json(self):
        with patch.object(db.DBConnection, 'selectone', side_effect=RuntimeError("Simulated secret DB failure: key=SECRET_123")):
            status, headers, body = self._http_get({
                'mode': 'want',
                'ComicID': '101',
                'IssueID': '1001'
            })
            self.assertEqual(status, 500)
            self.assertNotIn("SECRET_123", body)
            self.assertNotIn("Simulated secret DB failure", body)
            self._assert_safe_json_response(headers, body, 500, 'queue_issue_error')


if __name__ == '__main__':
    unittest.main()
