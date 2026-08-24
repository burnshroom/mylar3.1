"""
Metron Runtime Controller (Phase C4.4 & C4.5).

Handles HTTP requests for Metron provider integration:
- POST /testMetron (connection probe)
- GET /metronCompareCredits (single-issue credit comparison)

Enforces strict method restrictions, secret sanitation, and dependency injection.
"""

import json
import cherrypy

from mylar import logger
from mylar.extensions.providers.metron.config import (
    build_metron_credentials,
    CANONICAL_METRON_BASE_URL
)
from mylar.extensions.providers.metron.service import MetronConnectionService
from mylar.extensions.providers.metron.comparison_service import (
    MetronComparisonService,
    MetronLocalIssueNotFoundError,
    MetronProviderDisabledError
)
from mylar.extensions.providers.metron.issue_service import (
    MetronAmbiguousResultError
)
from mylar.extensions.providers.metron.client import (
    MetronError,
    MetronAuthenticationError,
    MetronRateLimitError,
    MetronTimeoutError,
    MetronTransportError,
    MetronInvalidRequestError,
    MetronInvalidResponseError,
    MetronNotFoundError
)


def handle_test_metron(auth_mode=None, api_token=None, username=None, password=None, service=None, **kwargs):
    """
    HTTP handler for POST /testMetron.
    Executes a single bounded read-only connection probe using provided form values or stored settings.
    Guarantees that zero credentials, authorization headers, or raw response bodies are returned.

    :param auth_mode: Optional 'token' or 'basic'
    :param api_token: Optional API token from form
    :param username: Optional username from form
    :param password: Optional password from form
    :param service: Optional injected MetronConnectionService for testing
    :return: JSON string response with Content-Type: application/json
    """
    if hasattr(cherrypy, 'response'):
        cherrypy.response.headers['Content-Type'] = 'application/json'

    # 1. Enforce POST method only
    if hasattr(cherrypy, 'request') and getattr(cherrypy.request, 'method', None) != 'POST':
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 405
        return json.dumps({
            'success': False,
            'status_code': 405,
            'message': 'Method Not Allowed. POST is required.',
            'error_code': 'method_not_allowed'
        })

    # 2. Strict input validation and length bounds
    clean_auth_mode = str(auth_mode).strip().lower() if auth_mode is not None and str(auth_mode).strip() else None
    if clean_auth_mode is not None and len(clean_auth_mode) > 32:
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 400
        return json.dumps({
            'success': False,
            'status_code': 400,
            'message': 'Invalid authentication mode length.',
            'error_code': 'invalid_request'
        })

    clean_token = str(api_token).strip() if api_token is not None and str(api_token).strip() else None
    if clean_token is not None and len(clean_token) > 4096:
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 400
        return json.dumps({
            'success': False,
            'status_code': 400,
            'message': 'Supplied API token exceeds maximum length.',
            'error_code': 'invalid_request'
        })

    clean_user = str(username).strip() if username is not None and str(username).strip() else None
    if clean_user is not None and len(clean_user) > 512:
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 400
        return json.dumps({
            'success': False,
            'status_code': 400,
            'message': 'Supplied username exceeds maximum length.',
            'error_code': 'invalid_request'
        })

    clean_pass = str(password).strip() if password is not None and str(password).strip() else None
    if clean_pass is not None and len(clean_pass) > 512:
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 400
        return json.dumps({
            'success': False,
            'status_code': 400,
            'message': 'Supplied password exceeds maximum length.',
            'error_code': 'invalid_request'
        })

    # 3. Build credentials safely
    try:
        creds = build_metron_credentials(
            auth_mode=clean_auth_mode,
            api_token=clean_token,
            username=clean_user,
            password=clean_pass
        )
    except MetronInvalidRequestError as e:
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 400
        return json.dumps({
            'success': False,
            'status_code': 400,
            'auth_mode': clean_auth_mode or 'unknown',
            'message': str(e),
            'error_code': 'missing_credentials'
        })

    # 4. Perform bounded probe
    try:
        conn_service = service if service is not None else MetronConnectionService()
        probe_res = conn_service.probe_connection(
            credentials=creds,
            client_options={'base_url': CANONICAL_METRON_BASE_URL}
        )

        http_status = probe_res.get('http_status')
        error_code = probe_res.get('error_code')
        rate_info = probe_res.get('rate_limit') or {}

        if probe_res.get('success'):
            status_code = 200
            msg = "Successfully connected to Metron API."
        else:
            if http_status in (401, 403) or error_code == 'authentication_failed':
                status_code = http_status or 401
                msg = (
                    "Metron authentication failed. Please verify your API token."
                    if creds.mode == 'token'
                    else "Metron authentication failed. Please verify your username and password."
                )
            elif http_status == 429 or error_code == 'rate_limited':
                status_code = 429
                msg = "Metron request rate limit reached. Please wait a moment before trying again."
            elif error_code == 'timeout' or http_status == 504:
                status_code = 504
                msg = "Connection timed out while contacting Metron API."
            elif error_code == 'transport_error' or http_status == 502:
                status_code = 502
                msg = "Unable to establish secure HTTPS connection to Metron API."
            elif error_code == 'invalid_response':
                status_code = 502
                msg = "Metron API returned an invalid response structure."
            elif http_status and http_status >= 500:
                status_code = http_status
                msg = "Metron API is currently unavailable (server error)."
            else:
                status_code = http_status or 500
                msg = probe_res.get('message') or "Metron connection probe failed."

        # Parse numeric rate limit info
        rem = rate_info.get('remaining')
        res = rate_info.get('reset')
        parsed_rem = int(rem) if rem is not None and str(rem).isdigit() else None
        parsed_res = int(res) if res is not None and str(res).isdigit() else None

        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = status_code

        return json.dumps({
            'success': bool(probe_res.get('success')),
            'status_code': status_code,
            'auth_mode': creds.mode,
            'message': msg,
            'error_code': error_code,
            'rate_limit_remaining': parsed_rem,
            'rate_limit_reset': parsed_res
        })

    except Exception:
        logger.fdebug("[METRON-CONTROLLER] Unexpected exception during connection test")
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 500
        return json.dumps({
            'success': False,
            'status_code': 500,
            'auth_mode': creds.mode,
            'message': "Unexpected error testing connection to Metron API.",
            'error_code': 'internal_error'
        })


