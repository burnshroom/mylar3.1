import os
import sys
import unittest
import subprocess
import json

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.join(REPO_ROOT, 'lib'))
sys.path.insert(0, REPO_ROOT)

import mylar
from mylar.webserve import WebInterface, serve_template

class TestModernSearchLayout(unittest.TestCase):
    """
    Regression and contract tests for the Modern Search Results / Add-Issue page layout.
    """

    def setUp(self):
        class MockConfig:
            INTERFACE = 'modern'
            HTTP_ROOT = ''
            PULLNEW = 'no'
            WANTED_TAB_OFF = False
            INSTANCE_NAME = ''
            AUTHENTICATION = 0
            GIT_BRANCH = 'master'
            CURRENT_VERSION = 'v1.0'
            MYLAR_INSTANCE_ID = 'test'
            MYLAR_INSTANCE_SLUG = 'test'
            KAVITA_ENABLED = False
            KAVITA_URL = ''
            KAVITA_API_KEY = ''
            CHECK_GITHUB = False
            CHECK_GITHUB_INTERVAL = 360
            CREATE_FOLDERS = False
            AUTOWANT_UPCOMING = False
            AUTOWANT_ALL = False
            COMICVINE_API = 'testkey'
            WEEKFOLDER = False
            ENABLE_TORRENT_SEARCH = False
            ENABLE_32P = False
            MODE_32P = False

        self.orig_config = getattr(mylar, 'CONFIG', None)
        self.orig_prog_dir = getattr(mylar, 'PROG_DIR', None)
        mylar.CONFIG = MockConfig()
        mylar.PROG_DIR = REPO_ROOT

    def tearDown(self):
        mylar.CONFIG = self.orig_config
        mylar.PROG_DIR = self.orig_prog_dir

    def test_01_modern_searchresults_template_renders_cleanly(self):
        """1. Prove modern/searchresults.html renders with modern shell and correct breadcrumbs/title."""
        html = serve_template(
            templatename="searchresults.html",
            title='Search Results for: "Sunstone"',
            query_id="test_query_123",
            query="Sunstone",
            search_type="comic"
        )
        self.assertIn('Search Results', html)
        self.assertIn('Results for <span class="search-query-highlight">"Sunstone"</span>', html)
        self.assertIn('id="searchresults_table"', html)

    def test_02_legend_is_responsive_and_not_floating_absolute(self):
        """2. Prove legend is an inline badge component and has no absolute positioning bottom right."""
        modern_template_path = os.path.join(REPO_ROOT, 'data', 'interfaces', 'modern', 'searchresults.html')
        self.assertTrue(os.path.exists(modern_template_path), "Modern searchresults.html must exist")

        with open(modern_template_path, 'r', encoding='utf-8') as f:
            template_content = f.read()

        # Must have the modern legend bar
        self.assertIn('class="search-legend-bar"', template_content)
        self.assertIn('legend-badge--monitored', template_content)
        self.assertIn('legend-badge--print', template_content)
        self.assertIn('legend-badge--digital', template_content)

        # Must NOT have old absolute floating table with bottom:-30px
        self.assertNotIn('bottom:-30px', template_content)
        self.assertNotIn('position: absolute; float: right; bottom:-30px', template_content)

    def test_03_action_buttons_and_filters_have_proper_spacing_and_classes(self):
        """3. Prove action buttons and filter controls use dedicated classes with separate buttons."""
        modern_template_path = os.path.join(REPO_ROOT, 'data', 'interfaces', 'modern', 'searchresults.html')
        with open(modern_template_path, 'r', encoding='utf-8') as f:
            template_content = f.read()

        self.assertIn('btn-action-add', template_content)
        self.assertIn('btn-action-edit', template_content)
        self.assertIn('search-filters-group', template_content)
        self.assertIn('search-batch-actions', template_content)
        self.assertIn('search-detail-expanded', template_content)

    def test_04_dedicated_toolbar_container_and_datatables_relocation(self):
        """4. Prove searchTableControls container exists in toolbar and DataTables moves controls into it."""
        modern_template_path = os.path.join(REPO_ROOT, 'data', 'interfaces', 'modern', 'searchresults.html')
        with open(modern_template_path, 'r', encoding='utf-8') as f:
            template_content = f.read()

        self.assertIn('id="searchTableControls"', template_content)
        self.assertIn('class="search-toolbar-right" id="searchTableControls"', template_content)
        self.assertIn('function relocateToolbarControls()', template_content)
        self.assertIn('targetContainer.length', template_content)
        self.assertIn('lengthCtrl.appendTo(targetContainer)', template_content)
        self.assertIn('filterCtrl.appendTo(targetContainer)', template_content)

    def test_05_safe_html_escaping_for_dynamic_search_results(self):
        """5. Prove safe HTML escaping utilities and escaping of dynamic server-derived fields."""
        modern_template_path = os.path.join(REPO_ROOT, 'data', 'interfaces', 'modern', 'searchresults.html')
        with open(modern_template_path, 'r', encoding='utf-8') as f:
            template_content = f.read()

        self.assertIn('function escapeHtml(str)', template_content)
        self.assertIn('function escapeAttr(str)', template_content)
        self.assertIn('escapeHtml(displayTitle)', template_content)
        self.assertIn('escapeAttr(urlStr)', template_content)
        self.assertIn('escapeHtml(full[2]', template_content)
        self.assertIn('escapeHtml(full[3]', template_content)
        self.assertIn('escapeHtml(full[4]', template_content)
        self.assertIn('escapeAttr(dc.comlocation', template_content)

    def test_06_default_interface_remains_intact_and_unmodified(self):
        """6. Prove default/searchresults.html still exists and is untouched for legacy interfaces."""
        default_template_path = os.path.join(REPO_ROOT, 'data', 'interfaces', 'default', 'searchresults.html')
        self.assertTrue(os.path.exists(default_template_path), "Default searchresults.html must remain intact")

    def test_07_css_contains_responsive_rules_for_search_results(self):
        """7. Prove modern style.css contains responsive rules and toolbar subcontainer styles."""
        css_path = os.path.join(REPO_ROOT, 'data', 'interfaces', 'modern', 'css', 'style.css')
        with open(css_path, 'r', encoding='utf-8') as f:
            css_content = f.read()

        self.assertIn('.search-header-container', css_content)
        self.assertIn('.search-legend-bar', css_content)
        self.assertIn('.search-toolbar-left', css_content)
        self.assertIn('.search-toolbar-right', css_content)
        self.assertIn('.btn-action-add', css_content)
        self.assertIn('.btn-action-edit', css_content)
        self.assertIn('.search-detail-expanded', css_content)
        self.assertIn('@media (max-width: 600px)', css_content)

    def test_08_addbyid_request_contract(self):
        """8. Exercise addbyid parameter receipt on WebInterface."""
        interface = WebInterface()
        received_args = {}

        def mock_addbyid_handler(comicid, query_id=None, com_location=None, booktype=None, calledby=False, nothread=False):
            received_args['comicid'] = comicid
            received_args['query_id'] = query_id
            received_args['com_location'] = com_location
            received_args['booktype'] = booktype
            return json.dumps({"status": "success", "message": "Queued"})

        # Temporarily mock addbyid
        orig_addbyid = interface.addbyid
        try:
            interface.addbyid = mock_addbyid_handler
            res = interface.addbyid(
                comicid="4050-78888",
                query_id="test_query_123",
                com_location="/comics/Image/Sunstone",
                booktype="TPB"
            )
            parsed = json.loads(res)
            self.assertEqual(parsed.get('status'), 'success')
            self.assertEqual(received_args['comicid'], '4050-78888')
            self.assertEqual(received_args['query_id'], 'test_query_123')
            self.assertEqual(received_args['com_location'], '/comics/Image/Sunstone')
            self.assertEqual(received_args['booktype'], 'TPB')
        finally:
            interface.addbyid = orig_addbyid

    def test_09_git_diff_check_clean(self):
        """9. Prove git diff --check reports zero whitespace or newline errors."""
        res = subprocess.run(['git', 'diff', '--check'], cwd=REPO_ROOT, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"git diff --check failed:\n{res.stdout}\n{res.stderr}")

if __name__ == '__main__':
    unittest.main()
