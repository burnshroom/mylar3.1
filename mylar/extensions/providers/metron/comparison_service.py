"""
Metron Runtime Comparison Service (Phase C4.5).

Coordinates local issue scope verification, local credit loading, provider snapshot caching,
Metron issue retrieval, and in-memory credit concordance comparison.
Operates strictly read-only with ZERO database writes, schema mutations, or entity modifications.
"""

import mylar
from mylar import db, logger
from mylar.extensions.creators.browser_service import CreatorBrowserService
from mylar.extensions.creators.candidate_service import CreatorCandidateService
from mylar.extensions.providers.metron.config import (
    build_metron_credentials,
    CANONICAL_METRON_BASE_URL
)
from mylar.extensions.providers.metron.cache import get_metron_cache
from mylar.extensions.providers.metron.client import (
    MetronError,
    MetronInvalidRequestError
)
from mylar.extensions.providers.metron.comparison import (
    compare_with_local_credits,
    CREDIT_COMPARISON_DISCLAIMER
)
from mylar.extensions.providers.metron.issue_service import (
    MetronIssueService,
    validate_comicvine_issue_id
)


class MetronLocalIssueNotFoundError(MetronError):
    """Raised when the requested issue is not found in the local Mylar database for the specified scope."""
    def __init__(self, message="Local issue not found in requested scope.", status_code=404):
        super().__init__(message, error_code='local_issue_not_found', status_code=status_code)


class MetronProviderDisabledError(MetronError):
    """Raised when Metron provider integration is disabled in configuration."""
    def __init__(self, message="Metron integration is disabled in Settings.", status_code=400):
        super().__init__(message, error_code='provider_disabled', status_code=status_code)


def validate_annual_scope(annual):
    """
    Validate that is_annual is strictly 0 or 1.
    Rejects booleans, None, numbers outside {0, 1}, and non-0/1 string representations.

    :param annual: 0, 1, '0', or '1'
    :return: int (0 or 1)
    :raises MetronInvalidRequestError: If annual scope is invalid
    """
    if isinstance(annual, bool):
        raise MetronInvalidRequestError("Invalid annual scope. Booleans are not permitted.")

    if isinstance(annual, int):
        if annual in (0, 1):
            return annual
        raise MetronInvalidRequestError("Invalid annual scope. Must be 0 or 1.")

    if isinstance(annual, str):
        if annual != annual.strip():
            raise MetronInvalidRequestError("Invalid annual scope. Whitespace padding is not permitted.")
        if annual == '0':
            return 0
        if annual == '1':
            return 1
        raise MetronInvalidRequestError("Invalid annual scope. Must be '0' or '1'.")

    raise MetronInvalidRequestError("Invalid annual scope. Must be 0 or 1.")


