"""
Metron HTTP Client Foundation (Phase C4.1).

Handles authenticated read-only communication with the Metron API.
Guarantees HTTPS transport, strict canonical endpoint validation, finite timeouts,
TLS verification, defensive rate-limit header parsing, and sanitized error mapping
with zero credential or exception leakage.
"""

import math
import time
import urllib.parse
import requests
from mylar import logger
from mylar.extensions.providers.metron.auth import (
    MetronCredentials,
    MetronConfigurationError,
    AUTH_MODE_NONE
)


DEFAULT_BASE_URL = 'https://metron.cloud/api/'
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 10.0
DEFAULT_USER_AGENT = 'Mylar3/1.0 (Metron Provider Client)'

FORBIDDEN_QUERY_KEYS = {
    'token',
    'access_token',
    'api_key',
    'apikey',
    'password',
    'secret',
    'authorization'
}


class MetronError(Exception):
    """Base exception for all Metron provider errors."""
    def __init__(self, message, error_code='provider_error', status_code=None, rate_limit=None):
        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code
        self.rate_limit = rate_limit or {}


class MetronAuthenticationError(MetronError):
    """Raised when authentication fails (HTTP 401 or 403). Zero retries permitted."""
    def __init__(self, message="Authentication failed. Check Metron credentials.", status_code=401, rate_limit=None):
        super().__init__(message, error_code='authentication_failed', status_code=status_code, rate_limit=rate_limit)


class MetronRateLimitError(MetronError):
    """Raised when the rate limit is exceeded (HTTP 429)."""
    def __init__(self, message="Metron rate limit exceeded.", status_code=429, rate_limit=None):
        super().__init__(message, error_code='rate_limited', status_code=status_code, rate_limit=rate_limit)


class MetronTimeoutError(MetronError):
    """Raised when connection or read timeouts occur."""
    def __init__(self, message="Request to Metron timed out.", timeout_type='read'):
        super().__init__(message, error_code='timeout')
        self.timeout_type = timeout_type


class MetronTransportError(MetronError):
    """Raised when TLS, DNS, or network connection errors occur."""
    def __init__(self, message="Transport/network error connecting to Metron."):
        super().__init__(message, error_code='transport_error')


class MetronUpstreamError(MetronError):
    """Raised when Metron returns a temporary 5xx server error."""
    def __init__(self, message="Metron service returned an upstream error.", status_code=500, rate_limit=None):
        super().__init__(message, error_code='upstream_error', status_code=status_code, rate_limit=rate_limit)


class MetronInvalidResponseError(MetronError):
    """Raised when Metron returns a malformed or non-JSON response payload."""
    def __init__(self, message="Received invalid or non-JSON response from Metron.", status_code=200):
        super().__init__(message, error_code='invalid_response', status_code=status_code)


class MetronNotFoundError(MetronError):
    """Raised when the requested Metron endpoint or resource is not found (HTTP 404)."""
    def __init__(self, message="Requested Metron resource not found.", status_code=404, rate_limit=None):
        super().__init__(message, error_code='not_found', status_code=status_code, rate_limit=rate_limit)


class MetronInvalidRequestError(MetronError):
    """Raised when the request is rejected by Metron or fails endpoint validation."""
    def __init__(self, message="Invalid Metron API endpoint path or request parameters.", status_code=400, rate_limit=None):
        super().__init__(message, error_code='invalid_request', status_code=status_code, rate_limit=rate_limit)


def _canonical_unquote(text, max_iterations=4):
    """
    Recursively/iteratively unquote text up to max_iterations to uncover nested percent-encodings.
    """
    if not isinstance(text, str):
        return ""
    curr = text
    for _ in range(max_iterations):
        unq = urllib.parse.unquote(curr)
        if unq == curr:
            break
        curr = unq
    return curr


