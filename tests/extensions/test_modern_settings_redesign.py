# -*- coding: utf-8 -*-
"""Unit and regression tests for Modern Settings interface redesign."""

import os
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))

class TestModernSettingsRedesign(unittest.TestCase):
    """Test suite verifying the Modern Settings shell, tabs, field parity, and sidebar structure."""

    def setUp(self):
        self.modern_base_path = os.path.join(REPO_ROOT, 'data', 'interfaces', 'modern', 'base.html')
        self.modern_config_path = os.path.join(REPO_ROOT, 'data', 'interfaces', 'modern', 'config.html')
        self.default_config_path = os.path.join(REPO_ROOT, 'data', 'interfaces', 'default', 'config.html')
        self.creators_path = os.path.join(REPO_ROOT, 'data', 'interfaces', 'modern', 'creators.html')
        self.creator_reg_path = os.path.join(REPO_ROOT, 'data', 'interfaces', 'modern', 'creator_registry.html')
        self.creator_detail_path = os.path.join(REPO_ROOT, 'data', 'interfaces', 'modern', 'creator_detail.html')
        self.kavita_diag_path = os.path.join(REPO_ROOT, 'data', 'interfaces', 'modern', 'kavita_diagnostics.html')

    def test_sidebar_single_primary_settings_item(self):
        """1. Verify that modern/base.html has exactly one primary Settings item and no subitems in sidebar."""
        with open(self.modern_base_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # Check single primary nav item for config
        self.assertIn('data-nav="config"', content)
        self.assertIn('<span class="nav-label">Settings</span>', content)

        # Confirm that sidebar no longer has subgroup items for creators or kavita in the sidebar
        self.assertNotIn('data-nav="creators"', content)
        self.assertNotIn('data-nav="kavita_diagnostics"', content)
        self.assertNotIn('nav-subgroup', content)

    def test_sidebar_active_route_highlighter(self):
        """2. Verify that modern/base.html active nav script highlights Settings for all settings routes."""
        with open(self.modern_base_path, 'r', encoding='utf-8') as f:
            content = f.read()

        self.assertIn("isSettings", content)
        self.assertIn("path === 'config'", content)
        self.assertIn("path === 'creators'", content)
        self.assertIn("path === 'creator_detail'", content)
        self.assertIn("path === 'creator_registry'", content)
        self.assertIn("path === 'kavita_diagnostics'", content)

    def test_modern_config_template_exists_and_is_isolated(self):
        """3. Verify modern/config.html exists and is distinct from default/config.html."""
        self.assertTrue(os.path.exists(self.modern_config_path))
        self.assertTrue(os.path.exists(self.default_config_path))

        with open(self.modern_config_path, 'r', encoding='utf-8') as f:
            modern_content = f.read()
        with open(self.default_config_path, 'r', encoding='utf-8') as f:
            default_content = f.read()

        # Modern contains modern settings container and tabs
        self.assertIn('settings-page-container', modern_content)
        self.assertIn('settings-nav-tabs', modern_content)

        # Default does not have modern container
        self.assertNotIn('settings-page-container', default_content)

    def test_eight_settings_tabs_presence(self):
        """4. Verify that all 8 Settings tabs are present in modern/config.html."""
        with open(self.modern_config_path, 'r', encoding='utf-8') as f:
            content = f.read()

        expected_tabs = [
            'data-tab="tabs-1"',  # General / Info
            'data-tab="tabs-2"',  # Web Interface
            'data-tab="tabs-3"',  # Download Settings
            'data-tab="tabs-4"',  # Search Providers
            'data-tab="tabs-5"',  # Quality & Post-Processing
            'data-tab="tabs-6"',  # Advanced Settings
            'data-tab="creators"', # Metadata & Identity
            'data-tab="kavita"'    # Integrations
        ]
        for tab in expected_tabs:
            self.assertIn(tab, content, f"Tab {tab} missing from modern config.html")

    def test_creators_and_kavita_have_settings_tabs(self):
        """5. Verify creators, registry, detail, and kavita have settings navigation tabs."""
        for path, active_key in [
            (self.creators_path, 'Metadata &amp; Identity'),
            (self.creator_reg_path, 'Metadata &amp; Identity'),
            (self.creator_detail_path, 'Metadata &amp; Identity'),
            (self.kavita_diag_path, 'Integrations')
        ]:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            self.assertIn('settings-nav-tabs', content, f"settings-nav-tabs missing in {path}")
            self.assertIn('href="config#tabs-1"', content, f"General link missing in {path}")
            self.assertIn('href="config#tabs-2"', content, f"Web Interface link missing in {path}")
            self.assertIn('href="config#tabs-3"', content, f"Download Settings link missing in {path}")
            self.assertIn('href="config#tabs-4"', content, f"Search Providers link missing in {path}")
            self.assertIn('href="config#tabs-5"', content, f"Quality link missing in {path}")
            self.assertIn('href="config#tabs-6"', content, f"Advanced link missing in {path}")
            self.assertIn('href="creators"', content, f"creators link missing in {path}")
            self.assertIn('href="kavita_diagnostics"', content, f"kavita link missing in {path}")

    def test_field_parity_with_default_config(self):
        """6. Verify critical configuration fields exist in modern/config.html."""
        with open(self.modern_config_path, 'r', encoding='utf-8') as f:
            content = f.read()

        critical_fields = [
            'name="http_host"',
            'name="http_port"',
            'name="enable_https"',
            'name="authentication"',
            'name="comicvine_api"',
            'name="api_enabled"',
            'name="metron_enabled"',
            'name="metron_auth_mode"',
            'name="sab_host"',
            'name="nzbget_host"',
            'name="enable_torrents"',
            'name="newznab"',
            'id="usenewznab"',
            'name="destination_dir"',
            'name="folder_format"',
            'name="file_format"',
            'name="enable_meta"',
            'name="enforce_perms"',
            'id="ignore_words"',
            'name="failed_download_handling"',
            'id="enable_failed"',
            'name="annuals_on"'
        ]
        for field in critical_fields:
            self.assertIn(field, content, f"Field {field} missing from modern config.html")

    def test_form_action_and_save_buttons(self):
        """7. Verify form action is configUpdate and save buttons are present."""
        with open(self.modern_config_path, 'r', encoding='utf-8') as f:
            content = f.read()

        self.assertIn('action="configUpdate"', content)
        self.assertIn('id="configUpdate"', content)
        self.assertIn("doAjaxCall('configUpdate'", content)

    def test_kavita_settings_page_has_prominent_save_button(self):
        """8. Verify that Kavita Settings page contains Save Configuration button and uses kavitaConfigUpdate."""
        with open(self.kavita_diag_path, 'r', encoding='utf-8') as f:
            content = f.read()

        self.assertIn('id="save_kavita_config_btn"', content)
        self.assertIn('Save Configuration', content)
        self.assertIn("url: 'kavitaConfigUpdate'", content)
        self.assertIn('id="kavita_action_feedback"', content)

    def test_general_settings_tabs_have_restart_notice(self):
        """9. Verify Web Interface settings tab contains established restart notice and all tabs have save buttons."""
        with open(self.modern_config_path, 'r', encoding='utf-8') as f:
            content = f.read()

        self.assertIn('Web Interface changes require a restart to take effect', content)
        self.assertEqual(content.count('Save Changes'), 6, "Each of the 6 settings tabs must have a Save Changes button")

    def test_kavita_persistence_across_process_restart(self):
        """10. Verify Kavita configuration persists to disk and survives full process restart."""
        import tempfile
        import json
        from unittest.mock import patch
        import mylar
        from mylar.extensions.providers.kavita.runtime_controller import handle_kavita_config_update

        tmpdir = tempfile.mkdtemp()
        ini_path = os.path.join(tmpdir, 'config.ini')
        db_path = os.path.join(tmpdir, 'mylar.db')
        with open(ini_path, 'w', encoding='utf-8') as f:
            f.write('[General]\nconfig_version = 19\ninterface = modern\nhttp_port = 8090\nhttp_host = 127.0.0.1\nlog_dir = ' + tmpdir.replace('\\', '/') + '\n')

        mylar.PROG_DIR = REPO_ROOT
        mylar.DATA_DIR = tmpdir
        mylar.CONFIG_FILE = ini_path
        mylar.DB_FILE = db_path
        mylar.initialize(ini_path)

        class MockRequest:
            method = 'POST'

        class MockResponse:
            headers = {}
            status = 200

        with patch('cherrypy.request', MockRequest()), patch('cherrypy.response', MockResponse()):
            # A. Save configuration with URL, enabled=True, and API key
            res = json.loads(handle_kavita_config_update(
                kavita_enabled='1',
                kavita_url='http://127.0.0.1:5000',
                kavita_api_key='secret-key-123'
            ))
            self.assertTrue(res['success'])
            self.assertTrue(res['kavita_enabled'])
            self.assertTrue(res['has_api_key'])

            # Verify disk config.ini contains Kavita values
            with open(ini_path, 'r', encoding='utf-8') as f:
                ini_text = f.read()
            self.assertIn('kavita_url = http://127.0.0.1:5000/', ini_text)

            # B. Simulate process restart by reloading Mylar
            mylar.initialize(ini_path)
            self.assertTrue(mylar.CONFIG.KAVITA_ENABLED)
            self.assertEqual(mylar.CONFIG.KAVITA_URL, 'http://127.0.0.1:5000/')
            self.assertEqual(mylar.CONFIG.KAVITA_API_KEY, 'secret-key-123')

            # C. Blank secret submission preserves existing key across restart
            res_blank = json.loads(handle_kavita_config_update(
                kavita_enabled='1',
                kavita_url='http://127.0.0.1:5000',
                kavita_api_key=''
            ))
            self.assertTrue(res_blank['success'])
            self.assertTrue(res_blank['has_api_key'])
            mylar.initialize(ini_path)
            self.assertEqual(mylar.CONFIG.KAVITA_API_KEY, 'secret-key-123')

            # D. Explicit clear removes key while preserving enabled state and URL
            res_clear = json.loads(handle_kavita_config_update(
                kavita_enabled='1',
                kavita_url='http://127.0.0.1:5000',
                kavita_api_key='',
                clear_kavita_api_key='1'
            ))
            self.assertTrue(res_clear['success'])
            self.assertFalse(res_clear['has_api_key'])
            mylar.initialize(ini_path)
            self.assertIsNone(mylar.CONFIG.KAVITA_API_KEY)
            self.assertTrue(mylar.CONFIG.KAVITA_ENABLED)
            self.assertEqual(mylar.CONFIG.KAVITA_URL, 'http://127.0.0.1:5000/')

if __name__ == '__main__':
    unittest.main()
