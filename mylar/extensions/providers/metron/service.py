"""
Metron Connection Service (Phase C4.1).

Orchestrates read-only connection probes and formats sanitized diagnostic results.
Operates statelessly with zero database writes, strict endpoint pre-validation,
and diagnostic endpoint redaction.
"""

from mylar import logger
from mylar.extensions.providers.metron.auth import (
    MetronCredentials,
    MetronConfigurationError,
    AUTH_MODE_NONE
)
from mylar.extensions.providers.metron.client import (
    MetronClient,
    MetronError,
    MetronAuthenticationError,
    MetronRateLimitError,
    MetronTimeoutError,
    MetronTransportError,
    MetronUpstreamError,
    MetronInvalidResponseError,
    MetronNotFoundError,
    MetronInvalidRequestError,
    validate_relative_endpoint
)


DEFAULT_PROBE_ENDPOINT = 'publisher/?page=1'


class MetronConnectionService:
    """
    Service layer for executing and evaluating Metron API health and connection probes.
    """

    def probe_connection(self, credentials=None, endpoint=DEFAULT_PROBE_ENDPOINT, client_options=None):
        """
        Execute a single bounded connection probe against the Metron API and return a sanitized result.

        :param credentials: MetronCredentials instance or dict with credential fields, or None
        :param endpoint: Probe endpoint (defaults to 'publisher/?page=1')
        :param client_options: Optional dict of client configuration (base_url, connect_timeout, read_timeout, session)
        :return: dict with sanitized connection diagnostics
        """
        client_opts = dict(client_options or {})
        raw_endpoint = endpoint or DEFAULT_PROBE_ENDPOINT

        # 1. Pre-validate endpoint before any credential or network evaluation
        try:
            validated_endpoint = validate_relative_endpoint(raw_endpoint)
        except MetronInvalidRequestError:
            logger.fdebug("[METRON-SERVICE] Endpoint pre-validation failed")
            return self._format_result(
                success=False,
                configured=False,
                auth_mode='unknown',
                endpoint="[rejected]",
                http_status=None,
                service_reachable=False,
                authenticated=False,
                response_shape='none',
                observed_result_count=0,
                rate_limit={},
                error_code='invalid_request',
                message="Invalid Metron API endpoint path."
            )

        # 2. Normalize and validate credentials
        creds = None
        if isinstance(credentials, MetronCredentials):
            creds = credentials
        elif isinstance(credentials, dict):
            try:
                creds = MetronCredentials(
                    mode=credentials.get('mode', 'token'),
                    token=credentials.get('token'),
                    username=credentials.get('username'),
                    password=credentials.get('password')
                )
            except MetronConfigurationError as config_err:
                return self._format_result(
                    success=False,
                    configured=False,
                    auth_mode=credentials.get('mode', 'unknown'),
                    endpoint=validated_endpoint,
                    http_status=None,
                    service_reachable=False,
                    authenticated=False,
                    response_shape='none',
                    observed_result_count=0,
                    rate_limit={},
                    error_code=config_err.error_code,
                    message=str(config_err)
                )
        else:
            creds = MetronCredentials(mode=AUTH_MODE_NONE)

        # 3. Check configuration completeness
        if not creds.is_configured:
            if creds.mode == AUTH_MODE_NONE:
                return self._format_result(
                    success=False,
                    configured=False,
                    auth_mode=creds.mode,
                    endpoint=validated_endpoint,
                    http_status=None,
                    service_reachable=False,
                    authenticated=False,
                    response_shape='none',
                    observed_result_count=0,
                    rate_limit={},
                    error_code='not_configured',
                    message="Metron credentials are not configured."
                )
            else:
                return self._format_result(
                    success=False,
                    configured=False,
                    auth_mode=creds.mode,
                    endpoint=validated_endpoint,
                    http_status=None,
                    service_reachable=False,
                    authenticated=False,
                    response_shape='none',
                    observed_result_count=0,
                    rate_limit={},
                    error_code='invalid_configuration',
                    message=f"Metron credentials for '{creds.mode}' mode are incomplete."
                )

        # 4. Instantiate client and execute single probe
        try:
            client = MetronClient(credentials=creds, **client_opts)
        except MetronConfigurationError as client_config_err:
            return self._format_result(
                success=False,
                configured=creds.is_configured,
                auth_mode=creds.mode,
                endpoint=validated_endpoint,
                http_status=None,
                service_reachable=False,
                authenticated=False,
                response_shape='none',
                observed_result_count=0,
                rate_limit={},
                error_code=client_config_err.error_code,
                message=str(client_config_err)
            )

        try:
            resp = client.get(validated_endpoint)
            return self._format_result(
                success=True,
                configured=True,
                auth_mode=creds.mode,
                endpoint=validated_endpoint,
                http_status=resp.status_code,
                service_reachable=True,
                authenticated=True,
                response_shape=resp.response_shape,
                observed_result_count=resp.observed_result_count,
                rate_limit=resp.rate_limit,
                error_code=None,
                message="Successfully connected and authenticated with Metron API."
            )

        except MetronAuthenticationError as auth_err:
            logger.fdebug("[METRON-SERVICE] Authentication failure")
            return self._format_result(
                success=False,
                configured=True,
                auth_mode=creds.mode,
                endpoint=validated_endpoint,
                http_status=auth_err.status_code,
                service_reachable=True,
                authenticated=False,
                response_shape='none',
                observed_result_count=0,
                rate_limit=auth_err.rate_limit,
                error_code='authentication_failed',
                message=str(auth_err)
            )

        except MetronRateLimitError as rate_err:
            logger.fdebug("[METRON-SERVICE] Rate limit exceeded")
            return self._format_result(
                success=False,
                configured=True,
                auth_mode=creds.mode,
                endpoint=validated_endpoint,
                http_status=rate_err.status_code,
                service_reachable=True,
                authenticated=True,
                response_shape='none',
                observed_result_count=0,
                rate_limit=rate_err.rate_limit,
                error_code='rate_limited',
                message=str(rate_err)
            )

        except MetronTimeoutError as timeout_err:
            logger.fdebug("[METRON-SERVICE] Timeout error")
            return self._format_result(
                success=False,
                configured=True,
                auth_mode=creds.mode,
                endpoint=validated_endpoint,
                http_status=None,
                service_reachable=False,
                authenticated=False,
                response_shape='none',
                observed_result_count=0,
                rate_limit={},
                error_code='timeout',
                message=str(timeout_err)
            )

        except MetronTransportError as transport_err:
            logger.fdebug("[METRON-SERVICE] Transport error")
            return self._format_result(
                success=False,
                configured=True,
                auth_mode=creds.mode,
                endpoint=validated_endpoint,
                http_status=None,
                service_reachable=False,
                authenticated=False,
                response_shape='none',
                observed_result_count=0,
                rate_limit={},
                error_code='transport_error',
                message=str(transport_err)
            )

        except MetronUpstreamError as upstream_err:
            logger.fdebug("[METRON-SERVICE] Upstream server error")
            return self._format_result(
                success=False,
                configured=True,
                auth_mode=creds.mode,
                endpoint=validated_endpoint,
                http_status=upstream_err.status_code,
                service_reachable=True,
                authenticated=False,
                response_shape='none',
                observed_result_count=0,
                rate_limit=upstream_err.rate_limit,
                error_code='upstream_error',
                message=str(upstream_err)
            )

        except MetronInvalidResponseError as invalid_err:
            logger.fdebug("[METRON-SERVICE] Invalid response format")
            return self._format_result(
                success=False,
                configured=True,
                auth_mode=creds.mode,
                endpoint=validated_endpoint,
                http_status=invalid_err.status_code,
                service_reachable=True,
                authenticated=True,
                response_shape='none',
                observed_result_count=0,
                rate_limit={},
                error_code='invalid_response',
                message=str(invalid_err)
            )

        except MetronNotFoundError as not_found_err:
            logger.fdebug("[METRON-SERVICE] Endpoint not found")
            return self._format_result(
                success=False,
                configured=True,
                auth_mode=creds.mode,
                endpoint=validated_endpoint,
                http_status=not_found_err.status_code,
                service_reachable=True,
                authenticated=True,
                response_shape='none',
                observed_result_count=0,
                rate_limit=not_found_err.rate_limit,
                error_code='not_found',
                message=str(not_found_err)
            )

        except MetronInvalidRequestError as req_err:
            logger.fdebug("[METRON-SERVICE] Invalid request")
            return self._format_result(
                success=False,
                configured=True,
                auth_mode=creds.mode,
                endpoint=validated_endpoint,
                http_status=req_err.status_code,
                service_reachable=False if req_err.status_code == 400 and req_err.rate_limit == {} else True,
                authenticated=False,
                response_shape='none',
                observed_result_count=0,
                rate_limit=req_err.rate_limit,
                error_code='invalid_request',
                message=str(req_err)
            )

        except MetronError as general_err:
            logger.fdebug("[METRON-SERVICE] General provider error")
            return self._format_result(
                success=False,
                configured=True,
                auth_mode=creds.mode,
                endpoint=validated_endpoint,
                http_status=general_err.status_code,
                service_reachable=True,
                authenticated=False,
                response_shape='none',
                observed_result_count=0,
                rate_limit=general_err.rate_limit,
                error_code=general_err.error_code,
                message=str(general_err)
            )

        except Exception:
            logger.fdebug("[METRON-SERVICE] Unhandled error")
            return self._format_result(
                success=False,
                configured=True,
                auth_mode=creds.mode,
                endpoint=validated_endpoint,
                http_status=None,
                service_reachable=False,
                authenticated=False,
                response_shape='none',
                observed_result_count=0,
                rate_limit={},
                error_code='internal_error',
                message="An internal error occurred during the Metron connection probe."
            )

    def _format_result(
        self,
        success,
        configured,
        auth_mode,
        endpoint,
        http_status,
        service_reachable,
        authenticated,
        response_shape,
        observed_result_count,
        rate_limit,
        error_code,
        message
    ):
        """
        Construct a sanitized, standardized dictionary representation.
        Guarantees that sensitive secrets or rejected endpoint paths are never included.
        """
        clean_rate = {
            'limit': rate_limit.get('limit') if rate_limit else None,
            'remaining': rate_limit.get('remaining') if rate_limit else None,
            'reset': rate_limit.get('reset') if rate_limit else None,
            'retry_after': rate_limit.get('retry_after') if rate_limit else None
        }

        safe_endpoint = "[rejected]" if endpoint == "[rejected]" else str(endpoint)

        return {
            'success': bool(success),
            'configured': bool(configured),
            'auth_mode': str(auth_mode),
            'endpoint': safe_endpoint,
            'http_status': int(http_status) if http_status is not None else None,
            'service_reachable': bool(service_reachable),
            'authenticated': bool(authenticated),
            'response_shape': str(response_shape),
            'observed_result_count': int(observed_result_count),
            'rate_limit': clean_rate,
            'error_code': str(error_code) if error_code is not None else None,
            'message': str(message)
        }
