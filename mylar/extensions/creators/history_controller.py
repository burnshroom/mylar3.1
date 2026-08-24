"""
Creator Decision History Controller (Phase C4.11).

Provides extension-owned HTTP request handlers for querying read-only decision history.
Sanitizes responses, catches domain exceptions, sets standard HTTP status codes, and ensures
ZERO secrets or raw SQL traces are exposed.
Stateless and read-only.
"""

import json
from typing import Optional

import cherrypy
from mylar import logger
from mylar.extensions.creators.history_service import (
    CreatorHistoryService,
    CreatorHistoryError,
    InvalidHistoryInputError,
    LocalNameRecordNotFoundError
)


def _json_error(message: str, error_code: str, status_code: int = 400) -> str:
    """Return a sanitized JSON error payload and set HTTP status."""
    try:
        cherrypy.response.status = status_code
        cherrypy.response.headers['Content-Type'] = 'application/json'
    except Exception:
        pass
    return json.dumps({
        'success': False,
        'error': message,
        'error_code': error_code
    })


def _json_success(data: dict) -> str:
    """Return a successful JSON payload."""
    try:
        cherrypy.response.status = 200
        cherrypy.response.headers['Content-Type'] = 'application/json'
    except Exception:
        pass
    return json.dumps(data)


def handle_get_creator_decision_history(
    name_record_id: Optional[str] = None,
    provider: Optional[str] = None,
    provider_creator_id: Optional[str] = None,
    limit: Optional[str] = '50',
    offset: Optional[str] = '0',
    service: Optional[CreatorHistoryService] = None,
    **kwargs
) -> str:
    """
    Handle GET request to retrieve immutable decision history for an exact local NameRecordID.

    :param name_record_id: Exact local NameRecordID
    :param provider: Optional provider namespace filter ('metron')
    :param provider_creator_id: Optional provider creator ID filter
    :param limit: Optional pagination limit (default 50)
    :param offset: Optional pagination offset (default 0)
    :param service: Optional CreatorHistoryService dependency override
    :return: Sanitized JSON string
    """
    if name_record_id is None or str(name_record_id).strip() == '':
        return _json_error("Missing required parameter 'name_record_id'.", 'missing_parameter', 400)

    history_svc = service or CreatorHistoryService()

    try:
        res = history_svc.get_decision_history(
            name_record_id=name_record_id,
            provider=provider if provider else None,
            provider_creator_id=provider_creator_id if provider_creator_id else None,
            limit=limit if limit is not None else 50,
            offset=offset if offset is not None else 0
        )
        return _json_success(res)

    except InvalidHistoryInputError as e:
        logger.warning(f"[CreatorHistory] Invalid history query parameters: {str(e)}")
        return _json_error(str(e), 'invalid_input', 400)

    except LocalNameRecordNotFoundError as e:
        logger.warning(f"[CreatorHistory] NameRecord not found: {str(e)}")
        return _json_error(str(e), 'record_not_found', 404)

    except CreatorHistoryError as e:
        logger.error(f"[CreatorHistory] Decision history error: {str(e)}")
        return _json_error(str(e), 'history_error', 400)

    except Exception as e:
        logger.error(f"[CreatorHistory] Unexpected error querying decision history: {str(e)}")
        return _json_error("Internal error retrieving creator decision history.", 'internal_error', 500)
