# -*- coding: utf-8 -*-
"""
Tests for Runtime Corrective (Story Arc Rendering, Build Identity, and Delete Modal).
Proves:
  1. Story Arcs main page and detail page render successfully without 'mylar' injected into template context.
  2. No Modern template contains 'mylar.extensions' or direct service/controller invocations.
  3. Automatic build identity renders 'v3.1.0 · Modern <short_sha>' when metadata is provided,
     and 'Modern Build · development' when metadata is absent.
  4. Delete modal overlay markup, CSRF tokens, and safety disclaimers are properly structured.
  5. Dockerfile and GitHub Action workflow metadata contracts are satisfied.
"""

import os
import sys
import unittest
from unittest.mock import patch

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, 'lib') not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, 'lib'))

import mylar
from mylar import webserve
from mylar.versioncheck import get_build_identity


class DummyConfig:
    AUTHENTICATION = 0
    HTTP_ROOT = '/'
    INTERFACE = 'modern'
    GIT_BRANCH = 'master'
    CBL_IMPORT_ISSUESONLY = True
    CBL_IMPORT_IGNOREARCHIVED = False
    TAB_MANAGE = 0
    TAB_LOG = 0
    TAB_HISTORY = 0
    TAB_UPCOMING = 0
    TAB_WANTED = 0
    TAB_STORYARCS = 0
    TAB_CREATORS = 0
    TAB_READINGLISTS = 0
    GIT_COMMIT = None
    GIT_USER = None
    GIT_TOKEN = None
    AUTO_UPDATE = False
    CHECK_GITHUB_ON_STARTUP = False
    GIT_PATH = None

    def __getattr__(self, name):
        return 0