def validate_and_normalize_base_url(base_url):
    """
    Validate that the base URL is a valid, secure HTTPS URL without credentials, query parameters, or fragments.
    Normalizes the path to end with a single trailing slash.

    :param base_url: Base URL string or None
    :return: Normalized HTTPS base URL string
    :raises MetronConfigurationError: If the URL fails HTTPS, credential, query, fragment, or hostname checks.
    """
    if base_url is None:
        return DEFAULT_BASE_URL

    if not isinstance(base_url, str):
        raise MetronConfigurationError(
            "Invalid Metron base URL. URL must be an HTTPS string.",
            error_code='invalid_configuration'
        )

    clean_url = base_url.strip()
    if not clean_url:
        raise MetronConfigurationError(
            "Invalid Metron base URL. URL cannot be empty.",
            error_code='invalid_configuration'
        )

    # Reject protocol-relative URLs
    if clean_url.startswith('//'):
        raise MetronConfigurationError(
            "Invalid Metron base URL. Protocol-relative URLs are not permitted.",
            error_code='invalid_configuration'
        )

    try:
        parsed = urllib.parse.urlsplit(clean_url)
    except Exception:
        raise MetronConfigurationError(
            "Invalid Metron base URL. Could not parse URL structure.",
            error_code='invalid_configuration'
        ) from None

    # Enforce HTTPS scheme
    if parsed.scheme.lower() != 'https':
        raise MetronConfigurationError(
            "Invalid Metron base URL. HTTPS transport is strictly required.",
            error_code='invalid_configuration'
        )

    # Reject missing hostnames
    if not parsed.netloc or not parsed.hostname:
        raise MetronConfigurationError(
            "Invalid Metron base URL. Missing valid hostname.",
            error_code='invalid_configuration'
        )

    # Reject embedded user information (@ or username/password)
    if parsed.username or parsed.password or '@' in parsed.netloc:
        raise MetronConfigurationError(
            "Invalid Metron base URL. Embedded user credentials are not permitted.",
            error_code='invalid_configuration'
        )

    # Reject query strings or URL fragments in base URL
    if parsed.query or parsed.fragment or '?' in clean_url or '#' in clean_url:
        raise MetronConfigurationError(
            "Invalid Metron base URL. Query parameters and fragments are not permitted in base URL.",
            error_code='invalid_configuration'
        )

    # Normalize path to end with single trailing slash
    path = parsed.path.rstrip('/') + '/'
    return f"https://{parsed.netloc}{path}"


def validate_relative_endpoint(endpoint):
    """
    Validate that an endpoint path is a strictly relative, canonical, safe API path.
    Rejects leading slashes, schemes, protocol-relative syntax, hostnames, user-info,
    raw/encoded path traversal, raw/encoded control characters or backslashes,
    fragments, and sensitive query parameter keys.

    :param endpoint: Relative endpoint string
    :return: Sanitized relative endpoint string
    :raises MetronInvalidRequestError: If the endpoint violates relative path safety rules.
    """
    if not endpoint or not isinstance(endpoint, str):
        raise MetronInvalidRequestError("Endpoint must be a non-empty relative path string.")

    # Reject leading or trailing whitespace
    if endpoint != endpoint.strip():
        raise MetronInvalidRequestError("Endpoint cannot contain leading or trailing whitespace.")

    clean_endpoint = endpoint

    # 1. Reject endpoints beginning with / or //
    if clean_endpoint.startswith('/') or clean_endpoint.startswith('//'):
        raise MetronInvalidRequestError("Endpoint must be strictly relative and cannot begin with a leading slash.")

    # 2. Check for non-ASCII or raw control characters
    for ch in clean_endpoint:
        code = ord(ch)
        if code < 32 or code > 126:
            raise MetronInvalidRequestError("Endpoint contains invalid non-printable or non-ASCII characters.")

    # 3. Canonicalize / recursively decode to uncover hidden bypasses
    canonical = _canonical_unquote(clean_endpoint)

    # Reject decoded control characters
    for ch in canonical:
        code = ord(ch)
        if code < 32 or code == 127:
            raise MetronInvalidRequestError("Endpoint contains forbidden control characters.")

    # Reject raw or decoded backslashes
    if '\\' in clean_endpoint or '\\' in canonical:
        raise MetronInvalidRequestError("Endpoint contains forbidden backslash characters.")

    # Reject raw or decoded URL fragments
    if '#' in clean_endpoint or '#' in canonical:
        raise MetronInvalidRequestError("Endpoint contains forbidden URL fragment syntax.")

    # Reject protocol-relative or absolute URL schemes
    if (
        clean_endpoint.startswith('//') or
        canonical.startswith('//') or
        '://' in clean_endpoint or
        '://' in canonical
    ):
        raise MetronInvalidRequestError("Absolute URLs and protocol-relative syntax are not permitted.")

    # Reject schemes
    canon_lower = canonical.lower()
    if canon_lower.startswith(('http:', 'https:', 'ftp:', 'file:')):
        raise MetronInvalidRequestError("URL scheme prefix is not permitted in relative endpoint.")

    # Reject userinfo syntax
    if '@' in clean_endpoint or '@' in canonical:
        raise MetronInvalidRequestError("User information syntax is not permitted in endpoint.")

    # 4. Separate path and query
    path_part, has_query, query_part = clean_endpoint.partition('?')
    canon_path, _, canon_query = canonical.partition('?')

    # Check path traversal on both raw and canonical path segments
    for p_str in (path_part, canon_path):
        segments = p_str.split('/')
        for seg in segments:
            seg_canon = _canonical_unquote(seg).strip()
            if seg_canon == '..' or seg == '..' or seg_canon.startswith('..'):
                raise MetronInvalidRequestError("Path traversal segments ('..') are not permitted.")

    # 5. Validate query parameters if present in URL
    if has_query and query_part:
        try:
            parsed_query = urllib.parse.parse_qsl(query_part, keep_blank_values=True)
            for k, _ in parsed_query:
                k_clean = _canonical_unquote(str(k)).strip().lower()
                if k_clean in FORBIDDEN_QUERY_KEYS:
                    raise MetronInvalidRequestError("Endpoint contains forbidden sensitive query parameter.")
        except MetronInvalidRequestError:
            raise
        except Exception:
            raise MetronInvalidRequestError("Malformed query parameters in endpoint.") from None

    return clean_endpoint


