"""
Kavita Authentication Component (Phase K2).

Provides credential validation, official header generation, and sanitized diagnostics.
Guarantees credentials and tokens are never exposed in string representations, logs, or exceptions.
Enforces the official Kavita contract:
- Auth keys are sent via the x-api-key header only.
- Saved API key is never emitted as Authorization: Bearer.
- Duplicate credential headers are never emitted simultaneously.
"""


class KavitaConfigurationError(Exception):
    """Raised when Kavita credentials or client configurations are missing or invalid."""
    def __init__(self, message="Kavita configuration error.", error_code='invalid_configuration'):
        super().__init__(message)
        self.error_code = error_code


class KavitaCredentials:
    """
    Encapsulates Kavita authentication credentials with strict in-memory security.
    """

    def __init__(self, api_key=None):
        """
        Initialize Kavita credentials.

        :param api_key: API key string
        """
        self._api_key = str(api_key).strip() if api_key is not None and str(api_key).strip() else None

    @property
    def is_configured(self):
        """Check whether a non-empty API key is configured."""
        return bool(self._api_key)

    def validate(self):
        """
        Validate that the credential configuration is non-empty.
        Raises KavitaConfigurationError if invalid or missing.
        """
        if not self._api_key:
            raise KavitaConfigurationError(
                "Kavita API key is required and cannot be empty.",
                error_code='missing_api_key'
            )

    def get_api_key(self):
        """
        Internal getter for API key.
        Never expose in logs, exceptions, or string representations.
        """
        self.validate()
        return self._api_key

    def get_auth_headers(self):
        """
        Generate HTTP headers for Kavita API requests using the official x-api-key header.
        Never emits Authorization: Bearer with the saved API key.
        Never emits multiple credential headers.

        :return: dict of HTTP request headers with exact {'x-api-key': apiKey}
        """
        self.validate()
        return {'x-api-key': self._api_key}

    def get_sanitized_info(self):
        """
        Return non-sensitive diagnostic metadata about credential state.

        :return: dict
        """
        return {
            'is_configured': self.is_configured,
            'api_key_masked': '••••••••' if self.is_configured else None
        }

    def __repr__(self):
        return f"<KavitaCredentials configured={self.is_configured}>"

    def __str__(self):
        return f"<KavitaCredentials configured={self.is_configured}>"
