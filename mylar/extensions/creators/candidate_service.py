"""
Creator Identity Candidate Discovery Service (Phase C4.9).

Provides deterministic, read-only candidate discovery connecting locally indexed
creator credits with normalized provider issue snapshots without performing database
writes, network calls, or automatic entity linkage.
"""

from mylar import db, logger
from mylar.extensions.creators.normalizer import normalize_name, slugify
from mylar.extensions.creators.identity_repository import IdentityRepository
from mylar.extensions.creators.schema import ROLES


# -----------------------------------------------------------------------------
# Extension-Owned Candidate Exceptions
# -----------------------------------------------------------------------------

class CandidateDiscoveryError(Exception):
    """Base exception for all creator identity candidate discovery errors."""
    pass


class InvalidCandidateInputError(CandidateDiscoveryError):
    """Raised when an invalid issue ID, scope, or unsupported provider is supplied."""
    pass


class MalformedProviderSnapshotError(CandidateDiscoveryError):
    """Raised when the caller-supplied provider snapshot is missing or malformed."""
    pass


class LocalIssueNotFoundError(CandidateDiscoveryError):
    """Raised when the specified issue does not exist in the local database."""
    pass


# Supported provider namespaces
SUPPORTED_PROVIDERS = {'metron', 'comicvine', 'gcd'}

# Role priority order for deterministic local credit sorting
ROLE_PRIORITY = {
    'writer': 1,
    'penciller': 2,
    'inker': 3,
    'colorist': 4,
    'letterer': 5,
    'editor': 6,
    'cover_artist': 7,
    'other': 8,
}


def is_composite_credit(raw_name):
    """
    Check if a raw creator name string is a composite collaborative credit.
    Checks for conjunctions with word boundaries (' and ', ' & ', ' / ', ' with ', ' feat. ', ' featuring ').
    """
    if not raw_name or not isinstance(raw_name, str):
        return False
    lower = raw_name.lower().strip()
    delimiters = [' and ', ' & ', ' / ', '/', ' with ', ' feat. ', ' feat ', ' featuring ']
    for d in delimiters:
        if d in lower:
            return True
    return False


