"""
Story Arc HTTP Controller.

Handles HTTP routes for Story Arc catalog, detail views, CBL import/preview/delete endpoints,
and DieselTech repository catalog browsing, formatting CherryPy responses and templates.
"""

import json
import secrets
import hmac
import cherrypy

from mylar.extensions.storyarcs import service, cbl_service, cbl_catalog


_GLOBAL_CBL_CSRF_FALLBACK = secrets.token_hex(32)


def get_or_create_cbl_csrf_token():
    """
    Retrieve or generate a secure session-backed CSRF token for Story Arc / CBL mutations.
    """
    if hasattr(cherrypy, 'session') and isinstance(cherrypy.session, dict):
        token = cherrypy.session.get('_cbl_csrf_token')
        if not token:
            token = secrets.token_hex(32)
            cherrypy.session['_cbl_csrf_token'] = token
        return token
    return _GLOBAL_CBL_CSRF_FALLBACK


def verify_cbl_csrf_token(token):
    """
    Verify the supplied CSRF token against current session or active fallback token.
    """
    if not token or not isinstance(token, str) or not token.strip():
        return False
    clean_token = token.strip()
    expected = None
    if hasattr(cherrypy, 'session') and isinstance(cherrypy.session, dict):
        expected = cherrypy.session.get('_cbl_csrf_token')
    if not expected:
        expected = _GLOBAL_CBL_CSRF_FALLBACK
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
    return verify_cbl_csrf_token(candidate_token)


def handle_get_cbl_csrf_token(**kwargs):
    """
    HTTP endpoint to retrieve the active CSRF token.
    """
    cherrypy.response.headers['Content-Type'] = 'application/json'
    token = get_or_create_cbl_csrf_token()
    return json.dumps({'status': 'success', 'csrf_token': token})


def handle_storyarc_main(arcid=None, serve_template_fn=None, **kwargs):
    """
    HTTP handler for /storyarc_main route.
    Renders the modern/classic Story Arc explorer catalog template or returns arc data.
    """
    arclist = service.get_storyarc_catalog(arcid=arcid)
    if arcid is None:
        if serve_template_fn:
            csrf_token = get_or_create_cbl_csrf_token()
            return serve_template_fn(
                templatename="storyarc.html",
                title="Story Arcs",
                arclist=arclist,
                delete_type=0,
                cbl_csrf_token=csrf_token,
                **kwargs
            )
        return arclist
    else:
        return arclist[0] if arclist else None


def handle_detail_storyarc(StoryArcID, StoryArcName=None, CV_ArcID=None, serve_template_fn=None, **kwargs):
    """
    HTTP handler for /detailStoryArc route.
    Renders the Story Arc detail view with complete template context for Classic, Carbon, and Modern interfaces.
    """
    detail = service.get_storyarc_detail(StoryArcID, storyarc_name=StoryArcName, cv_arc_id=CV_ArcID)
    if serve_template_fn:
        csrf_token = get_or_create_cbl_csrf_token()
        return serve_template_fn(
            templatename=detail.get('template', 'storyarc_detail.html'),
            title=f"Story Arc - {detail['storyarcname']}" if detail.get('found') else "Detailed Arc list",
            readlist=detail['readlist'],
            storyarcname=detail['storyarcname'],
            storyarcid=detail['storyarcid'],
            cvarcid=detail.get('cvarcid'),
            sdir=detail.get('sdir'),
            arcdetail=detail.get('arcdetail', {}),
            storyarcbanner=detail.get('storyarcbanner'),
            bannerheight=detail.get('bannerheight', '280'),
            bannerwidth=detail.get('bannerwidth', '960'),
            manifest=detail.get('manifest'),
            have_count=detail.get('have_count', 0),
            total_count=detail.get('total_count', 0),
            percent=detail.get('percent', 0),
            spanyears=detail.get('spanyears'),
            publisher=detail.get('publisher', 'Unknown'),
            cbl_csrf_token=csrf_token,
            **kwargs
        )
    return detail