def handle_metron_compare_credits(issueid=None, annual=0, service=None, **kwargs):
    """
    HTTP handler for GET /metronCompareCredits?issueid=<IssueID>&annual=<0|1>.
    Retrieves Metron metadata for an issue and compares it against locally indexed creator credits.

    :param issueid: Local IssueID query parameter
    :param annual: 0 for regular issue, 1 for Annual
    :param service: Optional injected MetronComparisonService for testing
    :return: JSON response string with Content-Type application/json
    """
    if hasattr(cherrypy, 'response'):
        cherrypy.response.headers['Content-Type'] = 'application/json'

    # 1. Enforce GET method only
    if hasattr(cherrypy, 'request') and getattr(cherrypy.request, 'method', None) != 'GET':
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 405
        return json.dumps({
            'success': False,
            'status_code': 405,
            'message': 'Method Not Allowed. GET is required.',
            'error_code': 'method_not_allowed'
        })

    # 2. Delegate to MetronComparisonService
    try:
        comp_service = service if service is not None else MetronComparisonService()
        result = comp_service.compare_issue_with_metron(issue_id=issueid, is_annual=annual)
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 200
        return json.dumps(result)

    except MetronLocalIssueNotFoundError as e:
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 404
        return json.dumps({
            'success': False,
            'status_code': 404,
            'message': str(e),
            'error_code': 'local_issue_not_found'
        })

    except MetronProviderDisabledError as e:
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 400
        return json.dumps({
            'success': False,
            'status_code': 400,
            'message': str(e),
            'error_code': 'provider_disabled'
        })

    except MetronInvalidRequestError as e:
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 400
        err_code = 'missing_credentials' if 'required' in str(e).lower() else 'invalid_request'
        return json.dumps({
            'success': False,
            'status_code': 400,
            'message': str(e),
            'error_code': err_code
        })

    except MetronNotFoundError:
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 404
        return json.dumps({
            'success': False,
            'status_code': 404,
            'message': "No matching issue found on Metron for the requested ComicVine IssueID.",
            'error_code': 'provider_issue_not_found'
        })

    except MetronAmbiguousResultError as e:
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 422
        return json.dumps({
            'success': False,
            'status_code': 422,
            'message': str(e),
            'error_code': 'ambiguous_result'
        })

    except MetronAuthenticationError:
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 401
        return json.dumps({
            'success': False,
            'status_code': 401,
            'message': "Metron authentication failed. Please verify your configured credentials.",
            'error_code': 'authentication_failed'
        })

    except MetronRateLimitError as e:
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 429
        return json.dumps({
            'success': False,
            'status_code': 429,
            'message': "Metron request rate limit reached. Please wait a moment before trying again.",
            'error_code': 'rate_limited'
        })

    except MetronTimeoutError:
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 504
        return json.dumps({
            'success': False,
            'status_code': 504,
            'message': "Connection timed out while contacting Metron API.",
            'error_code': 'timeout'
        })

    except MetronTransportError:
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 502
        return json.dumps({
            'success': False,
            'status_code': 502,
            'message': "Unable to establish secure HTTPS connection to Metron API.",
            'error_code': 'transport_error'
        })

    except MetronInvalidResponseError as e:
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 502
        return json.dumps({
            'success': False,
            'status_code': 502,
            'message': "Metron API returned an invalid or mismatched response.",
            'error_code': 'invalid_response'
        })

    except MetronError as e:
        status_code = getattr(e, 'status_code', 500) or 500
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = status_code
        return json.dumps({
            'success': False,
            'status_code': status_code,
            'message': "Metron API request failed.",
            'error_code': getattr(e, 'error_code', 'provider_error') or 'provider_error'
        })

    except Exception:
        logger.fdebug("[METRON-CONTROLLER] Unexpected exception during credit comparison")
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 500
        return json.dumps({
            'success': False,
            'status_code': 500,
            'message': "Unexpected server error during Metron issue credit comparison.",
            'error_code': 'internal_error'
        })
