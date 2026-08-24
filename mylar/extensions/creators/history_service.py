"""
Creator Identity Decision History Service (Phase C4.11).

Provides strictly read-only audit and decision history querying for exact local
NameRecordIDs using the existing C4.8 IdentityRepository persistence model.
Performs ZERO SQL writes, ZERO provider network calls, and ZERO entity modifications.
Functions fully even when external providers (e.g. Metron) are disabled or unconfigured.
"""

import json
from typing import Any, Dict, List, Optional, Tuple

from mylar.extensions.creators.identity_repository import IdentityRepository


class CreatorHistoryError(Exception):
    """Base exception for creator history operations."""
    pass


class InvalidHistoryInputError(CreatorHistoryError):
    """Raised when input parameters to history queries are invalid or malformed."""
    pass


class LocalNameRecordNotFoundError(CreatorHistoryError):
    """Raised when the specified local NameRecordID does not exist."""
    pass


SUPPORTED_PROVIDERS = {'metron', 'comicvine'}


class CreatorHistoryService:
    """
    Strictly read-only service to query and format immutable decision history and
    current resolution status for exact local NameRecordIDs.
    """

    def __init__(self, repository: Optional[IdentityRepository] = None, db_conn=None):
        self._repo = repository or IdentityRepository(db_conn=db_conn)

    def _validate_inputs(
        self,
        name_record_id: Any,
        provider: Optional[str] = None,
        provider_creator_id: Optional[Any] = None,
        limit: int = 50,
        offset: int = 0
    ) -> Tuple[int, Optional[str], Optional[str], int, int]:
        """Validate and sanitize input query parameters."""
        if name_record_id is None:
            raise InvalidHistoryInputError("name_record_id is required and cannot be None.")
        if isinstance(name_record_id, bool):
            raise InvalidHistoryInputError("name_record_id cannot be a boolean.")
        try:
            val_nr_id = int(str(name_record_id).strip())
            if val_nr_id <= 0:
                raise ValueError()
        except (ValueError, TypeError):
            raise InvalidHistoryInputError(
                f"Invalid name_record_id '{name_record_id}': must be a positive integer."
            )

        clean_provider = None
        if provider is not None:
            if not isinstance(provider, str) or not provider.strip():
                raise InvalidHistoryInputError("Provider namespace cannot be empty if specified.")
            clean_provider = provider.strip().lower()
            if clean_provider not in SUPPORTED_PROVIDERS:
                raise InvalidHistoryInputError(
                    f"Unsupported provider namespace '{provider}'. Supported: {sorted(SUPPORTED_PROVIDERS)}"
                )

        clean_provider_creator_id = None
        if provider_creator_id is not None:
            str_id = str(provider_creator_id).strip()
            if not str_id:
                raise InvalidHistoryInputError("provider_creator_id cannot be empty if specified.")
            clean_provider_creator_id = str_id

        try:
            val_limit = int(limit)
            if val_limit < 1:
                val_limit = 50
            elif val_limit > 100:
                val_limit = 100
        except (ValueError, TypeError):
            val_limit = 50

        try:
            val_offset = int(offset)
            if val_offset < 0:
                val_offset = 0
        except (ValueError, TypeError):
            val_offset = 0

        return val_nr_id, clean_provider, clean_provider_creator_id, val_limit, val_offset

    def get_decision_history(
        self,
        name_record_id: Any,
        provider: Optional[str] = None,
        provider_creator_id: Optional[Any] = None,
        limit: int = 50,
        offset: int = 0
    ) -> Dict[str, Any]:
        """
        Query the complete, read-only decision history and current state for an exact local NameRecordID.

        :param name_record_id: Exact local NameRecordID (positive integer)
        :param provider: Optional provider namespace filter ('metron')
        :param provider_creator_id: Optional provider creator ID filter
        :param limit: Maximum timeline events to return (1..100)
        :param offset: Timeline pagination offset (>= 0)
        :return: Sanitized dictionary containing current_state, timeline, and explanation
        """
        val_nr_id, clean_provider, clean_provider_id, val_limit, val_offset = self._validate_inputs(
            name_record_id, provider, provider_creator_id, limit, offset
        )

        # 1. Fetch exact NameRecord
        name_rec = self._repo.get_name_record(val_nr_id)
        if not name_rec:
            raise LocalNameRecordNotFoundError(f"Local NameRecord with ID {val_nr_id} was not found.")

        # 2. Determine current resolution state
        entity_id = name_rec.get('creator_entity_id')
        confirmed_entity = None
        external_ids = []
        if entity_id:
            entity_data = self._repo.get_entity_by_id(entity_id)
            if entity_data:
                confirmed_entity = {
                    'creator_entity_id': entity_data['creator_entity_id'],
                    'display_name': entity_data['display_name'],
                    'normalized_name': entity_data['normalized_name'],
                    'entity_slug': entity_data['entity_slug'],
                }
                external_ids = self._repo.get_external_ids_for_entity(entity_id)

        # Active rejection for this name record and provider/id
        active_rejection = None
        if clean_provider and clean_provider_id:
            rej = self._repo.get_active_rejection(val_nr_id, clean_provider, clean_provider_id)
            if rej:
                active_rejection = {
                    'rejection_id': rej.get('rejection_id'),
                    'provider': rej.get('provider'),
                    'external_id': rej.get('external_id'),
                    'reason': rej.get('reason'),
                    'created_at': rej.get('created_at')
                }

        # Shared canonical state derivation
        pairing_res = self._repo.derive_pairing_state(val_nr_id, clean_provider, clean_provider_id)
        current_status = pairing_res['state']
        explanation_summary = pairing_res['explanation']

        current_state = {
            'name_record_id': val_nr_id,
            'raw_local_name': name_rec['raw_name'],
            'normalized_name': name_rec['normalized_name'],
            'name_slug': name_rec['name_slug'],
            'resolution_source': name_rec.get('resolution_source', 'unresolved'),
            'status': current_status,
            'explanation': explanation_summary,
            'creator_entity_id': entity_id,
            'confirmed_entity': confirmed_entity,
            'external_ids': [
                {
                    'provider': ext['provider'],
                    'external_id': ext['external_id'],
                    'confidence': ext.get('confidence', 1.0)
                }
                for ext in external_ids
            ],
            'active_rejection': pairing_res.get('active_rejection')
        }

        # 3. Query immutable audit events (all events for this NameRecordID)
        # IdentityRepository.get_audit_history returns events ordered DESC by AuditID
        raw_audit_entries = self._repo.get_audit_history(name_record_id=val_nr_id, limit=500)

        # Filter by provider / external_id if specified
        filtered_entries = []
        for entry in raw_audit_entries:
            if clean_provider and entry.get('provider') and entry.get('provider').lower() != clean_provider:
                continue
            if clean_provider_id and entry.get('external_id') and str(entry.get('external_id')) != clean_provider_id:
                continue
            filtered_entries.append(entry)

        # Deterministic chronological order: oldest to newest
        filtered_entries.sort(key=lambda x: (x.get('audit_id', 0), x.get('created_at', '')))

        total_events = len(filtered_entries)
        paginated_entries = filtered_entries[val_offset:val_offset + val_limit]

        # 4. Format timeline events
        timeline: List[Dict[str, Any]] = []
        for entry in paginated_entries:
            action = entry.get('action', 'UNKNOWN_ACTION')
            actor = entry.get('actor') or 'historical_record'
            actor_label = {
                'human_reviewer': 'Human Reviewer',
                'system': 'System',
                'historical_record': 'Historical Record'
            }.get(actor, actor.replace('_', ' ').title())

            timestamp = entry.get('created_at')
            prov = entry.get('provider')
            ext_id = entry.get('external_id')
            prov_name = entry.get('provider_display_name')
            reason = entry.get('reason')
            conf_source = entry.get('confirmation_source')

            # Human-friendly action label and explanation
            action_label, explanation, reversal_info = self._explain_audit_event(
                action, prov, ext_id, prov_name, reason, conf_source, entry
            )

            timeline.append({
                'audit_id': entry.get('audit_id'),
                'action': action,
                'action_label': action_label,
                'actor': actor,
                'actor_label': actor_label,
                'timestamp': timestamp,
                'provider': prov,
                'external_id': ext_id,
                'provider_display_name': prov_name,
                'reason': reason,
                'confirmation_source': conf_source,
                'explanation': explanation,
                'reversal_info': reversal_info
            })

        # 5. Server-generated summary explanation
        summary_text = self._build_state_summary(current_state, total_events)

        return {
            'success': True,
            'name_record_id': val_nr_id,
            'current_state': current_state,
            'timeline': timeline,
            'total_events': total_events,
            'limit': val_limit,
            'offset': val_offset,
            'has_more': (val_offset + len(timeline)) < total_events,
            'explanation_summary': summary_text
        }

    def _explain_audit_event(
        self,
        action: str,
        provider: Optional[str],
        external_id: Optional[str],
        provider_display_name: Optional[str],
        reason: Optional[str],
        confirmation_source: Optional[str],
        entry: Dict[str, Any]
    ) -> Tuple[str, str, Optional[Dict[str, Any]]]:
        """Generate human-readable labels, truthful explanations, and reversal linkages."""
        prov_label = provider.upper() if provider else 'Provider'
        name_str = f" as {provider_display_name}" if provider_display_name else ""
        id_str = f" ({prov_label} ID: {external_id})" if external_id else ""

        reversal_info = None

        if action == 'CONFIRM_PROVIDER_IDENTITY':
            action_label = "Confirmed Provider Identity"
            explanation = f"Explicitly confirmed{name_str}{id_str}. Entity link established."
        elif action == 'REJECT_CANDIDATE':
            action_label = "Rejected Candidate"
            explanation = f"Actively suppressed candidate pairing with{name_str}{id_str}."
        elif action == 'TRANSFER_PROVIDER_MAPPING':
            action_label = "Transferred Provider Mapping"
            after_st = entry.get('after_state') or {}
            before_st = entry.get('before_state') or {}
            src_name = before_st.get('source_entity_name') or f"Entity #{before_st.get('source_entity_id', '?')}"
            dest_name = after_st.get('destination_entity_name') or f"Entity #{after_st.get('destination_entity_id', '?')}"
            explanation = f"Explicitly transferred provider mapping {id_str} from '{src_name}' to '{dest_name}'."
            if before_st.get('superseded_rejection_id'):
                explanation += f" (Superseded previous candidate rejection #{before_st['superseded_rejection_id']})."
        elif action == 'RESOLVE_CONFLICT_REJECT_COMPETING':
            action_label = "Conflict Resolved (Existing Retained)"
            explanation = f"Retained existing provider mapping {id_str} and explicitly rejected competing candidate pairing."
        elif action == 'REVERSE_TRANSFER_PROVIDER_MAPPING':
            action_label = "Reversed Provider Transfer"
            after_st = entry.get('after_state') or {}
            src_name = after_st.get('source_entity_name') or (f"Entity #{after_st.get('source_entity_id')}" if after_st.get('source_entity_id') else 'original entity')
            explanation = f"Reversed previous transfer of {id_str}, atomically restoring mapping to '{src_name}'."
            reversal_info = {
                'reverses_action': 'TRANSFER_PROVIDER_MAPPING',
                'target_provider': provider,
                'target_external_id': external_id
            }
        elif action == 'REVERSE_CONFIRMATION':
            action_label = "Reversed Confirmation"
            explanation = f"Reversed previous identity confirmation{id_str}, returning local credit to unlinked state."
            reversal_info = {
                'reverses_action': 'CONFIRM_PROVIDER_IDENTITY',
                'target_provider': provider,
                'target_external_id': external_id
            }
        elif action == 'REVERSE_REJECTION':
            action_label = "Reversed Rejection"
            explanation = f"Reversed candidate suppression{id_str}, restoring candidate for review."
            reversal_info = {
                'reverses_action': 'REJECT_CANDIDATE',
                'target_provider': provider,
                'target_external_id': external_id
            }
        elif action == 'CONFIRM_RAW_NAME':
            action_label = "Confirmed Entity Link"
            explanation = f"Linked local credit to entity{name_str}."
        else:
            action_label = action.replace('_', ' ').title()
            explanation = f"Action '{action}' recorded in decision audit log."

        if reason:
            explanation += f" Reason: {reason}"

        return action_label, explanation, reversal_info

    def _build_state_summary(self, current_state: Dict[str, Any], total_events: int) -> str:
        """Build truthful summary sentence for current resolution state."""
        raw_name = current_state['raw_local_name']
        if current_state.get('explanation'):
            return current_state['explanation']

        status = current_state['status']
        if status == 'transferred' and current_state.get('confirmed_entity'):
            ent = current_state['confirmed_entity']
            exts = current_state.get('external_ids', [])
            ext_str = f" [{exts[0]['provider'].upper()} ID: {exts[0]['external_id']}]" if exts else ""
            return f"Confirmed through an explicit provider-mapping transfer to '{ent['display_name']}'{ext_str}."
        elif status == 'confirmed' and current_state.get('confirmed_entity'):
            ent = current_state['confirmed_entity']
            exts = current_state.get('external_ids', [])
            ext_str = f" [{exts[0]['provider'].upper()} ID: {exts[0]['external_id']}]" if exts else ""
            return f"Local credit '{raw_name}' is currently confirmed as '{ent['display_name']}'{ext_str}."
        elif status == 'rejected' and current_state.get('active_rejection'):
            rej = current_state['active_rejection']
            return f"Local credit '{raw_name}' has an active rejection suppressing {rej['provider'].upper()} ID: {rej['external_id']}."
        else:
            if total_events > 0:
                return f"Local credit '{raw_name}' is currently unlinked. {total_events} decision event{'s' if total_events != 1 else ''} on record."
            return f"Local credit '{raw_name}' is currently unlinked with no decision history on record."
