"""
Kavita Runtime Controller (Phase K2 & K4).

Handles HTTP requests for Kavita provider integration:
- POST /testKavita (read-only capability check)
- POST /kavitaDiagnostics (read-only discovery & path alignment diagnostics)

Enforces strict method restrictions (POST-only), rejection of client-supplied URL/key overrides,
and dependency injection for isolated testing.
Zero full UUIDs, zero secrets, and zero mutation endpoints.
"""

import json
import cherrypy
import secrets
import hmac

from mylar import logger
from mylar.extensions.providers.kavita.config import (
    build_kavita_credentials,
    get_kavita_config_status
)
from mylar.extensions.providers.kavita.service import KavitaConnectionService
from mylar.extensions.providers.kavita.discovery import KavitaDiscoveryService
from mylar.extensions.providers.kavita.auth import KavitaConfigurationError


def handle_test_kavita(service=None, **kwargs):
    """
    HTTP handler for POST /testKavita.
    Executes a single bounded read-only capability check using saved, validated server settings.
    Ignores any client-supplied URL or API key overrides.
    Guarantees that zero credentials, authorization headers, or raw response bodies are returned.

    :param service: Optional injected KavitaConnectionService for testing
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

    config_status = get_kavita_config_status()

    # If unconfigured on server, fail closed immediately
    if not config_status.get('url'):
        return json.dumps({
            'success': False,
            'configured': False,
            'enabled': config_status.get('enabled', False),
            'reachable': False,
            'authenticated': False,
            'api_compatible': False,
            'library_list_readable': False,
            'supported_library_types': [],
            'comic_library_type_available': False,
            'discovered_comic_type': None,
            'library_specific_scan_advertised': False,
            'message': 'Kavita server URL is not configured.',
            'error_code': 'missing_url'
        })

    if not config_status.get('has_api_key'):
        return json.dumps({
            'success': False,
            'configured': False,
            'enabled': config_status.get('enabled', False),
            'reachable': False,
            'authenticated': False,
            'api_compatible': False,
            'library_list_readable': False,
            'supported_library_types': [],
            'comic_library_type_available': False,
            'discovered_comic_type': None,
            'library_specific_scan_advertised': False,
            'message': 'Kavita API key is not configured.',
            'error_code': 'missing_api_key'
        })

    try:
        credentials = build_kavita_credentials()
    except KavitaConfigurationError as e:
        return json.dumps({
            'success': False,
            'configured': False,
            'enabled': config_status.get('enabled', False),
            'reachable': False,
            'authenticated': False,
            'api_compatible': False,
            'library_list_readable': False,
            'supported_library_types': [],
            'comic_library_type_available': False,
            'discovered_comic_type': None,
            'library_specific_scan_advertised': False,
            'message': str(e),
            'error_code': e.error_code
        })

    svc = service or KavitaConnectionService()
    result = svc.check_capability(
        base_url=config_status.get('url'),
        credentials=credentials
    )

    return json.dumps(result)


def handle_kavita_diagnostics(service=None, **kwargs):
    """
    HTTP handler for POST /kavitaDiagnostics.
    Executes a read-only library discovery and path observation diagnostic run.
    Strictly ignores any client-supplied URL, key, endpoint, or parameter overrides.
    The diagnostic endpoint itself never initializes, generates, or rotates MYLAR_INSTANCE_ID.
    Performs zero database writes, zero mapping writes, zero root-binding writes, and zero filesystem writes.

    :param service: Optional injected KavitaDiscoveryService for testing
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

    # Client-supplied overrides in kwargs are strictly ignored; server-saved configuration is used exclusively
    svc = service or KavitaDiscoveryService()
    result = svc.run_discovery()

    try:
        from mylar.extensions.providers.kavita.publisher_service import (
            get_kavita_publisher_mappings,
            get_latest_automation_notice
        )
        result['publisher_mappings'] = get_kavita_publisher_mappings()
        result['automation_notice'] = get_latest_automation_notice()
    except Exception:
        result['publisher_mappings'] = []
        result['automation_notice'] = None

    return json.dumps(result)


