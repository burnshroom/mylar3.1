"""
Creator Name Normalization and Role Mapping.
"""

import re
import unicodedata

SUFFIXES = {
    'jr.', 'jr', 'sr.', 'sr', 'ii', 'iii', 'iv', 'v', 'vi',
    'ph.d.', 'phd', 'm.d.', 'md', 'esq.', 'esq'
}

TAG_ROLE_MAP = {
    'writer': 'writer',
    'penciller': 'penciller',
    'inker': 'inker',
    'colorist': 'colorist',
    'letterer': 'letterer',
    'editor': 'editor',
    'coverartist': 'cover_artist',
    'cover_artist': 'cover_artist',
}


def normalize_name(raw_name):
    """
    Generate lowercase, unaccented search text using Unicode NFKD normalization.
    """
    if not raw_name:
        return ""
    # Strip leading/trailing whitespace
    name = raw_name.strip()
    # Normalize unicode characters (e.g. Pérez -> Perez)
    nfkd = unicodedata.normalize('NFKD', name)
    unaccented = ''.join(c for c in nfkd if not unicodedata.combining(c))
    # Convert to lowercase and collapse multiple whitespace
    normalized = re.sub(r'\s+', ' ', unaccented.lower()).strip()
    return normalized


def slugify(raw_name):
    """
    Generate URL/route-safe slug from a creator name.
    """
    norm = normalize_name(raw_name)
    slug = re.sub(r'[^a-z0-9]+', '-', norm).strip('-')
    return slug or 'creator'


def parse_creator_names(raw_tag_value):
    """
    Conservatively parse a raw ComicInfo creator tag string into individual name strings.

    Rules:
    1. Splits exclusively on commas (',') and semicolons (';').
    2. Protects suffixes (e.g. 'Jr.', 'III') by merging them into the preceding token.
    3. Never splits on 'and', '&', '/', or 'with'.
    4. Deduplicates byte-for-byte duplicate entries within the single tag string.
    5. Returns a list of trimmed raw name strings preserving source casing and characters.
    """
    if not raw_tag_value or not raw_tag_value.strip():
        return []

    # Split on comma or semicolon
    raw_tokens = re.split(r'[,;]+', raw_tag_value)
    merged_tokens = []

    for token in raw_tokens:
        clean = token.strip()
        if not clean:
            continue

        clean_lower = clean.lower().rstrip('.')
        if clean_lower in SUFFIXES or clean.lower() in SUFFIXES:
            if merged_tokens:
                # Merge suffix with previous name (e.g. "Mike Deodato" + "Jr." -> "Mike Deodato Jr.")
                merged_tokens[-1] = f"{merged_tokens[-1]}, {clean}"
            else:
                merged_tokens.append(clean)
        else:
            merged_tokens.append(clean)

    # Deduplicate preserving order
    seen = set()
    result = []
    for name in merged_tokens:
        if name not in seen:
            seen.add(name)
            result.append(name)

    return result


def map_role(tag_name):
    """
    Map XML tag name (e.g. 'Writer', 'CoverArtist') to controlled role vocabulary.
    """
    if not tag_name:
        return 'other'
    clean = tag_name.strip().lower()
    return TAG_ROLE_MAP.get(clean, 'other')
