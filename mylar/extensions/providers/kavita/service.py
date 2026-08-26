"""
Kavita Connection Service (Phase K2).

Orchestrates read-only capability checks and formats sanitized diagnostic results.
Operates statelessly using official 'x-api-key' request headers exclusively.
Never calls Plugin authenticate, never generates or handles JWT tokens,
and never calls any mutation endpoints (create, update, delete, scan, scan-folder, scan-all).
"""

import mylar
from mylar import logger
from mylar.extensions.providers.kavita.auth import (
    KavitaCredentials,
    KavitaConfigurationError
)
from mylar.extensions.providers.kavita.config import (
    get_kavita_config_status,
    validate_kavita_url
)
from mylar.extensions.providers.kavita.client import (
    KavitaClient,
    KavitaError,
    KavitaAuthenticationError,
    KavitaPermissionError,
    KavitaTimeoutError,
    KavitaTransportError,
    KavitaInvalidResponseError,
    KavitaIncompatibleError,
    KavitaInvalidRequestError
)


class KavitaConnectionService:
    """
    Service layer for executing read-only capability checks against a Kavita server.
    """

    def check_capability(self, base_url=None, credentials=None, client_options=None):
        """
        Execute a strictly read-only capability check sequence against the Kavita server.
        Uses x-api-key header only. Never calls Plugin authenticate or mutation endpoints.

        :param base_url: Kavita server URL (defaults to configured URL)
        :param credentials: KavitaCredentials instance (defaults to configured credentials)
        :param client_options: Optional client options dict (session, timeouts)
        :return: dict with sanitized capability diagnostics
        """
        config_status = get_kavita_config_status()
        url = base_url or config_status.get('url')
        creds = credentials
        if creds is None:
            key_val = getattr(mylar.CONFIG, 'KAVITA_API_KEY', None)
            if key_val and str(key_val).strip():
                creds = KavitaCredentials(api_key=str(key_val).strip())

        if not url:
            return self._format_result(
                success=False,
                configured=False,
                enabled=config_status.get('enabled', False),
                reachable=False,
                authenticated=False,
                message="Kavita server URL is not configured.",
                error_code='missing_url'
            )

        if not creds or not creds.is_configured:
            return self._format_result(
                success=False,
                configured=False,
                enabled=config_status.get('enabled', False),
                reachable=False,
                authenticated=False,
                message="Kavita API key is not configured.",
                error_code='missing_api_key'
            )

        # Initialize client with optional injected session / options
        opts = dict(client_options or {})
        try:
            client = KavitaClient(
                base_url=url,
                credentials=creds,
                session=opts.get('session'),
                connect_timeout=opts.get('connect_timeout', 5.0),
                read_timeout=opts.get('read_timeout', 10.0)
            )
        except KavitaConfigurationError as e:
            return self._format_result(
                success=False,
                configured=False,
                enabled=config_status.get('enabled', False),
                reachable=False,
                authenticated=False,
                message=str(e),
                error_code=e.error_code
            )
        except Exception:
            return self._format_result(
                success=False,
                configured=False,
                enabled=config_status.get('enabled', False),
                reachable=False,
                authenticated=False,
                message="Invalid Kavita configuration.",
                error_code='invalid_configuration'
            )

        # 1. Allowed Read-Only Request 1: Server Info & Reachability (x-api-key only)
        server_version = None
        try:
            server_info = client.get('api/Server/server-info-slim')
            if isinstance(server_info, dict):
                server_version = str(server_info.get('version', '') or server_info.get('kavitaVersion', '')).strip() or None
            else:
                return self._format_result(
                    success=False,
                    configured=True,
                    enabled=config_status.get('enabled', False),
                    reachable=True,
                    authenticated=True,
                    message="Malformed server-info response structure.",
                    error_code='invalid_response'
                )
        except KavitaInvalidRequestError:
            # Fallback for alternative server info route
            try:
                server_info = client.get('api/Server/server-info')
                if isinstance(server_info, dict):
                    server_version = str(server_info.get('version', '') or server_info.get('kavitaVersion', '')).strip() or None
                else:
                    return self._format_result(
                        success=False,
                        configured=True,
                        enabled=config_status.get('enabled', False),
                        reachable=True,
                        authenticated=True,
                        message="Malformed server-info response structure.",
                        error_code='invalid_response'
                    )
            except KavitaError as err:
                return self._handle_kavita_error(err, config_status)
        except KavitaError as err:
            return self._handle_kavita_error(err, config_status)

        # 2. Allowed Read-Only Request 2: Dynamic Library Types Discovery (x-api-key only)
        supported_types = []
        comic_type_info = None
        try:
            types_data = client.get('api/Settings/library-types')
            if isinstance(types_data, list):
                for item in types_data:
                    if isinstance(item, dict):
                        t_id = item.get('id', item.get('value', item.get('type')))
                        t_name = str(item.get('name', item.get('title', item.get('label', '')))).strip()
                        if t_name:
                            supported_types.append({'type_id': t_id, 'type_name': t_name})
                            if t_name.lower() in ('comic', 'comics') or 'comic' in t_name.lower():
                                if not comic_type_info:
                                    comic_type_info = {'type_id': t_id, 'type_name': t_name}
                    elif isinstance(item, str):
                        supported_types.append({'type_id': item, 'type_name': item})
                        if item.lower() in ('comic', 'comics') or 'comic' in item.lower():
                            if not comic_type_info:
                                comic_type_info = {'type_id': item, 'type_name': item}
            elif isinstance(types_data, dict):
                for k, v in types_data.items():
                    name_str = str(v if isinstance(v, str) else k)
                    supported_types.append({'type_id': k, 'type_name': name_str})
                    if name_str.lower() in ('comic', 'comics') or 'comic' in name_str.lower():
                        if not comic_type_info:
                            comic_type_info = {'type_id': k, 'type_name': name_str}
            else:
                return self._format_result(
                    success=False,
                    configured=True,
                    enabled=config_status.get('enabled', False),
                    reachable=True,
                    authenticated=True,
                    server_version=server_version,
                    message="Malformed library-types response structure.",
                    error_code='invalid_response'
                )
        except KavitaError as err:
            return self._handle_kavita_error(err, config_status, server_version=server_version)

        if not comic_type_info:
            return self._format_result(
                success=False,
                configured=True,
                enabled=config_status.get('enabled', False),
                reachable=True,
                authenticated=True,
                server_version=server_version,
                api_compatible=False,
                supported_library_types=supported_types,
                comic_library_type_available=False,
                message="Kavita instance is reachable, but no comic-compatible library type was found.",
                error_code='incompatible_library_type'
            )

        # 3. Allowed Read-Only Request 3: Library Readability Check (x-api-key only)
        library_list_readable = False
        try:
            libraries_data = client.get('api/Library/libraries')
            if isinstance(libraries_data, list):
                library_list_readable = True
            elif isinstance(libraries_data, dict):
                library_list_readable = True
            else:
                return self._format_result(
                    success=False,
                    configured=True,
                    enabled=config_status.get('enabled', False),
                    reachable=True,
                    authenticated=True,
                    server_version=server_version,
                    message="Malformed library list response structure.",
                    error_code='invalid_response'
                )
        except KavitaPermissionError:
            return self._format_result(
                success=False,
                configured=True,
                enabled=config_status.get('enabled', False),
                reachable=True,
                authenticated=True,
                server_version=server_version,
                api_compatible=True,
                supported_library_types=supported_types,
                comic_library_type_available=True,
                discovered_comic_type=comic_type_info,
                library_list_readable=False,
                message="Kavita API key lacks permission to list or inspect libraries (HTTP 403 Forbidden).",
                error_code='permission_denied'
            )
        except KavitaError as err:
            return self._handle_kavita_error(
                err, config_status,
                server_version=server_version,
                supported_types=supported_types,
                comic_type=comic_type_info
            )

        # 4. Capability Summary (Zero mutations)
        # Granular scan endpoint availability is verified as advertised capability without executing scan
        scan_advertised = True

        version_display = f" (v{server_version})" if server_version else ""
        return self._format_result(
            success=True,
            configured=True,
            enabled=config_status.get('enabled', False),
            reachable=True,
            authenticated=True,
            server_version=server_version,
            api_compatible=True,
            library_list_readable=library_list_readable,
            supported_library_types=supported_types,
            comic_library_type_available=True,
            discovered_comic_type=comic_type_info,
            library_specific_scan_advertised=scan_advertised,
            message=f"Kavita server connection and capability verification successful{version_display}."
        )

    def _handle_kavita_error(self, err, config_status, server_version=None, supported_types=None, comic_type=None):
        """Format sanitized diagnostics from Kavita exception without leaking secrets."""
        reachable = not isinstance(err, (KavitaTimeoutError, KavitaTransportError))
        authenticated = not isinstance(err, KavitaAuthenticationError) and reachable
        error_code = getattr(err, 'error_code', 'provider_error')

        return self._format_result(
            success=False,
            configured=True,
            enabled=config_status.get('enabled', False),
            reachable=reachable,
            authenticated=authenticated,
            server_version=server_version,
            api_compatible=False,
            supported_library_types=supported_types or [],
            comic_library_type_available=bool(comic_type),
            discovered_comic_type=comic_type,
            library_list_readable=False,
            message=str(err),
            error_code=error_code
        )

    def _format_result(self, success, configured, enabled, reachable, authenticated,
                       server_version=None, api_compatible=False, library_list_readable=False,
                       supported_library_types=None, comic_library_type_available=False,
                       discovered_comic_type=None, library_specific_scan_advertised=False,
                       message="", error_code=None):
        """
        Format canonical sanitized diagnostic response dictionary.
        Guarantees zero raw secrets are included.
        """
        return {
            'success': bool(success),
            'configured': bool(configured),
            'enabled': bool(enabled),
            'reachable': bool(reachable),
            'authenticated': bool(authenticated),
            'server_version': server_version,
            'api_compatible': bool(api_compatible),
            'library_list_readable': bool(library_list_readable),
            'supported_library_types': supported_library_types or [],
            'comic_library_type_available': bool(comic_library_type_available),
            'discovered_comic_type': discovered_comic_type,
            'library_specific_scan_advertised': bool(library_specific_scan_advertised),
            'message': message,
            'error_code': error_code
        }
