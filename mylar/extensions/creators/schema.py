"""
Creator Extension Schema Constants and Definitions.
"""

ROLES = (
    'writer',
    'penciller',
    'inker',
    'colorist',
    'letterer',
    'editor',
    'cover_artist',
    'other',
)

SCAN_STATUSES = (
    'scanned_with_credits',
    'scanned_no_credits',
    'no_comicinfo',
    'inaccessible_or_unsupported',
    'pending_or_stale',
)

PROVENANCE_COMICINFO = 'comicinfo'
PROVENANCE_METRON = 'metron'
PROVENANCE_COMICVINE = 'comicvine'

RESOLUTION_UNRESOLVED = 'unresolved'
RESOLUTION_MANUAL = 'manual_user'
RESOLUTION_METRON = 'provider_metron'
RESOLUTION_COMICVINE = 'provider_comicvine'

# Maximum allowed ComicInfo.xml size in bytes (1 MB)
MAX_COMICINFO_BYTES = 1024 * 1024