def _to_bool(val, default=None):
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        v = val.strip().lower()
        if v in ('true', '1', 'yes', 'y', 't', 'on'):
            return True
        if v in ('false', '0', 'no', 'n', 'f', 'off'):
            return False
    return default


def handle_cbl_upload(cbl_file=None, csrf_token=None, **kwargs):
    """
    HTTP handler for /cbl_upload endpoint.
    Accepts multipart/form-data CBL uploads, enforces POST and CSRF validation, and returns initial reconciliation.
    """
    cherrypy.response.headers['Content-Type'] = 'application/json'
    if not _validate_request_method():
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 405
        return json.dumps({'status': 'error', 'error_code': 'method_not_allowed', 'message': 'POST request required for upload.'})

    if not _validate_csrf(csrf_token or kwargs.get('csrf_token')):
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 403
        return json.dumps({'status': 'error', 'error_code': 'invalid_csrf_token', 'message': 'CSRF verification failed.'})

    if not cbl_file:
        return json.dumps({'status': 'error', 'error_code': 'missing_file', 'message': 'No file was provided for upload.'})

    import_mode = kwargs.get('import_mode', 'apply_library')
    if import_mode not in ('apply_library', 'reading_list_only'):
        import_mode = 'apply_library'
    issuesonly = _to_bool(kwargs.get('issuesonly'))
    ignorearchived = _to_bool(kwargs.get('ignorearchived'))

    raw_bytes = None
    orig_filename = 'uploaded.cbl'
    try:
        if hasattr(cbl_file, 'file'):
            orig_filename = getattr(cbl_file, 'filename', '') or 'uploaded.cbl'
            raw_bytes = cbl_file.file.read(5 * 1024 * 1024 + 1)
        elif hasattr(cbl_file, 'value'):
            orig_filename = getattr(cbl_file, 'filename', '') or 'uploaded.cbl'
            raw_bytes = cbl_file.value
        elif isinstance(cbl_file, (bytes, bytearray)):
            raw_bytes = bytes(cbl_file)
        elif isinstance(cbl_file, str):
            raw_bytes = cbl_file.encode('utf-8')
    except Exception as e:
        return json.dumps({'status': 'error', 'message': f'Error reading upload: {e}'})

    result = cbl_service.upload_cbl_manifest(
        raw_bytes,
        orig_filename,
        import_mode=import_mode,
        issuesonly=issuesonly,
        ignorearchived=ignorearchived
    )
    return json.dumps(result)


def handle_cbl_preview(token=None, **kwargs):
    """
    HTTP handler for /cbl_preview endpoint.
    Returns authoritative reconciliation preview for a staged or uploaded token.
    """
    cherrypy.response.headers['Content-Type'] = 'application/json'
    if not token or not isinstance(token, str) or not token.strip():
        return json.dumps({'status': 'error', 'error_code': 'missing_token', 'message': 'No manifest token provided.'})

    import_mode = kwargs.get('import_mode', 'apply_library')
    if import_mode not in ('apply_library', 'reading_list_only'):
        import_mode = 'apply_library'
    issuesonly = _to_bool(kwargs.get('issuesonly'))
    ignorearchived = _to_bool(kwargs.get('ignorearchived'))

    result = cbl_service.preview_cbl_manifest(
        token=token.strip(),
        import_mode=import_mode,
        issuesonly=issuesonly,
        ignorearchived=ignorearchived
    )
    return json.dumps(result)


def handle_cbl_confirm_import(token=None, csrf_token=None, **kwargs):
    """
    HTTP handler for /cbl_confirm_import endpoint.
    Enforces POST and CSRF validation, and atomically imports the validated reading list into storyarc_manifests and storyarcs.
    """
    cherrypy.response.headers['Content-Type'] = 'application/json'
    if not _validate_request_method():
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 405
        return json.dumps({'status': 'error', 'error_code': 'method_not_allowed', 'message': 'POST request required for import confirmation.'})

    if not _validate_csrf(csrf_token or kwargs.get('csrf_token')):
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 403
        return json.dumps({'status': 'error', 'error_code': 'invalid_csrf_token', 'message': 'CSRF verification failed.'})

    if not token or not isinstance(token, str) or not token.strip():
        return json.dumps({'status': 'error', 'error_code': 'missing_token', 'message': 'No manifest token provided.'})

    import_mode = kwargs.get('import_mode', 'apply_library')
    if import_mode not in ('apply_library', 'reading_list_only'):
        import_mode = 'apply_library'
    issuesonly = _to_bool(kwargs.get('issuesonly'))
    ignorearchived = _to_bool(kwargs.get('ignorearchived'))

    result = cbl_service.confirm_cbl_import(
        token=token.strip(),
        filename=kwargs.get('filename'),
        import_mode=import_mode,
        issuesonly=issuesonly,
        ignorearchived=ignorearchived
    )
    return json.dumps(result)


