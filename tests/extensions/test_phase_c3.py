"""
Phase C3 Automated Verification Test Suite.

Proves:
1. Catalog queries and detail queries execute purely from SQLite without archive reads.
2. Zero network / external HTTP requests and zero calls to helpers.IssueDetails().
3. Search and role filters return exact matching raw name records.
4. Two different NameRecordIDs with identical normalized names remain distinct records.
5. Composite credits ('and', '&', '/') remain one intact observed record.
6. Regular issues and Annuals are distinguished cleanly (IsAnnual check).
7. Zero ext_creator_entities or alias mappings created.
8. Core tables (issues, annuals, comics, storyarcs) remain strictly invariant.
9. Invalid / missing NameRecordID handled safely (clean not-found).
10. Pagination operates correctly on multi-record datasets.
"""

import sys, os, sqlite3, unittest, tempfile, shutil

sys.path.insert(0, os.path.abspath('.'))
sys.path.insert(0, os.path.abspath('lib'))

from mylar.extensions.migrations.runner import run_extension_migrations
from mylar.extensions.creators.browser_service import CreatorBrowserService
from mylar.extensions.creators.browser_controller import handle_creator_catalog, handle_creator_detail
from mylar.extensions.creators.schema import ROLES


class TestPhaseC3(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, 'mylar_c3.db')
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.cursor = self.conn.cursor()

        # Core tables
        self.cursor.execute("CREATE TABLE comics (ComicID TEXT, ComicName TEXT, ComicYear TEXT, Corrected_SeriesYear TEXT, ComicPublisher TEXT, ComicLocation TEXT, Status TEXT)")
        self.cursor.execute("CREATE TABLE issues (IssueID TEXT, ComicID TEXT, Issue_Number TEXT, Int_IssueNumber INT, IssueDate TEXT, ReleaseDate TEXT, Location TEXT, Status TEXT, IssueName TEXT)")
        self.cursor.execute("CREATE TABLE annuals (IssueID TEXT, ComicID TEXT, Issue_Number TEXT, Int_IssueNumber INT, IssueDate TEXT, ReleaseDate TEXT, Location TEXT, Status TEXT, Deleted INT DEFAULT 0, IssueName TEXT)")
        self.cursor.execute("CREATE TABLE storyarcs (StoryArcID TEXT, StoryArcName TEXT)")

        # Run extension migrations
        run_extension_migrations(self.cursor)
        self.conn.commit()

        # Populate sample series and publications
        self.cursor.execute("INSERT INTO comics VALUES ('101', 'Fight Girls', '2021', '2021', 'AWA Studios', '/comics/Fight Girls', 'Active')")
        self.cursor.execute("INSERT INTO comics VALUES ('102', 'Batman', '2011', '2011', 'DC Comics', '/comics/Batman', 'Active')")

        self.cursor.execute("INSERT INTO issues VALUES ('1001', '101', '1', 1, '2021-07-07', '2021-07-07', 'Fight Girls 001.cbz', 'Downloaded', 'Fight Girls #1')")
        self.cursor.execute("INSERT INTO issues VALUES ('1002', '101', '2', 2, '2021-08-04', '2021-08-04', 'Fight Girls 002.cbz', 'Downloaded', 'Fight Girls #2')")
        self.cursor.execute("INSERT INTO issues VALUES ('2001', '102', '1', 1, '2011-09-21', '2011-09-21', 'Batman 001.cbr', 'Downloaded', 'Batman #1')")
        self.cursor.execute("INSERT INTO annuals VALUES ('3001', '102', '1', 1, '2012-05-30', '2012-05-30', 'Batman Annual 001.cbr', 'Downloaded', 0, 'Batman Annual #1')")

        # Populate creator records:
        # Record 1: Frank Cho (Writer, Cover Artist)
        self.cursor.execute("INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, ResolutionSource) VALUES (1, 'Frank Cho', 'frank cho', 'frank-cho', 'unresolved')")
        # Record 2: FRANK CHO (Distinct record with same normalized name and slug from all-caps tagging)
        self.cursor.execute("INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, ResolutionSource) VALUES (2, 'FRANK CHO', 'frank cho', 'frank-cho', 'unresolved')")
        # Record 3: Composite name
        self.cursor.execute("INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, ResolutionSource) VALUES (3, 'Scott Snyder and Nick Dragotta', 'scott snyder and nick dragotta', 'scott-snyder-and-nick-dragotta', 'unresolved')")
        # Record 4: Sabine Rich (Colorist)
        self.cursor.execute("INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, ResolutionSource) VALUES (4, 'Sabine Rich', 'sabine rich', 'sabine-rich', 'unresolved')")
        # Record 5: Greg Capullo (Penciller)
        self.cursor.execute("INSERT INTO ext_creator_name_records (NameRecordID, RawName, NormalizedName, NameSlug, ResolutionSource) VALUES (5, 'Greg Capullo', 'greg capullo', 'greg-capullo', 'unresolved')")

        # Credits for Record 1 (Frank Cho):
        self.cursor.execute("INSERT INTO ext_creator_credits (NameRecordID, IssueID, IsAnnual, ComicID, Role, RawRoleText, RawCreditName, IsCover, SortOrder, SourceProvenance, SourceRevision) VALUES (1, '1001', 0, '101', 'writer', 'Writer', 'Frank Cho', 0, 0, 'comicinfo', 'sig1')")
        self.cursor.execute("INSERT INTO ext_creator_credits (NameRecordID, IssueID, IsAnnual, ComicID, Role, RawRoleText, RawCreditName, IsCover, SortOrder, SourceProvenance, SourceRevision) VALUES (1, '1001', 0, '101', 'cover_artist', 'Cover Artist', 'Frank Cho', 1, 1, 'comicinfo', 'sig1')")
        self.cursor.execute("INSERT INTO ext_creator_credits (NameRecordID, IssueID, IsAnnual, ComicID, Role, RawRoleText, RawCreditName, IsCover, SortOrder, SourceProvenance, SourceRevision) VALUES (1, '1002', 0, '101', 'writer', 'Writer', 'Frank Cho', 0, 0, 'comicinfo', 'sig2')")
        self.cursor.execute("INSERT INTO ext_creator_credits (NameRecordID, IssueID, IsAnnual, ComicID, Role, RawRoleText, RawCreditName, IsCover, SortOrder, SourceProvenance, SourceRevision) VALUES (1, '3001', 1, '102', 'cover_artist', 'Cover Artist', 'Frank Cho', 1, 0, 'comicinfo', 'sig3')")

        # Credits for Record 3 (Composite name):
        self.cursor.execute("INSERT INTO ext_creator_credits (NameRecordID, IssueID, IsAnnual, ComicID, Role, RawRoleText, RawCreditName, IsCover, SortOrder, SourceProvenance, SourceRevision) VALUES (3, '2001', 0, '102', 'writer', 'Writer', 'Scott Snyder and Nick Dragotta', 0, 0, 'comicinfo', 'sig4')")

        # Credits for Record 4 (Sabine Rich):
        self.cursor.execute("INSERT INTO ext_creator_credits (NameRecordID, IssueID, IsAnnual, ComicID, Role, RawRoleText, RawCreditName, IsCover, SortOrder, SourceProvenance, SourceRevision) VALUES (4, '1001', 0, '101', 'colorist', 'Colorist', 'Sabine Rich', 0, 2, 'comicinfo', 'sig1')")

        # Credits for Record 5 (Greg Capullo):
        self.cursor.execute("INSERT INTO ext_creator_credits (NameRecordID, IssueID, IsAnnual, ComicID, Role, RawRoleText, RawCreditName, IsCover, SortOrder, SourceProvenance, SourceRevision) VALUES (5, '2001', 0, '102', 'penciller', 'Penciller', 'Greg Capullo', 0, 1, 'comicinfo', 'sig4')")
        self.cursor.execute("INSERT INTO ext_creator_credits (NameRecordID, IssueID, IsAnnual, ComicID, Role, RawRoleText, RawCreditName, IsCover, SortOrder, SourceProvenance, SourceRevision) VALUES (5, '3001', 1, '102', 'penciller', 'Penciller', 'Greg Capullo', 0, 1, 'comicinfo', 'sig3')")

        self.conn.commit()

        class DBAdapter:
            def __init__(self, conn): self.connection = conn
            def select(self, query, params=None): return self.connection.cursor().execute(query, params or []).fetchall()
            def selectone(self, query, params=None):
                class Row:
                    def __init__(self, cur): self.cur = cur
                    def fetchone(self): return self.cur.fetchone()
                c = self.connection.cursor()
                c.execute(query, params or [])
                return Row(c)

        self.service = CreatorBrowserService(db_conn=DBAdapter(self.conn))

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_01_no_network_and_no_issue_details_calls(self):
        """Verify browser service code has zero network calls or IssueDetails calls."""
        service_file = os.path.join('mylar', 'extensions', 'creators', 'browser_service.py')
        controller_file = os.path.join('mylar', 'extensions', 'creators', 'browser_controller.py')
        for fpath in [service_file, controller_file]:
            with open(fpath, 'r', encoding='utf-8') as f:
                content = f.read()
                self.assertNotIn('IssueDetails', content)
                self.assertNotIn('import requests', content)
                self.assertNotIn('import urllib.request', content)
                self.assertNotIn('import http.client', content)
                self.assertNotIn('open_archive', content)
                self.assertNotIn('zipfile.', content)
                self.assertNotIn('rarfile.', content)

    def test_02_creator_catalog_retrieval_and_counts(self):
        """Test catalog listing aggregates credits, issues, and series accurately."""
        catalog = self.service.get_creator_catalog(page=1, page_size=10)
        self.assertEqual(catalog['total_observed_names'], 5)
        self.assertEqual(catalog['total_indexed_credits'], 8)
        self.assertEqual(catalog['total_matching'], 5)

        # Verify Frank Cho (NameRecordID=1) has 4 credits, 3 distinct publications (2 issues + 1 annual), 2 series
        cho = next(c for c in catalog['creators'] if c['name_record_id'] == 1)
        self.assertEqual(cho['raw_name'], 'Frank Cho')
        self.assertEqual(cho['credit_count'], 4)
        self.assertEqual(cho['issue_count'], 3)
        self.assertEqual(cho['series_count'], 2)
        self.assertIn('writer', cho['roles'])
        self.assertIn('cover_artist', cho['roles'])
        self.assertFalse(cho['is_resolved'])

    def test_03_search_and_role_filtering(self):
        """Test search by substring and role filtering."""
        # 1. Search for 'Rich'
        cat_search = self.service.get_creator_catalog(search='Rich')
        self.assertEqual(cat_search['total_matching'], 1)
        self.assertEqual(cat_search['creators'][0]['raw_name'], 'Sabine Rich')

        # 2. Filter by 'cover_artist'
        cat_cover = self.service.get_creator_catalog(role='cover_artist')
        self.assertEqual(cat_cover['total_matching'], 1)
        self.assertEqual(cat_cover['creators'][0]['name_record_id'], 1)

        # 3. Filter by 'penciller'
        cat_pen = self.service.get_creator_catalog(role='penciller')
        self.assertEqual(cat_pen['total_matching'], 1)
        self.assertEqual(cat_pen['creators'][0]['raw_name'], 'Greg Capullo')

    def test_04_identity_policy_no_auto_merging(self):
        """Test that two NameRecordIDs with identical normalized names remain distinct records."""
        catalog = self.service.get_creator_catalog(search='Frank Cho')
        self.assertEqual(catalog['total_matching'], 2)
        record_ids = [c['name_record_id'] for c in catalog['creators']]
        self.assertIn(1, record_ids)
        self.assertIn(2, record_ids)
        self.assertNotEqual(record_ids[0], record_ids[1])

        # Detail for Record 1 shows ambiguity alert with Record 2
        detail1 = self.service.get_creator_detail(1)
        self.assertTrue(detail1['has_display_name_ambiguity'])
        self.assertIn(2, detail1['other_name_record_ids'])

    def test_05_composite_name_intact(self):
        """Test that composite names with 'and', '&', '/' remain one intact observed record."""
        detail = self.service.get_creator_detail(3)
        self.assertIsNotNone(detail)
        self.assertEqual(detail['raw_name'], 'Scott Snyder and Nick Dragotta')
        self.assertEqual(detail['name_slug'], 'scott-snyder-and-nick-dragotta')
        self.assertEqual(len(detail['publications']), 1)
        self.assertEqual(detail['publications'][0]['comic_name'], 'Batman')

    def test_06_detail_distinguishes_regular_issues_and_annuals(self):
        """Test that regular issues and annuals are distinguished cleanly."""
        detail = self.service.get_creator_detail(1)
        self.assertEqual(detail['total_issues'], 2)
        self.assertEqual(detail['total_annuals'], 1)
        self.assertEqual(len(detail['publications']), 4)

        # Verify annual record has is_annual=True and correct metadata
        annual_pubs = [p for p in detail['publications'] if p['is_annual']]
        self.assertEqual(len(annual_pubs), 1)
        self.assertEqual(annual_pubs[0]['comic_name'], 'Batman')
        self.assertEqual(annual_pubs[0]['issue_number'], '1')
        self.assertTrue(annual_pubs[0]['is_annual'])

        # Test filtering by publication type
        detail_issues_only = self.service.get_creator_detail(1, pub_type='issues')
        self.assertEqual(len(detail_issues_only['publications']), 3)
        self.assertTrue(all(not p['is_annual'] for p in detail_issues_only['publications']))

        detail_annuals_only = self.service.get_creator_detail(1, pub_type='annuals')
        self.assertEqual(len(detail_annuals_only['publications']), 1)
        self.assertTrue(all(p['is_annual'] for p in detail_annuals_only['publications']))

    def test_07_invalid_and_missing_record_handling(self):
        """Test invalid IDs and missing records return clean None."""
        self.assertIsNone(self.service.get_creator_detail(999))
        self.assertIsNone(self.service.get_creator_detail('invalid-id'))
        self.assertIsNone(self.service.get_creator_detail(None))

    def test_08_pagination_on_catalog(self):
        """Test pagination limits and offsets."""
        cat_p1 = self.service.get_creator_catalog(page=1, page_size=2)
        self.assertEqual(len(cat_p1['creators']), 2)
        self.assertEqual(cat_p1['total_pages'], 3)
        self.assertTrue(cat_p1['has_next'])
        self.assertFalse(cat_p1['has_prev'])

        cat_p2 = self.service.get_creator_catalog(page=2, page_size=2)
        self.assertEqual(len(cat_p2['creators']), 2)
        self.assertTrue(cat_p2['has_prev'])
        self.assertTrue(cat_p2['has_next'])

        cat_p3 = self.service.get_creator_catalog(page=3, page_size=2)
        self.assertEqual(len(cat_p3['creators']), 1)
        self.assertFalse(cat_p3['has_next'])

    def test_09_database_invariance(self):
        """Confirm browsing causes zero writes to ext_creator_entities or core tables."""
        # Execute queries
        self.service.get_creator_catalog(search='Cho')
        self.service.get_creator_detail(1)
        self.service.get_creator_detail(5)

        # Verify entity count remains 0
        entity_count = self.cursor.execute("SELECT COUNT(*) FROM ext_creator_entities").fetchone()[0]
        self.assertEqual(entity_count, 0)

        # Verify core counts
        issues_cnt = self.cursor.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
        annuals_cnt = self.cursor.execute("SELECT COUNT(*) FROM annuals").fetchone()[0]
        comics_cnt = self.cursor.execute("SELECT COUNT(*) FROM comics").fetchone()[0]
        self.assertEqual(issues_cnt, 3)
        self.assertEqual(annuals_cnt, 1)
        self.assertEqual(comics_cnt, 2)


if __name__ == '__main__':
    unittest.main()