def validate_query_params(params):
    """
    Validate caller-supplied params dictionary or sequence.
    Rejects malformed structures and sensitive query keys.

    :param params: dict, sequence of pairs, or None
    :raises MetronInvalidRequestError: If params are malformed or contain sensitive keys.
    """
    if params is None:
        return

    if not isinstance(params, (dict, list, tuple)):
        raise MetronInvalidRequestError("Query parameters must be a dictionary or sequence of key-value pairs.")

    items = params.items() if isinstance(params, dict) else params
    for item in items:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise MetronInvalidRequestError("Malformed query parameter structure.")
        key, _ = item
        k_clean = _canonical_unquote(str(key)).strip().lower()
        if k_clean in FORBIDDEN_QUERY_KEYS:
            raise MetronInvalidRequestError("Query parameters contain forbidden sensitive parameter key.")


class MetronResponse:
    """
    Encapsulates a sanitized Metron HTTP response.
    """
    def __init__(self, status_code, data, headers, rate_limit=None, is_not_modified=False, elapsed_seconds=0.0):
        self.status_code = status_code
        self.data = data
        self.headers = headers
        self.rate_limit = rate_limit or {}
        self.is_not_modified = is_not_modified
        self.elapsed_seconds = elapsed_seconds

    @property
    def response_shape(self):
        """Classify the structural shape of the returned payload."""
        if self.is_not_modified:
            return 'not_modified'
        if isinstance(self.data, dict):
            if 'results' in self.data:
                return 'paginated_envelope'
            return 'object'
        elif isinstance(self.data, list):
            return 'flat_list'
        elif self.data is None:
            return 'none'
        return 'primitive'

    @property
    def observed_result_count(self):
        """Count of records in the current page/response without assuming global page size."""
        if isinstance(self.data, dict) and 'results' in self.data:
            res = self.data['results']
            return len(res) if isinstance(res, list) else 0
        elif isinstance(self.data, list):
            return len(self.data)
        elif isinstance(self.data, dict) and self.data:
            return 1
        return 0


