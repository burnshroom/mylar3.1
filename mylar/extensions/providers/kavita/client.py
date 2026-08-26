"""
Kavita HTTP Client Foundation (Phase K2).

Handles authenticated read-only communication with the Kavita API.
Enforces strict Kavita authentication protocols:
- Pure Auth Key authentication via 'x-api-key' request header ONLY.
- Saved API key is NEVER sent in URLs, query strings, request bodies, or Authorization headers.
- No Authorization headers are ever generated or emitted.
- No temporary JWT tokens are generated, parsed, or retained.
- Never logs, returns, or leaks raw API keys.
- Enforces strict URL validation, bounded timeouts, query key filtering, and sanitized errors.
"""

import urllib.parse
import requests
from mylar import logger
from mylar.extensions.providers.kavita.auth import (
    KavitaCredentials,
    KavitaConfigurationError
)
from mylar.extensions.providers.kavita.config import validate_kavita_url

DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 10.0
DEFAULT_USER_AGENT = 'Mylar3/1.0 (Kavita Provider Client)'

FORBIDDEN_QUERY_KEYS = {
    'token',
    'access_token',
    'api_key',
    'apikey',
    'password',
    'secret',
    'authorization'
}


class KavitaError(Exception):
    """Base exception for all Kavita provider errors."""
    def __init__(self, message="Kavita provider error.", error_code='provider_error', status_code=None):
        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code


class KavitaAuthenticationError(KavitaError):
    """Raised when authentication fails (HTTP 401). Zero retries permitted."""
    def __init__(self, message="Authentication failed. Check Kavita API key.", status_code=401):
        super().__init__(message, error_code='authentication_failed', status_code=status_code)


class KavitaPermissionError(KavitaError):
    """Raised when access is forbidden or lacks admin permissions (HTTP 403)."""
    def __init__(self, message="Access forbidden. Ensure API key has sufficient permissions.", status_code=403):
        super().__init__(message, error_code='permission_denied', status_code=status_code)


class KavitaTimeoutError(KavitaError):
    """Raised when connection or read timeouts occur."""
    def __init__(self, message="Request to Kavita server timed out.", timeout_type='read'):
        super().__init__(message, error_code='timeout')
        self.timeout_type = timeout_type


class KavitaTransportError(KavitaError):
    """Raised when TLS, DNS, or network connection errors occur."""
    def __init__(self, message="Network/transport error connecting to Kavita server."):
        super().__init__(message, error_code='transport_error')


class KavitaInvalidResponseError(KavitaError):
    """Raised when Kavita returns a malformed or non-JSON response payload."""
    def __init__(self, message="Received invalid or non-JSON response from Kavita server.", status_code=200):
        super().__init__(message, error_code='invalid_response', status_code=status_code)


class KavitaIncompatibleError(KavitaError):
    """Raised when Kavita server or API contract is incompatible (e.g. missing comic library type)."""
    def __init__(self, message="Kavita server or API contract is incompatible with Mylar.", error_code='incompatible_library_type'):
        super().__init__(message, error_code=error_code)


class KavitaInvalidRequestError(KavitaError):
    """Raised when request parameters or relative endpoint are malformed."""
    def __init__(self, message="Invalid Kavita API endpoint or parameters.", status_code=400):
        super().__init__(message, error_code='invalid_request', status_code=status_code)


class KavitaProviderDisabledError(KavitaError):
    """Raised when Kavita integration is disabled in configuration."""
    def __init__(self, message="Kavita integration is disabled in configuration."):
        super().__init__(message, error_code='provider_disabled')


def validate_relative_endpoint(endpoint):
    """
    Validate relative endpoint path.
    Rejects absolute URLs, protocol-relative URLs, path traversal, and forbidden query parameters.

    :param endpoint: Relative endpoint string
    :return: Sanitized endpoint path
    :raises KavitaInvalidRequestError: If endpoint is invalid or insecure
    """
    if not endpoint or not isinstance(endpoint, str):
        raise KavitaInvalidRequestError("API endpoint path must be a non-empty string.")

    cleaned = endpoint.strip()
    if cleaned.startswith(('http://', 'https://', '//', '\\')):
        raise KavitaInvalidRequestError("Absolute URLs or protocol-relative paths are forbidden in endpoint parameter.")

    parsed = urllib.parse.urlsplit(cleaned)
    if '..' in parsed.path.split('/'):
        raise KavitaInvalidRequestError("Path traversal sequences are forbidden in API endpoint.")

    if parsed.query:
        query_params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        for key in query_params:
            if key.lower() in FORBIDDEN_QUERY_KEYS:
                raise KavitaInvalidRequestError(
                    f"Forbidden query parameter '{key}' in API endpoint. Pass credentials via auth headers only."
                )

    return cleaned.lstrip('/')


