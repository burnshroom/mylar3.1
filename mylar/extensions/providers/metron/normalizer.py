"""
Metron Issue and Creator Credit Normalizer (Phase C4.2).

Provides deterministic, provenance-preserving normalization for Metron issue records
and creator credits without database writes or entity linking.
Strictly adheres to the approved canonical role mappings from metron_capability_audit.md.
"""

CANONICAL_ROLES = {
    'writer',
    'penciller',
    'inker',
    'colorist',
    'letterer',
    'editor',
    'cover_artist',
    'other'
}

# Role mapping table strictly limited to confirmed mappings in metron_capability_audit.md
ROLE_MAPPING = {
    # Writer
    'writer': 'writer',
    'script': 'writer',
    'plot': 'writer',

    # Penciller
    'penciller': 'penciller',
    'pencils': 'penciller',

    # Inker
    'inker': 'inker',
    'inks': 'inker',

    # Colorist
    'colorist': 'colorist',
    'colors': 'colorist',

    # Letterer
    'letterer': 'letterer',
    'letters': 'letterer',

    # Editor
    'editor': 'editor',
    'editing': 'editor',
    'assistant editor': 'editor',

    # Cover Artist
    'cover': 'cover_artist',
    'cover artist': 'cover_artist',
    'variant cover': 'cover_artist',
}


def normalize_role(raw_role_str):
    """
    Map raw provider role text to an approved canonical role.
    Strictly checks the approved ROLE_MAPPING table; any unconfirmed role maps to 'other'.

    :param raw_role_str: Raw role string from provider
    :return: tuple (canonical_role, is_cover_credit)
    """
    if not raw_role_str or not isinstance(raw_role_str, str):
        return ('other', False)

    clean = raw_role_str.strip().lower()
    canonical = ROLE_MAPPING.get(clean, 'other')
    is_cover = (canonical == 'cover_artist')

    return (canonical, is_cover)


def normalize_metron_credit(credit_entry, order=0):
    """
    Normalize an individual Metron credit entry defensively.
    Preserves provider creator IDs, raw name, raw role, canonical role, and order.
    Losslessly retains multi-valued role lists and avoids heuristic cover inference.

    :param credit_entry: Credit dictionary from Metron issue response
    :param order: Credit sequence index
    :return: tuple (normalized_credit_dict, warning_str_or_None)
    """
    if not isinstance(credit_entry, dict):
        return (None, f"Malformed credit entry at index {order}: not a dictionary.")

    # Extract creator information
    creator_info = credit_entry.get('creator')
    metron_creator_id = None
    cv_creator_id = None
    gcd_creator_id = None
    raw_creator_name = None

    if isinstance(creator_info, dict):
        metron_creator_id = creator_info.get('id')
        cv_creator_id = creator_info.get('cv_id')
        gcd_creator_id = creator_info.get('gcd_id')
        raw_creator_name = creator_info.get('name')
    elif isinstance(creator_info, str):
        raw_creator_name = creator_info.strip()
        metron_creator_id = credit_entry.get('creator_id') or credit_entry.get('id')
    else:
        # Fallback to top-level fields
        raw_creator_name = credit_entry.get('name') or credit_entry.get('creator_name')
        metron_creator_id = credit_entry.get('creator_id') or credit_entry.get('id')
        cv_creator_id = credit_entry.get('cv_id')
        gcd_creator_id = credit_entry.get('gcd_id')

    if not raw_creator_name or not isinstance(raw_creator_name, str) or not raw_creator_name.strip():
        return (None, f"Malformed credit entry at index {order}: missing creator name.")

    # Extract role information losslessly
    role_info = credit_entry.get('role')
    raw_role_text = "Unknown"
    warning = None

    if isinstance(role_info, dict):
        raw_role_text = str(role_info.get('name') or "Unknown").strip()
        canonical_role, is_cover = normalize_role(raw_role_text)
    elif isinstance(role_info, str):
        raw_role_text = role_info.strip()
        canonical_role, is_cover = normalize_role(raw_role_text)
    elif isinstance(role_info, (list, tuple)):
        extracted_roles = []
        for r_item in role_info:
            if isinstance(r_item, dict):
                r_name = r_item.get('name')
                if r_name and str(r_name).strip():
                    extracted_roles.append(str(r_name).strip())
            elif isinstance(r_item, str) and r_item.strip():
                extracted_roles.append(r_item.strip())

        if len(extracted_roles) == 1:
            raw_role_text = extracted_roles[0]
            canonical_role, is_cover = normalize_role(raw_role_text)
        elif len(extracted_roles) > 1:
            raw_role_text = "; ".join(extracted_roles)
            canonical_role = 'other'
            is_cover = False
            warning = f"Credit entry at index {order} contains multiple provider roles; raw roles were retained without canonical expansion."
        else:
            raw_role_text = "Unknown"
            canonical_role = 'other'
            is_cover = False
    else:
        canonical_role, is_cover = normalize_role(raw_role_text)

    # Explicit cover indicator override if supplied as boolean True by provider
    if credit_entry.get('is_cover') is True or credit_entry.get('cover') is True:
        is_cover = True

    normalized_credit = {
        'metron_creator_id': int(metron_creator_id) if metron_creator_id is not None and str(metron_creator_id).isdigit() else None,
        'comicvine_creator_id': int(cv_creator_id) if cv_creator_id is not None and str(cv_creator_id).isdigit() else None,
        'gcd_creator_id': int(gcd_creator_id) if gcd_creator_id is not None and str(gcd_creator_id).isdigit() else None,
        'raw_creator_name': str(raw_creator_name).strip(),
        'raw_role_text': str(raw_role_text).strip(),
        'canonical_role': canonical_role,
        'order': int(order),
        'is_cover_credit': bool(is_cover),
        'source_provenance': 'metron'
    }

    return (normalized_credit, warning)


