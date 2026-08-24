"""
Creator Browser Controller (Phase C3).

HTTP controller handling routes for the Creator Credits Catalog and Detail views.
"""

import json
import cherrypy

from mylar import logger
from mylar.extensions.creators.browser_service import CreatorBrowserService
from mylar.extensions.creators.schema import ROLES


def handle_creator_catalog(search=None, role=None, sort='name_asc', page=1, page_size=24, serve_template_fn=None, **kwargs):
    """
    HTTP handler for /creators catalog route.

    :param search: Search string for raw/normalized creator name
    :param role: Filter by specific role (e.g. 'writer', 'cover_artist')
    :param sort: Sorting key
    :param page: 1-indexed page number
    :param page_size: Page size
    :param serve_template_fn: Mylar template renderer function
    :return: HTML string from template or JSON dict
    """
    service = CreatorBrowserService()
    catalog = service.get_creator_catalog(
        search=search,
        role=role,
        sort=sort,
        page=page,
        page_size=page_size
    )

    if kwargs.get('format') == 'json':
        cherrypy.response.headers['Content-Type'] = 'application/json'
        return json.dumps(catalog)

    if serve_template_fn:
        return serve_template_fn(
            templatename="creators.html",
            title="Creator Credits",
            catalog=catalog,
            roles=ROLES,
            search=search or '',
            current_role=role or '',
            current_sort=sort or 'name_asc'
        )

    return catalog


def handle_creator_detail(name_record_id=None, NameRecordID=None, role=None, type=None, comicid=None, sort='date_desc', serve_template_fn=None, **kwargs):
    """
    HTTP handler for /creator_detail route.

    :param name_record_id: Authoritative NameRecordID
    :param NameRecordID: Alias parameter for NameRecordID
    :param role: Optional publication role filter
    :param type: Publication type filter ('all', 'issues', 'annuals')
    :param comicid: Optional series ComicID filter
    :param sort: Sort order for publications
    :param serve_template_fn: Mylar template renderer function
    :return: HTML string from template or JSON dict
    """
    rec_id = NameRecordID if NameRecordID is not None else name_record_id
    if rec_id is None:
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 400
        if serve_template_fn:
            return serve_template_fn(
                templatename="creator_detail.html",
                title="Creator Credit Not Found",
                detail={'found': False, 'error': 'Missing NameRecordID'}
            )
        return {'found': False, 'error': 'Missing NameRecordID'}

    service = CreatorBrowserService()
    detail = service.get_creator_detail(
        name_record_id=rec_id,
        role_filter=role,
        pub_type=type or 'all',
        series_filter=comicid,
        sort=sort or 'date_desc'
    )

    if not detail or not detail.get('found'):
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 404
        if serve_template_fn:
            return serve_template_fn(
                templatename="creator_detail.html",
                title="Creator Credit Not Found",
                detail={'found': False, 'error': 'The requested creator credit record was not found or has been removed.'}
            )
        return {'found': False, 'error': 'Record not found'}

    if kwargs.get('format') == 'json':
        cherrypy.response.headers['Content-Type'] = 'application/json'
        return json.dumps(detail)

    if serve_template_fn:
        return serve_template_fn(
            templatename="creator_detail.html",
            title=f"Creator Credit — {detail['raw_name']}",
            detail=detail,
            roles=ROLES,
            current_role=role or '',
            current_type=type or 'all',
            current_comicid=comicid or '',
            current_sort=sort or 'date_desc'
        )

    return detail


def handle_issue_creator_credits(issueid=None, annual=0, **kwargs):
    """
    HTTP handler for /issueCreatorCredits endpoint.
    Returns structured creator credits for an issue or annual publication.

    :param issueid: Issue identifier (int or numeric string)
    :param annual: 0 for regular issue, 1 for annual
    :return: JSON string with Content-Type: application/json
    """
    if hasattr(cherrypy, 'response'):
        cherrypy.response.headers['Content-Type'] = 'application/json'

    # 1. Validate issueid
    if issueid is None:
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 400
        return json.dumps({'error': 'Missing issueid parameter', 'success': False})

    try:
        val_issueid = int(str(issueid).strip())
        if val_issueid <= 0:
            raise ValueError("IssueID must be positive")
    except (ValueError, TypeError):
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 400
        return json.dumps({'error': 'Invalid issueid parameter (must be a positive integer)', 'success': False})

    # 2. Validate annual
    if str(annual).strip() not in ('0', '1', 'True', 'False', 'true', 'false'):
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 400
        return json.dumps({'error': 'Invalid annual parameter (must be 0 or 1)', 'success': False})

    val_annual = 1 if str(annual).strip() in ('1', 'True', 'true') else 0

    service = CreatorBrowserService()
    result = service.get_issue_creator_credits(val_issueid, is_annual=val_annual)
    return json.dumps(result)

