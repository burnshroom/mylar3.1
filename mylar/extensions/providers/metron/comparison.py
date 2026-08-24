"""
Metron In-Memory Credit Comparison Engine (Phase C4.2).

Performs strict observational credit concordance comparisons between Metron provider snapshots
and caller-supplied local credits in memory.
Performs ZERO database queries, ZERO identity linking, and ZERO entity modifications.
"""

CREDIT_COMPARISON_DISCLAIMER = (
    "Matched by normalized display name and role only. This is not creator identity "
    "confirmation. No creator records were linked or modified."
)


def _normalize_local_credit(cred, index=0):
    """
    Safely extract standardized fields from a caller-supplied local credit dictionary or object.
    Always includes 'original_credit' to prevent KeyError on non-dictionary inputs.
    """
    if not isinstance(cred, dict):
        return {
            'raw_creator_name': str(cred).strip() if cred is not None else '',
            'raw_role_text': 'Unknown',
            'canonical_role': 'other',
            'is_cover_credit': False,
            'source_provenance': 'local',
            'index': index,
            'original_credit': cred
        }

    raw_name = cred.get('raw_creator_name') or cred.get('raw_name') or cred.get('name') or cred.get('CreatorName') or ''
    canonical_role = cred.get('canonical_role') or cred.get('role') or cred.get('Role') or 'other'
    raw_role = cred.get('raw_role_text') or cred.get('raw_role') or cred.get('RawRoleText') or str(canonical_role)
    is_cover = bool(cred.get('is_cover_credit') or cred.get('is_cover') or cred.get('IsCover'))

    return {
        'raw_creator_name': str(raw_name).strip(),
        'raw_role_text': str(raw_role).strip(),
        'canonical_role': str(canonical_role).strip().lower(),
        'is_cover_credit': is_cover,
        'source_provenance': 'local',
        'index': index,
        'original_credit': cred
    }


def compare_with_local_credits(provider_snapshot, local_credits):
    """
    Compare provider issue credits against caller-supplied local credits.

    :param provider_snapshot: Normalized issue snapshot dictionary returned by MetronIssueService
    :param local_credits: list of local credit dictionaries or objects
    :return: dict containing structured comparison results
    """
    if not isinstance(provider_snapshot, dict):
        raise ValueError("provider_snapshot must be a dictionary")

    raw_local = list(local_credits or [])
    norm_local = [_normalize_local_credit(c, idx) for idx, c in enumerate(raw_local)]
    provider_credits = list(provider_snapshot.get('credits') or [])

    exact_overlaps = []
    local_only = []
    provider_only = []
    role_discrepancies = []

    matched_local_indices = set()
    matched_provider_indices = set()

    # 1. Identify exact overlaps: exact raw creator name (case-sensitive) AND canonical role
    for p_idx, p_cred in enumerate(provider_credits):
        p_name = p_cred.get('raw_creator_name', '')
        p_role = p_cred.get('canonical_role', '').lower()

        matched = False
        for l_idx, l_cred in enumerate(norm_local):
            if l_idx in matched_local_indices:
                continue

            l_name = l_cred.get('raw_creator_name', '')
            l_role = l_cred.get('canonical_role', '').lower()

            # Exact match: strict string equality on name and role
            if p_name == l_name and p_role == l_role:
                exact_overlaps.append({
                    'creator_name': p_name,
                    'canonical_role': p_role,
                    'local_credit': l_cred['original_credit'],
                    'provider_credit': p_cred
                })
                matched_local_indices.add(l_idx)
                matched_provider_indices.add(p_idx)
                matched = True
                break

    # 2. Identify unmatched provider-only and local-only credits
    for p_idx, p_cred in enumerate(provider_credits):
        if p_idx not in matched_provider_indices:
            provider_only.append(p_cred)

    for l_idx, l_cred in enumerate(norm_local):
        if l_idx not in matched_local_indices:
            local_only.append(l_cred['original_credit'])

    # 3. Identify role discrepancies among unmatched credits where creator names match exactly
    # but canonical roles differ
    for p_cred in provider_only:
        p_name = p_cred.get('raw_creator_name', '')
        p_role = p_cred.get('canonical_role', '').lower()

        for l_cred in norm_local:
            l_name = l_cred.get('raw_creator_name', '')
            l_role = l_cred.get('canonical_role', '').lower()

            if p_name == l_name and p_role != l_role:
                role_discrepancies.append({
                    'creator_name': p_name,
                    'local_canonical_role': l_role,
                    'provider_canonical_role': p_role,
                    'local_credit': l_cred['original_credit'],
                    'provider_credit': p_cred
                })

    return {
        'provider_snapshot': provider_snapshot,
        'local_credits': raw_local,
        'exact_overlaps': exact_overlaps,
        'local_only_credits': local_only,
        'provider_only_credits': provider_only,
        'role_discrepancies': role_discrepancies,
        'normalization_warnings': list(provider_snapshot.get('warnings') or []),
        'disclaimer': CREDIT_COMPARISON_DISCLAIMER
    }
