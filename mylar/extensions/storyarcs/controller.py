"""
Story Arc HTTP Controller.

Handles HTTP routes for Story Arc catalog, detail views, CBL import/preview/delete endpoints,
and DieselTech repository catalog browsing, formatting CherryPy responses and templates.
"""

import json
import cherrypy

from mylar.extensions.storyarcs import service, cbl_service, cbl_catalog


def handle_storyarc_main(arcid=None, serve_template_fn=None, **kwargs):
    """
    HTTP handler for /storyarc_main route.
    Renders the modern/classic Story Arc explorer catalog template or returns arc data.
    """
    arclist = service.get_storyarc_catalog(arcid=arcid)
    if arcid is None:
        if serve_template_fn:
            return serve_template_fn(templatename="storyarc.html", title="Story Arcs", arclist=arclist, delete_type=0)
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
            publisher=detail.get('publisher', 'Unknown')
        )
    return detail


def handle_cbl_upload(cbl_file=None, **kwargs):
    """
    HTTP handler for /cbl_upload endpoint.
    Accepts multipart/form-data CBL uploads, performs security validation and initial reconciliation.
    """
    cherrypy.response.headers['Content-Type'] = 'application/json'
    if not cbl_file:
        return json.dumps({'status': 'error', 'message': 'No file was provided for upload.'})

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

    result = cbl_service.upload_cbl_manifest(raw_bytes, orig_filename)
    return json.dumps(result)


def handle_cbl_preview(token=None, **kwargs):
    """
    HTTP handler for /cbl_preview endpoint.
    Returns authoritative reconciliation preview for a staged or uploaded token.
    """
    cherrypy.response.headers['Content-Type'] = 'application/json'
    result = cbl_service.preview_cbl_manifest(token=token)
    return json.dumps(result)


def handle_cbl_confirm_import(token=None, **kwargs):
    """
    HTTP handler for /cbl_confirm_import endpoint.
    Atomically imports the validated reading list into storyarc_manifests and storyarcs.
    """
    cherrypy.response.headers['Content-Type'] = 'application/json'
    result = cbl_service.confirm_cbl_import(token=token, filename=kwargs.get('filename'))
    return json.dumps(result)


def handle_cbl_delete_arc(storyarcid=None, **kwargs):
    """
    HTTP handler for /cbl_delete_arc endpoint.
    Safely removes a Story Arc and cleans up unreferenced raw CBL files.
    """
    cherrypy.response.headers['Content-Type'] = 'application/json'
    result = cbl_service.delete_cbl_arc(storyarcid=storyarcid)
    return json.dumps(result)


def handle_cbl_catalog_status(**kwargs):
    """
    HTTP handler for /cbl_catalog_status endpoint.
    Returns local snapshot status metadata without making outbound requests.
    """
    cherrypy.response.headers['Content-Type'] = 'application/json'
    status = cbl_catalog.get_catalog_status()
    return json.dumps(status)


def handle_cbl_catalog_refresh(**kwargs):
    """
    HTTP handler for /cbl_catalog_refresh endpoint.
    Explicitly fetches and saves the latest DieselTech repository tree snapshot.
    """
    cherrypy.response.headers['Content-Type'] = 'application/json'
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
    result = cbl_catalog.fetch_and_stage_catalog_cbl(entry_id=entry_id)
    return json.dumps(result)