def handle_kavita_config_update(
    kavita_enabled=None,
    kavita_url=None,
    kavita_api_key=None,
    clear_kavita_api_key=False,
    **kwargs
):
    """
    HTTP handler for POST /kavitaConfigUpdate.
    Updates and persists Kavita configuration settings from Modern interface.
    Preserves blank secret submissions and respects explicit clear checkbox.
    Returns sanitized JSON status response with zero secret leakage.
    """
    if hasattr(cherrypy, 'response'):
        cherrypy.response.headers['Content-Type'] = 'application/json'

    if hasattr(cherrypy, 'request') and getattr(cherrypy.request, 'method', None) != 'POST':
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 405
        return json.dumps({
            'success': False,
            'status_code': 405,
            'message': 'Method Not Allowed. POST is required.',
            'error_code': 'method_not_allowed'
        })

    import mylar
    from mylar import encrypted
    from mylar.config import get_mylar_instance_slug
    from mylar.extensions.providers.kavita.config import validate_kavita_url

    # 1. Update kavita_enabled if explicitly passed
    if kavita_enabled is not None:
        if isinstance(kavita_enabled, bool):
            mylar.CONFIG.KAVITA_ENABLED = kavita_enabled
        else:
            mylar.CONFIG.KAVITA_ENABLED = str(kavita_enabled).strip().lower() in ('1', 'true', 'on', 'yes')

    # 2. Update kavita_url if passed
    if kavita_url is not None:
        url_str = str(kavita_url).strip()
        if url_str:
            try:
                mylar.CONFIG.KAVITA_URL = validate_kavita_url(url_str)
            except Exception as e:
                logger.warn(f"[KAVITA] Invalid server URL provided: {e}")
                if hasattr(cherrypy, 'response'):
                    cherrypy.response.status = 400
                return json.dumps({
                    'success': False,
                    'status_code': 400,
                    'message': f"Invalid Kavita Server URL: {e}",
                    'error_code': 'invalid_url'
                })
        else:
            mylar.CONFIG.KAVITA_URL = ''

    # 3. Update kavita_api_key
    is_clear = clear_kavita_api_key in (True, '1', 'true', 'True', 'on', 1)
    if is_clear:
        mylar.CONFIG.KAVITA_API_KEY = None
    elif kavita_api_key is not None:
        key_str = str(kavita_api_key).strip()
        if key_str:
            mylar.CONFIG.KAVITA_API_KEY = key_str
        # If key_str is empty, preserve existing stored key unchanged

    # 4. Persist to config.ini
    try:
        if hasattr(mylar, 'CONFIG') and hasattr(mylar.CONFIG, 'writeconfig'):
            kavita_values = {
                'kavita_enabled': mylar.CONFIG.KAVITA_ENABLED,
                'kavita_url': mylar.CONFIG.KAVITA_URL,
                'kavita_api_key': mylar.CONFIG.KAVITA_API_KEY
            }
            mylar.CONFIG.writeconfig(values=kavita_values)
    except Exception as e:
        logger.error(f"[KAVITA] Failed to persist config to disk: {e}")
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 500
        return json.dumps({
            'success': False,
            'status_code': 500,
            'message': 'Failed to save configuration to disk.',
            'error_code': 'write_failed'
        })

    return json.dumps({
        'success': True,
        'status_code': 200,
        'message': 'Kavita configuration saved successfully.',
        'kavita_enabled': bool(getattr(mylar.CONFIG, 'KAVITA_ENABLED', False)),
        'kavita_url': getattr(mylar.CONFIG, 'KAVITA_URL', '') or '',
        'has_api_key': bool(getattr(mylar.CONFIG, 'KAVITA_API_KEY', None)),
        'mylar_instance_slug': get_mylar_instance_slug()
    })


# -----------------------------------------------------------------------------
# CSRF Helpers & Backfill Handlers (Phase K8)
# -----------------------------------------------------------------------------

_GLOBAL_KAVITA_CSRF_FALLBACK = secrets.token_hex(32)


def get_or_create_kavita_csrf_token():
    """Retrieve or initialize the active CSRF token for Kavita web endpoints."""
    if hasattr(cherrypy, 'session') and isinstance(cherrypy.session, dict):
        token = cherrypy.session.get('_kavita_csrf_token')
        if not token:
            token = secrets.token_hex(32)
            cherrypy.session['_kavita_csrf_token'] = token
        return token
    return _GLOBAL_KAVITA_CSRF_FALLBACK


def verify_kavita_csrf_token(token):
    """Verify the supplied CSRF token against current session or fallback token."""
    if not token or not isinstance(token, str) or not token.strip():
        return False

    clean_token = token.strip()
    expected = None
    if hasattr(cherrypy, 'session') and isinstance(cherrypy.session, dict):
        expected = cherrypy.session.get('_kavita_csrf_token')
    if not expected:
        expected = _GLOBAL_KAVITA_CSRF_FALLBACK

    return hmac.compare_digest(clean_token, expected)


