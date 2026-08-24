"""
Creator Identity Decision Controller (Phase C4.10).

Handles explicit human-initiated creator identity decisions (confirm, reject, undo/reverse)
with strict CSRF protection, POST-only HTTP method enforcement, input bounds validation,
Metron configuration gates, and zero browser-controlled identity parameters.
"""

import hmac
import json
import secrets
import cherrypy

import mylar
from mylar import logger, db
from mylar.extensions.creators.identity_service import (
    CreatorIdentityService,
    IdentityResolutionError,
    InvalidIdentityInputError,
    NameRecordNotFoundError,
    ActiveCandidateRejectionError,
    ProviderIDCollisionError,
    ConflictingNameRecordLinkError,
    UnsafeReversalError,
    TargetNotFoundError,
)
from mylar.extensions.creators.identity_repository import IdentityRepository
from mylar.extensions.providers.metron.config import (
    build_metron_credentials,
)
from mylar.extensions.providers.metron.client import (
    MetronInvalidRequestError,
)

# Supported provider for candidate decisions
ALLOWED_PROVIDERS = {'metron'}

# Module-level fallback token for environments/tests without active CherryPy sessions
_GLOBAL_CSRF_FALLBACK = secrets.token_hex(32)


# -----------------------------------------------------------------------------
# CSRF Utilities
# -----------------------------------------------------------------------------

def get_or_create_csrf_token():
    """
    Retrieve or generate a secure session-backed CSRF token.
    """
    if hasattr(cherrypy, 'session') and isinstance(cherrypy.session, dict):
        token = cherrypy.session.get('_creator_csrf_token')
        if not token:
            token = secrets.token_hex(32)
            cherrypy.session['_creator_csrf_token'] = token
        return token
    return _GLOBAL_CSRF_FALLBACK


def verify_csrf_token(token):
    """
    Verify the supplied CSRF token against the current session or active fallback token.
    """
    if not token or not isinstance(token, str) or not token.strip():
        return False

    clean_token = token.strip()
    expected = None

    if hasattr(cherrypy, 'session') and isinstance(cherrypy.session, dict):
        expected = cherrypy.session.get('_creator_csrf_token')

    if not expected:
        expected = _GLOBAL_CSRF_FALLBACK

    return hmac.compare_digest(clean_token, expected)


# -----------------------------------------------------------------------------
# Input Validation & Sanitization Helpers
# -----------------------------------------------------------------------------

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
    return verify_csrf_token(candidate_token)


def _validate_mutation_inputs(name_record_id, provider, provider_creator_id):
    """Strictly validate and parse core decision parameters."""
    if name_record_id is None:
        raise InvalidIdentityInputError("NameRecordID is required.")
    if isinstance(name_record_id, bool):
        raise InvalidIdentityInputError("NameRecordID cannot be a boolean.")
    try:
        val_nr_id = int(str(name_record_id).strip())
        if val_nr_id <= 0:
            raise ValueError()
    except (ValueError, TypeError):
        raise InvalidIdentityInputError(f"Invalid NameRecordID '{name_record_id}': must be a positive integer.")

    if not provider or not isinstance(provider, str) or not provider.strip():
        raise InvalidIdentityInputError("Provider namespace is required.")
    clean_provider = provider.strip().lower()
    if clean_provider not in ALLOWED_PROVIDERS:
        raise InvalidIdentityInputError(f"Unsupported provider '{provider}'. Must be 'metron'.")

    if provider_creator_id is None or (isinstance(provider_creator_id, str) and not provider_creator_id.strip()):
        raise InvalidIdentityInputError("Provider creator ID is required.")
    clean_ext_id = str(provider_creator_id).strip()
    if len(clean_ext_id) > 128:
        raise InvalidIdentityInputError("Provider creator ID exceeds maximum length.")

    return val_nr_id, clean_provider, clean_ext_id


def _check_metron_gate(provider):
    """Ensure Metron is enabled and configured for candidate-derived decisions."""
    if provider == 'metron':
        if not getattr(mylar.CONFIG, 'METRON_ENABLED', False):
            raise InvalidIdentityInputError("Metron provider is disabled in Settings.")
        try:
            build_metron_credentials()
        except MetronInvalidRequestError as e:
            raise InvalidIdentityInputError(f"Metron credentials are not configured: {str(e)}")


def _json_response(payload, status_code=200):
    """Format sanitized JSON response with appropriate headers and status."""
    if hasattr(cherrypy, 'response'):
        cherrypy.response.headers['Content-Type'] = 'application/json'
        cherrypy.response.status = status_code
    return json.dumps(payload)


# -----------------------------------------------------------------------------
# Decision Action Handlers
# -----------------------------------------------------------------------------

