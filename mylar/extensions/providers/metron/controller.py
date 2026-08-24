"""
Metron Provider Controller (Phase C4.1).

Provides an in-memory controller interface for connection probes.
Does NOT expose a public web route or modify CherryPy in this phase.
"""

from mylar.extensions.providers.metron.service import MetronConnectionService


def probe_connection_controller(credentials=None, client_options=None, endpoint=None):
    """
    In-memory controller for probing the Metron API.
    Used for programmatic health checks and isolated unit tests.

    :param credentials: MetronCredentials instance, dict of credential fields, or None
    :param client_options: Optional dict of client configuration (base_url, connect_timeout, read_timeout, session)
    :param endpoint: Optional probe endpoint override (defaults to 'publisher/?page=1')
    :return: JSON-serializable sanitized result dictionary
    """
    service = MetronConnectionService()
    return service.probe_connection(
        credentials=credentials,
        endpoint=endpoint,
        client_options=client_options
    )