class MetronComparisonService:
    """
    Service layer coordinating local credit retrieval, Metron provider querying,
    in-memory caching, and credit comparison.
    """

    def __init__(self, db_conn=None, cache=None, issue_service=None, client_options=None):
        """
        Initialize MetronComparisonService.
        Performs ZERO network calls or database writes on initialization.

        :param db_conn: Optional custom database connection/mock for dependency injection
        :param cache: Optional MetronIssueCache instance (defaults to global cache)
        :param issue_service: Optional MetronIssueService instance for dependency injection
        :param client_options: Optional client configuration options dictionary
        """
        self._custom_db = db_conn
        self._cache = cache if cache is not None else get_metron_cache()
        self._issue_service = issue_service
        self._client_options = client_options

    def _get_db(self):
        if self._custom_db is not None:
            return self._custom_db
        return db.DBConnection()

    def _verify_local_issue_exists(self, issue_id, is_annual):
        """
        Verify that the requested issue exists in the local database under the exact requested scope.
        Never crosses between issues and annuals tables.
        """
        table_name = 'annuals' if is_annual == 1 else 'issues'
        my_db = self._get_db()

        if hasattr(my_db, 'select'):
            rows = my_db.select(f"SELECT IssueID FROM {table_name} WHERE IssueID = ?", [str(issue_id)])
        elif hasattr(my_db, 'execute'):
            rows = my_db.execute(f"SELECT IssueID FROM {table_name} WHERE IssueID = ?", [str(issue_id)]).fetchall()
        elif hasattr(my_db, 'cursor'):
            cur = my_db.cursor()
            rows = cur.execute(f"SELECT IssueID FROM {table_name} WHERE IssueID = ?", [str(issue_id)]).fetchall()
        else:
            rows = []

        if not rows:
            logger.fdebug(f"[METRON-COMPARISON] Local issue '{issue_id}' not found in {table_name} table")
            raise MetronLocalIssueNotFoundError(
                f"Local issue '{issue_id}' was not found in the local {table_name} table."
            )

    def compare_issue_with_metron(self, issue_id, is_annual=0):
        """
        Compare local indexed creator credits for an issue against Metron provider metadata.

        :param issue_id: Local IssueID (positive integer or pure digit string)
        :param is_annual: 0 for regular issue, 1 for Annual
        :return: Structured comparison dictionary
        """
        # 1. Validate inputs strictly
        val_issue_id = validate_comicvine_issue_id(issue_id)
        val_annual = validate_annual_scope(is_annual)

        # 2. Verify local issue exists in requested scope
        self._verify_local_issue_exists(val_issue_id, val_annual)

        # Authoritative ComicVine IssueID is the local IssueID
        cv_issue_id = val_issue_id

        # 3. Confirm Metron provider is enabled before checking cache
        if not getattr(mylar.CONFIG, 'METRON_ENABLED', False):
            logger.fdebug("[METRON-COMPARISON] Metron provider is disabled in configuration")
            raise MetronProviderDisabledError("Metron integration is disabled in Settings.")

        # 4. Validate currently saved credentials before checking cache
        # (raises MetronInvalidRequestError if missing/incomplete)
        creds = build_metron_credentials()

        # 5. Freshly load locally indexed creator credits
        browser_service = CreatorBrowserService(db_conn=self._get_db())
        local_data = browser_service.get_issue_creator_credits(issue_id=val_issue_id, is_annual=val_annual)
        local_credits = local_data.get('credits', [])

        # 6. Consult in-memory Metron snapshot cache
        cached_snapshot = self._cache.get(cv_issue_id)
        if cached_snapshot is not None:
            # 7. Cache hit: perform zero provider requests
            provider_snapshot = cached_snapshot
            cached = True
            logger.fdebug(f"[METRON-COMPARISON] Using cached provider snapshot for ComicVine IssueID {cv_issue_id}")
        else:
            # 8. Cache miss: perform exactly one bounded provider request
            cached = False
            issue_svc = self._issue_service
            if issue_svc is None:
                issue_svc = MetronIssueService(
                    credentials=creds,
                    client_options=self._client_options or {'base_url': CANONICAL_METRON_BASE_URL}
                )

            # Fetch and normalize from provider (single bounded request)
            provider_snapshot = issue_svc.fetch_issue_by_comicvine_id(cv_issue_id)

            # Cache ONLY successful normalized snapshot
            self._cache.set(cv_issue_id, provider_snapshot)

        # 9. Execute in-memory credit comparison
        comp_res = compare_with_local_credits(provider_snapshot, local_credits)

        # 10. Generate read-only creator identity candidates from provider snapshot
        cand_svc = CreatorCandidateService(db_conn=self._get_db())
        cand_res = cand_svc.build_candidates(
            issue_id=val_issue_id,
            is_annual=val_annual,
            provider='metron',
            provider_snapshot=provider_snapshot
        )

        # 11. Return safe observational comparison result
        return {
            'success': True,
            'issue_id': str(val_issue_id),
            'is_annual': int(val_annual),
            'comicvine_issue_id': int(cv_issue_id),
            'cached': bool(cached),
            'provider_snapshot': provider_snapshot,
            'exact_overlaps': comp_res.get('exact_overlaps', []),
            'local_only_credits': comp_res.get('local_only_credits', []),
            'provider_only_credits': comp_res.get('provider_only_credits', []),
            'role_discrepancies': comp_res.get('role_discrepancies', []),
            'normalization_warnings': provider_snapshot.get('normalization_warnings', []),
            'identity_candidates': cand_res.get('identity_candidates', []),
            'disclaimer': CREDIT_COMPARISON_DISCLAIMER
        }