def handle_get_creator_csrf_token(**kwargs):
    """
    HTTP handler for GET /getCreatorCSRFToken.
    Returns the active CSRF token for the current session.
    """
    token = get_or_create_csrf_token()
    return _json_response({'success': True, 'csrf_token': token})


def handle_confirm_creator_identity(name_record_id=None, provider=None, provider_creator_id=None,
                                    provider_display_name=None, csrf_token=None, service=None, **kwargs):
    """
    HTTP handler for POST /confirmCreatorIdentity.
    Explicitly confirms a local NameRecordID to an authoritative provider creator ID.
    """
    if not _validate_request_method():
        return _json_response({
            'success': False,
            'status_code': 405,
            'error': 'Method Not Allowed. POST is required.',
            'error_code': 'method_not_allowed'
        }, 405)

    if not _validate_csrf(csrf_token):
        return _json_response({
            'success': False,
            'status_code': 403,
            'error': 'Invalid or missing CSRF token.',
            'error_code': 'invalid_csrf_token'
        }, 403)

    try:
        val_nr_id, clean_provider, clean_ext_id = _validate_mutation_inputs(
            name_record_id, provider, provider_creator_id
        )
        _check_metron_gate(clean_provider)

        svc = service or CreatorIdentityService()
        result = svc.confirm_provider_identity(
            name_record_id=val_nr_id,
            provider=clean_provider,
            provider_creator_id=clean_ext_id,
            provider_display_name=str(provider_display_name).strip() if provider_display_name else None,
            actor="user",
            confirmation_source="explicit_user"
        )

        return _json_response({
            'success': True,
            'status_code': 200,
            'action': 'confirm',
            'name_record_id': result['name_record_id'],
            'creator_entity_id': result['creator_entity_id'],
            'provider': result['provider'],
            'provider_creator_id': result['external_id'],
            'audit_id': result['audit_id'],
            'message': 'Creator identity confirmed successfully.'
        })

    except NameRecordNotFoundError:
        return _json_response({
            'success': False,
            'status_code': 404,
            'error': 'The targeted local creator record was not found.',
            'error_code': 'name_record_not_found'
        }, 404)
    except ActiveCandidateRejectionError:
        return _json_response({
            'success': False,
            'status_code': 400,
            'error': 'This candidate is actively rejected. Undo the rejection before confirming.',
            'error_code': 'active_rejection_blocked'
        }, 400)
    except ProviderIDCollisionError:
        return _json_response({
            'success': False,
            'status_code': 400,
            'error': 'Provider creator ID is mapped to a conflicting creator entity.',
            'error_code': 'provider_id_collision'
        }, 400)
    except ConflictingNameRecordLinkError:
        return _json_response({
            'success': False,
            'status_code': 400,
            'error': 'This local credit is already linked to a different creator entity.',
            'error_code': 'conflicting_link'
        }, 400)
    except InvalidIdentityInputError as e:
        return _json_response({
            'success': False,
            'status_code': 400,
            'error': str(e),
            'error_code': 'invalid_input'
        }, 400)
    except Exception as e:
        logger.error(f"[CREATOR-DECISION] Unexpected error confirming identity: {e}")
        return _json_response({
            'success': False,
            'status_code': 500,
            'error': 'An internal error occurred while confirming creator identity.',
            'error_code': 'internal_error'
        }, 500)


def handle_reject_creator_candidate(name_record_id=None, provider=None, provider_creator_id=None,
                                    provider_display_name=None, reason=None, csrf_token=None,
                                    service=None, **kwargs):
    """
    HTTP handler for POST /rejectCreatorCandidate.
    Actively suppresses a candidate match for a specific NameRecordID and provider creator ID.
    """
    if not _validate_request_method():
        return _json_response({
            'success': False,
            'status_code': 405,
            'error': 'Method Not Allowed. POST is required.',
            'error_code': 'method_not_allowed'
        }, 405)

    if not _validate_csrf(csrf_token):
        return _json_response({
            'success': False,
            'status_code': 403,
            'error': 'Invalid or missing CSRF token.',
            'error_code': 'invalid_csrf_token'
        }, 403)

    try:
        val_nr_id, clean_provider, clean_ext_id = _validate_mutation_inputs(
            name_record_id, provider, provider_creator_id
        )
        _check_metron_gate(clean_provider)

        clean_reason = str(reason).strip() if reason and str(reason).strip() else "User explicit rejection"
        if len(clean_reason) > 500:
            clean_reason = clean_reason[:500]

        svc = service or CreatorIdentityService()
        result = svc.reject_candidate(
            name_record_id=val_nr_id,
            provider=clean_provider,
            provider_creator_id=clean_ext_id,
            provider_display_name=str(provider_display_name).strip() if provider_display_name else None,
            reason=clean_reason,
            actor="user"
        )

        return _json_response({
            'success': True,
            'status_code': 200,
            'action': 'reject',
            'name_record_id': result['name_record_id'],
            'rejection_id': result['rejection_id'],
            'provider': result['provider'],
            'provider_creator_id': result['external_id'],
            'status': result['status'],
            'message': 'Candidate rejected successfully.'
        })

    except NameRecordNotFoundError:
        return _json_response({
            'success': False,
            'status_code': 404,
            'error': 'The targeted local creator record was not found.',
            'error_code': 'name_record_not_found'
        }, 404)
    except InvalidIdentityInputError as e:
        return _json_response({
            'success': False,
            'status_code': 400,
            'error': str(e),
            'error_code': 'invalid_input'
        }, 400)
    except Exception as e:
        logger.error(f"[CREATOR-DECISION] Unexpected error rejecting candidate: {e}")
        return _json_response({
            'success': False,
            'status_code': 500,
            'error': 'An internal error occurred while rejecting creator candidate.',
            'error_code': 'internal_error'
        }, 500)


