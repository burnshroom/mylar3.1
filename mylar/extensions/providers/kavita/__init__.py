"""
Kavita Provider Extension (Phase K2 & K4).

Provides secure, opt-in integration with Kavita readers and servers.
Stateless and read-only with zero network activity on package import.
"""

from mylar.extensions.providers.kavita.auth import (
    KavitaCredentials,
    KavitaConfigurationError
)
from mylar.extensions.providers.kavita.config import (
    validate_kavita_url,
    get_kavita_config_status,
    get_kavita_availability,
    build_kavita_credentials
)
from mylar.extensions.providers.kavita.client import (
    KavitaClient,
    KavitaError,
    KavitaAuthenticationError,
    KavitaTimeoutError,
    KavitaTransportError,
    KavitaInvalidResponseError,
    KavitaIncompatibleError,
    KavitaPermissionError,
    KavitaInvalidRequestError,
    KavitaProviderDisabledError
)
from mylar.extensions.providers.kavita.service import (
    KavitaConnectionService
)
from mylar.extensions.providers.kavita.discovery import (
    KavitaDiscoveryService
)
from mylar.extensions.providers.kavita.runtime_controller import (
    handle_test_kavita,
    handle_kavita_diagnostics,
    handle_kavita_config_update
)
from mylar.extensions.providers.kavita.publisher_service import (
    KavitaPublisherService,
    handle_post_processing_kavita_automation,
    get_kavita_publisher_mappings,
    get_latest_automation_notice,
    derive_materialized_publisher_root,
    normalize_path_str
)

__all__ = [
    'KavitaCredentials',
    'KavitaConfigurationError',
    'validate_kavita_url',
    'get_kavita_config_status',
    'get_kavita_availability',
    'build_kavita_credentials',
    'KavitaClient',
    'KavitaError',
    'KavitaAuthenticationError',
    'KavitaTimeoutError',
    'KavitaTransportError',
    'KavitaInvalidResponseError',
    'KavitaIncompatibleError',
    'KavitaPermissionError',
    'KavitaInvalidRequestError',
    'KavitaProviderDisabledError',
    'KavitaConnectionService',
    'KavitaDiscoveryService',
    'handle_test_kavita',
    'handle_kavita_diagnostics',
    'handle_kavita_config_update',
    'KavitaPublisherService',
    'handle_post_processing_kavita_automation',
    'get_kavita_publisher_mappings',
    'get_latest_automation_notice',
    'derive_materialized_publisher_root',
    'normalize_path_str',
]