def normalize_metron_issue(raw_issue_data, query_comicvine_issue_id, response_shape='object', observed_result_count=1):
    """
    Transform raw Metron issue JSON payload into a clean, deterministic issue snapshot.

    :param raw_issue_data: dict representing a single Metron issue
    :param query_comicvine_issue_id: Authoritative ComicVine IssueID queried
    :param response_shape: Observed response shape classification ('paginated', 'list', 'object')
    :param observed_result_count: Observed result count from inspected payload
    :return: dict (normalized issue snapshot)
    """
    if not isinstance(raw_issue_data, dict):
        raise ValueError("raw_issue_data must be a dictionary")

    # Series extraction
    raw_series = raw_issue_data.get('series')
    series_dict = {
        'metron_series_id': None,
        'comicvine_series_id': None,
        'name': None,
        'volume_year': None,
        'volume': None,
        'publisher': None,
        'imprint': None
    }

    if isinstance(raw_series, dict):
        series_dict['metron_series_id'] = raw_series.get('id')
        series_dict['comicvine_series_id'] = raw_series.get('cv_id')
        series_dict['name'] = raw_series.get('name')
        series_dict['volume_year'] = raw_series.get('year_began') or raw_series.get('volume_year')
        series_dict['volume'] = raw_series.get('volume')

        pub = raw_series.get('publisher')
        if isinstance(pub, dict):
            series_dict['publisher'] = pub.get('name')
        elif isinstance(pub, str):
            series_dict['publisher'] = pub.strip()

        imp = raw_series.get('imprint')
        if isinstance(imp, dict):
            series_dict['imprint'] = imp.get('name')
        elif isinstance(imp, str):
            series_dict['imprint'] = imp.strip()

    elif isinstance(raw_series, str):
        series_dict['name'] = raw_series.strip()

    # Credits extraction and normalization
    raw_credits = (
        raw_issue_data.get('credits') or
        raw_issue_data.get('creators') or
        raw_issue_data.get('credit_set') or
        []
    )

    normalized_credits = []
    warnings = []

    if isinstance(raw_credits, list):
        for idx, entry in enumerate(raw_credits):
            norm_cred, warning = normalize_metron_credit(entry, order=idx)
            if warning:
                warnings.append(warning)
            if norm_cred:
                normalized_credits.append(norm_cred)
    else:
        warnings.append("Credits field in issue record is not a list.")

    # Page count conversion
    raw_page = raw_issue_data.get('page') or raw_issue_data.get('page_count')
    page_count = None
    if raw_page is not None and str(raw_page).isdigit():
        page_count = int(raw_page)

    return {
        'provider': 'metron',
        'query_comicvine_issue_id': int(query_comicvine_issue_id),
        'metron_issue_id': raw_issue_data.get('id'),
        'comicvine_issue_id': raw_issue_data.get('cv_id'),
        'gcd_issue_id': raw_issue_data.get('gcd_id'),
        'series': series_dict,
        'number': str(raw_issue_data.get('number')) if raw_issue_data.get('number') is not None else None,
        'title': str(raw_issue_data.get('title')) if raw_issue_data.get('title') is not None else None,
        'cover_date': str(raw_issue_data.get('cover_date')) if raw_issue_data.get('cover_date') is not None else None,
        'store_date': str(raw_issue_data.get('store_date')) if raw_issue_data.get('store_date') is not None else None,
        'page_count': page_count,
        'description': str(raw_issue_data.get('desc') or raw_issue_data.get('description') or raw_issue_data.get('summary')) if (raw_issue_data.get('desc') or raw_issue_data.get('description') or raw_issue_data.get('summary')) is not None else None,
        'credits': normalized_credits,
        'response_shape': str(response_shape),
        'observed_result_count': int(observed_result_count),
        'warnings': warnings
    }