def handle_reverse_creator_decision(name_record_id=None, provider=None, provider_creator_id=None,
                                    reason=None, csrf_token=None, service=None, repository=None, **kwargs):
    """
    HTTP handler for POST /reverseCreatorDecision.
    Safely reverts an active confirmation or active rejection based on server-side evaluation.
    Does NOT require active Metron network connection or credentials.
    """
    if not _validate_request_method():
        return _json_response({
            'success': False,
            'status_code': 405,
            'error': 'Method Not Allowed. POST is required.',
            'error_code': 'method_not_allowed'
        }, 405)

    if not _validate_csrf(csrf_token):
        return _json_response({
            'success': False,
            'status_code': 403,
            'error': 'Invalid or missing CSRF token.',
            'error_code': 'invalid_csrf_token'
        }, 403)

    try:
        val_nr_id, clean_provider, clean_ext_id = _validate_mutation_inputs(
            name_record_id, provider, provider_creator_id
        )

        repo = repository or IdentityRepository()
        svc = service or CreatorIdentityService(repository=repo)

        # 1. Determine current server-side resolution state
        name_rec = repo.get_name_record(val_nr_id)
        if not name_rec:
            raise NameRecordNotFoundError(f"Name record {val_nr_id} not found.")

        active_rej = repo.get_active_rejection(val_nr_id, clean_provider, clean_ext_id)
        clean_reason = str(reason).strip() if reason and str(reason).strip() else "User explicit reversal"

        # Check if active rejection should be reversed
        if active_rej:
            res = svc.reverse_candidate_rejection(
                rejection_id=active_rej['rejection_id'],
                actor="user",
                reason=clean_reason
            )
            return _json_response({
                'success': True,
                'status_code': 200,
                'action': 'reverse_rejection',
                'name_record_id': val_nr_id,
                'provider': clean_provider,
                'provider_creator_id': clean_ext_id,
                'rejection_id': res['rejection_id'],
                'message': 'Candidate rejection reversed successfully.'
            })

        # Check if active confirmation should be reversed
        if name_rec.get('creator_entity_id'):
            res = svc.reverse_provider_confirmation(
                name_record_id=val_nr_id,
                provider=clean_provider,
                provider_creator_id=clean_ext_id,
                actor="user",
                reason=clean_reason
            )
            return _json_response({
                'success': True,
                'status_code': 200,
                'action': 'reverse_confirmation',
                'name_record_id': val_nr_id,
                'provider': clean_provider,
                'provider_creator_id': clean_ext_id,
                'message': 'Creator identity confirmation reversed successfully.'
            })

        # Neither active rejection nor confirmation matched
        return _json_response({
            'success': False,
            'status_code': 404,
            'error': 'No active confirmation or rejection found for this candidate to reverse.',
            'error_code': 'target_not_found'
        }, 404)

    except NameRecordNotFoundError:
        return _json_response({
            'success': False,
            'status_code': 404,
            'error': 'The targeted local creator record was not found.',
            'error_code': 'name_record_not_found'
        }, 404)
    except TargetNotFoundError:
        return _json_response({
            'success': False,
            'status_code': 404,
            'error': 'No active confirmation or rejection found to reverse.',
            'error_code': 'target_not_found'
        }, 404)
    except UnsafeReversalError as e:
        return _json_response({
            'success': False,
            'status_code': 400,
            'error': str(e),
            'error_code': 'unsafe_reversal'
        }, 400)
    except InvalidIdentityInputError as e:
        return _json_response({
            'success': False,
            'status_code': 400,
            'error': str(e),
            'error_code': 'invalid_input'
        }, 400)
    except Exception as e:
        logger.error(f"[CREATOR-DECISION] Unexpected error reversing decision: {e}")
        return _json_response({
            'success': False,
            'status_code': 500,
            'error': 'An internal error occurred while reversing creator decision.',
            'error_code': 'internal_error'
        }, 500)
