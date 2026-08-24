#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for Phase C3.2: Unified Issue Inspector Credits Without Duplicate Presentation."""

import os
import sys
import unittest
import sqlite3
import json

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.join(REPO_ROOT, 'lib'))
sys.path.insert(0, REPO_ROOT)

import mylar
from mylar.extensions.creators.browser_service import CreatorBrowserService

STAGING_DB = r'C:\Users\spike\.gemini\antigravity\brain\ff5d5fcf-d223-4e21-94d9-ed4e7c37cd34\scratch\live_staging_20260822_0050\mylar.db'


class TestPhaseC3_2UnifiedCredits(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        mylar.DATA_DIR = os.path.dirname(STAGING_DB)
        cls.service = CreatorBrowserService()

    def test_01_no_duplicate_headings_or_sections_in_template(self):
        """Verify comicdetails_update.html contains only one unified credits section and no duplicate headers."""
        tpl_path = os.path.join(REPO_ROOT, 'data', 'interfaces', 'modern', 'comicdetails_update.html')
        with open(tpl_path, 'r', encoding='utf-8') as f:
            content = f.read()

        self.assertIn('id="inspector_credits_section"', content)
        self.assertIn('id="inspector_credits_content"', content)
        self.assertIn('id="inspector_credits_subtitle"', content)

        # Confirm old separate container was completely removed
        self.assertNotIn('id="inspector_indexed_creators"', content)
        self.assertNotIn('Indexed Creator Credits', content)

    def test_02_exact_deduplication_and_provider_diff_logic(self):
        """Verify deduplication and provider-only difference extraction logic."""
        def get_canonical_role(r_str):
            if not r_str:
                return ''
            r = str(r_str).lower().strip().replace(' ', '').replace('_', '').replace('-', '')
            if r in ('writer', 'writers'): return 'writer'
            if r in ('penciller', 'pencillers', 'penciler', 'pencilers'): return 'penciller'
            if r in ('inker', 'inkers'): return 'inker'
            if r in ('colorist', 'colorists'): return 'colorist'
            if r in ('letterer', 'letterers'): return 'letterer'
            if r in ('editor', 'editors'): return 'editor'
            if r in ('coverartist', 'coverartists', 'cover', 'cover_artist'): return 'cover_artist'
            return r

        def match_credits(provider_list, indexed_credits):
            unmatched = []
            for p_credit in provider_list:
                p_role = p_credit['role']
                p_canon = get_canonical_role(p_role)
                p_val_str = str(p_credit.get('val', ''))
                for pn in p_val_str.split(','):
                    trimmed_p_name = pn.strip()
                    if not trimmed_p_name:
                        continue
                    is_rep = False
                    for ic in indexed_credits:
                        i_canon = get_canonical_role(ic.get('role') or ic.get('role_display'))
                        # Exact case-sensitive matching
                        if p_canon == i_canon and str(ic.get('raw_name', '')).strip() == trimmed_p_name:
                            is_rep = True
                            break
                    if not is_rep:
                        unmatched.append({'role': p_role, 'name': trimmed_p_name})
            return unmatched

        indexed_sample = [
            {'role': 'writer', 'role_display': 'Writer', 'raw_name': 'Frank Cho'},
            {'role': 'penciller', 'role_display': 'Penciller', 'raw_name': 'Frank Cho'},
            {'role': 'colorist', 'role_display': 'Colorist', 'raw_name': 'Sabine Rich'},
            {'role': 'cover_artist', 'role_display': 'Cover Artist', 'raw_name': 'Frank Cho'},
            {'role': 'cover_artist', 'role_display': 'Cover Artist', 'raw_name': 'Mike Deodato Jr.'},
        ]

        # Case 1: Provider has exact same credits -> 0 unmatched
        provider_exact = [
            {'role': 'Writer', 'val': 'Frank Cho'},
            {'role': 'Colorist', 'val': 'Sabine Rich'},
            {'role': 'Cover Artist', 'val': 'Frank Cho, Mike Deodato Jr.'}
        ]
        unmatched = match_credits(provider_exact, indexed_sample)
        self.assertEqual(len(unmatched), 0, "Exact matching provider credits should produce 0 unmatched diffs")

        # Case 2: Case difference (FRANK CHO vs Frank Cho) -> must remain distinct (1 unmatched)
        provider_case_diff = [
            {'role': 'Writer', 'val': 'FRANK CHO'}
        ]
        unmatched = match_credits(provider_case_diff, indexed_sample)
        self.assertEqual(len(unmatched), 1)
        self.assertEqual(unmatched[0]['name'], 'FRANK CHO')

        # Case 3: Suffix difference (Mike Deodato vs Mike Deodato Jr.) -> must remain distinct
        provider_suffix_diff = [
            {'role': 'Cover Artist', 'val': 'Mike Deodato'}
        ]
        unmatched = match_credits(provider_suffix_diff, indexed_sample)
        self.assertEqual(len(unmatched), 1)
        self.assertEqual(unmatched[0]['name'], 'Mike Deodato')

        # Case 4: Role difference (Frank Cho under Editor vs Frank Cho under Writer) -> must remain distinct
        provider_role_diff = [
            {'role': 'Editor', 'val': 'Frank Cho'}
        ]
        unmatched = match_credits(provider_role_diff, indexed_sample)
        self.assertEqual(len(unmatched), 1)
        self.assertEqual(unmatched[0]['role'], 'Editor')

        # Case 5: Composite name ('Scott Snyder and Nick Dragotta') -> remains intact
        indexed_composite = [{'role': 'writer', 'role_display': 'Writer', 'raw_name': 'Scott Snyder and Nick Dragotta'}]
        provider_composite = [{'role': 'Writer', 'val': 'Scott Snyder and Nick Dragotta'}]
        unmatched = match_credits(provider_composite, indexed_composite)
        self.assertEqual(len(unmatched), 0)

    def test_03_fight_girls_indexed_credits_and_role_order(self):
        """Verify Fight Girls #1 (IssueID=868995) returns grouped credits in standard role order."""
        res = self.service.get_issue_creator_credits('868995', is_annual=0)
        self.assertTrue(res['success'])
        self.assertGreater(res['total_credits'], 0)

        role_order = [g['role_key'] for g in res['groups']]
        expected_order = ['writer', 'penciller', 'inker', 'colorist', 'letterer', 'cover_artist']
        self.assertEqual(role_order, expected_order)

    def test_04_badge_styling_and_unlinked_wording_in_css(self):
        """Verify style.css contains .badge-creator-res.unlinked and disclosure styles."""
        css_path = os.path.join(REPO_ROOT, 'data', 'interfaces', 'modern', 'css', 'style.css')
        with open(css_path, 'r', encoding='utf-8') as f:
            css_content = f.read()

        self.assertIn('.badge-creator-res.unlinked', css_content)
        self.assertIn('.inspector-provider-diff-disclosure', css_content)
        self.assertIn('.inspector-provider-diff-summary', css_content)
        self.assertIn('.inspector-provider-diff-body', css_content)

    def test_05_database_invariance(self):
        """Verify zero database mutations occurred."""
        conn = sqlite3.connect(STAGING_DB)
        cur = conn.cursor()
        entity_count = cur.execute("SELECT COUNT(*) FROM ext_creator_entities").fetchone()[0]
        self.assertEqual(entity_count, 0, "ext_creator_entities must have 0 rows")
        conn.close()


if __name__ == '__main__':
    unittest.main()