class CreatorCandidateService:
    """
    Read-only service for discovering and scoring creator identity candidates
    from normalized provider snapshots against locally indexed credits.
    """

    def __init__(self, db_conn=None, repository=None):
        self._db = db_conn
        self._repo = repository or IdentityRepository(db_conn=db_conn)

    def _get_db(self):
        if self._db:
            return self._db
        return db.DBConnection()

    def _validate_inputs(self, issue_id, is_annual, provider, provider_snapshot):
        """Validate input types and scopes."""
        # 1. Validate issue_id
        if issue_id is None:
            raise InvalidCandidateInputError("issue_id is required and cannot be None.")
        if isinstance(issue_id, bool):
            raise InvalidCandidateInputError("issue_id cannot be a boolean.")
        try:
            val_issue_id = str(int(str(issue_id).strip()))
            if int(val_issue_id) <= 0:
                raise ValueError()
        except (ValueError, TypeError):
            raise InvalidCandidateInputError(f"Invalid issue_id '{issue_id}': must be a positive integer or digit string.")

        # 2. Validate is_annual
        if is_annual in (1, '1', True, 'true', 'True'):
            val_annual = 1
        elif is_annual in (0, '0', False, 'false', 'False'):
            val_annual = 0
        else:
            raise InvalidCandidateInputError(f"Invalid is_annual scope '{is_annual}': must be 0 or 1.")

        # 3. Validate provider
        if not provider or not isinstance(provider, str) or not provider.strip():
            raise InvalidCandidateInputError("Provider namespace is required and cannot be empty.")
        clean_provider = provider.strip().lower()
        if clean_provider not in SUPPORTED_PROVIDERS:
            raise InvalidCandidateInputError(
                f"Unsupported provider namespace '{provider}'. Supported: {sorted(SUPPORTED_PROVIDERS)}"
            )

        # 4. Validate provider_snapshot
        if not isinstance(provider_snapshot, dict):
            raise MalformedProviderSnapshotError("provider_snapshot must be a dictionary.")
        if 'credits' not in provider_snapshot or not isinstance(provider_snapshot['credits'], list):
            raise MalformedProviderSnapshotError("provider_snapshot must contain a 'credits' list.")

        return val_issue_id, val_annual, clean_provider

    def _verify_local_issue_exists(self, issue_id, is_annual, cursor):
        """Verify that the target issue exists in local database."""
        table = 'annuals' if is_annual == 1 else 'issues'
        cursor.execute(f"SELECT COUNT(*) FROM {table} WHERE IssueID = ?", (str(issue_id),))
        row = cursor.fetchone()
        if not row or row[0] == 0:
            scope_desc = "Annual" if is_annual == 1 else "Issue"
            raise LocalIssueNotFoundError(f"Local {scope_desc} with IssueID {issue_id} was not found.")

    def _load_local_credits(self, issue_id, is_annual, cursor):
        """Load locally indexed creator credits and name records for the issue."""
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
        cursor.execute(query, (str(issue_id), int(is_annual)))
        rows = cursor.fetchall()
        credits = []
        for r in rows:
            credits.append({
                'credit_id': r[0],
                'name_record_id': r[1],
                'role': r[2],
                'raw_role_text': r[3],
                'raw_credit_name': r[4],
                'is_cover': bool(r[5]),
                'sort_order': r[6],
                'source_provenance': r[7],
                'raw_name': r[8],
                'normalized_name': r[9],
                'name_slug': r[10],
                'creator_entity_id': r[11],
                'resolution_source': r[12],
                'confirmed_entity_name': r[13],
                'confirmed_entity_slug': r[14],
            })
        return credits

    def build_candidates(self, issue_id, is_annual, provider, provider_snapshot):
        """
        Build reviewable identity candidates for an issue from an in-memory normalized provider snapshot.
        Stateless and 100% read-only with ZERO database writes and ZERO network requests.

        :param issue_id: Issue identifier (int or str)
        :param is_annual: 0 for regular issue, 1 for annual
        :param provider: Provider namespace ('metron', 'comicvine', etc.)
        :param provider_snapshot: Normalized issue snapshot dict with 'credits' list
        :return: Deterministic candidate discovery result dictionary
        """
        val_issue_id, val_annual, clean_provider = self._validate_inputs(
            issue_id, is_annual, provider, provider_snapshot
        )

        cursor, _ = self._repo._get_cursor()

        # Verify issue existence in local DB
        self._verify_local_issue_exists(val_issue_id, val_annual, cursor)

        # Load local credits
        local_credits_raw = self._load_local_credits(val_issue_id, val_annual, cursor)
        provider_credits_raw = list(provider_snapshot.get('credits') or [])

        # Process each local credit
        processed_local_credits = []
        flat_identity_candidates = []
        total_candidates_count = 0
        has_any_conflicts = False
        has_any_suppressed = False

        for l_cred in local_credits_raw:
            nr_id = l_cred['name_record_id']
            raw_name = l_cred['raw_name']
            norm_name = l_cred['normalized_name']
            local_role = l_cred['role']
            linked_entity_id = l_cred['creator_entity_id']
            is_composite = is_composite_credit(raw_name)

            candidates_pool = []
            distinct_provider_ids = set()

            # Compare against each provider credit in the snapshot
            for p_idx, p_cred in enumerate(provider_credits_raw):
                if not isinstance(p_cred, dict):
                    continue

                p_raw_name = str(p_cred.get('raw_creator_name') or p_cred.get('name') or '').strip()
                p_norm_name = normalize_name(p_raw_name)
                p_canonical_role = str(p_cred.get('canonical_role') or p_cred.get('role') or 'other').strip().lower()
                p_raw_role = str(p_cred.get('raw_role_text') or p_cred.get('raw_role') or p_canonical_role).strip()
                is_cover = bool(p_cred.get('is_cover_credit') or p_cred.get('is_cover'))

                # Extract provider creator ID
                p_creator_id = (
                    p_cred.get(f'{clean_provider}_creator_id') or
                    p_cred.get('provider_creator_id') or
                    p_cred.get('creator_id') or
                    p_cred.get('id')
                )
                if p_creator_id is not None:
                    p_creator_id = str(p_creator_id).strip()
                    if not p_creator_id:
                        p_creator_id = None

                # Evaluate name match
                exact_name_match = (raw_name.lower() == p_raw_name.lower())
                norm_name_match = (norm_name == p_norm_name)

                # If names don't match, this provider credit is not a candidate for this local credit
                if not norm_name_match and not exact_name_match:
                    continue

                # Build evidence codes and warnings for this match
                evidence_codes = []
                warnings = []
                score = 0.0

                if exact_name_match:
                    evidence_codes.append('exact_display_name_match')
                    score += 0.4
                if norm_name_match:
                    evidence_codes.append('normalized_name_match')
                    score += 0.3

                # Role evaluation
                if local_role == p_canonical_role:
                    evidence_codes.append('role_match')
                    score += 0.2
                else:
                    evidence_codes.append('role_difference')
                    warnings.append(f"Role discrepancy: local {local_role} vs provider {p_canonical_role}.")
                    score -= 0.1

                # Composite check
                if is_composite:
                    evidence_codes.append('composite_raw_credit')
                    warnings.append("Composite credit containing multiple creators; cannot be automatically resolved.")

                # Provider ID check
                if not p_creator_id:
                    evidence_codes.append('provider_id_missing')
                    warnings.append("Provider credit lacks an authoritative creator identifier.")
                    score -= 0.5
                else:
                    distinct_provider_ids.add(p_creator_id)

                # Database checks (read-only): external ID mappings & active rejections
                existing_entity_id = None
                existing_entity_name = None
                has_collision = False
                is_suppressed = False

                if p_creator_id:
                    # Check active rejection
                    is_suppressed = self._repo.get_active_rejection(
                        nr_id, clean_provider, p_creator_id, cursor=cursor
                    ) is not None

                    if is_suppressed:
                        evidence_codes.append('active_rejection')
                        warnings.append(f"Candidate {clean_provider}:{p_creator_id} is actively rejected for this local name.")
                        has_any_suppressed = True
                        score -= 0.5

                    # Check existing external ID mapping in DB
                    ext_mapping = self._repo.get_external_id_mapping(
                        clean_provider, p_creator_id, cursor=cursor
                    )
                    if ext_mapping:
                        existing_entity_id = ext_mapping['creator_entity_id']
                        evidence_codes.append('existing_external_id')
                        score += 0.1

                        # Get entity display name
                        entity_obj = self._repo.get_entity_by_id(existing_entity_id, cursor=cursor)
                        if entity_obj:
                            existing_entity_name = entity_obj['display_name']

                        # Collision check: local credit is already linked to Entity X, but provider ID maps to Entity Y
                        if linked_entity_id and linked_entity_id != existing_entity_id:
                            evidence_codes.append('external_id_collision')
                            warnings.append("Provider ID maps to a conflicting local creator entity.")
                            has_collision = True
                            has_any_conflicts = True
                            score -= 0.5

                    # Already confirmed check
                    if linked_entity_id and existing_entity_id == linked_entity_id:
                        evidence_codes.append('already_confirmed')

                # Check canonical state derivation from shared repository
                pairing_res = None
                if nr_id and p_creator_id and not is_composite:
                    pairing_res = self._repo.derive_pairing_state(nr_id, clean_provider, p_creator_id, cursor=cursor)

                if not p_creator_id or is_composite:
                    candidate_state = 'insufficient_provider_identity'
                    is_confirmed = False
                    is_rejected = False
                    is_conflicted = False
                    is_transferred = False
                    allow_confirmation = False
                    explanation = "Provider credit lacks an authoritative creator identifier or is composite."
                elif pairing_res and pairing_res['state'] == 'transferred':
                    candidate_state = 'transferred'
                    is_confirmed = False
                    is_rejected = False
                    is_conflicted = False
                    is_transferred = True
                    allow_confirmation = False
                    if 'transferred_mapping' not in evidence_codes:
                        evidence_codes.append('transferred_mapping')
                    explanation = pairing_res['explanation']
                elif pairing_res and pairing_res['state'] == 'rejected':
                    candidate_state = 'rejected'
                    is_confirmed = False
                    is_rejected = True
                    is_conflicted = False
                    is_transferred = False
                    allow_confirmation = False
                    explanation = pairing_res['explanation']
                elif pairing_res and pairing_res['state'] == 'conflicted':
                    candidate_state = 'conflicted'
                    is_confirmed = False
                    is_rejected = False
                    is_conflicted = True
                    is_transferred = False
                    allow_confirmation = False
                    explanation = pairing_res['explanation']
                elif pairing_res and pairing_res['state'] == 'confirmed':
                    candidate_state = 'confirmed'
                    is_confirmed = True
                    is_rejected = False
                    is_conflicted = False
                    is_transferred = False
                    allow_confirmation = False
                    if 'already_confirmed' not in evidence_codes:
                        evidence_codes.append('already_confirmed')
                    explanation = pairing_res['explanation']
                elif existing_entity_id is not None:
                    candidate_state = 'candidate'
                    is_confirmed = False
                    is_rejected = False
                    is_conflicted = False
                    is_transferred = False
                    allow_confirmation = True
                    explanation = f"Authoritative provider ID matches existing local entity #{existing_entity_id} ('{existing_entity_name or 'Unknown'}')."
                else:
                    candidate_state = 'candidate'
                    is_confirmed = False
                    is_rejected = False
                    is_conflicted = False
                    is_transferred = False
                    allow_confirmation = True
                    explanation = f"Discovered candidate on {clean_provider} ('{p_raw_name}') available for explicit user confirmation."

                cand_obj = {
                    'name_record_id': nr_id,
                    'raw_local_name': raw_name,
                    'local_role': local_role,
                    'provider': clean_provider,
                    'provider_creator_id': p_creator_id,
                    'provider_display_name': p_raw_name,
                    'provider_role': p_canonical_role,
                    'provider_raw_role': p_raw_role,
                    'is_cover_credit': is_cover,
                    'candidate_state': candidate_state,
                    'state': candidate_state,  # alias for backwards compatibility
                    'evidence': sorted(list(set(evidence_codes))),
                    'evidence_codes': sorted(list(set(evidence_codes))),  # alias
                    'warnings': sorted(list(set(warnings))),
                    'is_confirmed': is_confirmed,
                    'is_rejected': is_rejected,
                    'is_conflicted': is_conflicted,
                    'is_transferred': is_transferred,
                    'explanation': explanation,
                    'existing_creator_entity_id': existing_entity_id,
                    'existing_entity_name': existing_entity_name,
                    'allow_confirmation': allow_confirmation,
                    'is_suppressed': is_suppressed,
                    'score': round(score, 3),
                }

                candidates_pool.append(cand_obj)

            # Check if multiple distinct provider IDs matched this single local name
            if len(distinct_provider_ids) > 1:
                has_any_conflicts = True
                for cand in candidates_pool:
                    if cand['provider_creator_id']:
                        if 'multiple_provider_ids' not in cand['evidence']:
                            cand['evidence'].append('multiple_provider_ids')
                            cand['evidence'].sort()
                            cand['evidence_codes'] = list(cand['evidence'])
                        if 'Multiple provider records matched this local name.' not in cand['warnings']:
                            cand['warnings'].append('Multiple provider records matched this local name.')
                            cand['warnings'].sort()
                        if cand['candidate_state'] == 'candidate':
                            cand['is_conflicted'] = True

            # Deterministic sorting of candidates:
            # 1. Non-rejected before rejected
            # 2. Score Descending (-score)
            # 3. Candidate state Ascending
            # 4. Provider display name Ascending
            # 5. Provider creator ID Ascending
            candidates_pool.sort(
                key=lambda c: (
                    1 if c['is_rejected'] else 0,
                    -c['score'],
                    c['candidate_state'],
                    c['provider_display_name'],
                    str(c['provider_creator_id'] or '')
                )
            )

            total_candidates_count += len(candidates_pool)
            flat_identity_candidates.extend(candidates_pool)

            # Determine credit-level status
            if linked_entity_id:
                credit_status = 'confirmed'
                credit_explanation = f"Linked to confirmed entity #{linked_entity_id} ('{l_cred['confirmed_entity_name'] or 'Entity'}')."
            elif any(c['is_conflicted'] for c in candidates_pool) or len(distinct_provider_ids) > 1:
                credit_status = 'conflicted'
                credit_explanation = "Conflicting candidate evidence or multiple provider matches require manual review."
            elif any(c['allow_confirmation'] for c in candidates_pool):
                credit_status = 'has_candidates'
                credit_explanation = f"Found {len(candidates_pool)} candidate(s) for user review."
            elif all(c['is_rejected'] for c in candidates_pool) and len(candidates_pool) > 0:
                credit_status = 'suppressed'
                credit_explanation = "All discovered candidates are currently suppressed by active rejections."
            else:
                credit_status = 'no_candidates'
                credit_explanation = "No matching candidates found on provider."

            processed_local_credits.append({
                'credit_id': l_cred['credit_id'],
                'name_record_id': nr_id,
                'raw_name': raw_name,
                'normalized_name': norm_name,
                'name_slug': l_cred['name_slug'],
                'role': local_role,
                'raw_role_text': l_cred['raw_role_text'],
                'is_cover': l_cred['is_cover'],
                'sort_order': l_cred['sort_order'],
                'is_composite': is_composite,
                'creator_entity_id': linked_entity_id,
                'confirmed_entity_name': l_cred['confirmed_entity_name'],
                'resolution_source': l_cred['resolution_source'],
                'resolution_state': 'confirmed' if linked_entity_id else 'unlinked',
                'candidates': candidates_pool,
                'status': credit_status,
                'explanation': credit_explanation,
            })

        return {
            'success': True,
            'issue_id': str(val_issue_id),
            'is_annual': int(val_annual),
            'provider': clean_provider,
            'total_local_credits': len(processed_local_credits),
            'total_candidates': total_candidates_count,
            'has_conflicts': has_any_conflicts,
            'has_suppressed': has_any_suppressed,
            'identity_candidates': flat_identity_candidates,
            'local_credits': processed_local_credits,
        }