class MetronClient:
    """
    HTTP client for Metron API.
    Enforces HTTPS transport, strict canonical endpoint validation, timeouts,
    TLS verification, defensive rate-limit header parsing, and sanitized error mapping.
    """

    def __init__(
        self,
        credentials=None,
        base_url=None,
        connect_timeout=DEFAULT_CONNECT_TIMEOUT,
        read_timeout=DEFAULT_READ_TIMEOUT,
        verify_ssl=True,
        session=None,
        user_agent=DEFAULT_USER_AGENT
    ):
        """
        Initialize the Metron HTTP client.
        Performs ZERO network calls during initialization.

        :param credentials: MetronCredentials instance or None
        :param base_url: Base URL string (defaults to https://metron.cloud/api/)
        :param connect_timeout: Connection timeout in seconds (finite positive float)
        :param read_timeout: Read timeout in seconds (finite positive float)
        :param verify_ssl: TLS certificate verification (strictly enforced)
        :param session: Optional requests.Session instance for dependency injection
        :param user_agent: User-Agent string (non-empty ASCII string without control chars)
        """
        self.credentials = credentials or MetronCredentials(mode=AUTH_MODE_NONE)
        self.base_url = validate_and_normalize_base_url(base_url)

        # Validate connect_timeout
        if (
            isinstance(connect_timeout, bool) or
            not isinstance(connect_timeout, (int, float)) or
            not math.isfinite(connect_timeout) or
            connect_timeout <= 0
        ):
            raise MetronConfigurationError(
                "Invalid connect timeout configuration. Timeout must be a finite positive number.",
                error_code='invalid_configuration'
            )

        # Validate read_timeout
        if (
            isinstance(read_timeout, bool) or
            not isinstance(read_timeout, (int, float)) or
            not math.isfinite(read_timeout) or
            read_timeout <= 0
        ):
            raise MetronConfigurationError(
                "Invalid read timeout configuration. Timeout must be a finite positive number.",
                error_code='invalid_configuration'
            )

        # Enforce TLS verification
        if verify_ssl is not True:
            raise MetronConfigurationError(
                "TLS certificate verification cannot be disabled.",
                error_code='invalid_configuration'
            )

        # Validate user_agent
        if not isinstance(user_agent, str) or not user_agent.strip():
            raise MetronConfigurationError(
                "Invalid user agent configuration. User agent must be a non-empty string.",
                error_code='invalid_configuration'
            )

        for ch in user_agent:
            code = ord(ch)
            if code < 32 or code == 127:
                raise MetronConfigurationError(
                    "Invalid user agent configuration. User agent cannot contain control characters.",
                    error_code='invalid_configuration'
                )

        self.connect_timeout = float(connect_timeout)
        self.read_timeout = float(read_timeout)
        self.verify_ssl = True
        self.user_agent = user_agent.strip()
        self._session = session or requests.Session()

    def _parse_rate_limit_headers(self, headers):
        """
        Defensively parse rate-limiting headers from response.
        Preserves raw string values without making unverified epoch vs relative assumptions.
        """
        if not headers:
            return {
                'limit': None,
                'remaining': None,
                'reset': None,
                'retry_after': None
            }

        return {
            'limit': headers.get('X-RateLimit-Limit') or headers.get('x-ratelimit-limit'),
            'remaining': headers.get('X-RateLimit-Remaining') or headers.get('x-ratelimit-remaining'),
            'reset': headers.get('X-RateLimit-Reset') or headers.get('x-ratelimit-reset'),
            'retry_after': headers.get('Retry-After') or headers.get('retry-after')
        }

    def get(self, endpoint, params=None):
        """
        Execute a single bounded read-only GET request against the Metron API.
        Performs exactly one request with zero automatic retries.

        :param endpoint: Safe relative endpoint string (e.g. 'publisher/?page=1' or 'role/')
        :param params: Optional query parameter dictionary or sequence of pairs
        :return: MetronResponse instance
        """
        clean_endpoint = validate_relative_endpoint(endpoint)
        validate_query_params(params)
        url = f"{self.base_url}{clean_endpoint}"

        headers = {
            'User-Agent': self.user_agent,
            'Accept': 'application/json'
        }

        # Inject authorization headers safely if credentials configured
        if self.credentials.is_configured:
            headers.update(self.credentials.get_auth_headers())

        logger.fdebug("[METRON-CLIENT] Executing bounded GET probe")
        start_time = time.time()

        try:
            resp = self._session.get(
                url,
                params=params,
                headers=headers,
                timeout=(self.connect_timeout, self.read_timeout),
                verify=True
            )
            elapsed = time.time() - start_time
            rate_limit = self._parse_rate_limit_headers(resp.headers)

            logger.fdebug(
                f"[METRON-CLIENT] Response status: {resp.status_code} (elapsed: {elapsed:.3f}s)"
            )

            # Classify response codes
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except Exception:
                    logger.fdebug("[METRON-CLIENT] JSON decode failure on 200 response")
                    raise MetronInvalidResponseError(
                        "Received non-JSON response from Metron API.",
                        status_code=200
                    ) from None

                # Strict structural response validation: accept dict (object/envelope) or list
                if not isinstance(data, (dict, list)):
                    logger.fdebug("[METRON-CLIENT] Non-dictionary/non-list JSON primitive received on 200 response")
                    raise MetronInvalidResponseError(
                        "Received invalid or non-dictionary/non-list JSON response from Metron API.",
                        status_code=200
                    )

                return MetronResponse(
                    status_code=200,
                    data=data,
                    headers=resp.headers,
                    rate_limit=rate_limit,
                    elapsed_seconds=elapsed
                )

            elif resp.status_code == 304:
                return MetronResponse(
                    status_code=304,
                    data=None,
                    headers=resp.headers,
                    rate_limit=rate_limit,
                    is_not_modified=True,
                    elapsed_seconds=elapsed
                )

            elif resp.status_code in (401, 403):
                msg = "Authentication failed (HTTP 401 Unauthorized)." if resp.status_code == 401 else "Authentication failed (HTTP 403 Forbidden)."
                raise MetronAuthenticationError(msg, status_code=resp.status_code, rate_limit=rate_limit)

            elif resp.status_code == 404:
                raise MetronNotFoundError(
                    "Metron endpoint resource not found (HTTP 404).",
                    status_code=404,
                    rate_limit=rate_limit
                )

            elif resp.status_code == 429:
                retry_val = rate_limit.get('retry_after') or rate_limit.get('reset')
                msg = f"Metron rate limit exceeded (HTTP 429). Retry after: {retry_val or 'unknown'}."
                raise MetronRateLimitError(msg, status_code=429, rate_limit=rate_limit)

            elif resp.status_code == 400:
                raise MetronInvalidRequestError(
                    "Metron rejected the request parameters (HTTP 400).",
                    status_code=400,
                    rate_limit=rate_limit
                )

            elif 500 <= resp.status_code <= 599:
                raise MetronUpstreamError(
                    f"Metron returned upstream server error (HTTP {resp.status_code}).",
                    status_code=resp.status_code,
                    rate_limit=rate_limit
                )

            else:
                raise MetronError(
                    f"Unexpected HTTP status {resp.status_code} from Metron API.",
                    error_code='unexpected_status',
                    status_code=resp.status_code,
                    rate_limit=rate_limit
                )

        except requests.exceptions.ConnectTimeout:
            logger.fdebug(f"[METRON-CLIENT] Connection timeout after {self.connect_timeout}s")
            raise MetronTimeoutError("Connection to Metron timed out.", timeout_type='connect') from None

        except requests.exceptions.ReadTimeout:
            logger.fdebug(f"[METRON-CLIENT] Read timeout after {self.read_timeout}s")
            raise MetronTimeoutError("Read from Metron timed out.", timeout_type='read') from None

        except requests.exceptions.Timeout:
            logger.fdebug("[METRON-CLIENT] Request timeout")
            raise MetronTimeoutError("Request to Metron timed out.") from None

        except requests.exceptions.SSLError:
            logger.fdebug("[METRON-CLIENT] TLS/SSL certificate verification failure")
            raise MetronTransportError("TLS certificate verification failed connecting to Metron.") from None

        except requests.exceptions.ConnectionError:
            logger.fdebug("[METRON-CLIENT] Network connection error")
            raise MetronTransportError("Network connection error reaching Metron service.") from None

        except requests.exceptions.RequestException:
            logger.fdebug("[METRON-CLIENT] Generic transport error")
            raise MetronTransportError("Transport error connecting to Metron.") from None
