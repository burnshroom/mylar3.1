"""
Metron Issue Service (Phase C4.2).

Provides authoritative single-issue retrieval by ComicVine IssueID,
response shape evaluation, strict returned-ID verification, normalization, and in-memory credit comparison.
Stateless and read-only with ZERO database writes.
"""

from mylar import logger
from mylar.extensions.providers.metron.client import (
    MetronClient,
    MetronError,
    MetronNotFoundError,
    MetronInvalidRequestError,
    MetronInvalidResponseError
)
from mylar.extensions.providers.metron.normalizer import normalize_metron_issue
from mylar.extensions.providers.metron.comparison import compare_with_local_credits


class MetronAmbiguousResultError(MetronError):
    """Raised when multiple matching records are returned for an authoritative unique identifier lookup."""
    def __init__(self, message="Ambiguous provider result. Multiple matching issues returned.", status_code=200):
        super().__init__(message, error_code='ambiguous_result', status_code=status_code)


def validate_comicvine_issue_id(comicvine_issue_id):
    """
    Validate that the supplied ComicVine IssueID is a strictly valid positive integer.
    Rejects booleans, zero, negative numbers, floats, whitespace-padded values, signs,
    decimal strings, and nonnumeric values.

    :param comicvine_issue_id: Positive integer or pure ASCII digit string
    :return: int
    :raises MetronInvalidRequestError: If the input is invalid or nonpositive
    """
    if isinstance(comicvine_issue_id, bool):
        raise MetronInvalidRequestError("Invalid ComicVine IssueID. Booleans are not permitted.")

    if isinstance(comicvine_issue_id, int):
        if comicvine_issue_id <= 0:
            raise MetronInvalidRequestError("Invalid ComicVine IssueID. Must be a positive integer.")
        return comicvine_issue_id

    if isinstance(comicvine_issue_id, str):
        # Reject whitespace-padded values
        if comicvine_issue_id != comicvine_issue_id.strip():
            raise MetronInvalidRequestError("Invalid ComicVine IssueID. Whitespace padding is not permitted.")

        # Reject empty or non-digit / non-ASCII strings (rejects signs +, -, decimals ., and letters)
        if not (comicvine_issue_id.isascii() and comicvine_issue_id.isdigit()):
            raise MetronInvalidRequestError("Invalid ComicVine IssueID. Must be a positive integer digit string.")

        val = int(comicvine_issue_id)
        if val <= 0:
            raise MetronInvalidRequestError("Invalid ComicVine IssueID. Must be a positive integer.")
        return val

    raise MetronInvalidRequestError("Invalid ComicVine IssueID. Must be a positive integer or digit string.")


def _verify_returned_comicvine_id(issue_data, requested_cv_id):
    """
    Verify that the returned issue dictionary contains a valid cv_id matching the requested ID.
    Accepts strictly a positive integer or pure ASCII decimal digit string.
    Rejects booleans, missing values, whitespace-padded strings, signs, decimals,
    non-ASCII numerals, zero, negative values, and mismatched IDs.

    :param issue_data: dict representing issue record from provider
    :param requested_cv_id: int requested ComicVine IssueID
    :raises MetronInvalidResponseError: If cv_id is missing, invalid, or mismatched
    """
    if not isinstance(issue_data, dict):
        raise MetronInvalidResponseError("Malformed issue record structure received from Metron API.")

    ret_cv_id = issue_data.get('cv_id')

    if isinstance(ret_cv_id, bool) or ret_cv_id is None:
        logger.fdebug("[METRON-ISSUE] Returned issue record missing valid cv_id")
        raise MetronInvalidResponseError("Metron returned an issue that did not match the requested ComicVine IssueID.")

    parsed_cv_id = None
    if isinstance(ret_cv_id, int):
        if ret_cv_id > 0:
            parsed_cv_id = ret_cv_id
    elif isinstance(ret_cv_id, str):
        # Strict ASCII decimal digit check without whitespace, signs, or non-ASCII numerals
        if ret_cv_id.isascii() and ret_cv_id.isdigit() and ret_cv_id == ret_cv_id.strip():
            val = int(ret_cv_id)
            if val > 0:
                parsed_cv_id = val

    if parsed_cv_id is None or parsed_cv_id != requested_cv_id:
        logger.fdebug(f"[METRON-ISSUE] Returned cv_id does not match requested ({requested_cv_id})")
        raise MetronInvalidResponseError("Metron returned an issue that did not match the requested ComicVine IssueID.")