def handle_cbl_reconcile_arc(storyarcid=None, csrf_token=None, **kwargs):
    """
    HTTP handler for /cbl_reconcile_arc endpoint.
    Reconciles an already-imported Story Arc with the library.
    GET allowed for preview (apply=false); POST + CSRF required when apply=true.
    """
    cherrypy.response.headers['Content-Type'] = 'application/json'
    apply_changes = _to_bool(kwargs.get('apply'), default=False)

    if apply_changes:
        if not _validate_request_method():
            if hasattr(cherrypy, 'response'):
                cherrypy.response.status = 405
            return json.dumps({'status': 'error', 'error_code': 'method_not_allowed', 'message': 'POST request required to apply reconciliation.'})
        if not _validate_csrf(csrf_token or kwargs.get('csrf_token')):
            if hasattr(cherrypy, 'response'):
                cherrypy.response.status = 403
            return json.dumps({'status': 'error', 'error_code': 'invalid_csrf_token', 'message': 'CSRF verification failed.'})

    if not storyarcid or not isinstance(storyarcid, str) or not storyarcid.strip():
        return json.dumps({'status': 'error', 'error_code': 'missing_storyarcid', 'message': 'StoryArcID is required.'})

    import_mode = kwargs.get('import_mode', 'apply_library')
    if import_mode not in ('apply_library', 'reading_list_only'):
        import_mode = 'apply_library'
    issuesonly = _to_bool(kwargs.get('issuesonly'))
    ignorearchived = _to_bool(kwargs.get('ignorearchived'))

    result = cbl_service.reconcile_existing_storyarc(
        storyarc_id=storyarcid.strip(),
        import_mode=import_mode,
        issuesonly=issuesonly,
        ignorearchived=ignorearchived,
        apply_changes=apply_changes
    )
    return json.dumps(result)


def handle_cbl_entry_action(storyarcid=None, issue_arc_id=None, action=None, csrf_token=None, **kwargs):
    """
    HTTP handler for /cbl_entry_action endpoint.
    Enforces POST, CSRF validation, action whitelisting, and executes a single granular action on a Story Arc entry.
    """
    cherrypy.response.headers['Content-Type'] = 'application/json'
    if not _validate_request_method():
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 405
        return json.dumps({'status': 'error', 'error_code': 'method_not_allowed', 'message': 'POST request required for entry action.'})

    if not _validate_csrf(csrf_token or kwargs.get('csrf_token')):
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 403
        return json.dumps({'status': 'error', 'error_code': 'invalid_csrf_token', 'message': 'CSRF verification failed.'})

    if not storyarcid or not isinstance(storyarcid, str) or not storyarcid.strip():
        return json.dumps({'status': 'error', 'error_code': 'missing_storyarcid', 'message': 'StoryArcID is required.'})

    target_issue_arc = issue_arc_id or kwargs.get('issueid') or kwargs.get('comicid')
    if not target_issue_arc or not isinstance(target_issue_arc, str) or not target_issue_arc.strip():
        return json.dumps({'status': 'error', 'error_code': 'missing_entry_id', 'message': 'Entry identifier is required.'})

    if not action or action not in ('add_series', 'mark_wanted', 'retry_resolution'):
        return json.dumps({'status': 'error', 'error_code': 'invalid_action', 'message': f'Invalid action: {action}'})

    issuesonly = _to_bool(kwargs.get('issuesonly'))
    ignorearchived = _to_bool(kwargs.get('ignorearchived'))

    result = cbl_service.execute_entry_action(
        storyarc_id=storyarcid.strip(),
        issue_arc_id=target_issue_arc.strip(),
        action=action,
        issuesonly=issuesonly,
        ignorearchived=ignorearchived
    )
    return json.dumps(result)


