"""
Metron Authentication Component (Phase C4.1).

Provides credential validation, safe header generation, and sanitized diagnostics.
Guarantees credentials are never exposed in string representations, logs, or exceptions.
"""

import base64


AUTH_MODE_TOKEN = 'token'
AUTH_MODE_BASIC = 'basic'
AUTH_MODE_NONE = 'none'

SUPPORTED_AUTH_MODES = {AUTH_MODE_TOKEN, AUTH_MODE_BASIC, AUTH_MODE_NONE}


class MetronConfigurationError(Exception):
    """Raised when Metron credentials or client configurations are missing or invalid."""
    def __init__(self, message, error_code='invalid_configuration'):
        super().__init__(message)
        self.error_code = error_code


class MetronCredentials:
    """
    Encapsulates Metron authentication credentials with strict in-memory security.
    """

    def __init__(self, mode=AUTH_MODE_TOKEN, token=None, username=None, password=None):
        """
        Initialize Metron credentials.

        :param mode: Authentication mode ('token', 'basic', or 'none')
        :param token: API token string (for 'token' mode)
        :param username: Username string (for 'basic' mode)
        :param password: Password string (for 'basic' mode)
        """
        mode_clean = str(mode).strip().lower() if mode else AUTH_MODE_NONE
        if mode_clean not in SUPPORTED_AUTH_MODES:
            raise MetronConfigurationError(
                f"Unsupported authentication mode '{mode_clean}'. Supported modes: {', '.join(sorted(SUPPORTED_AUTH_MODES))}",
                error_code='invalid_configuration'
            )

        self._mode = mode_clean
        self._token = str(token).strip() if token is not None else None
        self._username = str(username).strip() if username is not None else None
        self._password = str(password) if password is not None else None

    @property
    def mode(self):
        """Return the active authentication mode."""
        return self._mode

    @property
    def is_configured(self):
        """Check whether sufficient credentials have been provided for the active mode."""
        if self._mode == AUTH_MODE_TOKEN:
            return bool(self._token)
        elif self._mode == AUTH_MODE_BASIC:
            return bool(self._username and self._password)
        elif self._mode == AUTH_MODE_NONE:
            return False
        return False

    def validate(self):
        """
        Validate that the credential configuration is complete and non-empty.
        Raises MetronConfigurationError if invalid or incomplete.
        """
        if self._mode == AUTH_MODE_NONE:
            raise MetronConfigurationError(
                "Metron authentication mode is set to 'none'. Credentials are not configured.",
                error_code='not_configured'
            )
        elif self._mode == AUTH_MODE_TOKEN:
            if not self._token:
                raise MetronConfigurationError(
                    "Metron token authentication requires a non-empty API token.",
                    error_code='invalid_configuration'
                )
        elif self._mode == AUTH_MODE_BASIC:
            if not self._username or not self._password:
                raise MetronConfigurationError(
                    "Metron basic authentication requires both username and password.",
                    error_code='invalid_configuration'
                )

    def get_auth_headers(self):
        """
        Generate HTTP authorization headers according to the Metron specification.

        - Token mode: 'Authorization': 'Token <token>'
        - Basic mode: 'Authorization': 'Basic <base64>'
        - None mode: {}

        :return: dict of HTTP request headers
        """
        self.validate()
        if self._mode == AUTH_MODE_TOKEN:
            return {'Authorization': f"Token {self._token}"}
        elif self._mode == AUTH_MODE_BASIC:
            user_pass = f"{self._username}:{self._password}"
            encoded = base64.b64encode(user_pass.encode('utf-8')).decode('ascii')
            return {'Authorization': f"Basic {encoded}"}
        return {}

    def get_sanitized_info(self):
        """
        Return non-sensitive diagnostic metadata about the credential configuration.

        :return: dict
        """
        return {
            'mode': self._mode,
            'is_configured': self.is_configured
        }

    def __repr__(self):
        """
        Safe string representation that never exposes tokens or passwords.
        """
        return f"<MetronCredentials mode={self._mode} configured={self.is_configured}>"
