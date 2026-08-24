"""
Metron Configuration Module (Phase C4.4).

Provides secure, extension-owned configuration loading, validation, and credential construction
from mylar.CONFIG without secret leakage.
Stateless and read-only with ZERO network activity on import or execution.
"""

import mylar
from mylar import logger
from mylar.extensions.providers.metron.auth import (
    MetronCredentials,
    AUTH_MODE_TOKEN,
    AUTH_MODE_BASIC
)
from mylar.extensions.providers.metron.client import MetronInvalidRequestError

CANONICAL_METRON_BASE_URL = 'https://metron.cloud/api/'
VALID_AUTH_MODES = ('token', 'basic')


def get_metron_config_status():
    """
    Return sanitized Metron configuration status dictionary without exposing any secrets.
    """
    enabled = bool(getattr(mylar.CONFIG, 'METRON_ENABLED', False))
    auth_mode = str(getattr(mylar.CONFIG, 'METRON_AUTH_MODE', 'token') or 'token').strip().lower()
    has_token = bool(getattr(mylar.CONFIG, 'METRON_API_TOKEN', None))
    has_username = bool(getattr(mylar.CONFIG, 'METRON_USERNAME', None))
    has_password = bool(getattr(mylar.CONFIG, 'METRON_PASSWORD', None))

    is_configured = False
    if auth_mode == 'token' and has_token:
        is_configured = True
    elif auth_mode == 'basic' and has_username and has_password:
        is_configured = True

    return {
        'enabled': enabled,
        'auth_mode': auth_mode if auth_mode in VALID_AUTH_MODES else 'token',
        'has_token': has_token,
        'has_username': has_username,
        'has_password': has_password,
        'base_url': CANONICAL_METRON_BASE_URL,
        'is_configured': is_configured
    }


def get_metron_availability():
    """
    Return non-secret Metron availability status.
    Performs ZERO external network requests.
    """
    status = get_metron_config_status()
    enabled = bool(status['enabled'])
    configured = bool(status['is_configured'])
    return {
        'enabled': enabled,
        'configured': configured,
        'available': bool(enabled and configured)
    }


def build_metron_credentials(auth_mode=None, api_token=None, username=None, password=None):
    """
    Validate and construct a MetronCredentials object from supplied parameters or stored configuration.
    Fails closed when configuration is incomplete or invalid.
    Performs ZERO network requests.

    :param auth_mode: 'token' or 'basic' (defaults to stored METRON_AUTH_MODE)
    :param api_token: Optional token override
    :param username: Optional username override
    :param password: Optional password override
    :return: MetronCredentials instance
    :raises MetronInvalidRequestError: If authentication mode is invalid or required credentials are missing
    """
    mode = auth_mode if auth_mode is not None else getattr(mylar.CONFIG, 'METRON_AUTH_MODE', 'token')
    if not mode or not isinstance(mode, str):
        raise MetronInvalidRequestError("Invalid Metron authentication mode. Must be 'token' or 'basic'.")

    mode_clean = mode.strip().lower()
    if mode_clean not in VALID_AUTH_MODES:
        raise MetronInvalidRequestError(f"Unsupported Metron authentication mode: '{mode}'. Must be 'token' or 'basic'.")

    if mode_clean == 'token':
        # Use provided token or fallback to stored token
        token_val = api_token if api_token is not None else getattr(mylar.CONFIG, 'METRON_API_TOKEN', None)
        if not token_val or not str(token_val).strip():
            raise MetronInvalidRequestError("Metron API token is required for token authentication mode.")

        return MetronCredentials(mode=AUTH_MODE_TOKEN, token=str(token_val).strip())

    elif mode_clean == 'basic':
        user_val = username if username is not None else getattr(mylar.CONFIG, 'METRON_USERNAME', None)
        pass_val = password if password is not None else getattr(mylar.CONFIG, 'METRON_PASSWORD', None)

        if not user_val or not str(user_val).strip():
            raise MetronInvalidRequestError("Metron username is required for Basic authentication mode.")
        if not pass_val or not str(pass_val).strip():
            raise MetronInvalidRequestError("Metron password is required for Basic authentication mode.")

        return MetronCredentials(mode=AUTH_MODE_BASIC, username=str(user_val).strip(), password=str(pass_val).strip())

    raise MetronInvalidRequestError("Invalid Metron authentication mode.")
