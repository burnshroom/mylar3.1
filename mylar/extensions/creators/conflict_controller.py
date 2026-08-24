"""
Creator Identity Conflict Analysis Controller (Phase C4.13).

HTTP controller handling requests for creator identity conflict analysis.
Sanitizes responses, maps domain exceptions to HTTP status codes, and ensures zero mutation endpoints.
"""

import json
from typing import Any, Optional
import cherrypy

from mylar import logger
from mylar.extensions.creators.conflict_service import (
    CreatorConflictService,
    InvalidConflictInputError,
    LocalNameRecordNotFoundError,
    StaleConflictStateError,
    TransferTargetUnavailableError,
    CreatorConflictError
)


def handle_get_creator_conflict_analysis(
    name_record_id: Optional[Any] = None,
    provider: Optional[str] = None,
    provider_creator_id: Optional[Any] = None,
    service: Optional[CreatorConflictService] = None,
    **kwargs
) -> str:
    """
    HTTP handler for /getCreatorConflictAnalysis route.
    Returns sanitized JSON payload with conflict analysis breakdown.
    """
    if hasattr(cherrypy, 'response'):
        cherrypy.response.headers['Content-Type'] = 'application/json'

    conf_service = service or CreatorConflictService()

    try:
        data = conf_service.analyze_creator_conflict(
            name_record_id=name_record_id,
            provider=provider,
            provider_creator_id=provider_creator_id
        )
        return json.dumps(data)
    except InvalidConflictInputError as e:
        logger.warning(f"[CreatorConflict] Invalid conflict analysis parameters: {e}")
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 400
        return json.dumps({
            'success': False,
            'error': str(e),
            'error_code': 'invalid_input'
        })
    except LocalNameRecordNotFoundError as e:
        logger.warning(f"[CreatorConflict] NameRecord not found: {e}")
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 404
        return json.dumps({
            'success': False,
            'error': str(e),
            'error_code': 'record_not_found'
        })
    except Exception as e:
        logger.error(f"[CreatorConflict] Unexpected error during conflict analysis: {e}")
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 500
        return json.dumps({
            'success': False,
            'error': 'Internal server error while analyzing creator identity conflict.',
            'error_code': 'server_error'
        })


def handle_resolve_creator_conflict(
    name_record_id: Optional[Any] = None,
    provider: Optional[str] = None,
    provider_creator_id: Optional[Any] = None,
    action: Optional[str] = None,
    resolution_action: Optional[str] = None,
    csrf_token: Optional[str] = None,
    reason: Optional[str] = None,
    service: Optional[CreatorConflictService] = None,
    **kwargs
) -> str:
    """
    HTTP handler for POST /resolveCreatorConflict route (Phase C4.15).
    Resolves external ID conflicts by retaining existing mapping and rejecting competing candidate pairing.
    """
    if hasattr(cherrypy, 'response'):
        cherrypy.response.headers['Content-Type'] = 'application/json'

    # 1. Require POST
    if hasattr(cherrypy, 'request'):
        method = getattr(cherrypy.request, 'method', 'GET')
        if method != 'POST':
            if hasattr(cherrypy, 'response'):
                cherrypy.response.status = 405
            return json.dumps({
                'success': False,
                'status_code': 405,
                'error': 'Method Not Allowed. POST is required.',
                'error_code': 'method_not_allowed'
            })

    # 2. Verify CSRF
    from mylar.extensions.creators.decision_controller import verify_csrf_token
    header_token = None
    if hasattr(cherrypy, 'request') and hasattr(cherrypy.request, 'headers'):
        header_token = (
            cherrypy.request.headers.get('X-CSRF-Token') or
            cherrypy.request.headers.get('X-CSRFToken')
        )
    cand_token = csrf_token or header_token
    if not verify_csrf_token(cand_token):
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 403
        return json.dumps({
            'success': False,
            'status_code': 403,
            'error': 'Invalid or missing CSRF token.',
            'error_code': 'invalid_csrf_token'
        })

    # 3. Validate action
    act = (action or resolution_action or '').strip().lower()
    allowed_actions = {
        'keep_existing_reject_competing', 'reject_competing',
        'transfer_mapping', 'transfer',
        'reverse_transfer', 'undo_transfer'
    }
    if act not in allowed_actions:
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 400
        return json.dumps({
            'success': False,
            'status_code': 400,
            'error': f"Unsupported resolution action '{act}'. Allowed: 'keep_existing_reject_competing', 'transfer_mapping', 'reverse_transfer'.",
            'error_code': 'invalid_action'
        })

    conf_service = service or CreatorConflictService()

    try:
        if act in {'keep_existing_reject_competing', 'reject_competing'}:
            data = conf_service.resolve_conflict_keep_existing_reject_competing(
                name_record_id=name_record_id,
                provider=provider,
                provider_creator_id=provider_creator_id,
                reason=reason,
                actor="user"
            )
        elif act in {'transfer_mapping', 'transfer'}:
            data = conf_service.resolve_conflict_transfer_mapping(
                name_record_id=name_record_id,
                provider=provider,
                provider_creator_id=provider_creator_id,
                reason=reason,
                actor="user"
            )
        elif act in {'reverse_transfer', 'undo_transfer'}:
            data = conf_service.reverse_conflict_transfer_mapping(
                name_record_id=name_record_id,
                provider=provider,
                provider_creator_id=provider_creator_id,
                reason=reason,
                actor="user"
            )
        return json.dumps(data)
    except InvalidConflictInputError as e:
        logger.warning(f"[CreatorConflict] Invalid resolution parameters: {e}")
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 400
        return json.dumps({
            'success': False,
            'error': str(e),
            'error_code': 'invalid_input'
        })
    except LocalNameRecordNotFoundError as e:
        logger.warning(f"[CreatorConflict] NameRecord not found: {e}")
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 404
        return json.dumps({
            'success': False,
            'error': str(e),
            'error_code': 'record_not_found'
        })
    except TransferTargetUnavailableError as e:
        logger.warning(f"[CreatorConflict] Transfer target unavailable: {e}")
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 409
        return json.dumps({
            'success': False,
            'status_code': 409,
            'error': str(e),
            'error_code': 'transfer_target_unavailable'
        })
    except StaleConflictStateError as e:
        logger.warning(f"[CreatorConflict] Stale conflict state: {e}")
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 409
        return json.dumps({
            'success': False,
            'status_code': 409,
            'error': str(e),
            'error_code': 'stale_conflict_state'
        })
    except Exception as e:
        logger.error(f"[CreatorConflict] Unexpected error during conflict resolution: {e}")
        if hasattr(cherrypy, 'response'):
            cherrypy.response.status = 500
        return json.dumps({
            'success': False,
            'error': 'Internal server error while resolving creator identity conflict.',
            'error_code': 'server_error'
        })
