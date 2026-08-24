"""
Creator Identity Registry Service (Phase C4.12).

Provides strictly read-only querying of locally recorded creator identity decisions,
rejections, reversals, and conflicts across the library.
Reads solely from existing C4.8-C4.11 database tables with ZERO SQL writes, ZERO provider/network
calls, and ZERO candidate discovery or heuristic inference.
Functions identically whether external providers (e.g. Metron) are enabled or disabled.
"""

from typing import Any, Dict, List, Optional, Tuple

from mylar.extensions.creators.identity_repository import IdentityRepository


class CreatorRegistryError(Exception):
    """Base exception for creator registry operations."""
    pass


class InvalidRegistryInputError(CreatorRegistryError):
    """Raised when query parameters for the registry are invalid or unsupported."""
    pass


STATE_ALLOWLIST = {'all', 'confirmed', 'rejected', 'reversed', 'conflicted', 'unresolved', 'transferred'}
PROVIDER_ALLOWLIST = {'all', 'metron', 'comicvine'}
SORT_ALLOWLIST = {'name_asc', 'name_desc', 'recent_desc', 'recent_asc', 'events_desc'}


class CreatorRegistryService:
    """
    Dedicated read-only query service for the Creator Identity Registry.
    """

    def __init__(self, repository: Optional[IdentityRepository] = None, db_conn=None):
        self._repo = repository or IdentityRepository(db_conn=db_conn)

    def _get_db(self):
        return self._repo._get_db()

    def _validate_query_params(
        self,
        state: Optional[str] = 'all',
        provider: Optional[str] = 'all',
        search: Optional[str] = None,
        sort: Optional[str] = 'name_asc',
        page: int = 1,
        page_size: int = 25
    ) -> Tuple[str, str, Optional[str], str, int, int]:
        """Validate and clamp all query parameters against strict allowlists."""
        clean_state = 'all'
        if state is not None and str(state).strip():
            st = str(state).strip().lower()
            if st not in STATE_ALLOWLIST:
                raise InvalidRegistryInputError(
                    f"Invalid state filter '{state}'. Supported: {sorted(STATE_ALLOWLIST)}"
                )
            clean_state = st

        clean_provider = 'all'
        if provider is not None and str(provider).strip():
            prov = str(provider).strip().lower()
            if prov not in PROVIDER_ALLOWLIST:
                raise InvalidRegistryInputError(
                    f"Invalid provider filter '{provider}'. Supported: {sorted(PROVIDER_ALLOWLIST)}"
                )
            clean_provider = prov

        clean_search = None
        if search is not None and str(search).strip():
            s = str(search).strip()
            if len(s) > 100:
                s = s[:100]
            clean_search = s

        clean_sort = 'name_asc'
        if sort is not None and str(sort).strip():
            srt = str(sort).strip().lower()
            if srt not in SORT_ALLOWLIST:
                raise InvalidRegistryInputError(
                    f"Invalid sort parameter '{sort}'. Supported: {sorted(SORT_ALLOWLIST)}"
                )
            clean_sort = srt

        try:
            val_page = int(page)
            if val_page < 1:
                val_page = 1
        except (ValueError, TypeError):
            val_page = 1

        try:
            val_page_size = int(page_size)
            if val_page_size < 1:
                val_page_size = 25
            elif val_page_size > 100:
                val_page_size = 100
        except (ValueError, TypeError):
            val_page_size = 25

        return clean_state, clean_provider, clean_search, clean_sort, val_page, val_page_size

    def get_registry_entries(
        self,
        state: Optional[str] = 'all',
        provider: Optional[str] = 'all',
        search: Optional[str] = None,
        sort: Optional[str] = 'name_asc',
        page: int = 1,
        page_size: int = 25
    ) -> Dict[str, Any]:
        """
        Query all local records with recorded identity decisions or audit events.

        :param state: Filter by resolution state ('all', 'confirmed', 'rejected', 'reversed', 'conflicted', 'unresolved')
        :param provider: Filter by provider namespace ('all', 'metron', 'comicvine')
        :param search: Substring search over local raw name and provider display name
        :param sort: Sort key ('name_asc', 'name_desc', 'recent_desc', 'recent_asc', 'events_desc')
        :param page: 1-indexed page number
        :param page_size: Results per page (1..100)
        :return: Sanitized structured registry catalog dictionary
        """
        clean_state, clean_prov, clean_search, clean_sort, val_page, val_page_size = self._validate_query_params(
            state, provider, search, sort, page, page_size
        )

        my_db = self._get_db()
        cur, _ = self._repo._get_cursor()

        # 1. Discover all distinct NameRecordIDs that possess recorded decision/audit info
        # Scoped strictly to records with stored resolution audit entries, candidate rejections, or linked entities
        cur.execute("""
            SELECT DISTINCT NameRecordID FROM ext_creator_resolution_audit WHERE NameRecordID IS NOT NULL
            UNION
            SELECT DISTINCT NameRecordID FROM ext_creator_candidate_rejections
            UNION
            SELECT DISTINCT NameRecordID FROM ext_creator_name_records WHERE CreatorEntityID IS NOT NULL
        """)
        eligible_ids = [r[0] for r in cur.fetchall() if r[0] is not None]

        # Assemble full record summaries
        all_records: List[Dict[str, Any]] = []

        for nr_id in eligible_ids:
            name_rec = self._repo.get_name_record(nr_id, cursor=cur)
            if not name_rec:
                continue

            entity_id = name_rec.get('creator_entity_id')
            confirmed_entity = None
            external_ids = []
            if entity_id:
                entity_data = self._repo.get_entity_by_id(entity_id, cursor=cur)
                if entity_data:
                    confirmed_entity = {
                        'creator_entity_id': entity_data['creator_entity_id'],
                        'display_name': entity_data['display_name'],
                        'normalized_name': entity_data['normalized_name'],
                        'entity_slug': entity_data['entity_slug'],
                    }
                    external_ids = self._repo.get_external_ids_for_entity(entity_id, cursor=cur)

            # Check for active rejection
            cur.execute(
                "SELECT RejectionID, Provider, ExternalID, Reason, ProviderDisplayName, RejectedAt FROM ext_creator_candidate_rejections WHERE NameRecordID = ? AND Status = 'active' ORDER BY RejectionID DESC LIMIT 1",
                (nr_id,)
            )
            rej_row = cur.fetchone()
            active_rejection = None
            if rej_row:
                active_rejection = {
                    'rejection_id': rej_row[0],
                    'provider': rej_row[1],
                    'external_id': rej_row[2],
                    'reason': rej_row[3],
                    'provider_display_name': rej_row[4] if len(rej_row) > 4 else None,
                    'created_at': rej_row[5] if len(rej_row) > 5 else None
                }

            # Fetch audit entries for this NameRecord
            cur.execute(
                "SELECT AuditID, Action, Provider, ExternalID, ProviderDisplayName, Actor, Reason, ConfirmationSource, CreatedAt FROM ext_creator_resolution_audit WHERE NameRecordID = ? ORDER BY AuditID DESC",
                (nr_id,)
            )
            audit_rows = cur.fetchall()
            decision_count = len(audit_rows)

            latest_decision = None
            if audit_rows:
                lat = audit_rows[0]
                act = lat[1]
                actor = lat[5] or 'historical_record'
                actor_lbl = {
                    'human_reviewer': 'Human Reviewer',
                    'system': 'System',
                    'historical_record': 'Historical Record'
                }.get(actor, actor.replace('_', ' ').title())

                action_lbl = {
                    'CONFIRM_PROVIDER_IDENTITY': 'Confirmed Provider Identity',
                    'REJECT_CANDIDATE': 'Rejected Candidate',
                    'TRANSFER_PROVIDER_MAPPING': 'Transferred Provider Mapping',
                    'REVERSE_TRANSFER_PROVIDER_MAPPING': 'Reversed Provider Transfer',
                    'RESOLVE_CONFLICT_REJECT_COMPETING': 'Conflict Resolved (Existing Retained)',
                    'REVERSE_CONFIRMATION': 'Reversed Confirmation',
                    'REVERSE_REJECTION': 'Reversed Rejection',
                    'CONFIRM_RAW_NAME': 'Confirmed Entity Link',
                    'LEGACY_EXTERNAL_ID_COLLISION': 'External ID Collision Detected'
                }.get(act, act.replace('_', ' ').title())

                latest_decision = {
                    'audit_id': lat[0],
                    'action': act,
                    'action_label': action_lbl,
                    'provider': lat[2],
                    'external_id': lat[3],
                    'provider_display_name': lat[4],
                    'actor': actor,
                    'actor_label': actor_lbl,
                    'reason': lat[6],
                    'confirmation_source': lat[7],
                    'timestamp': lat[8]
                }

            # Primary provider and provider ID
            prov = None
            prov_id = None
            prov_name = None
            if latest_decision and latest_decision.get('provider'):
                prov = latest_decision['provider']
                prov_id = latest_decision.get('external_id')
                prov_name = latest_decision.get('provider_display_name')
            elif confirmed_entity and external_ids:
                prov = external_ids[0]['provider']
                prov_id = external_ids[0]['external_id']
                prov_name = external_ids[0].get('provider_display_name') or confirmed_entity['display_name']
            elif active_rejection:
                prov = active_rejection['provider']
                prov_id = active_rejection['external_id']
                prov_name = active_rejection.get('provider_display_name')

            # Shared canonical state derivation
            pairing_res = self._repo.derive_pairing_state(nr_id, prov, prov_id, cursor=cur)
            curr_state = pairing_res['state']
            if curr_state == 'unresolved' and decision_count > 0:
                curr_state = 'reversed'

            # Fetch source credit / issue context if available
            source_context = self._fetch_source_context(nr_id, cur)

            all_records.append({
                'name_record_id': nr_id,
                'raw_local_name': name_rec['raw_name'],
                'normalized_name': name_rec['normalized_name'],
                'name_slug': name_rec['name_slug'],
                'resolution_source': name_rec.get('resolution_source', 'unresolved'),
                'current_state': curr_state,
                'provider': prov,
                'provider_creator_id': prov_id,
                'provider_display_name': prov_name,
                'confirmed_entity': confirmed_entity,
                'latest_decision': latest_decision,
                'decision_event_count': decision_count,
                'source_context': source_context
            })

        # Calculate state counts across all eligible records
        counts = {
            'all': len(all_records),
            'transferred': sum(1 for r in all_records if r['current_state'] == 'transferred'),
            'confirmed': sum(1 for r in all_records if r['current_state'] == 'confirmed'),
            'rejected': sum(1 for r in all_records if r['current_state'] == 'rejected'),
            'reversed': sum(1 for r in all_records if r['current_state'] in ('reversed', 'unresolved')),
            'conflicted': sum(1 for r in all_records if r['current_state'] == 'conflicted'),
        }

        # Apply State Filter
        filtered = all_records
        if clean_state != 'all':
            if clean_state == 'reversed' or clean_state == 'unresolved':
                filtered = [r for r in filtered if r['current_state'] in ('reversed', 'unresolved')]
            else:
                filtered = [r for r in filtered if r['current_state'] == clean_state]

        # Apply Provider Filter
        if clean_prov != 'all':
            filtered = [r for r in filtered if (r['provider'] or '').lower() == clean_prov]

        # Apply Search Filter (substring match on local name or provider display name)
        if clean_search:
            s_lower = clean_search.lower()
            filtered = [
                r for r in filtered
                if s_lower in r['raw_local_name'].lower()
                or (r['provider_display_name'] and s_lower in r['provider_display_name'].lower())
                or (r['confirmed_entity'] and s_lower in r['confirmed_entity']['display_name'].lower())
            ]

        # Apply Deterministic Sorting
        if clean_sort == 'name_asc':
            filtered.sort(key=lambda x: (x['raw_local_name'].lower(), x['name_record_id']))
        elif clean_sort == 'name_desc':
            filtered.sort(key=lambda x: (x['raw_local_name'].lower(), x['name_record_id']), reverse=True)
        elif clean_sort == 'recent_desc':
            filtered.sort(
                key=lambda x: (
                    x['latest_decision']['timestamp'] if x['latest_decision'] and x['latest_decision']['timestamp'] else '',
                    x['name_record_id']
                ),
                reverse=True
            )
        elif clean_sort == 'recent_asc':
            filtered.sort(
                key=lambda x: (
                    x['latest_decision']['timestamp'] if x['latest_decision'] and x['latest_decision']['timestamp'] else '9999',
                    x['name_record_id']
                )
            )
        elif clean_sort == 'events_desc':
            filtered.sort(key=lambda x: (x['decision_event_count'], x['raw_local_name'].lower()), reverse=True)

        # Pagination
        total_entries = len(filtered)
        total_pages = max(1, (total_entries + val_page_size - 1) // val_page_size) if total_entries > 0 else 1
        current_page = min(val_page, total_pages)
        start_idx = (current_page - 1) * val_page_size
        end_idx = start_idx + val_page_size

        paginated_records = filtered[start_idx:end_idx]

        return {
            'success': True,
            'entries': paginated_records,
            'total_entries': total_entries,
            'total_pages': total_pages,
            'current_page': current_page,
            'page_size': val_page_size,
            'has_prev': current_page > 1,
            'has_next': current_page < total_pages,
            'counts': counts,
            'state_filter': clean_state,
            'provider_filter': clean_prov,
            'search_query': clean_search or '',
            'current_sort': clean_sort
        }

    def _fetch_source_context(self, name_record_id: int, cursor) -> Optional[Dict[str, Any]]:
        """
        Validate and fetch source issue/comic navigation context for a NameRecordID.
        Ensures scope and existence are validated against the database.
        """
        cursor.execute(
            "SELECT IssueID, IsAnnual, ComicID, Role FROM ext_creator_credits WHERE NameRecordID = ? LIMIT 1",
            (name_record_id,)
        )
        c_row = cursor.fetchone()
        if not c_row:
            return None

        issue_id = c_row[0]
        is_annual = bool(c_row[1])
        comic_id = c_row[2]
        role = c_row[3]

        if not issue_id or not comic_id:
            return None

        # Validate existence against issues/annuals and comics
        table = "annuals" if is_annual else "issues"
        cursor.execute(f"SELECT Issue_Number, IssueName, IssueDate FROM {table} WHERE IssueID = ?", (issue_id,))
        iss_row = cursor.fetchone()
        if not iss_row:
            return None

        cursor.execute("SELECT ComicName, ComicYear FROM comics WHERE ComicID = ?", (comic_id,))
        com_row = cursor.fetchone()
        comic_name = com_row[0] if com_row else "Unknown Comic"

        return {
            'issue_id': str(issue_id),
            'is_annual': is_annual,
            'comic_id': str(comic_id),
            'comic_name': comic_name,
            'issue_number': str(iss_row[0]) if iss_row[0] is not None else "",
            'issue_name': iss_row[1] or "",
            'issue_date': iss_row[2] or "",
            'role': role
        }
