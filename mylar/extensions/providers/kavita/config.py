"""
Kavita Configuration Module (Phase K2).

Provides secure, extension-owned configuration loading, URL validation, and credential construction
from mylar.CONFIG without secret leakage.
Stateless and read-only with ZERO network activity on import or execution.
"""

import urllib.parse
import mylar
from mylar import logger
from mylar.extensions.providers.kavita.auth import (
    KavitaCredentials,
    KavitaConfigurationError
)


def validate_kavita_url(url):
    """
    Validate and normalize a Kavita server base URL.

    Requirements:
    - Accept only http or https schemes.
    - Must include a valid network location (host).
    - Reject credentials (user:pass@).
    - Reject query strings (?...) and fragments (#...).
    - Normalize with a single trailing slash.

    :param url: Raw URL string
    :return: Normalized URL string ending with '/'
    :raises KavitaConfigurationError: If URL is malformed or invalid
    """
    if not url or not isinstance(url, str) or not url.strip():
        raise KavitaConfigurationError("Kavita server URL cannot be empty.", error_code='missing_url')

    clean_url = url.strip()
    try:
        parsed = urllib.parse.urlparse(clean_url)
    except Exception as e:
        raise KavitaConfigurationError(f"Invalid Kavita server URL: {e}", error_code='invalid_url')

    scheme = parsed.scheme.lower()
    if scheme not in ('http', 'https'):
        raise KavitaConfigurationError(
            f"Invalid URL scheme '{parsed.scheme}'. Only 'http' and 'https' are supported.",
            error_code='invalid_scheme'
        )

    if not parsed.netloc:
        raise KavitaConfigurationError("Kavita server URL must include a valid host.", error_code='missing_host')

    if parsed.username or parsed.password:
        raise KavitaConfigurationError(
            "Kavita server URL must not contain embedded username or password credentials.",
            error_code='credentials_in_url'
        )

    if parsed.query:
        raise KavitaConfigurationError(
            "Kavita server URL must not contain query parameters.",
            error_code='query_in_url'
        )

    if parsed.fragment:
        raise KavitaConfigurationError(
            "Kavita server URL must not contain URL fragments.",
            error_code='fragment_in_url'
        )

    # Normalize path ensuring single trailing slash
    path = parsed.path.rstrip('/') + '/'
    normalized = urllib.parse.urlunparse((scheme, parsed.netloc, path, '', '', ''))
    return normalized


def get_kavita_config_status():
    """
    Return sanitized Kavita configuration status dictionary without exposing any secrets.
    """
    enabled = bool(getattr(mylar.CONFIG, 'KAVITA_ENABLED', False))
    raw_url = str(getattr(mylar.CONFIG, 'KAVITA_URL', '') or '').strip()
    has_api_key = bool(getattr(mylar.CONFIG, 'KAVITA_API_KEY', None))

    is_configured = bool(raw_url and has_api_key)

    return {
        'enabled': enabled,
        'url': raw_url,
        'has_api_key': has_api_key,
        'is_configured': is_configured
    }


def get_kavita_availability():
    """
    Return non-secret Kavita availability status.
    Performs ZERO external network requests.
    """
    status = get_kavita_config_status()
    enabled = bool(status['enabled'])
    configured = bool(status['is_configured'])
    return {
        'enabled': enabled,
        'configured': configured,
        'available': bool(enabled and configured)
    }


def build_kavita_credentials(api_key=None):
    """
    Validate and construct a KavitaCredentials object from supplied parameters or stored configuration.
    Fails closed when configuration is incomplete or invalid.
    Performs ZERO network requests.

    :param api_key: Optional API key override
    :return: KavitaCredentials instance
    :raises KavitaConfigurationError: If API key is missing or empty
    """
    key_val = api_key if api_key is not None else getattr(mylar.CONFIG, 'KAVITA_API_KEY', None)
    if not key_val or not str(key_val).strip():
        raise KavitaConfigurationError(
            "Kavita API key is required and cannot be empty.",
            error_code='missing_api_key'
        )

    return KavitaCredentials(api_key=str(key_val).strip())
