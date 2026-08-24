"""
Creator Identity Conflict Analysis Service (Phase C4.13).

Provides strictly read-only analysis and breakdown of creator identity conflicts recorded in the local database.
Evaluates why direct resolution is blocked between a local NameRecord and a provider identity.
Performs ZERO SQL writes, ZERO provider/network calls, and contains ZERO mutation logic.
"""

from typing import Any, Dict, List, Optional, Tuple

from mylar.extensions.creators.identity_repository import IdentityRepository
from mylar.extensions.creators.normalizer import normalize_name, slugify


class CreatorConflictError(Exception):
    """Base exception for creator conflict operations."""
    pass


class InvalidConflictInputError(CreatorConflictError):
    """Raised when input parameters for conflict analysis are invalid."""
    pass


class LocalNameRecordNotFoundError(CreatorConflictError):
    """Raised when the specified local NameRecordID does not exist."""
    pass


class StaleConflictStateError(CreatorConflictError):
    """Raised when the conflict state has changed, is stale, or is no longer present."""
    pass


class TransferTargetUnavailableError(CreatorConflictError):
    """Raised when the competing NameRecord is unlinked or its creator entity is missing."""
    pass


SUPPORTED_PROVIDERS = {'metron', 'comicvine'}


class CreatorConflictService:
    """
    Dedicated read-only service for analyzing and explaining creator identity conflicts.
    """

    def __init__(self, repository: Optional[IdentityRepository] = None, db_conn=None):
        self._repo = repository or IdentityRepository(db_conn=db_conn)

    def _get_db(self):
        return self._repo._get_db()

    def _validate_inputs(
        self,
        name_record_id: Any,
        provider: Optional[str] = None,
        provider_creator_id: Optional[Any] = None
    ) -> Tuple[int, Optional[str], Optional[str]]:
        """Validate and clean input parameters."""
        # 1. Validate name_record_id
        if name_record_id is None:
            raise InvalidConflictInputError("name_record_id is required and cannot be None.")
        if isinstance(name_record_id, bool):
            raise InvalidConflictInputError("name_record_id cannot be a boolean.")
        try:
            val_id = int(str(name_record_id).strip())
            if val_id <= 0:
                raise ValueError()
        except (ValueError, TypeError):
            raise InvalidConflictInputError(
                f"Invalid name_record_id '{name_record_id}': must be a positive integer."
            )

        # 2. Validate provider namespace if provided
        clean_prov = None
        if provider is not None and str(provider).strip():
            p_str = str(provider).strip().lower()
            if p_str not in SUPPORTED_PROVIDERS:
                raise InvalidConflictInputError(
                    f"Unsupported provider namespace '{provider}'. Supported: {sorted(SUPPORTED_PROVIDERS)}"
                )
            clean_prov = p_str

        # 3. Validate provider_creator_id if provided
        clean_ext_id = None
        if provider_creator_id is not None and str(provider_creator_id).strip():
            ext_str = str(provider_creator_id).strip()
            if not ext_str.isdigit() or int(ext_str) <= 0:
                raise InvalidConflictInputError(
                    f"Invalid provider_creator_id '{provider_creator_id}': must be a positive numeric ID."
                )
            clean_ext_id = ext_str

        return val_id, clean_prov, clean_ext_id

    def analyze_creator_conflict(
        self,
        name_record_id: Any,
        provider: Optional[str] = None,
        provider_creator_id: Optional[Any] = None
    ) -> Dict[str, Any]:
        """
        Perform a strictly read-only inspection of why a local credit pairing is conflicted.

        :param name_record_id: Exact local NameRecordID
        :param provider: Optional provider namespace (e.g. 'metron')
        :param provider_creator_id: Optional provider creator identifier (e.g. '999')
        :return: Structured sanitized dictionary explaining the conflict
        """
        val_name_rec_id, clean_prov, clean_ext_id = self._validate_inputs(
            name_record_id, provider, provider_creator_id
        )

        cur, _ = self._repo._get_cursor()

        # 1. Fetch requested local name record
        name_rec = self._repo.get_name_record(val_name_rec_id, cursor=cur)
        if not name_rec:
            raise LocalNameRecordNotFoundError(
                f"Local NameRecord with ID {val_name_rec_id} was not found."
            )

        linked_entity_id = name_rec.get('creator_entity_id')
        linked_entity = None
        if linked_entity_id:
            linked_entity = self._repo.get_entity_by_id(linked_entity_id, cursor=cur)

        # 2. Check provider external ID mapping
        competing_mapping = None
        has_external_id_collision = False
        target_provider_info = {
            'provider': clean_prov,
            'provider_creator_id': clean_ext_id,
            'provider_display_name': None
        }

        if clean_prov and clean_ext_id:
            ext_map = self._repo.get_external_id_mapping(clean_prov, clean_ext_id, cursor=cur)
            if ext_map:
                mapped_entity_id = ext_map['creator_entity_id']
                mapped_entity = self._repo.get_entity_by_id(mapped_entity_id, cursor=cur)
                target_provider_info['provider_display_name'] = ext_map.get('provider_display_name') or (mapped_entity['display_name'] if mapped_entity else None)

                # Check if current record is linked to a DIFFERENT entity
                if linked_entity_id and linked_entity_id != mapped_entity_id:
                    has_external_id_collision = True
                    other_name_records = self._repo.get_name_records_for_entity(mapped_entity_id, cursor=cur)
                    competing_name_recs = [
                        {
                            'name_record_id': r['name_record_id'],
                            'raw_local_name': r['raw_name'],
                            'normalized_name': r['normalized_name'],
                            'resolution_source': r.get('resolution_source', 'unresolved')
                        }
                        for r in other_name_records if r['name_record_id'] != val_name_rec_id
                    ]
                    competing_mapping = {
                        'competing_entity_id': mapped_entity_id,
                        'competing_entity_name': mapped_entity['display_name'] if mapped_entity else "Unknown Entity",
                        'competing_entity_slug': mapped_entity['entity_slug'] if mapped_entity else "",
                        'bound_external_id': clean_ext_id,
                        'provider': clean_prov,
                        'competing_name_records': competing_name_recs
                    }

        # 3. Query audit log for recorded collision events
        cur.execute(
            """
            SELECT AuditID, Action, Provider, ExternalID, ProviderDisplayName, Actor, Reason, ConfirmationSource, CreatedAt
            FROM ext_creator_resolution_audit
            WHERE NameRecordID = ? AND (Action = 'LEGACY_EXTERNAL_ID_COLLISION' OR Reason LIKE '%collision%' OR Reason LIKE '%conflict%')
            ORDER BY AuditID DESC
            """,
            (val_name_rec_id,)
        )
        audit_collision_rows = cur.fetchall()
        audit_refs: List[Dict[str, Any]] = []
        for a in audit_collision_rows:
            actor = a[5] or 'historical_record'
            actor_lbl = {
                'human_reviewer': 'Human Reviewer',
                'system': 'System',
                'historical_record': 'Historical Record'
            }.get(actor, actor.replace('_', ' ').title())

            act = a[1]
            act_lbl = {
                'LEGACY_EXTERNAL_ID_COLLISION': 'External ID Collision Detected',
                'CONFIRM_PROVIDER_IDENTITY': 'Confirmed Provider Identity',
                'REJECT_CANDIDATE': 'Rejected Candidate'
            }.get(act, act.replace('_', ' ').title())

            audit_refs.append({
                'audit_id': a[0],
                'action': act,
                'action_label': act_lbl,
                'provider': a[2],
                'external_id': a[3],
                'provider_display_name': a[4],
                'actor': actor,
                'actor_label': actor_lbl,
                'reason': a[6] or "Collision recorded in history",
                'timestamp': a[8]
            })

        # 4. Synthesize conflict status & blocking reason
        if has_external_id_collision:
            is_conflicted = True
            conflict_type = 'EXTERNAL_ID_COLLISION'
            comp_ent_name = competing_mapping['competing_entity_name'] if competing_mapping else "Competing Entity"
            comp_ent_id = competing_mapping['competing_entity_id'] if competing_mapping else "-"
            curr_ent_name = linked_entity['display_name'] if linked_entity else "Current Entity"
            curr_ent_id = linked_entity_id or "-"
            blocking_reason = (
                f"Provider {clean_prov.upper()} ID {clean_ext_id} already maps to local CreatorEntity #{comp_ent_id} ('{comp_ent_name}'), "
                f"while local credit #{val_name_rec_id} ('{name_rec['raw_name']}') is currently linked to CreatorEntity #{curr_ent_id} ('{curr_ent_name}'). "
                f"Direct resolution is blocked to prevent overwriting existing identity mappings."
            )
            explanation = "Both local credit entities claim independent identities for the same external provider ID. Direct linking is blocked."
        elif audit_refs:
            is_conflicted = True
            conflict_type = 'RECORDED_COLLISION_AUDIT'
            blocking_reason = audit_refs[0]['reason']
            explanation = "An external ID collision or conflict event was previously recorded in the audit log for this credit."
        else:
            is_conflicted = False
            conflict_type = 'NO_ACTIVE_CONFLICT'
            blocking_reason = "No active identity collision or conflict detected for this credit."
            explanation = "The requested local credit and provider pairing does not currently have competing identity claims."

        # 5. Fetch source issue / comic navigation context
        source_context = self._fetch_source_context(val_name_rec_id, cur)

        # 6. Canonical derived pairing state
        pairing_state = self._repo.derive_pairing_state(val_name_rec_id, clean_prov, clean_ext_id, cursor=cur)

        return {
            'success': True,
            'is_conflicted': is_conflicted,
            'conflict_type': conflict_type,
            'derived_state': pairing_state['state'],
            'derived_explanation': pairing_state['explanation'],
            'blocking_reason': blocking_reason,
            'explanation': explanation,
            'requested_record': {
                'name_record_id': val_name_rec_id,
                'raw_local_name': name_rec['raw_name'],
                'normalized_name': name_rec['normalized_name'],
                'name_slug': name_rec['name_slug'],
                'resolution_source': name_rec.get('resolution_source', 'unresolved'),
                'linked_entity_id': linked_entity_id,
                'linked_entity_name': linked_entity['display_name'] if linked_entity else None,
                'linked_entity_slug': linked_entity['entity_slug'] if linked_entity else None,
            },
            'target_provider': target_provider_info,
            'competing_mapping': competing_mapping,
            'audit_references': audit_refs,
            'source_context': source_context,
            'resolution_available': False
        }

    def _fetch_source_context(self, name_record_id: int, cursor) -> Optional[Dict[str, Any]]:
        """Validate and fetch source issue context from local DB."""
        cursor.execute(
            "SELECT IssueID, IsAnnual, ComicID, Role FROM ext_creator_credits WHERE NameRecordID = ? LIMIT 1",
            (name_record_id,)
        )
        c_row = cursor.fetchone()
        if not c_row:
            return None

        issue_id = c_row[0]
        is_annual = bool(c_row[1])
        comic_id = c_row[2]
        role = c_row[3]

        if not issue_id or not comic_id:
            return None

        table = "annuals" if is_annual else "issues"
        cursor.execute(f"SELECT Issue_Number, IssueName, IssueDate FROM {table} WHERE IssueID = ?", (issue_id,))
        iss_row = cursor.fetchone()
        if not iss_row:
            return None

        cursor.execute("SELECT ComicName, ComicYear FROM comics WHERE ComicID = ?", (comic_id,))
        com_row = cursor.fetchone()
        comic_name = com_row[0] if com_row else "Unknown Comic"

        return {
            'issue_id': str(issue_id),
            'is_annual': is_annual,
            'comic_id': str(comic_id),
            'comic_name': comic_name,
            'issue_number': str(iss_row[0]) if iss_row[0] is not None else "",
            'issue_name': iss_row[1] or "",
            'issue_date': iss_row[2] or "",
            'role': role
        }

    def resolve_conflict_keep_existing_reject_competing(
        self,
        name_record_id: Any,
        provider: Optional[str] = None,
        provider_creator_id: Optional[Any] = None,
        reason: Optional[str] = None,
        actor: str = "user"
    ) -> Dict[str, Any]:
        """
        Explicitly retain the existing provider external-ID mapping and record a rejection
        for the competing local NameRecord and provider creator ID (Phase C4.15 Outcome 1).

        Inside a single atomic transaction:
        1. Validates inputs.
        2. Verifies the local NameRecord exists.
        3. Checks for existing active rejection (idempotent repeat submission).
        4. Verifies active external ID mapping and ensures collision state exists (stale-state protection).
        5. Inserts or reactivates the rejection in ext_creator_candidate_rejections.
        6. Inserts exactly one immutable audit event (RESOLVE_CONFLICT_REJECT_COMPETING).
        7. Commits the transaction and returns structured result.
        """
        val_name_rec_id, clean_prov, clean_ext_id = self._validate_inputs(
            name_record_id, provider, provider_creator_id
        )

        if not clean_prov:
            raise InvalidConflictInputError("Provider namespace is required for conflict resolution.")
        if not clean_ext_id:
            raise InvalidConflictInputError("Provider creator ID is required for conflict resolution.")

        clean_reason = str(reason).strip() if reason and str(reason).strip() else "Keep existing mapping and reject competing candidate"
        if len(clean_reason) > 500:
            clean_reason = clean_reason[:500]

        my_db = self._get_db()
        if hasattr(my_db, 'cursor') and callable(getattr(my_db, 'cursor')):
            conn = my_db
        else:
            conn = getattr(my_db, 'connection', getattr(my_db, 'conn', None))
        cursor = conn.cursor()

        try:
            # 1. Verify local name record exists
            name_rec = self._repo.get_name_record(val_name_rec_id, cursor=cursor)
            if not name_rec:
                raise LocalNameRecordNotFoundError(f"Local NameRecord with ID {val_name_rec_id} was not found.")

            # 2. Check existing active rejection (Idempotency)
            active_rej = self._repo.get_active_rejection(val_name_rec_id, clean_prov, clean_ext_id, cursor=cursor)
            if active_rej:
                # Mapping already rejected; return existing state idempotently without duplicate writes
                ext_map = self._repo.get_external_id_mapping(clean_prov, clean_ext_id, cursor=cursor)
                retained_id = ext_map['creator_entity_id'] if ext_map else None
                retained_ent = self._repo.get_entity_by_id(retained_id, cursor=cursor) if retained_id else None
                return {
                    'success': True,
                    'action': 'keep_existing_reject_competing',
                    'name_record_id': val_name_rec_id,
                    'provider': clean_prov,
                    'provider_creator_id': clean_ext_id,
                    'retained_entity_id': retained_id,
                    'retained_entity_name': retained_ent['display_name'] if retained_ent else None,
                    'rejection_id': active_rej['rejection_id'],
                    'audit_id': None,
                    'is_idempotent': True,
                    'message': "Candidate pairing is already rejected; existing mapping retained."
                }

            # 3. Verify external ID mapping exists and conflicts with this record
            ext_map = self._repo.get_external_id_mapping(clean_prov, clean_ext_id, cursor=cursor)
            if not ext_map:
                raise StaleConflictStateError("The provider external mapping is no longer present in the database.")

            retained_entity_id = ext_map['creator_entity_id']
            retained_entity = self._repo.get_entity_by_id(retained_entity_id, cursor=cursor)
            retained_entity_name = retained_entity['display_name'] if retained_entity else "Unknown Entity"

            if name_rec.get('creator_entity_id') and name_rec['creator_entity_id'] == retained_entity_id:
                raise StaleConflictStateError("This local credit is already linked to the provider-mapped entity; no conflict exists.")

            # 4. Insert or reactivate rejection
            any_rej = self._repo.get_any_rejection(val_name_rec_id, clean_prov, clean_ext_id, cursor=cursor)
            if any_rej:
                self._repo.reactivate_rejection(
                    any_rej['rejection_id'],
                    reason=clean_reason,
                    actor=actor,
                    provider_display_name=retained_entity_name,
                    cursor=cursor
                )
                rejection_id = any_rej['rejection_id']
            else:
                rejection_id = self._repo.insert_rejection(
                    name_record_id=val_name_rec_id,
                    provider=clean_prov,
                    external_id=clean_ext_id,
                    provider_display_name=retained_entity_name,
                    reason=clean_reason,
                    actor=actor,
                    cursor=cursor
                )

            # 5. Insert one immutable audit event
            before_state = {
                'conflict_type': 'EXTERNAL_ID_COLLISION',
                'retained_entity_id': retained_entity_id,
                'retained_entity_name': retained_entity_name,
                'rejected_name_record_id': val_name_rec_id,
                'rejected_raw_name': name_rec['raw_name'],
                'status': 'conflicted'
            }
            after_state = {
                'action_taken': 'keep_existing_reject_competing',
                'retained_entity_id': retained_entity_id,
                'retained_entity_name': retained_entity_name,
                'rejection_id': rejection_id,
                'status': 'rejected'
            }

            audit_id = self._repo.insert_audit_entry(
                action='RESOLVE_CONFLICT_REJECT_COMPETING',
                name_record_id=val_name_rec_id,
                creator_entity_id=name_rec.get('creator_entity_id'),
                provider=clean_prov,
                external_id=clean_ext_id,
                provider_display_name=retained_entity_name,
                actor=actor,
                reason=clean_reason,
                before_state=before_state,
                after_state=after_state,
                cursor=cursor
            )

            conn.commit()

            return {
                'success': True,
                'action': 'keep_existing_reject_competing',
                'name_record_id': val_name_rec_id,
                'provider': clean_prov,
                'provider_creator_id': clean_ext_id,
                'retained_entity_id': retained_entity_id,
                'retained_entity_name': retained_entity_name,
                'rejection_id': rejection_id,
                'audit_id': audit_id,
                'is_idempotent': False,
                'message': f"Retained mapping for '{retained_entity_name}' (Entity #{retained_entity_id}); candidate pairing for '{name_rec['raw_name']}' rejected."
            }
        except Exception:
            conn.rollback()
            raise

    def resolve_conflict_transfer_mapping(
        self,
        name_record_id: Any,
        provider: Optional[str] = None,
        provider_creator_id: Optional[Any] = None,
        reason: Optional[str] = None,
        actor: str = "user"
    ) -> Dict[str, Any]:
        """
        Explicitly transfer the provider external-ID mapping from its current local
        entity to the competing local creator entity (Phase C4.16 Outcome 2).

        Inside a single atomic transaction:
        1. Validates inputs.
        2. Re-reads and locks/validates current conflict state and external ID mapping.
        3. Derives source_entity_id and destination_entity_id on the server.
        4. Safely supersedes any active rejection for this exact pairing.
        5. Reassigns the external ID mapping to destination_entity_id.
        6. Links the competing NameRecord to destination_entity_id.
        7. Updates ext_creator_credits.CreatorEntityID for NameRecordID.
        8. Inserts one composite TRANSFER_PROVIDER_MAPPING audit entry.
        9. Commits the transaction and returns structured result.
        """
        val_name_rec_id, clean_prov, clean_ext_id = self._validate_inputs(
            name_record_id, provider, provider_creator_id
        )

        if not clean_prov:
            raise InvalidConflictInputError("Provider namespace is required for transfer.")
        if not clean_ext_id:
            raise InvalidConflictInputError("Provider creator ID is required for transfer.")

        clean_reason = str(reason).strip() if reason and str(reason).strip() else "Explicit transfer of provider mapping to competing local entity"
        if len(clean_reason) > 500:
            clean_reason = clean_reason[:500]

        my_db = self._get_db()
        if hasattr(my_db, 'cursor') and callable(getattr(my_db, 'cursor')):
            conn = my_db
        else:
            conn = getattr(my_db, 'connection', getattr(my_db, 'conn', None))
        cursor = conn.cursor()

        try:
            # 1. Verify local name record exists
            name_rec = self._repo.get_name_record(val_name_rec_id, cursor=cursor)
            if not name_rec:
                raise LocalNameRecordNotFoundError(f"Local NameRecord with ID {val_name_rec_id} was not found.")

            # 2. Verify external ID mapping exists
            ext_map = self._repo.get_external_id_mapping(clean_prov, clean_ext_id, cursor=cursor)
            if not ext_map:
                raise StaleConflictStateError("The provider external mapping is no longer present in the database.")

            source_entity_id = ext_map['creator_entity_id']
            source_entity = self._repo.get_entity_by_id(source_entity_id, cursor=cursor)
            if not source_entity:
                raise TransferTargetUnavailableError(
                    f"Transfer target is unavailable: current provider-ID owner CreatorEntity #{source_entity_id} does not exist."
                )
            source_entity_name = source_entity['display_name']

            # 3. Determine and validate pre-existing destination entity
            dest_entity_id = name_rec.get('creator_entity_id')
            if not dest_entity_id:
                raise TransferTargetUnavailableError(
                    f"Transfer target is unavailable: competing local NameRecord #{val_name_rec_id} ('{name_rec.get('raw_name', '')}') is unlinked. "
                    f"Transfer requires an existing local creator entity."
                )

            dest_entity = self._repo.get_entity_by_id(dest_entity_id, cursor=cursor)
            if not dest_entity:
                raise TransferTargetUnavailableError(
                    f"Transfer target is unavailable: destination CreatorEntity #{dest_entity_id} for NameRecord #{val_name_rec_id} does not exist."
                )
            dest_entity_name = dest_entity['display_name']

            # If the mapping is already transferred to this record's entity, check idempotency / collision
            if dest_entity_id == source_entity_id:
                raise StaleConflictStateError("This local credit is already linked to the provider-mapped entity; no conflict exists to transfer.")

            # Check if destination entity already has a conflicting mapping for the same provider
            dest_exts = self._repo.get_external_ids_for_entity(dest_entity_id, cursor=cursor)
            for ext in dest_exts:
                if ext['provider'] == clean_prov and str(ext['external_id']) != clean_ext_id:
                    raise StaleConflictStateError(
                        f"Destination entity #{dest_entity_id} ('{dest_entity_name}') already has a mapping for {clean_prov}:{ext['external_id']}."
                    )

            # 4. Supersede active rejection if one exists
            active_rej = self._repo.get_active_rejection(val_name_rec_id, clean_prov, clean_ext_id, cursor=cursor)
            superseded_rej_id = None
            if active_rej:
                superseded_rej_id = active_rej['rejection_id']
                self._repo.supersede_rejection(
                    superseded_rej_id,
                    actor=actor,
                    reason="Superseded by explicit conflict transfer",
                    cursor=cursor
                )

            # 5. Atomically reassign external ID mapping
            self._repo.reassign_external_id(clean_prov, clean_ext_id, dest_entity_id, cursor=cursor)

            # 6. Link name record and update credit cache
            self._repo.link_name_record(val_name_rec_id, dest_entity_id, resolution_source='explicit_user', cursor=cursor)
            self._repo.update_credits_entity(val_name_rec_id, dest_entity_id, cursor=cursor)

            # 7. Insert composite audit event
            before_state = {
                'source_entity_id': source_entity_id,
                'source_entity_name': source_entity_name,
                'destination_entity_id': dest_entity_id,
                'destination_entity_name': dest_entity_name,
                'name_record': {
                    'name_record_id': val_name_rec_id,
                    'raw_name': name_rec['raw_name'],
                    'creator_entity_id': name_rec.get('creator_entity_id'),
                    'resolution_source': name_rec.get('resolution_source')
                },
                'provider': clean_prov,
                'external_id': clean_ext_id,
                'superseded_rejection_id': superseded_rej_id,
                'status': 'conflicted'
            }
            after_state = {
                'source_entity_id': source_entity_id,
                'source_entity_name': source_entity_name,
                'destination_entity_id': dest_entity_id,
                'destination_entity_name': dest_entity_name,
                'name_record': {
                    'name_record_id': val_name_rec_id,
                    'raw_name': name_rec['raw_name'],
                    'creator_entity_id': dest_entity_id,
                    'resolution_source': 'explicit_user'
                },
                'provider': clean_prov,
                'external_id': clean_ext_id,
                'superseded_rejection_id': superseded_rej_id,
                'status': 'transferred'
            }

            audit_id = self._repo.insert_audit_entry(
                action='TRANSFER_PROVIDER_MAPPING',
                name_record_id=val_name_rec_id,
                creator_entity_id=dest_entity_id,
                provider=clean_prov,
                external_id=clean_ext_id,
                provider_display_name=source_entity_name,
                actor=actor,
                reason=clean_reason,
                confirmation_source='explicit_user',
                before_state=before_state,
                after_state=after_state,
                cursor=cursor
            )

            conn.commit()

            return {
                'success': True,
                'action': 'transfer_mapping',
                'name_record_id': val_name_rec_id,
                'provider': clean_prov,
                'provider_creator_id': clean_ext_id,
                'source_entity_id': source_entity_id,
                'source_entity_name': source_entity_name,
                'destination_entity_id': dest_entity_id,
                'destination_entity_name': dest_entity_name,
                'superseded_rejection_id': superseded_rej_id,
                'audit_id': audit_id,
                'message': f"Provider mapping for {clean_prov.upper()} ID {clean_ext_id} transferred from '{source_entity_name}' (Entity #{source_entity_id}) to '{dest_entity_name}' (Entity #{dest_entity_id})."
            }
        except Exception:
            conn.rollback()
            raise

    def reverse_conflict_transfer_mapping(
        self,
        name_record_id: Any,
        provider: Optional[str] = None,
        provider_creator_id: Optional[Any] = None,
        reason: Optional[str] = None,
        actor: str = "user"
    ) -> Dict[str, Any]:
        """
        Explicitly reverse a previous provider mapping transfer (Undo transfer).

        Inside a single atomic transaction:
        1. Validates inputs.
        2. Retrieves the latest TRANSFER_PROVIDER_MAPPING audit entry.
        3. Validates that current state exactly matches expected post-transfer state.
        4. Validates that the transfer has not already been reversed.
        5. Atomically reassigns the external ID mapping back to source_entity_id.
        6. Restores the competing NameRecord to its pre-transfer CreatorEntityID / ResolutionSource.
        7. Restores ext_creator_credits.CreatorEntityID.
        8. Restores any superseded rejection back to active status.
        9. Inserts a REVERSE_TRANSFER_PROVIDER_MAPPING audit entry.
        10. Commits the transaction and returns structured result.
        """
        val_name_rec_id, clean_prov, clean_ext_id = self._validate_inputs(
            name_record_id, provider, provider_creator_id
        )

        if not clean_prov:
            raise InvalidConflictInputError("Provider namespace is required for transfer reversal.")
        if not clean_ext_id:
            raise InvalidConflictInputError("Provider creator ID is required for transfer reversal.")

        clean_reason = str(reason).strip() if reason and str(reason).strip() else "Reversal of explicit provider mapping transfer"
        if len(clean_reason) > 500:
            clean_reason = clean_reason[:500]

        my_db = self._get_db()
        if hasattr(my_db, 'cursor') and callable(getattr(my_db, 'cursor')):
            conn = my_db
        else:
            conn = getattr(my_db, 'connection', getattr(my_db, 'conn', None))
        cursor = conn.cursor()

        try:
            # 1. Verify local name record exists
            name_rec = self._repo.get_name_record(val_name_rec_id, cursor=cursor)
            if not name_rec:
                raise LocalNameRecordNotFoundError(f"Local NameRecord with ID {val_name_rec_id} was not found.")

            # 2. Find latest transfer audit entry
            transfer_audit = self._repo.get_latest_transfer_audit(val_name_rec_id, clean_prov, clean_ext_id, cursor=cursor)
            if not transfer_audit:
                raise StaleConflictStateError("No prior transfer record found for this local credit and provider ID.")

            # 3. Check if already reversed
            cursor.execute(
                """
                SELECT COUNT(*) FROM ext_creator_resolution_audit
                WHERE Action = 'REVERSE_TRANSFER_PROVIDER_MAPPING'
                  AND NameRecordID = ?
                  AND Provider = ?
                  AND ExternalID = ?
                  AND CreatedAt >= ?
                """,
                (val_name_rec_id, clean_prov, clean_ext_id, transfer_audit['created_at'])
            )
            already_rev = cursor.fetchone()[0]
            if already_rev > 0:
                raise StaleConflictStateError("This transfer has already been reversed.")

            before_state = transfer_audit.get('before_state') or {}
            after_state = transfer_audit.get('after_state') or {}

            src_entity_id = before_state.get('source_entity_id')
            dest_entity_id = after_state.get('destination_entity_id')
            superseded_rej_id = before_state.get('superseded_rejection_id')
            prior_nr_entity_id = before_state.get('name_record', {}).get('creator_entity_id')
            prior_nr_resolution_source = before_state.get('name_record', {}).get('resolution_source', 'unresolved')

            if not src_entity_id or not dest_entity_id:
                raise StaleConflictStateError("Transfer audit record lacks required entity state for safe reversal.")

            # 4. Verify external ID mapping is currently bound to dest_entity_id
            ext_map = self._repo.get_external_id_mapping(clean_prov, clean_ext_id, cursor=cursor)
            if not ext_map or ext_map['creator_entity_id'] != dest_entity_id:
                raise StaleConflictStateError("The provider mapping is no longer bound to the transferred entity; reversal cannot be safely applied.")

            # 5. Verify source entity still exists
            src_ent = self._repo.get_entity_by_id(src_entity_id, cursor=cursor)
            if not src_ent:
                raise StaleConflictStateError(f"Original source entity #{src_entity_id} no longer exists; reversal cannot be safely applied.")

            # 6. Verify name record is still linked to dest_entity_id
            if name_rec.get('creator_entity_id') != dest_entity_id:
                raise StaleConflictStateError("Local credit is no longer linked to the transferred entity; reversal cannot be safely applied.")

            # 7. Atomically reassign external ID mapping back to source_entity_id
            self._repo.reassign_external_id(clean_prov, clean_ext_id, src_entity_id, cursor=cursor)

            # 8. Restore NameRecord and credit cache
            if prior_nr_entity_id is None:
                self._repo.unlink_name_record(val_name_rec_id, cursor=cursor)
                self._repo.update_credits_entity(val_name_rec_id, None, cursor=cursor)
            else:
                self._repo.link_name_record(val_name_rec_id, prior_nr_entity_id, resolution_source=prior_nr_resolution_source, cursor=cursor)
                self._repo.update_credits_entity(val_name_rec_id, prior_nr_entity_id, cursor=cursor)

            # 9. Restore superseded rejection if one existed
            if superseded_rej_id:
                self._repo.restore_superseded_rejection(
                    superseded_rej_id,
                    actor=actor,
                    reason="Restored after transfer reversal",
                    cursor=cursor
                )

            # 10. Record reversal audit event
            rev_before_state = {
                'provider': clean_prov,
                'external_id': clean_ext_id,
                'destination_entity_id': dest_entity_id,
                'source_entity_id': src_entity_id,
                'reversed_transfer_audit_id': transfer_audit['audit_id'],
                'status': 'transferred'
            }
            rev_after_state = {
                'provider': clean_prov,
                'external_id': clean_ext_id,
                'destination_entity_id': dest_entity_id,
                'source_entity_id': src_entity_id,
                'source_entity_name': src_ent['display_name'],
                'restored_superseded_rejection_id': superseded_rej_id,
                'status': 'reversed'
            }

            rev_audit_id = self._repo.insert_audit_entry(
                action='REVERSE_TRANSFER_PROVIDER_MAPPING',
                name_record_id=val_name_rec_id,
                creator_entity_id=src_entity_id,
                provider=clean_prov,
                external_id=clean_ext_id,
                provider_display_name=src_ent['display_name'],
                actor=actor,
                reason=clean_reason,
                before_state=rev_before_state,
                after_state=rev_after_state,
                cursor=cursor
            )

            conn.commit()

            return {
                'success': True,
                'action': 'reverse_transfer',
                'name_record_id': val_name_rec_id,
                'provider': clean_prov,
                'provider_creator_id': clean_ext_id,
                'restored_entity_id': src_entity_id,
                'restored_entity_name': src_ent['display_name'],
                'restored_rejection_id': superseded_rej_id,
                'audit_id': rev_audit_id,
                'message': f"Reversed provider mapping transfer for {clean_prov.upper()} ID {clean_ext_id}; mapping restored to '{src_ent['display_name']}' (Entity #{src_entity_id})."
            }
        except Exception:
            conn.rollback()
            raise
