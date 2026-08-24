"""
Metron Provider Extension (Phase C4.1, C4.2, C4.4 & C4.5).

Public API exports for the Metron metadata provider foundation, issue comparison core,
configuration / connection test controller, in-memory cache, and runtime comparison service.
"""

from mylar.extensions.providers.metron.auth import (
    MetronCredentials,
    MetronConfigurationError,
    AUTH_MODE_TOKEN,
    AUTH_MODE_BASIC,
    AUTH_MODE_NONE
)
from mylar.extensions.providers.metron.client import (
    MetronClient,
    MetronResponse,
    MetronError,
    MetronAuthenticationError,
    MetronRateLimitError,
    MetronTimeoutError,
    MetronTransportError,
    MetronUpstreamError,
    MetronInvalidResponseError,
    MetronNotFoundError,
    MetronInvalidRequestError
)
from mylar.extensions.providers.metron.service import MetronConnectionService
from mylar.extensions.providers.metron.controller import probe_connection_controller
from mylar.extensions.providers.metron.normalizer import (
    normalize_metron_issue,
    normalize_metron_credit,
    normalize_role,
    CANONICAL_ROLES,
    ROLE_MAPPING
)
from mylar.extensions.providers.metron.comparison import (
    compare_with_local_credits,
    CREDIT_COMPARISON_DISCLAIMER
)
from mylar.extensions.providers.metron.issue_service import (
    MetronIssueService,
    MetronAmbiguousResultError,
    validate_comicvine_issue_id
)
from mylar.extensions.providers.metron.config import (
    get_metron_config_status,
    get_metron_availability,
    build_metron_credentials,
    CANONICAL_METRON_BASE_URL
)
from mylar.extensions.providers.metron.cache import (
    MetronIssueCache,
    get_metron_cache,
    DEFAULT_CACHE_TTL,
    DEFAULT_MAX_ENTRIES
)
from mylar.extensions.providers.metron.comparison_service import (
    MetronComparisonService,
    MetronLocalIssueNotFoundError,
    MetronProviderDisabledError,
    validate_annual_scope
)
from mylar.extensions.providers.metron.runtime_controller import (
    handle_test_metron,
    handle_metron_compare_credits
)

__all__ = [
    'MetronCredentials',
    'MetronConfigurationError',
    'AUTH_MODE_TOKEN',
    'AUTH_MODE_BASIC',
    'AUTH_MODE_NONE',
    'MetronClient',
    'MetronResponse',
    'MetronError',
    'MetronAuthenticationError',
    'MetronRateLimitError',
    'MetronTimeoutError',
    'MetronTransportError',
    'MetronUpstreamError',
    'MetronInvalidResponseError',
    'MetronNotFoundError',
    'MetronInvalidRequestError',
    'MetronConnectionService',
    'probe_connection_controller',
    'normalize_metron_issue',
    'normalize_metron_credit',
    'normalize_role',
    'CANONICAL_ROLES',
    'ROLE_MAPPING',
    'compare_with_local_credits',
    'CREDIT_COMPARISON_DISCLAIMER',
    'MetronIssueService',
    'MetronAmbiguousResultError',
    'validate_comicvine_issue_id',
    'get_metron_config_status',
    'get_metron_availability',
    'build_metron_credentials',
    'CANONICAL_METRON_BASE_URL',
    'MetronIssueCache',
    'get_metron_cache',
    'DEFAULT_CACHE_TTL',
    'DEFAULT_MAX_ENTRIES',
    'MetronComparisonService',
    'MetronLocalIssueNotFoundError',
    'MetronProviderDisabledError',
    'validate_annual_scope',
    'handle_test_metron',
    'handle_metron_compare_credits'
]