def handle_cbl_delete_arc(storyarcid=None, csrf_token=None, **kwargs):
    """
    HTTP handler for /cbl_delete_arc endpoint.
    Enforces POST and CSRF validation, safely removes a Story Arc, and cleans up unreferenced raw CBL files.
    """
    cherrypy.response.headers['Content-Type'] = 'application/json'
    if not _validate_request_method():
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 405
        return json.dumps({'status': 'error', 'error_code': 'method_not_allowed', 'message': 'POST request required for arc deletion.'})

    if not _validate_csrf(csrf_token or kwargs.get('csrf_token')):
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 403
        return json.dumps({'status': 'error', 'error_code': 'invalid_csrf_token', 'message': 'CSRF verification failed.'})

    if not storyarcid or not isinstance(storyarcid, str) or not storyarcid.strip():
        return json.dumps({'status': 'error', 'error_code': 'missing_storyarcid', 'message': 'StoryArcID is required.'})

    result = cbl_service.delete_cbl_arc(storyarcid=storyarcid.strip())
    return json.dumps(result)


def handle_cbl_catalog_status(**kwargs):
    """
    HTTP handler for /cbl_catalog_status endpoint.
    Returns local snapshot status metadata without making outbound requests.
    """
    cherrypy.response.headers['Content-Type'] = 'application/json'
    status = cbl_catalog.get_catalog_status()
    return json.dumps(status)


def handle_cbl_catalog_refresh(csrf_token=None, **kwargs):
    """
    HTTP handler for /cbl_catalog_refresh endpoint.
    Enforces POST and CSRF validation, and fetches/saves latest DieselTech repository tree snapshot.
    """
    cherrypy.response.headers['Content-Type'] = 'application/json'
    if not _validate_request_method():
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 405
        return json.dumps({'status': 'error', 'error_code': 'method_not_allowed', 'message': 'POST request required for catalog refresh.'})

    if not _validate_csrf(csrf_token or kwargs.get('csrf_token')):
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 403
        return json.dumps({'status': 'error', 'error_code': 'invalid_csrf_token', 'message': 'CSRF verification failed.'})

    result = cbl_catalog.refresh_dieseltech_catalog()
    return json.dumps(result)


def handle_cbl_catalog_search(q="", publisher="", category="", limit=200, **kwargs):
    """
    HTTP handler for /cbl_catalog_search endpoint.
    Searches the local snapshot without querying external services.
    """
    cherrypy.response.headers['Content-Type'] = 'application/json'
    try:
        limit_val = int(limit)
    except (ValueError, TypeError):
        limit_val = 200
    result = cbl_catalog.search_catalog(query=q, publisher=publisher, category=category, limit=limit_val)
    return json.dumps(result)


def handle_cbl_catalog_preview(entry_id=None, **kwargs):
    """
    HTTP handler for /cbl_catalog_preview endpoint.
    Fetches raw CBL bytes for a selected entry ID from raw GitHub and returns reconciliation preview.
    """
    cherrypy.response.headers['Content-Type'] = 'application/json'
    if not entry_id or not isinstance(entry_id, str) or not entry_id.strip():
        return json.dumps({'status': 'error', 'error_code': 'missing_entry_id', 'message': 'entry_id is required.'})

    import_mode = kwargs.get('import_mode', 'apply_library')
    if import_mode not in ('apply_library', 'reading_list_only'):
        import_mode = 'apply_library'
    issuesonly = _to_bool(kwargs.get('issuesonly'))
    ignorearchived = _to_bool(kwargs.get('ignorearchived'))

    result = cbl_catalog.fetch_and_stage_catalog_cbl(
        entry_id=entry_id.strip(),
        import_mode=import_mode,
        issuesonly=issuesonly,
        ignorearchived=ignorearchived
    )
    return json.dumps(result)