def _validate_request_method():
    """Ensure HTTP method is POST."""
    if hasattr(cherrypy, 'request'):
        method = getattr(cherrypy.request, 'method', 'GET')
        if method != 'POST':
            return False
    return True


def _validate_csrf(csrf_token):
    """Check CSRF token from argument or HTTP header."""
    header_token = None
    if hasattr(cherrypy, 'request') and hasattr(cherrypy.request, 'headers'):
        header_token = (
            cherrypy.request.headers.get('X-CSRF-Token') or
            cherrypy.request.headers.get('X-CSRFToken')
        )
    candidate_token = csrf_token or header_token
    return verify_kavita_csrf_token(candidate_token)


def handle_kavita_sync_backfill_preview(worker=None, csrf_token=None, **kwargs):
    """
    HTTP handler for POST /kavitaSyncBackfillPreview.
    Performs read-only candidate analysis with zero remote Kavita API calls.
    Enforces POST method and CSRF validation.
    """
    if hasattr(cherrypy, 'response'):
        cherrypy.response.headers['Content-Type'] = 'application/json'

    if not _validate_request_method():
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 405
        return json.dumps({
            'status': 'error',
            'status_code': 405,
            'message': 'Method Not Allowed. POST is required.',
            'error_code': 'method_not_allowed'
        })

    token = csrf_token or kwargs.get('csrf_token')
    if not _validate_csrf(token):
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 403
        return json.dumps({
            'status': 'error',
            'status_code': 403,
            'message': 'Invalid or missing CSRF token.',
            'error_code': 'invalid_csrf_token'
        })

    from mylar.extensions.providers.kavita.backfill_worker import KavitaSyncBackfillWorker
    active_worker = worker or KavitaSyncBackfillWorker()
    preview_res = active_worker.compute_preview()
    if preview_res.get('status') == 'error':
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 400
    return json.dumps(preview_res)


def handle_kavita_sync_backfill(worker=None, service=None, csrf_token=None, **kwargs):
    """
    HTTP handler for POST /kavitaSyncBackfill.
    Starts background synchronization job for existing unmapped series.
    Returns promptly with job ID while worker runs asynchronously.
    Enforces POST method and CSRF validation.
    """
    if hasattr(cherrypy, 'response'):
        cherrypy.response.headers['Content-Type'] = 'application/json'

    if not _validate_request_method():
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 405
        return json.dumps({
            'status': 'error',
            'status_code': 405,
            'message': 'Method Not Allowed. POST is required.',
            'error_code': 'method_not_allowed'
        })

    token = csrf_token or kwargs.get('csrf_token')
    if not _validate_csrf(token):
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 403
        return json.dumps({
            'status': 'error',
            'status_code': 403,
            'message': 'Invalid or missing CSRF token.',
            'error_code': 'invalid_csrf_token'
        })

    from mylar.extensions.providers.kavita.backfill_worker import KavitaSyncBackfillWorker
    active_worker = worker or KavitaSyncBackfillWorker()
    start_res = active_worker.start_backfill(service=service)

    if start_res.get('status') == 'busy':
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 409
    elif start_res.get('status') == 'error':
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 400

    return json.dumps(start_res)


def handle_kavita_sync_backfill_status(worker=None, csrf_token=None, job_id=None, **kwargs):
    """
    HTTP handler for POST /kavitaSyncBackfillStatus.
    Returns sanitized in-memory snapshot of worker progress and metric counters.
    Rejects mismatched job_id with 404 job_not_found.
    Enforces POST method and CSRF validation.
    """
    if hasattr(cherrypy, 'response'):
        cherrypy.response.headers['Content-Type'] = 'application/json'

    if not _validate_request_method():
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 405
        return json.dumps({
            'status': 'error',
            'status_code': 405,
            'message': 'Method Not Allowed. POST is required.',
            'error_code': 'method_not_allowed'
        })

    token = csrf_token or kwargs.get('csrf_token')
    if not _validate_csrf(token):
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 403
        return json.dumps({
            'status': 'error',
            'status_code': 403,
            'message': 'Invalid or missing CSRF token.',
            'error_code': 'invalid_csrf_token'
        })

    from mylar.extensions.providers.kavita.backfill_worker import KavitaSyncBackfillWorker
    active_worker = worker or KavitaSyncBackfillWorker()
    jid = job_id or kwargs.get('job_id')
    status_res = active_worker.get_status(job_id=jid)
    if status_res.get('status_code') == 404:
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 404
    return json.dumps(status_res)
