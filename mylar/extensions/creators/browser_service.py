"""
Creator Browser Service (Phase C3).

Provides read-only queries for exploring locally indexed creator credits,
name records, and associated publications across series, regular issues, and annuals.

Strict Invariants:
- All queries are 100% read-only. Zero writes, zero mutations.
- Records are strictly keyed by NameRecordID.
- Records with identical normalized names or slugs are NEVER merged.
- Composite names ('and', '&', '/') remain intact.
"""

import math
from mylar import db, logger
from mylar.extensions.creators.schema import (
    ROLES,
    PROVENANCE_COMICINFO,
    RESOLUTION_UNRESOLVED
)


class CreatorBrowserService:
    """
    Read-only service layer for the Creator Credits Catalog and Detail views.
    """

    def __init__(self, db_conn=None):
        self._custom_db = db_conn

    def _get_db(self):
        if self._custom_db:
            return self._custom_db
        return db.DBConnection()

    def get_creator_catalog(self, search=None, role=None, sort='name_asc', page=1, page_size=24):
        """
        Query paginated creator-credit name records with aggregated metrics.

        :param search: Optional string query matching RawName or NormalizedName
        :param role: Optional role filter (e.g. 'writer', 'penciller', etc.)
        :param sort: Sorting order ('name_asc', 'name_desc', 'credits_desc', 'issues_desc', 'series_desc')
        :param page: 1-indexed page number
        :param page_size: Number of records per page
        :return: Dict containing creators list, pagination metadata, and global index counts
        """
        my_db = self._get_db()

        # Sanitize pagination parameters
        try:
            page = max(1, int(page))
        except (ValueError, TypeError):
            page = 1

        try:
            page_size = max(1, min(100, int(page_size)))
        except (ValueError, TypeError):
            page_size = 24

        offset = (page - 1) * page_size

        # 1. Global index statistics
        stats_cursor = my_db.selectone(
            """
            SELECT 
                (SELECT COUNT(*) FROM ext_creator_name_records),
                (SELECT COUNT(*) FROM ext_creator_credits)
            """
        )
        stats_row = stats_cursor.fetchone() if stats_cursor else None
        total_observed_names = stats_row[0] if stats_row else 0
        total_indexed_credits = stats_row[1] if stats_row else 0

        # Build WHERE clauses
        where_clauses = []
        params = []

        if search and search.strip():
            clean_search = f"%{search.strip().lower()}%"
            where_clauses.append("(LOWER(n.RawName) LIKE ? OR LOWER(n.NormalizedName) LIKE ?)")
            params.extend([clean_search, clean_search])

        if role and role.strip() and role.strip().lower() in ROLES:
            clean_role = role.strip().lower()
            where_clauses.append("c.Role = ?")
            params.append(clean_role)

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        # 2. Count total matching distinct NameRecordIDs
        count_query = f"""
            SELECT COUNT(DISTINCT n.NameRecordID)
            FROM ext_creator_name_records n
            LEFT JOIN ext_creator_credits c ON n.NameRecordID = c.NameRecordID
            {where_sql}
        """
        count_cursor = my_db.selectone(count_query, params)
        count_row = count_cursor.fetchone() if count_cursor else None
        total_matching = count_row[0] if count_row else 0

        total_pages = max(1, math.ceil(total_matching / page_size))
        if page > total_pages and total_matching > 0:
            page = total_pages
            offset = (page - 1) * page_size

        # 3. Sort ordering
        sort_map = {
            'name_asc': 'n.RawName COLLATE NOCASE ASC',
            'name_desc': 'n.RawName COLLATE NOCASE DESC',
            'credits_desc': 'CreditCount DESC, n.RawName COLLATE NOCASE ASC',
            'issues_desc': 'IssueCount DESC, n.RawName COLLATE NOCASE ASC',
            'series_desc': 'SeriesCount DESC, n.RawName COLLATE NOCASE ASC',
        }
        order_by_sql = sort_map.get(sort, 'n.RawName COLLATE NOCASE ASC')

        # 4. Fetch paginated records with aggregated counts
        records_query = f"""
            SELECT 
                n.NameRecordID,
                n.RawName,
                n.NormalizedName,
                n.NameSlug,
                n.CreatorEntityID,
                n.ResolutionSource,
                n.CreatedAt,
                e.DisplayName AS ConfirmedEntityName,
                e.EntitySlug AS ConfirmedEntitySlug,
                COUNT(c.CreditID) AS CreditCount,
                COUNT(DISTINCT (c.IssueID || '-' || c.IsAnnual)) AS IssueCount,
                COUNT(DISTINCT c.ComicID) AS SeriesCount,
                GROUP_CONCAT(DISTINCT c.Role) AS DistinctRoles,
                GROUP_CONCAT(DISTINCT c.SourceProvenance) AS Provenances
            FROM ext_creator_name_records n
            LEFT JOIN ext_creator_credits c ON n.NameRecordID = c.NameRecordID
            LEFT JOIN ext_creator_entities e ON n.CreatorEntityID = e.CreatorEntityID
            {where_sql}
            GROUP BY n.NameRecordID
            ORDER BY {order_by_sql}
            LIMIT ? OFFSET ?
        """

        query_params = list(params) + [page_size, offset]
        rows = my_db.select(records_query, query_params)

        creators = []
        for r in rows:
            roles_str = r['DistinctRoles'] if isinstance(r, dict) else r[12]
            roles_list = [x.strip() for x in roles_str.split(',')] if roles_str else []

            provenance_str = r['Provenances'] if isinstance(r, dict) else r[13]
            provenance_list = [x.strip() for x in provenance_str.split(',')] if provenance_str else ['comicinfo']

            name_rec_id = r['NameRecordID'] if isinstance(r, dict) else r[0]
            raw_name = r['RawName'] if isinstance(r, dict) else r[1]
            norm_name = r['NormalizedName'] if isinstance(r, dict) else r[2]
            name_slug = r['NameSlug'] if isinstance(r, dict) else r[3]
            entity_id = r['CreatorEntityID'] if isinstance(r, dict) else r[4]
            res_source = r['ResolutionSource'] if isinstance(r, dict) else r[5]
            created_at = r['CreatedAt'] if isinstance(r, dict) else r[6]
            conf_name = r['ConfirmedEntityName'] if isinstance(r, dict) else r[7]
            conf_slug = r['ConfirmedEntitySlug'] if isinstance(r, dict) else r[8]
            credit_count = r['CreditCount'] if isinstance(r, dict) else r[9]
            issue_count = r['IssueCount'] if isinstance(r, dict) else r[10]
            series_count = r['SeriesCount'] if isinstance(r, dict) else r[11]

            creators.append({
                'name_record_id': name_rec_id,
                'raw_name': raw_name,
                'normalized_name': norm_name,
                'name_slug': name_slug,
                'creator_entity_id': entity_id,
                'resolution_source': res_source,
                'is_resolved': bool(entity_id),
                'confirmed_entity_name': conf_name,
                'confirmed_entity_slug': conf_slug,
                'created_at': created_at,
                'credit_count': credit_count or 0,
                'issue_count': issue_count or 0,
                'series_count': series_count or 0,
                'roles': roles_list,
                'provenance': provenance_list,
            })

        return {
            'creators': creators,
            'total_matching': total_matching,
            'total_observed_names': total_observed_names,
            'total_indexed_credits': total_indexed_credits,
            'page': page,
            'page_size': page_size,
            'total_pages': total_pages,
            'has_prev': page > 1,
            'has_next': page < total_pages,
            'search': search or '',
            'role': role or '',
            'sort': sort or 'name_asc',
        }

    def get_creator_detail(self, name_record_id, role_filter=None, pub_type='all', series_filter=None, sort='date_desc'):
        """
        Query full metadata and associated publications for a single NameRecordID.

        :param name_record_id: Authoritative NameRecordID
        :param role_filter: Optional role filter within this creator's credits
        :param pub_type: 'all', 'issues', or 'annuals'
        :param series_filter: Optional ComicID filter
        :param sort: 'date_desc', 'date_asc', 'series_asc', 'issue_asc'
        :return: Dict containing creator metadata and publications list, or None if not found
        """
        try:
            val_id = int(str(name_record_id).strip())
        except (ValueError, TypeError):
            return None

        my_db = self._get_db()

        # 1. Fetch Name Record
        name_cursor = my_db.selectone(
            """
            SELECT 
                n.NameRecordID,
                n.RawName,
                n.NormalizedName,
                n.NameSlug,
                n.CreatorEntityID,
                n.ResolutionSource,
                n.CreatedAt,
                n.UpdatedAt,
                e.DisplayName AS ConfirmedEntityName,
                e.EntitySlug AS ConfirmedEntitySlug,
                e.Notes AS ConfirmedEntityNotes
            FROM ext_creator_name_records n
            LEFT JOIN ext_creator_entities e ON n.CreatorEntityID = e.CreatorEntityID
            WHERE n.NameRecordID = ?
            """,
            [val_id]
        )
        name_row = name_cursor.fetchone() if name_cursor else None
        if not name_row:
            return None

        raw_name = name_row['RawName'] if isinstance(name_row, dict) else name_row[1]
        norm_name = name_row['NormalizedName'] if isinstance(name_row, dict) else name_row[2]
        name_slug = name_row['NameSlug'] if isinstance(name_row, dict) else name_row[3]
        entity_id = name_row['CreatorEntityID'] if isinstance(name_row, dict) else name_row[4]
        res_source = name_row['ResolutionSource'] if isinstance(name_row, dict) else name_row[5]
        created_at = name_row['CreatedAt'] if isinstance(name_row, dict) else name_row[6]
        updated_at = name_row['UpdatedAt'] if isinstance(name_row, dict) else name_row[7]
        conf_name = name_row['ConfirmedEntityName'] if isinstance(name_row, dict) else name_row[8]
        conf_slug = name_row['ConfirmedEntitySlug'] if isinstance(name_row, dict) else name_row[9]
        conf_notes = name_row['ConfirmedEntityNotes'] if isinstance(name_row, dict) else name_row[10]

        # 2. Check for other NameRecordIDs sharing the same normalized/display name (ambiguity indicator)
        same_name_rows = my_db.select(
            "SELECT NameRecordID FROM ext_creator_name_records WHERE (LOWER(RawName) = LOWER(?) OR NormalizedName = ?) AND NameRecordID != ?",
            [raw_name, norm_name, val_id]
        )
        other_name_ids = [r[0] if not isinstance(r, dict) else r['NameRecordID'] for r in same_name_rows]

        # 3. Overall stats for this NameRecordID
        overall_stats = my_db.selectone(
            """
            SELECT 
                COUNT(CreditID) AS TotalCredits,
                COUNT(DISTINCT CASE WHEN IsAnnual = 0 THEN IssueID END) AS TotalIssues,
                COUNT(DISTINCT CASE WHEN IsAnnual = 1 THEN IssueID END) AS TotalAnnuals,
                COUNT(DISTINCT ComicID) AS TotalSeries,
                GROUP_CONCAT(DISTINCT Role) AS DistinctRoles,
                GROUP_CONCAT(DISTINCT SourceProvenance) AS Provenances
            FROM ext_creator_credits
            WHERE NameRecordID = ?
            """,
            [val_id]
        )
        stat_row = overall_stats.fetchone() if overall_stats else None
        total_credits = stat_row['TotalCredits'] if stat_row and isinstance(stat_row, dict) else (stat_row[0] if stat_row else 0)
        total_issues = stat_row['TotalIssues'] if stat_row and isinstance(stat_row, dict) else (stat_row[1] if stat_row else 0)
        total_annuals = stat_row['TotalAnnuals'] if stat_row and isinstance(stat_row, dict) else (stat_row[2] if stat_row else 0)
        total_series = stat_row['TotalSeries'] if stat_row and isinstance(stat_row, dict) else (stat_row[3] if stat_row else 0)
        all_roles_str = stat_row['DistinctRoles'] if stat_row and isinstance(stat_row, dict) else (stat_row[4] if stat_row else '')
        all_roles = [r.strip() for r in all_roles_str.split(',')] if all_roles_str else []
        provenances_str = stat_row['Provenances'] if stat_row and isinstance(stat_row, dict) else (stat_row[5] if stat_row else '')
        provenances = [p.strip() for p in provenances_str.split(',')] if provenances_str else ['comicinfo']

        # 4. Query available series for filtering
        series_rows = my_db.select(
            """
            SELECT DISTINCT cm.ComicID, cm.ComicName, cm.ComicYear
            FROM ext_creator_credits c
            JOIN comics cm ON c.ComicID = cm.ComicID
            WHERE c.NameRecordID = ?
            ORDER BY cm.ComicName COLLATE NOCASE ASC
            """,
            [val_id]
        )
        available_series = [
            {
                'comic_id': r['ComicID'] if isinstance(r, dict) else r[0],
                'comic_name': r['ComicName'] if isinstance(r, dict) else r[1],
                'comic_year': r['ComicYear'] if isinstance(r, dict) else r[2]
            }
            for r in series_rows
        ]

        # 5. Build publications query
        pub_where = ["c.NameRecordID = ?"]
        pub_params = [val_id]

        if role_filter and role_filter.strip() and role_filter.strip().lower() in ROLES:
            pub_where.append("c.Role = ?")
            pub_params.append(role_filter.strip().lower())

        if pub_type == 'issues':
            pub_where.append("c.IsAnnual = 0")
        elif pub_type == 'annuals':
            pub_where.append("c.IsAnnual = 1")

        if series_filter and series_filter.strip():
            pub_where.append("c.ComicID = ?")
            pub_params.append(series_filter.strip())

        where_clause_sql = "WHERE " + " AND ".join(pub_where)

        # Sort order for publications
        order_map = {
            'date_desc': 'COALESCE(i.ReleaseDate, a.ReleaseDate, i.IssueDate, a.IssueDate) DESC, cm.ComicName ASC, Int_IssueNumber DESC',
            'date_asc': 'COALESCE(i.ReleaseDate, a.ReleaseDate, i.IssueDate, a.IssueDate) ASC, cm.ComicName ASC, Int_IssueNumber ASC',
            'series_asc': 'cm.ComicName COLLATE NOCASE ASC, c.IsAnnual ASC, Int_IssueNumber ASC, c.SortOrder ASC',
            'issue_asc': 'Int_IssueNumber ASC, cm.ComicName ASC',
        }
        pub_order_sql = order_map.get(sort, order_map['date_desc'])

        # Detailed publications query: join regular issues and annuals safely
        publications_query = f"""
            SELECT 
                c.CreditID,
                c.NameRecordID,
                c.IssueID,
                c.IsAnnual,
                c.ComicID,
                c.Role,
                c.RawRoleText,
                c.RawCreditName,
                c.IsCover,
                c.SortOrder,
                c.SourceProvenance,
                cm.ComicName,
                cm.ComicYear,
                cm.Corrected_SeriesYear,
                cm.ComicPublisher,
                cm.ComicLocation,
                COALESCE(i.Issue_Number, a.Issue_Number) AS IssueNumber,
                COALESCE(i.Int_IssueNumber, a.Int_IssueNumber) AS Int_IssueNumber,
                COALESCE(i.IssueName, a.IssueName) AS IssueTitle,
                COALESCE(i.IssueDate, a.IssueDate) AS IssueDate,
                COALESCE(i.ReleaseDate, a.ReleaseDate) AS ReleaseDate,
                COALESCE(i.Location, a.Location) AS Location,
                COALESCE(i.Status, a.Status) AS IssueStatus,
                s.RelativePath,
                s.ScanStatus,
                s.LastSuccessAt
            FROM ext_creator_credits c
            JOIN comics cm ON c.ComicID = cm.ComicID
            LEFT JOIN issues i ON (c.IssueID = i.IssueID AND c.IsAnnual = 0)
            LEFT JOIN annuals a ON (c.IssueID = a.IssueID AND c.IsAnnual = 1)
            LEFT JOIN ext_creator_scan_records s ON (c.IssueID = s.IssueID AND c.IsAnnual = s.IsAnnual AND c.SourceProvenance = s.SourceType)
            {where_clause_sql}
            ORDER BY {pub_order_sql}
        """

        pub_rows = my_db.select(publications_query, pub_params)

        publications = []
        for p in pub_rows:
            credit_id = p['CreditID'] if isinstance(p, dict) else p[0]
            issue_id = p['IssueID'] if isinstance(p, dict) else p[2]
            is_annual = bool(p['IsAnnual'] if isinstance(p, dict) else p[3])
            comic_id = p['ComicID'] if isinstance(p, dict) else p[4]
            role = p['Role'] if isinstance(p, dict) else p[5]
            raw_role = p['RawRoleText'] if isinstance(p, dict) else p[6]
            raw_credit_name = p['RawCreditName'] if isinstance(p, dict) else p[7]
            is_cover = bool(p['IsCover'] if isinstance(p, dict) else p[8])
            sort_order = p['SortOrder'] if isinstance(p, dict) else p[9]
            source_prov = p['SourceProvenance'] if isinstance(p, dict) else p[10]
            comic_name = p['ComicName'] if isinstance(p, dict) else p[11]
            comic_year = p['ComicYear'] if isinstance(p, dict) else p[12]
            corrected_year = p['Corrected_SeriesYear'] if isinstance(p, dict) else p[13]
            publisher = p['ComicPublisher'] if isinstance(p, dict) else p[14]
            comic_loc = p['ComicLocation'] if isinstance(p, dict) else p[15]
            issue_number = p['IssueNumber'] if isinstance(p, dict) else p[16]
            int_issue_number = p['Int_IssueNumber'] if isinstance(p, dict) else p[17]
            issue_title = p['IssueTitle'] if isinstance(p, dict) else p[18]
            issue_date = p['IssueDate'] if isinstance(p, dict) else p[19]
            release_date = p['ReleaseDate'] if isinstance(p, dict) else p[20]
            location = p['Location'] if isinstance(p, dict) else p[21]
            issue_status = p['IssueStatus'] if isinstance(p, dict) else p[22]
            rel_path = p['RelativePath'] if isinstance(p, dict) else p[23]
            scan_status = p['ScanStatus'] if isinstance(p, dict) else p[24]
            last_success = p['LastSuccessAt'] if isinstance(p, dict) else p[25]

            # Detect archive format from location or relative path
            archive_ext = ''
            loc_str = (rel_path or location or '').lower()
            if loc_str.endswith('.cbz'):
                archive_ext = 'CBZ'
            elif loc_str.endswith('.cbr'):
                archive_ext = 'CBR'
            elif loc_str.endswith('.zip'):
                archive_ext = 'ZIP'
            elif loc_str.endswith('.rar'):
                archive_ext = 'RAR'

            display_year = corrected_year if (corrected_year and corrected_year != 'None') else (comic_year or '')
            display_date = release_date or issue_date or ''

            publications.append({
                'credit_id': credit_id,
                'issue_id': issue_id,
                'is_annual': is_annual,
                'comic_id': comic_id,
                'role': role,
                'raw_role_text': raw_role,
                'raw_credit_name': raw_credit_name,
                'is_cover': is_cover,
                'sort_order': sort_order,
                'source_provenance': source_prov,
                'comic_name': comic_name or 'Unknown Comic',
                'comic_year': display_year,
                'publisher': publisher or '',
                'issue_number': issue_number or '?',
                'int_issue_number': int_issue_number or 0,
                'issue_title': issue_title or '',
                'display_date': display_date,
                'archive_type': archive_ext,
                'issue_status': issue_status or '',
                'scan_status': scan_status or '',
                'last_success_at': last_success or '',
            })

        return {
            'found': True,
            'name_record_id': val_id,
            'raw_name': raw_name,
            'normalized_name': norm_name,
            'name_slug': name_slug,
            'creator_entity_id': entity_id,
            'resolution_source': res_source,
            'is_resolved': bool(entity_id),
            'confirmed_entity_name': conf_name,
            'confirmed_entity_slug': conf_slug,
            'confirmed_entity_notes': conf_notes,
            'created_at': created_at,
            'updated_at': updated_at,
            'other_name_record_ids': other_name_ids,
            'has_display_name_ambiguity': len(other_name_ids) > 0,
            'total_credits': total_credits,
            'total_issues': total_issues,
            'total_annuals': total_annuals,
            'total_series': total_series,
            'roles': all_roles,
            'provenance': provenances,
            'available_series': available_series,
            'publications': publications,
            'current_role_filter': role_filter or '',
            'current_pub_type': pub_type or 'all',
            'current_series_filter': series_filter or '',
            'current_sort': sort or 'date_desc',
        }

    def get_issue_creator_credits(self, issue_id, is_annual=0):
        """
        Retrieve locally indexed creator credits for a specific IssueID and IsAnnual scope.

        :param issue_id: Issue identifier (int or str)
        :param is_annual: 0 for regular issue, 1 for annual
        :return: Dict containing issue metadata, total credits, and ordered role groups with clickable links metadata
        """
        try:
            val_issue_id = str(int(str(issue_id).strip()))
        except (ValueError, TypeError):
            return {
                'success': False,
                'error': 'Invalid issue_id',
                'issue_id': str(issue_id) if issue_id is not None else '',
                'is_annual': 0,
                'total_credits': 0,
                'credits': [],
                'groups': []
            }

        val_annual = 1 if is_annual in (1, '1', True, 'true', 'True') else 0

        my_db = self._get_db()

        query = """
            SELECT 
                c.CreditID,
                c.NameRecordID,
                c.Role,
                c.RawRoleText,
                c.RawCreditName,
                c.IsCover,
                c.SortOrder,
                c.SourceProvenance,
                n.RawName,
                n.NormalizedName,
                n.NameSlug,
                n.CreatorEntityID,
                n.ResolutionSource,
                e.DisplayName AS ConfirmedEntityName,
                e.EntitySlug AS ConfirmedEntitySlug
            FROM ext_creator_credits c
            JOIN ext_creator_name_records n ON c.NameRecordID = n.NameRecordID
            LEFT JOIN ext_creator_entities e ON n.CreatorEntityID = e.CreatorEntityID
            WHERE c.IssueID = ? AND c.IsAnnual = ?
            ORDER BY 
                CASE c.Role
                    WHEN 'writer' THEN 1
                    WHEN 'penciller' THEN 2
                    WHEN 'inker' THEN 3
                    WHEN 'colorist' THEN 4
                    WHEN 'letterer' THEN 5
                    WHEN 'editor' THEN 6
                    WHEN 'cover_artist' THEN 7
                    ELSE 8
                END ASC,
                c.SortOrder ASC,
                c.CreditID ASC
        """

        rows = my_db.select(query, [val_issue_id, val_annual])

        role_display_names = {
            'writer': 'Writer',
            'penciller': 'Penciller',
            'inker': 'Inker',
            'colorist': 'Colorist',
            'letterer': 'Letterer',
            'editor': 'Editor',
            'cover_artist': 'Cover Artist',
            'other': 'Other',
        }

        credits = []
        groups_dict = {}
        for r in rows:
            credit_id = r['CreditID'] if isinstance(r, dict) else r[0]
            name_record_id = r['NameRecordID'] if isinstance(r, dict) else r[1]
            role = r['Role'] if isinstance(r, dict) else r[2]
            raw_role = r['RawRoleText'] if isinstance(r, dict) else r[3]
            raw_credit = r['RawCreditName'] if isinstance(r, dict) else r[4]
            is_cover = bool(r['IsCover'] if isinstance(r, dict) else r[5])
            sort_order = r['SortOrder'] if isinstance(r, dict) else r[6]
            source_prov = r['SourceProvenance'] if isinstance(r, dict) else r[7]
            raw_name = r['RawName'] if isinstance(r, dict) else r[8]
            norm_name = r['NormalizedName'] if isinstance(r, dict) else r[9]
            name_slug = r['NameSlug'] if isinstance(r, dict) else r[10]
            entity_id = r['CreatorEntityID'] if isinstance(r, dict) else r[11]
            res_source = r['ResolutionSource'] if isinstance(r, dict) else r[12]
            conf_name = r['ConfirmedEntityName'] if isinstance(r, dict) else r[13]
            conf_slug = r['ConfirmedEntitySlug'] if isinstance(r, dict) else r[14]

            credit_item = {
                'credit_id': credit_id,
                'name_record_id': name_record_id,
                'raw_name': raw_name,
                'raw_credit_name': raw_credit,
                'normalized_name': norm_name,
                'name_slug': name_slug,
                'role': role,
                'raw_role_text': raw_role,
                'sort_order': sort_order,
                'is_cover': is_cover,
                'creator_entity_id': entity_id,
                'is_resolved': bool(entity_id),
                'resolution_source': res_source or 'unresolved',
                'confirmed_entity_name': conf_name,
                'confirmed_entity_slug': conf_slug,
                'source_provenance': source_prov or 'comicinfo',
            }
            credits.append(credit_item)

            if role not in groups_dict:
                groups_dict[role] = []
            groups_dict[role].append(credit_item)

        # Standard presentation order
        ordered_role_keys = ['writer', 'penciller', 'inker', 'colorist', 'letterer', 'editor', 'cover_artist', 'other']
        groups = []
        for r_key in ordered_role_keys:
            if r_key in groups_dict and groups_dict[r_key]:
                groups.append({
                    'role_key': r_key,
                    'role_display': role_display_names.get(r_key, r_key.replace('_', ' ').title()),
                    'credits': groups_dict[r_key]
                })

        # Catch any unexpected roles not in standard list
        for r_key, c_list in groups_dict.items():
            if r_key not in ordered_role_keys and c_list:
                groups.append({
                    'role_key': r_key,
                    'role_display': r_key.replace('_', ' ').title(),
                    'credits': c_list
                })

        return {
            'success': True,
            'issue_id': val_issue_id,
            'is_annual': val_annual,
            'total_credits': len(credits),
            'credits': credits,
            'groups': groups
        }
