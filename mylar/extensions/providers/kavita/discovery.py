"""
Kavita Read-Only Discovery and Diagnostics Service (Phase K4).

Executes strictly read-only capability and library discovery queries against Kavita.
Evaluates observed raw paths, unvalidated raw equality, cross-container unaligned states,
and collision-qualified future naming proposals with zero mutations.
Never generates or rotates MYLAR_INSTANCE_ID, never writes to the database or filesystem,
and never exposes the full UUID in logs, JSON responses, or template contexts.
"""

import os
import posixpath
import mylar
from mylar import logger
from mylar.config import get_mylar_instance_slug
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


def normalize_path_str(path_str):
    """Normalize path string for observation comparisons without modifying casing or slashes."""
    if not path_str or not isinstance(path_str, str):
        return ""
    cleaned = path_str.strip().replace('\\', '/')
    while '//' in cleaned:
        cleaned = cleaned.replace('//', '/')
    return cleaned.rstrip('/')


class KavitaDiscoveryService:
    """
    Read-only discovery service for inspecting Kavita libraries and calculating diagnostic states.
    Operates statelessly with zero database writes, zero file writes, and zero external mutations.
    """

    def run_discovery(self, target_publisher=None, target_path=None, client_options=None):
        """
        Execute read-only Kavita discovery sequence and evaluate diagnostic states.

        :param target_publisher: Optional publisher name (e.g. 'DC Comics')
        :param target_path: Optional target Mylar directory path
        :param client_options: Optional injected dict (session, timeouts) for testing
        :return: dict with sanitized diagnostic payload
        """
        config_status = get_kavita_config_status()
        instance_slug = get_mylar_instance_slug()

        # 1. State: kavita_disabled
        if not config_status.get('enabled', False):
            return self._format_response(
                success=False,
                status_code=200,
                instance_slug=instance_slug,
                diagnostic_state='kavita_disabled',
                state_summary="Kavita integration is disabled in configuration.",
                error_code='provider_disabled'
            )

        # 2. State: kavita_unconfigured
        url = config_status.get('url')
        key_val = getattr(mylar.CONFIG, 'KAVITA_API_KEY', None)
        if not url or not key_val or not str(key_val).strip():
            return self._format_response(
                success=False,
                status_code=200,
                instance_slug=instance_slug,
                diagnostic_state='kavita_unconfigured',
                state_summary="Kavita server URL or API key is not configured.",
                error_code='missing_configuration'
            )

        # Resolve local target path
        resolved_target_path = target_path
        if not resolved_target_path:
            comic_dir = getattr(mylar.CONFIG, 'COMIC_DIR', None)
            if target_publisher and comic_dir:
                resolved_target_path = os.path.join(comic_dir, target_publisher)
            elif comic_dir:
                resolved_target_path = comic_dir
            elif target_publisher:
                resolved_target_path = f"/comics/{target_publisher}"
            else:
                resolved_target_path = "/comics"

        norm_target_path = normalize_path_str(resolved_target_path)
        publisher_name = str(target_publisher).strip() if target_publisher else "DC Comics"

        # Initialize KavitaClient with pure x-api-key header
        opts = dict(client_options or {})
        creds = KavitaCredentials(api_key=str(key_val).strip())

        try:
            client = KavitaClient(
                base_url=url,
                credentials=creds,
                session=opts.get('session'),
                connect_timeout=opts.get('connect_timeout', 5.0),
                read_timeout=opts.get('read_timeout', 10.0)
            )
        except Exception:
            return self._format_response(
                success=False,
                status_code=200,
                instance_slug=instance_slug,
                target_mylar_path=resolved_target_path,
                diagnostic_state='kavita_unconfigured',
                state_summary="Invalid Kavita server configuration.",
                error_code='invalid_configuration'
            )

        # 3. Read-Only Discovery: Server Info (reachability & version)
        server_version = None
        try:
            server_info = client.get('api/Server/server-info-slim')
            if isinstance(server_info, dict):
                server_version = str(server_info.get('version', '') or server_info.get('kavitaVersion', '')).strip() or None
        except KavitaInvalidRequestError:
            try:
                server_info = client.get('api/Server/server-info')
                if isinstance(server_info, dict):
                    server_version = str(server_info.get('version', '') or server_info.get('kavitaVersion', '')).strip() or None
            except KavitaError as err:
                return self._handle_provider_error(err, instance_slug, resolved_target_path)
        except KavitaError as err:
            return self._handle_provider_error(err, instance_slug, resolved_target_path)

        # 4. Read-Only Discovery: Library Types
        comic_type_found = False
        try:
            types_data = client.get('api/Settings/library-types')
            if isinstance(types_data, list):
                for item in types_data:
                    name_str = item.get('name', '') if isinstance(item, dict) else str(item)
                    if 'comic' in name_str.lower():
                        comic_type_found = True
                        break
            elif isinstance(types_data, dict):
                for k, v in types_data.items():
                    name_str = str(v if isinstance(v, str) else k)
                    if 'comic' in name_str.lower():
                        comic_type_found = True
                        break
        except KavitaError as err:
            return self._handle_provider_error(err, instance_slug, resolved_target_path, server_version=server_version)

        if not comic_type_found:
            return self._format_response(
                success=False,
                status_code=200,
                instance_slug=instance_slug,
                kavita_server_version=server_version,
                target_mylar_path=resolved_target_path,
                diagnostic_state='incompatible_library_type',
                state_summary="Kavita instance is reachable, but no comic-compatible library type was found.",
                error_code='incompatible_library_type'
            )

        # 5. Read-Only Discovery: Library List
        try:
            libraries_data = client.get('api/Library/libraries')
        except KavitaError as err:
            return self._handle_provider_error(err, instance_slug, resolved_target_path, server_version=server_version)

        if not isinstance(libraries_data, list):
            libraries_data = []

        # 6. Analyze Discovered Libraries & Raw Paths
        discovered_libraries = []
        observed_path_counts = {}
        exact_raw_path_libraries = []
        name_colliding_libraries = []

        for lib in libraries_data:
            if not isinstance(lib, dict):
                continue
            lib_id = lib.get('id', lib.get('libraryId'))
            lib_name = str(lib.get('name', '')).strip()
            lib_type = lib.get('type')
            raw_folders = [str(f).strip() for f in lib.get('folders', []) if str(f).strip()]

            # Track folder paths for ambiguity detection
            norm_folders = [normalize_path_str(f) for f in raw_folders]
            for nf in norm_folders:
                observed_path_counts[nf] = observed_path_counts.get(nf, 0) + 1

            # Path observation comparison
            is_raw_equal = norm_target_path in norm_folders
            if is_raw_equal:
                exact_raw_path_libraries.append(lib)

            if lib_name.lower() == publisher_name.lower():
                name_colliding_libraries.append(lib)

            alignment_status = "raw_path_equal_unvalidated" if is_raw_equal else "cross_container_unaligned"
            evidence_status = "unproven_without_binding"

            warning_text = None
            if is_raw_equal:
                warning_text = "Raw path strings are identical, but identical string values do not prove shared container storage. Remote library remains untrusted and unselected."
            else:
                warning_text = f"Remote path '{raw_folders[0] if raw_folders else 'empty'}' differs from local path. Cross-container alignment is unverified."

            discovered_libraries.append({
                'library_id': lib_id,
                'library_name': lib_name,
                'library_type': lib_type,
                'observed_raw_paths': raw_folders,
                'path_alignment_status': alignment_status,
                'alignment_evidence_status': evidence_status,
                'warning': warning_text
            })

        # 7. State Machine Evaluation
        # A. Ambiguous multi-path match (multiple Kavita libraries claim the same folder)
        if any(cnt > 1 for path, cnt in observed_path_counts.items() if path == norm_target_path):
            return self._format_response(
                success=True,
                status_code=200,
                instance_slug=instance_slug,
                kavita_server_version=server_version,
                target_mylar_path=resolved_target_path,
                diagnostic_state='ambiguous_multi_path_match',
                state_summary="Multiple Kavita libraries report the exact same folder path. Ambiguity detected; zero candidate proposals generated.",
                libraries_discovered_count=len(discovered_libraries),
                discovered_libraries=discovered_libraries,
                future_naming_proposals=[]
            )

        # B. Raw Path Equal Unvalidated State
        if exact_raw_path_libraries:
            proposals = []
            has_name_collision = any(l.get('name', '').strip().lower() == publisher_name.lower() for l in exact_raw_path_libraries)
            if has_name_collision:
                slug_suffix = f" ({instance_slug})" if instance_slug else ""
                proposals.append({
                    'publisher_name': publisher_name,
                    'proposed_name': f"{publisher_name}{slug_suffix}",
                    'proposal_type': 'instance_qualified',
                    'reason': f"Existing Kavita library '{publisher_name}' claims the clean display name on an unvalidated raw path. Qualified proposal avoids duplicate-name conflict while physical equivalence is unproven.",
                    'disclaimer': "Future naming proposal only. This proposal is not an active mapping, selection, or mutation."
                })
            else:
                proposals.append({
                    'publisher_name': publisher_name,
                    'proposed_name': publisher_name,
                    'proposal_type': 'clean_unqualified',
                    'reason': "Raw path equality observed with non-colliding remote library name. Future naming proposal is clean display name.",
                    'disclaimer': "Future naming proposal only. This proposal is not an active mapping, selection, or mutation."
                })

            return self._format_response(
                success=True,
                status_code=200,
                instance_slug=instance_slug,
                kavita_server_version=server_version,
                target_mylar_path=resolved_target_path,
                diagnostic_state='raw_path_equal_unvalidated',
                state_summary=f"Observed raw path string equality with Kavita library, but physical storage equivalence is unproven without a validated root binding.",
                libraries_discovered_count=len(discovered_libraries),
                discovered_libraries=discovered_libraries,
                future_naming_proposals=proposals
            )

        # C. Cross-Container Unaligned with Name Collision
        if name_colliding_libraries:
            slug_suffix = f" ({instance_slug})" if instance_slug else ""
            return self._format_response(
                success=True,
                status_code=200,
                instance_slug=instance_slug,
                kavita_server_version=server_version,
                target_mylar_path=resolved_target_path,
                diagnostic_state='collision_qualified_candidate',
                state_summary=f"Name collision detected on unaligned path. Future naming proposal is instance-qualified.",
                libraries_discovered_count=len(discovered_libraries),
                discovered_libraries=discovered_libraries,
                future_naming_proposals=[{
                    'publisher_name': publisher_name,
                    'proposed_name': f"{publisher_name}{slug_suffix}",
                    'proposal_type': 'instance_qualified',
                    'reason': f"Existing Kavita library '{publisher_name}' uses a different unaligned path. Qualified proposal avoids duplicate-name conflict.",
                    'disclaimer': "Future naming proposal only. This proposal is not an active mapping, selection, or mutation."
                }]
            )

        # D. Clean Name Candidate (No libraries exist or clean name is free)
        if not discovered_libraries:
            diag_state = 'no_candidate'
            summary_msg = "No existing libraries discovered on Kavita server. Target path is unmapped."
        else:
            diag_state = 'clean_name_candidate'
            summary_msg = "Publisher display name is free on Kavita. Target path is unmapped."

        return self._format_response(
            success=True,
            status_code=200,
            instance_slug=instance_slug,
            kavita_server_version=server_version,
            target_mylar_path=resolved_target_path,
            diagnostic_state=diag_state,
            state_summary=summary_msg,
            libraries_discovered_count=len(discovered_libraries),
            discovered_libraries=discovered_libraries,
            future_naming_proposals=[{
                'publisher_name': publisher_name,
                'proposed_name': publisher_name,
                'proposal_type': 'clean_unqualified',
                'reason': "Clean publisher display name is free and unmapped on Kavita.",
                'disclaimer': "Future naming proposal only. This proposal is not an active mapping, selection, or mutation."
            }]
        )

    def _handle_provider_error(self, err, instance_slug, target_path, server_version=None):
        """Map Kavita transport/auth exceptions to sanitized diagnostic states without leaking secrets."""
        if isinstance(err, (KavitaAuthenticationError, KavitaPermissionError)):
            state = 'library_list_unavailable'
            msg = "Kavita API authentication failed or lacks library inspection permissions (HTTP 401/403)."
            code = 'authentication_failed'
        elif isinstance(err, (KavitaTimeoutError, KavitaTransportError)):
            state = 'capability_check_required'
            msg = "Unable to connect to Kavita server. Check host reachability and network configuration."
            code = 'transport_error'
        else:
            state = 'capability_check_required'
            msg = "Kavita server returned an invalid response."
            code = 'invalid_response'

        return self._format_response(
            success=False,
            status_code=200,
            instance_slug=instance_slug,
            kavita_server_version=server_version,
            target_mylar_path=target_path,
            diagnostic_state=state,
            state_summary=msg,
            error_code=code
        )

    def _format_response(self, success, status_code=200, instance_slug=None, kavita_server_version=None,
                         target_mylar_path=None, diagnostic_state='kavita_disabled', state_summary="",
                         libraries_discovered_count=0, discovered_libraries=None, future_naming_proposals=None,
                         error_code=None):
        """
        Format canonical sanitized diagnostic response dictionary.
        Guarantees zero full UUIDs, zero API keys, and zero mutation state.
        """
        return {
            'success': bool(success),
            'status_code': int(status_code),
            'mylar_instance_slug': instance_slug,
            'kavita_server_version': kavita_server_version,
            'target_mylar_path': target_mylar_path,
            'diagnostic_state': diagnostic_state,
            'state_summary': state_summary,
            'libraries_discovered_count': int(libraries_discovered_count),
            'discovered_libraries': discovered_libraries or [],
            'future_naming_proposals': future_naming_proposals or [],
            'read_only_notice': "This diagnostic endpoint performs zero library mutations, mappings, root bindings, database writes, or filesystem writes.",
            'error_code': error_code
        }