class MetronIssueService:
    """
    Service layer for querying individual Metron issue records by ComicVine IssueID
    and comparing provider credits with local credits.
    """

    def __init__(self, credentials=None, client=None, client_options=None):
        """
        Initialize MetronIssueService.
        Performs ZERO network calls on initialization.

        :param credentials: MetronCredentials instance or None
        :param client: Optional pre-configured MetronClient instance for dependency injection
        :param client_options: Optional client configuration dictionary
        """
        if client is not None:
            self.client = client
        else:
            self.client = MetronClient(credentials=credentials, **(client_options or {}))

    def fetch_issue_by_comicvine_id(self, comicvine_issue_id):
        """
        Query the Metron API for an issue using an authoritative ComicVine IssueID.
        Executes exactly one bounded HTTP request.

        :param comicvine_issue_id: Positive integer or valid digit string
        :return: Normalized issue snapshot dictionary
        :raises MetronInvalidRequestError: On invalid ComicVine IssueID
        :raises MetronNotFoundError: When zero matching records are returned
        :raises MetronAmbiguousResultError: When multiple matching records are returned
        :raises MetronInvalidResponseError: On malformed responses or mismatched returned IDs
        """
        cv_id = validate_comicvine_issue_id(comicvine_issue_id)

        logger.fdebug(f"[METRON-ISSUE] Fetching issue by ComicVine IssueID: {cv_id}")
        resp = self.client.get('issue/', params={'cv_id': str(cv_id)})

        issue_data = None
        data = resp.data
        derived_shape = 'object'
        derived_count = 0

        # 1. Paginated envelope shape
        if isinstance(data, dict) and 'results' in data:
            results = data['results']
            if not isinstance(results, list):
                logger.fdebug("[METRON-ISSUE] Malformed envelope: results is not a list")
                raise MetronInvalidResponseError("Malformed results envelope received from Metron API.")

            derived_shape = 'paginated'
            derived_count = len(results)

            if len(results) == 0:
                logger.fdebug(f"[METRON-ISSUE] Zero matching issues found for ComicVine IssueID {cv_id}")
                raise MetronNotFoundError(f"No Metron issue found matching ComicVine IssueID {cv_id}.", status_code=404)

            if len(results) > 1:
                logger.fdebug(f"[METRON-ISSUE] Ambiguous result: {len(results)} issues returned for ComicVine IssueID {cv_id}")
                raise MetronAmbiguousResultError(f"Ambiguous provider result. Multiple issues ({len(results)}) returned for ComicVine IssueID {cv_id}.")

            issue_data = results[0]

        # 2. Flat list shape
        elif isinstance(data, list):
            derived_shape = 'list'
            derived_count = len(data)

            if len(data) == 0:
                logger.fdebug(f"[METRON-ISSUE] Zero matching issues found for ComicVine IssueID {cv_id}")
                raise MetronNotFoundError(f"No Metron issue found matching ComicVine IssueID {cv_id}.", status_code=404)

            if len(data) > 1:
                logger.fdebug(f"[METRON-ISSUE] Ambiguous result: {len(data)} issues returned for ComicVine IssueID {cv_id}")
                raise MetronAmbiguousResultError(f"Ambiguous provider result. Multiple issues ({len(data)}) returned for ComicVine IssueID {cv_id}.")

            issue_data = data[0]

        # 3. Single object shape
        elif isinstance(data, dict):
            if not data:
                logger.fdebug(f"[METRON-ISSUE] Empty object returned for ComicVine IssueID {cv_id}")
                raise MetronNotFoundError(f"No Metron issue found matching ComicVine IssueID {cv_id}.", status_code=404)
            derived_shape = 'object'
            derived_count = 1
            issue_data = data

        else:
            logger.fdebug("[METRON-ISSUE] Unexpected payload structure received from Metron API")
            raise MetronInvalidResponseError("Unexpected payload structure received from Metron API.")

        # Authoritative returned ID validation
        _verify_returned_comicvine_id(issue_data, cv_id)

        return normalize_metron_issue(
            raw_issue_data=issue_data,
            query_comicvine_issue_id=cv_id,
            response_shape=derived_shape,
            observed_result_count=derived_count
        )

    def compare_issue_credits(self, comicvine_issue_id, local_credits):
        """
        Retrieve provider issue snapshot by ComicVine IssueID and compare against caller-supplied local credits.

        :param comicvine_issue_id: Positive integer or valid digit string
        :param local_credits: list of local credit dictionaries
        :return: Structured comparison dictionary
        """
        snapshot = self.fetch_issue_by_comicvine_id(comicvine_issue_id)
        return compare_with_local_credits(snapshot, local_credits)