class KavitaClient:
    """
    HTTP client for authenticated communication with Kavita API.
    """

    def __init__(self, base_url, credentials=None, session=None,
                 connect_timeout=DEFAULT_CONNECT_TIMEOUT, read_timeout=DEFAULT_READ_TIMEOUT):
        """
        Initialize KavitaClient.

        :param base_url: Validated base URL
        :param credentials: Optional KavitaCredentials instance
        :param session: Optional requests.Session for testing or connection pooling
        :param connect_timeout: Connect timeout in seconds
        :param read_timeout: Read timeout in seconds
        """
        self.base_url = validate_kavita_url(base_url)
        self.credentials = credentials
        self._session = session or requests.Session()
        self.connect_timeout = float(connect_timeout)
        self.read_timeout = float(read_timeout)

    def _build_full_url(self, relative_endpoint):
        """Construct canonical full URL from base_url and relative endpoint."""
        clean_endpoint = validate_relative_endpoint(relative_endpoint)
        return urllib.parse.urljoin(self.base_url, clean_endpoint)

    def _build_headers(self, custom_headers=None):
        """
        Build request headers merging defaults and x-api-key header.

        Authentication Rules:
        - Uses 'x-api-key: <apiKey>' header ONLY.
        - NEVER generates or emits 'Authorization' or Bearer headers.
        - Strips any accidental 'authorization' header in custom_headers.
        """
        headers = {
            'User-Agent': DEFAULT_USER_AGENT,
            'Accept': 'application/json'
        }

        if self.credentials and self.credentials.is_configured:
            headers.update(self.credentials.get_auth_headers())

        if custom_headers and isinstance(custom_headers, dict):
            for k, v in custom_headers.items():
                if k.lower() not in ('authorization', 'x-api-key'):
                    headers[k] = v
        return headers

    def request(self, method, endpoint, params=None, json_body=None, custom_headers=None, timeout_override=None):
        """
        Execute an HTTP request against the Kavita server and parse JSON response.

        :param method: HTTP method ('GET' or 'POST')
        :param endpoint: Relative endpoint path
        :param params: Optional query parameters dict
        :param json_body: Optional JSON request payload
        :param custom_headers: Optional dict of extra headers
        :param timeout_override: Optional tuple (connect_timeout, read_timeout) to bound budget
        :return: Parsed JSON response (dict or list) or raw text if empty
        :raises KavitaError: Mapped subclass on transport, HTTP, or parsing failure
        """
        method_clean = str(method).upper().strip()
        if method_clean not in ('GET', 'POST'):
            raise KavitaInvalidRequestError(f"Unsupported HTTP method '{method}'. Only GET and POST are supported.")

        full_url = self._build_full_url(endpoint)
        headers = self._build_headers(custom_headers)

        # Validate params for forbidden keys
        if params:
            for k in params:
                if str(k).lower() in FORBIDDEN_QUERY_KEYS:
                    raise KavitaInvalidRequestError(f"Forbidden query key '{k}' in request parameters.")

        if timeout_override is not None and isinstance(timeout_override, (tuple, list)) and len(timeout_override) == 2:
            timeout = (float(timeout_override[0]), float(timeout_override[1]))
        elif timeout_override is not None:
            timeout = float(timeout_override)
        else:
            timeout = (self.connect_timeout, self.read_timeout)

        try:
            response = self._session.request(
                method=method_clean,
                url=full_url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=timeout,
                allow_redirects=False
            )
        except requests.exceptions.ConnectTimeout:
            raise KavitaTimeoutError("Connection to Kavita server timed out.", timeout_type='connect')
        except requests.exceptions.ReadTimeout:
            raise KavitaTimeoutError("Read timeout while awaiting response from Kavita server.", timeout_type='read')
        except (requests.exceptions.ConnectionError, requests.exceptions.SSLError) as e:
            raise KavitaTransportError("Network connection or TLS handshake failed connecting to Kavita server.")
        except requests.exceptions.RequestException as e:
            raise KavitaTransportError("Failed to communicate with Kavita server.")

        # HTTP error classification
        status = response.status_code
        if status == 401:
            raise KavitaAuthenticationError("Authentication failed. Invalid or expired Kavita API key.", status_code=401)
        elif status == 403:
            raise KavitaPermissionError("Access forbidden. API key lacks permissions to manage or read libraries.", status_code=403)
        elif status == 404:
            raise KavitaInvalidRequestError("Requested Kavita API endpoint not found.", status_code=404)
        elif status >= 500:
            raise KavitaTransportError(f"Kavita server returned internal error (HTTP {status}).")
        elif status >= 400:
            raise KavitaInvalidRequestError(f"Kavita API returned client error (HTTP {status}).", status_code=status)

        # Parse response body
        if not response.content:
            return {}

        try:
            return response.json()
        except ValueError:
            raise KavitaInvalidResponseError("Received non-JSON response from Kavita server.", status_code=status)

    def get(self, endpoint, params=None, custom_headers=None, timeout_override=None):
        """Execute authenticated GET request."""
        return self.request('GET', endpoint, params=params, custom_headers=custom_headers, timeout_override=timeout_override)

    def post(self, endpoint, json_body=None, params=None, custom_headers=None, timeout_override=None):
        """Execute authenticated POST request."""
        return self.request('POST', endpoint, params=params, json_body=json_body, custom_headers=custom_headers, timeout_override=timeout_override)
