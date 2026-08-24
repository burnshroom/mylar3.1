"""
Creator Extension Controller.

Exposes JSON API for user-initiated creator indexing and status polling.
Enforces POST HTTP methods and CSRF token validation on all mutating actions.
"""

import json
import cherrypy
from mylar import logger
from mylar.extensions.creators.service import CreatorService
from mylar.extensions.creators.worker import CreatorIndexWorker
from mylar.extensions.creators.decision_controller import (
    _validate_request_method,
    _validate_csrf
)


class CreatorController:
    """
    Controller handling creator indexing web actions.
    """

    def __init__(self):
        self.worker = CreatorIndexWorker()
        self.service = CreatorService()

    def start_series_index(self, comic_id, issue_ids=None, csrf_token=None):
        """
        Trigger background indexing for a series with strict POST and CSRF validation.
        """
        if not _validate_request_method():
            if hasattr(cherrypy, 'response'):
                cherrypy.response.status = 405
            return {
                'status': 'error',
                'status_code': 405,
                'error': 'Method Not Allowed. POST is required.',
                'error_code': 'method_not_allowed'
            }

        if not _validate_csrf(csrf_token):
            if hasattr(cherrypy, 'response'):
                cherrypy.response.status = 403
            return {
                'status': 'error',
                'status_code': 403,
                'error': 'Invalid or missing CSRF token.',
                'error_code': 'invalid_csrf_token'
            }

        if not comic_id:
            if hasattr(cherrypy, 'response'):
                cherrypy.response.status = 400
            return {'status': 'error', 'status_code': 400, 'error': 'Missing ComicID', 'error_code': 'invalid_input'}

        try:
            parsed_issue_ids = None
            if issue_ids:
                if isinstance(issue_ids, str):
                    parsed_issue_ids = [x.strip() for x in issue_ids.split(',') if x.strip()]
                elif isinstance(issue_ids, (list, tuple)):
                    parsed_issue_ids = [str(x) for x in issue_ids]

            result = self.worker.start_series_scan(comic_id, parsed_issue_ids)
            return result
        except Exception as e:
            logger.error(f"[CREATOR-CONTROLLER] Error starting creator index: {e}")
            if hasattr(cherrypy, 'response'):
                cherrypy.response.status = 500
            return {'status': 'error', 'status_code': 500, 'error': str(e), 'error_code': 'server_error'}

    def get_index_status(self, job_id=None):
        """
        Get current worker status snapshot (Read-only GET).
        """
        try:
            return self.worker.get_status(job_id)
        except Exception as e:
            logger.error(f"[CREATOR-CONTROLLER] Error retrieving status: {e}")
            if hasattr(cherrypy, 'response'):
                cherrypy.response.status = 500
            return {'status': 'error', 'status_code': 500, 'error': str(e), 'error_code': 'server_error'}

    def cancel_index(self, job_id=None, csrf_token=None):
        """
        Cancel a running index job with strict POST and CSRF validation.
        """
        if not _validate_request_method():
            if hasattr(cherrypy, 'response'):
                cherrypy.response.status = 405
            return {
                'status': 'error',
                'status_code': 405,
                'error': 'Method Not Allowed. POST is required.',
                'error_code': 'method_not_allowed'
            }

        if not _validate_csrf(csrf_token):
            if hasattr(cherrypy, 'response'):
                cherrypy.response.status = 403
            return {
                'status': 'error',
                'status_code': 403,
                'error': 'Invalid or missing CSRF token.',
                'error_code': 'invalid_csrf_token'
            }

        try:
            return self.worker.cancel(job_id)
        except Exception as e:
            logger.error(f"[CREATOR-CONTROLLER] Error cancelling index job: {e}")
            if hasattr(cherrypy, 'response'):
                cherrypy.response.status = 500
            return {'status': 'error', 'status_code': 500, 'error': str(e), 'error_code': 'server_error'}

    def get_series_summary(self, comic_id):
        """
        Get creator indexing summary for a series (Read-only GET).
        """
        if not comic_id:
            if hasattr(cherrypy, 'response'):
                cherrypy.response.status = 400
            return {'status': 'error', 'status_code': 400, 'error': 'Missing ComicID', 'error_code': 'invalid_input'}
        try:
            return self.service.get_series_creator_summary(comic_id)
        except Exception as e:
            logger.error(f"[CREATOR-CONTROLLER] Error fetching series summary: {e}")
            if hasattr(cherrypy, 'response'):
                cherrypy.response.status = 500
            return {'status': 'error', 'status_code': 500, 'error': str(e), 'error_code': 'server_error'}
