"""
Creator Identity Registry Controller (Phase C4.12).

Thin, extension-owned HTTP controller for the Creator Identity Registry surface and API.
Handles input extraction, domain error handling, and sanitized responses.
Contains zero SQL queries, zero provider logic, and zero mutation logic.
"""

import json
from typing import Any, Callable, Dict, Optional
import cherrypy

from mylar import logger
from mylar.extensions.creators.registry_service import (
    CreatorRegistryService,
    InvalidRegistryInputError,
    CreatorRegistryError
)


def handle_creator_registry(
    state: Optional[str] = 'all',
    provider: Optional[str] = 'all',
    search: Optional[str] = None,
    sort: Optional[str] = 'name_asc',
    page: int = 1,
    page_size: int = 25,
    service: Optional[CreatorRegistryService] = None,
    serve_template_fn: Optional[Callable[..., str]] = None,
    **kwargs
) -> Any:
    """
    HTTP handler for the /creator_registry route.
    Renders the Modern Creator Identity Registry template or returns JSON.
    """
    reg_service = service or CreatorRegistryService()

    try:
        data = reg_service.get_registry_entries(
            state=state,
            provider=provider,
            search=search,
            sort=sort,
            page=page,
            page_size=page_size
        )
    except InvalidRegistryInputError as e:
        logger.warning(f"[CreatorRegistry] Invalid input parameter: {e}")
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 400
        err_dict = {
            'success': False,
            'error': str(e),
            'error_code': 'invalid_input',
            'entries': [],
            'total_entries': 0,
            'counts': {'all': 0, 'confirmed': 0, 'rejected': 0, 'reversed': 0, 'conflicted': 0}
        }
        if kwargs.get('format') == 'json':
            cherrypy.response.headers['Content-Type'] = 'application/json'
            return json.dumps(err_dict)
        if serve_template_fn:
            return serve_template_fn(
                templatename="creator_registry.html",
                title="Creator Identity Registry",
                registry=err_dict,
                search=search or '',
                current_state=state or 'all',
                current_provider=provider or 'all',
                current_sort=sort or 'name_asc',
                error_msg=str(e)
            )
        return err_dict
    except Exception as e:
        logger.error(f"[CreatorRegistry] Internal error loading registry: {e}")
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 500
        err_dict = {
            'success': False,
            'error': 'Internal server error while loading creator registry.',
            'error_code': 'server_error',
            'entries': [],
            'total_entries': 0,
            'counts': {'all': 0, 'confirmed': 0, 'rejected': 0, 'reversed': 0, 'conflicted': 0}
        }
        if kwargs.get('format') == 'json':
            cherrypy.response.headers['Content-Type'] = 'application/json'
            return json.dumps(err_dict)
        if serve_template_fn:
            return serve_template_fn(
                templatename="creator_registry.html",
                title="Creator Identity Registry",
                registry=err_dict,
                search=search or '',
                current_state=state or 'all',
                current_provider=provider or 'all',
                current_sort=sort or 'name_asc',
                error_msg='Internal server error while loading creator registry.'
            )
        return err_dict

    if kwargs.get('format') == 'json':
        if hasattr(cherrypy, 'response'):
            cherrypy.response.headers['Content-Type'] = 'application/json'
        return json.dumps(data)

    if serve_template_fn:
        return serve_template_fn(
            templatename="creator_registry.html",
            title="Creator Identity Registry",
            registry=data,
            search=search or '',
            current_state=state or 'all',
            current_provider=provider or 'all',
            current_sort=sort or 'name_asc',
            error_msg=None
        )

    return data


def handle_get_creator_registry_json(
    state: Optional[str] = 'all',
    provider: Optional[str] = 'all',
    search: Optional[str] = None,
    sort: Optional[str] = 'name_asc',
    page: int = 1,
    page_size: int = 25,
    service: Optional[CreatorRegistryService] = None,
    **kwargs
) -> str:
    """
    JSON API handler for AJAX queries from the Creator Identity Registry UI.
    """
    if hasattr(cherrypy, 'response'):
        cherrypy.response.headers['Content-Type'] = 'application/json'

    reg_service = service or CreatorRegistryService()

    try:
        data = reg_service.get_registry_entries(
            state=state,
            provider=provider,
            search=search,
            sort=sort,
            page=page,
            page_size=page_size
        )
        return json.dumps(data)
    except InvalidRegistryInputError as e:
        logger.warning(f"[CreatorRegistry] Invalid JSON query input: {e}")
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 400
        return json.dumps({
            'success': False,
            'error': str(e),
            'error_code': 'invalid_input'
        })
    except Exception as e:
        logger.error(f"[CreatorRegistry] Unexpected error during registry JSON query: {e}")
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 500
        return json.dumps({
            'success': False,
            'error': 'Internal server error while retrieving creator registry.',
            'error_code': 'server_error'
        })