class TestRuntimeCorrective(unittest.TestCase):

    def setUp(self):
        import tempfile
        import sqlite3
        self.test_dir = tempfile.mkdtemp()
        mylar.DATA_DIR = self.test_dir
        self.db_path = os.path.join(self.test_dir, 'mylar.db')
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute('''CREATE TABLE IF NOT EXISTS storyarcs (
            StoryArcID TEXT, StoryArc TEXT, ReadingOrder INTEGER, ComicID TEXT,
            IssueID TEXT, ComicName TEXT, Issue_Number TEXT, Status TEXT
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS storyarc_manifests (
            StoryArcID TEXT PRIMARY KEY, StoryArcName TEXT, SourceType TEXT,
            SourceName TEXT, RepoURL TEXT, RepoPath TEXT, RepoCommit TEXT,
            FileHash TEXT, TotalEntries INTEGER, ImportTime TEXT, RawManifest BLOB
        )''')
        conn.commit()
        conn.close()

        mylar.CONFIG = DummyConfig()
        mylar.GLOBAL_MESSAGES = []
        mylar.SSE_KEY = 'test_sse_key'
        mylar.UPDATE_VALUE = None
        mylar.CURRENT_VERSION = None
        mylar.CURRENT_VERSION_NAME = "v3.1.0"
        mylar.PROG_DIR = REPO_ROOT

    def tearDown(self):
        import shutil
        if hasattr(self, 'test_dir') and os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)


    def test_01_no_modern_templates_contain_mylar_extensions(self):
        """Verify no template in data/interfaces/modern/ references mylar.extensions directly."""
        modern_dir = os.path.join(mylar.PROG_DIR, 'data', 'interfaces', 'modern')
        self.assertTrue(os.path.isdir(modern_dir), f"Directory not found: {modern_dir}")

        offending_files = []
        for root, _, files in os.walk(modern_dir):
            for file in files:
                if file.endswith('.html'):
                    filepath = os.path.join(root, file)
                    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                        if 'mylar.extensions' in content:
                            offending_files.append(filepath)

        self.assertEqual(offending_files, [], f"Found mylar.extensions in modern templates: {offending_files}")

    def test_02_storyarc_main_renders_without_mylar_in_context(self):
        """Verify storyarc.html renders without AttributeError and contains CSRF token."""
        # Call serve_template directly without passing mylar=mylar
        html = webserve.serve_template(
            templatename="storyarc.html",
            title="Story Arcs",
            arclist=[],
            delete_type=0
        )
        if isinstance(html, bytes):
            html = html.decode('utf-8')
        self.assertNotIn("500 Internal Server Error", html)
        self.assertNotIn("AttributeError", html)
        self.assertNotIn("Undefined", html)
        self.assertIn('id="page_name" value="storyarc"', html)
        self.assertIn('id="cbl_csrf_token"', html)
        self.assertIn('Import CBL Reading List', html)
        self.assertIn('id="cblImportModal"', html)
        self.assertIn('id="cblIssuesOnly"', html)
        self.assertIn('id="cblIgnoreArchived"', html)

    def test_03_storyarc_detail_renders_without_mylar_in_context(self):
        """Verify storyarc_detail.html renders cleanly without AttributeError and contains delete modal."""
        html = webserve.serve_template(
            templatename="storyarc_detail.html",
            title="Story Arc - Test Arc",
            readlist=[{
                'StoryArcID': 'arc123',
                'StoryArc': 'Test Arc',
                'ReadingOrder': 1,
                'ComicID': '4014',
                'IssueID': '115447',
                'ComicName': 'Batman',
                'Issue_Number': '60',
                'Status': 'Downloaded',
                'LocalStatus': 'Downloaded',
                'ActionableState': 'DOWNLOADED',
                'SeriesMonitored': True,
                'VolumePresent': True,
                'IssueMonitored': True,
                'IsActiveSeries': True
            }],
            storyarcname="Test Arc",
            storyarcid="arc123",
            cvarcid="999",
            sdir="/comics/Batman",
            arcdetail={'publisher': 'DC Comics', 'totalissues': 1},
            storyarcbanner=None,
            bannerheight='280',
            bannerwidth='960',
            manifest={'SourceType': 'upload', 'TotalEntries': 1},
            have_count=1,
            total_count=1,
            percent=100,
            spanyears='2020',
            publisher='DC Comics'
        )
        if isinstance(html, bytes):
            html = html.decode('utf-8')
        self.assertNotIn("500 Internal Server Error", html)
        self.assertNotIn("AttributeError", html)
        self.assertNotIn("Undefined", html)
        self.assertIn('id="page_name" value="storyarc_detail"', html)
        self.assertIn('id="cbl_csrf_token"', html)
        self.assertIn('id="deleteArcModal"', html)
        self.assertIn('id="deleteModalTitle"', html)
        self.assertIn('id="confirmDeleteArcBtn"', html)
        self.assertIn('id="deleteModalAlert"', html)
        self.assertIn('No comic books or series in your library will be deleted', html)

    def test_04_build_identity_supplied_sha(self):
        """Verify supplied MYLAR_BUILD_SHA formats as 'v3.1.0 · Modern <short_sha>'."""
        with patch.dict(os.environ, {'MYLAR_BUILD_SHA': '09a135eb97f5205fa50e20bbf0cefa33397d6816'}):
            identity = get_build_identity()
            self.assertEqual(identity, "v3.1.0 · Modern 09a135eb")
            self.assertEqual(len(identity.split(' ')[-1]), 8)

    def test_05_build_identity_shortened_sha_only(self):
        """Verify build identity only exposes shortened 8-character SHA."""
        with patch.dict(os.environ, {'MYLAR_BUILD_SHA': 'abcdef1234567890'}):
            identity = get_build_identity()
            self.assertEqual(identity, "v3.1.0 · Modern abcdef12")

    def test_06_build_identity_missing_metadata(self):
        """Verify absent or unknown build metadata displays 'Modern Build · development'."""
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(mylar, 'CURRENT_VERSION', None):
                identity = get_build_identity()
                self.assertEqual(identity, "Modern Build · development")

        with patch.dict(os.environ, {'MYLAR_BUILD_SHA': 'unknown'}):
            identity = get_build_identity()
            self.assertEqual(identity, "Modern Build · development")

    def test_07_sidebar_displays_build_identity(self):
        """Verify base.html sidebar renders the computed build identity."""
        with patch.dict(os.environ, {'MYLAR_BUILD_SHA': '09a135eb97f5205fa50e20bbf0cefa33397d6816'}):
            html = webserve.serve_template(
                templatename="storyarc.html",
                title="Story Arcs",
                arclist=[],
                delete_type=0
            )
            if isinstance(html, bytes):
                html = html.decode('utf-8')
            self.assertIn('class="sidebar-version"', html)
            self.assertIn('v3.1.0 · Modern 09a135eb', html)

        with patch.dict(os.environ, {'MYLAR_BUILD_SHA': 'unknown'}):
            with patch.object(mylar, 'CURRENT_VERSION', None):
                html = webserve.serve_template(
                    templatename="storyarc.html",
                    title="Story Arcs",
                    arclist=[],
                    delete_type=0
                )
                if isinstance(html, bytes):
                    html = html.decode('utf-8')
                self.assertIn('class="sidebar-version"', html)
                self.assertIn('Modern Build · development', html)

    def test_08_delete_modal_structure_and_safety_notice(self):
        """Verify delete modal structure contains overlay, dialog, close controls, and safety notice."""
        detail_path = os.path.join(mylar.PROG_DIR, 'data', 'interfaces', 'modern', 'storyarc_detail.html')
        with open(detail_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # Check modal overlay container and role
        self.assertIn('<div id="deleteArcModal" class="modal-overlay" style="display:none;" role="dialog" aria-modal="true"', content)
        # Check modal card container
        self.assertIn('<div class="modal-card"', content)
        # Check close button
        self.assertIn('onclick="closeDeleteArcModal()"', content)
        # Check cancel and delete action buttons
        self.assertIn('id="confirmDeleteArcBtn"', content)
        # Check error container
        self.assertIn('id="deleteModalAlert"', content)
        # Check safety disclaimer
        self.assertIn('No comic books or series in your library will be deleted or modified', content)

    def test_09_dockerfile_contains_revision_label_and_build_sha_env(self):
        """Verify Dockerfile contains ARG VCS_REF, ENV MYLAR_BUILD_SHA, and revision label."""
        dockerfile_path = os.path.join(mylar.PROG_DIR, 'Dockerfile')
        self.assertTrue(os.path.isfile(dockerfile_path))

        with open(dockerfile_path, 'r', encoding='utf-8') as f:
            df = f.read()

        self.assertIn('ARG VCS_REF', df)
        self.assertIn('ENV PUID=1000', df)
        self.assertIn('MYLAR_BUILD_SHA="${VCS_REF}"', df)
        self.assertIn('org.opencontainers.image.revision="${VCS_REF}"', df)

    def test_10_github_workflow_passes_commit_sha_to_vcs_ref(self):
        """Verify GitHub Actions workflow passes github.sha into VCS_REF build-arg."""
        wf_path = os.path.join(mylar.PROG_DIR, '.github', 'workflows', 'build_feature_container.yml')
        self.assertTrue(os.path.isfile(wf_path))

        with open(wf_path, 'r', encoding='utf-8') as f:
            wf = f.read()

        self.assertIn('vcs_ref=${{ github.sha }}', wf)
        self.assertIn('VCS_REF=${{ steps.vars.outputs.vcs_ref }}', wf)

    def test_11_audit_exact_webserve_controller_route_path(self):
        """
        Audit exact real execution paths:
          webserve.storyarc_main -> handle_storyarc_main -> serve_template -> storyarc.html
          webserve.detailStoryArc -> handle_detail_storyarc -> serve_template -> storyarc_detail.html
        Proves no NameError: name 'mylar' is not defined occurs when calling webserve routes directly.
        """
        from mylar.webserve import WebInterface
        interface = WebInterface()

        # 1. Exercise webserve.storyarc_main
        main_html = interface.storyarc_main()
        if isinstance(main_html, bytes):
            main_html = main_html.decode('utf-8')
        self.assertNotIn("500 Internal Server Error", main_html)
        self.assertNotIn("NameError", main_html)
        self.assertNotIn("AttributeError", main_html)
        self.assertIn('id="page_name" value="storyarc"', main_html)
        self.assertIn('id="cbl_csrf_token"', main_html)

        # 2. Exercise webserve.detailStoryArc with mock storyarc data
        with patch('mylar.extensions.storyarcs.service.get_storyarc_detail') as mock_get_detail:
            mock_get_detail.return_value = {
                'template': 'storyarc_detail.html',
                'storyarcname': 'Test Story Arc',
                'storyarcid': 'arc_audit_101',
                'cvarcid': '5555',
                'sdir': '/comics/Test',
                'arcdetail': {'publisher': 'Marvel', 'totalissues': 2},
                'storyarcbanner': None,
                'bannerheight': '280',
                'bannerwidth': '960',
                'manifest': {'SourceType': 'upload', 'SourceName': 'test.cbl'},
                'have_count': 1,
                'total_count': 2,
                'percent': 50,
                'spanyears': '2021',
                'publisher': 'Marvel',
                'readlist': [{
                    'StoryArcID': 'arc_audit_101',
                    'StoryArc': 'Test Story Arc',
                    'ReadingOrder': 1,
                    'ComicID': '101',
                    'IssueID': '201',
                    'ComicName': 'X-Men',
                    'SeriesYear': '2021',
                    'IssueNumber': '1',
                    'IssueName': 'Part 1',
                    'StoreDate': '2021-01-01',
                    'Status': 'Downloaded',
                    'ResolutionState': 'Downloaded',
                    'IsMonitored': True
                }],
                'found': True
            }
            detail_html = interface.detailStoryArc(StoryArcID='arc_audit_101')
            if isinstance(detail_html, bytes):
                detail_html = detail_html.decode('utf-8')
            self.assertNotIn("500 Internal Server Error", detail_html)
            self.assertNotIn("NameError", detail_html)
            self.assertNotIn("AttributeError", detail_html)
            self.assertIn('id="page_name" value="storyarc_detail"', detail_html)
            self.assertIn('id="deleteArcModal"', detail_html)


if __name__ == '__main__':
    unittest.main()
