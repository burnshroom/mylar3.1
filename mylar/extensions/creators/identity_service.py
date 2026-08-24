"""
Creator Identity Resolution Service (Phase C4.8).

Provides high-level, transactional business logic for candidate rejections,
explicit provider identity confirmations, safe reversals, and immutable audit logging.
"""

from mylar import db, logger
from mylar.extensions.creators.identity_repository import IdentityRepository
from mylar.extensions.creators.normalizer import normalize_name, slugify


# -----------------------------------------------------------------------------
# Extension-Owned Identity Exceptions
# -----------------------------------------------------------------------------

class IdentityResolutionError(Exception):
    """Base exception for all creator identity resolution failures."""
    pass


class InvalidIdentityInputError(IdentityResolutionError):
    """Raised when an invalid argument or unsupported parameter is supplied."""
    pass


class NameRecordNotFoundError(IdentityResolutionError):
    """Raised when the specified NameRecordID does not exist."""
    pass


class ActiveCandidateRejectionError(IdentityResolutionError):
    """Raised when attempting to confirm an actively rejected candidate."""
    pass


class ProviderIDCollisionError(IdentityResolutionError):
    """Raised when a provider ID already maps to a conflicting creator entity."""
    pass


class ConflictingNameRecordLinkError(IdentityResolutionError):
    """Raised when a name record is already linked to a different creator entity."""
    pass


class UnsafeReversalError(IdentityResolutionError):
    """Raised when reversing a confirmation would damage another confirmed association."""
    pass


class TargetNotFoundError(IdentityResolutionError):
    """Raised when the targeted rejection or confirmation link is not found."""
    pass


# Allowed explicit confirmation sources
PERMITTED_CONFIRMATION_SOURCES = {
    'explicit_user',
    'verified_propagation',
    'admin_override',
    'manual_user',
}


class CreatorIdentityService:
    """
    Service enforcing creator identity governance rules, atomic transactions,
    rejection tracking, explicit confirmation, and immutable audit history.
    """

    def __init__(self, db_conn=None, repository=None):
        self._db = db_conn
        self._repo = repository or IdentityRepository(db_conn=db_conn)

    def _get_db(self):
        if self._db:
            return self._db
        return db.DBConnection()

    def _validate_common_inputs(self, name_record_id, provider, provider_creator_id):
        """Validate NameRecordID, provider namespace, and provider creator ID."""
        if not isinstance(name_record_id, int) or name_record_id <= 0:
            raise InvalidIdentityInputError(f"Invalid NameRecordID '{name_record_id}': must be a positive integer.")

        if not provider or not isinstance(provider, str) or not provider.strip():
            raise InvalidIdentityInputError("Provider namespace is required and cannot be empty.")

        if provider_creator_id is None or (isinstance(provider_creator_id, str) and not provider_creator_id.strip()):
            raise InvalidIdentityInputError("Provider creator ID is required and cannot be empty.")

        clean_provider = provider.strip().lower()
        clean_ext_id = str(provider_creator_id).strip()

        return clean_provider, clean_ext_id

    # -------------------------------------------------------------------------
    # Candidate Rejections
    # -------------------------------------------------------------------------

    def reject_candidate(self, name_record_id, provider, provider_creator_id,
                         provider_display_name=None, reason=None, actor="system"):
        """
        Actively suppress a candidate match and log an immutable audit event.
        Repeating the same active rejection is idempotent and creates no duplicate active rows.

        :return: dict representing the active rejection record
        """
        clean_provider, clean_ext_id = self._validate_common_inputs(name_record_id, provider, provider_creator_id)

        my_db = self._get_db()
        conn = getattr(my_db, 'connection', getattr(my_db, 'conn', None))
        cursor = conn.cursor()

        try:
            # 1. Verify name record exists
            name_rec = self._repo.get_name_record(name_record_id, cursor=cursor)
            if not name_rec:
                raise NameRecordNotFoundError(f"Name record with ID {name_record_id} not found.")

            # 2. Check existing active rejection
            active_rej = self._repo.get_active_rejection(name_record_id, clean_provider, clean_ext_id, cursor=cursor)
            if active_rej:
                # Idempotent: return existing active rejection without duplicate rows
                return active_rej

            # 3. Check if a previously reversed rejection exists
            any_rej = self._repo.get_any_rejection(name_record_id, clean_provider, clean_ext_id, cursor=cursor)
            if any_rej:
                # Reactivate the existing record
                self._repo.reactivate_rejection(
                    any_rej['rejection_id'],
                    reason=reason,
                    actor=actor,
                    provider_display_name=provider_display_name,
                    cursor=cursor
                )
                rejection_id = any_rej['rejection_id']
            else:
                # Insert a new rejection
                rejection_id = self._repo.insert_rejection(
                    name_record_id=name_record_id,
                    provider=clean_provider,
                    external_id=clean_ext_id,
                    provider_display_name=provider_display_name,
                    reason=reason,
                    actor=actor,
                    cursor=cursor
                )

            # 4. Record immutable audit event
            self._repo.insert_audit_entry(
                action='REJECT_CANDIDATE',
                name_record_id=name_record_id,
                creator_entity_id=name_rec.get('creator_entity_id'),
                provider=clean_provider,
                external_id=clean_ext_id,
                provider_display_name=provider_display_name,
                actor=actor,
                reason=reason,
                before_state={'status': any_rej['status']} if any_rej else None,
                after_state={'status': 'active', 'rejection_id': rejection_id},
                cursor=cursor
            )

            conn.commit()
            return self._repo.get_rejection_by_id(rejection_id, cursor=cursor)

        except Exception:
            conn.rollback()
            raise

    def reverse_candidate_rejection(self, rejection_id=None, name_record_id=None, provider=None,
                                    provider_creator_id=None, actor="system", reason=None):
        """
        Reverse an active rejection without deleting its history.
        """
        my_db = self._get_db()
        conn = getattr(my_db, 'connection', getattr(my_db, 'conn', None))
        cursor = conn.cursor()

        try:
            target_rej = None
            if rejection_id is not None:
                if not isinstance(rejection_id, int) or rejection_id <= 0:
                    raise InvalidIdentityInputError(f"Invalid rejection_id '{rejection_id}'.")
                target_rej = self._repo.get_rejection_by_id(rejection_id, cursor=cursor)
            elif name_record_id is not None and provider is not None and provider_creator_id is not None:
                clean_provider, clean_ext_id = self._validate_common_inputs(name_record_id, provider, provider_creator_id)
                target_rej = self._repo.get_active_rejection(name_record_id, clean_provider, clean_ext_id, cursor=cursor)
            else:
                raise InvalidIdentityInputError("Either rejection_id or (name_record_id, provider, provider_creator_id) must be provided.")

            if not target_rej or target_rej.get('status') != 'active':
                raise TargetNotFoundError("No active candidate rejection found matching target.")

            rej_id = target_rej['rejection_id']
            self._repo.reverse_rejection(rej_id, actor=actor, reason=reason, cursor=cursor)

            # Record immutable audit event
            self._repo.insert_audit_entry(
                action='REVERSE_REJECTION',
                name_record_id=target_rej['name_record_id'],
                provider=target_rej['provider'],
                external_id=target_rej['external_id'],
                provider_display_name=target_rej['provider_display_name'],
                actor=actor,
                reason=reason,
                before_state={'status': 'active', 'rejection_id': rej_id},
                after_state={'status': 'reversed', 'rejection_id': rej_id},
                cursor=cursor
            )

            conn.commit()
            return self._repo.get_rejection_by_id(rej_id, cursor=cursor)

        except Exception:
            conn.rollback()
            raise

    def is_candidate_rejected(self, name_record_id, provider, provider_creator_id):
        """
        Check if a candidate match is actively suppressed.
        """
        try:
            clean_provider, clean_ext_id = self._validate_common_inputs(name_record_id, provider, provider_creator_id)
            rej = self._repo.get_active_rejection(name_record_id, clean_provider, clean_ext_id)
            return bool(rej and rej.get('status') == 'active')
        except Exception:
            return False

    # -------------------------------------------------------------------------
    # Explicit Provider Identity Confirmation
    # -------------------------------------------------------------------------

    def confirm_provider_identity(self, name_record_id, provider, provider_creator_id,
                                  provider_display_name=None, actor="system",
                                  confirmation_source="explicit_user"):
        """
        Explicitly confirm a raw NameRecordID against an authoritative provider creator ID.
        Reuses an existing local entity only when the same authoritative provider ID maps to it.
        Otherwise creates a new entity explicitly for this confirmation.
        """
        clean_provider, clean_ext_id = self._validate_common_inputs(name_record_id, provider, provider_creator_id)

        if not confirmation_source or confirmation_source not in PERMITTED_CONFIRMATION_SOURCES:
            raise InvalidIdentityInputError(
                f"Confirmation source '{confirmation_source}' is not permitted. Allowed: {sorted(PERMITTED_CONFIRMATION_SOURCES)}"
            )

        my_db = self._get_db()
        conn = getattr(my_db, 'connection', getattr(my_db, 'conn', None))
        cursor = conn.cursor()

        try:
            # 1. Verify name record exists
            name_rec = self._repo.get_name_record(name_record_id, cursor=cursor)
            if not name_rec:
                raise NameRecordNotFoundError(f"Name record with ID {name_record_id} not found.")

            # 2. Refuse confirmation if candidate is actively rejected
            if self._repo.get_active_rejection(name_record_id, clean_provider, clean_ext_id, cursor=cursor):
                raise ActiveCandidateRejectionError(
                    f"Candidate {clean_provider}:{clean_ext_id} is actively rejected for name record {name_record_id}."
                )

            # 3. Check existing external ID mapping
            existing_ext_mapping = self._repo.get_external_id_mapping(clean_provider, clean_ext_id, cursor=cursor)
            before_state = {
                'name_record': dict(name_rec),
                'existing_external_mapping': existing_ext_mapping,
            }

            target_entity_id = None

            if existing_ext_mapping:
                existing_entity_id = existing_ext_mapping['creator_entity_id']
                # Check if current name record is already linked to a DIFFERENT entity
                if name_rec.get('creator_entity_id') and name_rec['creator_entity_id'] != existing_entity_id:
                    raise ConflictingNameRecordLinkError(
                        f"Name record {name_record_id} is already linked to entity {name_rec['creator_entity_id']}, "
                        f"which conflicts with provider {clean_provider}:{clean_ext_id} mapped to entity {existing_entity_id}."
                    )
                target_entity_id = existing_entity_id
            else:
                # No external ID mapping yet for this (provider, external_id)
                # Check if name record is already linked to an entity
                if name_rec.get('creator_entity_id'):
                    target_entity_id = name_rec['creator_entity_id']
                else:
                    # Create a new creator entity explicitly
                    disp_name = str(provider_display_name).strip() if provider_display_name and str(provider_display_name).strip() else name_rec['raw_name']
                    norm_name = normalize_name(disp_name)
                    base_slug = slugify(disp_name) or "creator"

                    # Ensure unique slug
                    final_slug = base_slug
                    suffix = 2
                    while self._repo.get_entity_by_slug(final_slug, cursor=cursor) is not None:
                        final_slug = f"{base_slug}-{suffix}"
                        suffix += 1

                    target_entity_id = self._repo.create_entity(
                        display_name=disp_name,
                        normalized_name=norm_name,
                        entity_slug=final_slug,
                        cursor=cursor
                    )

                # Insert the new provider external ID mapping
                self._repo.insert_external_id(
                    entity_id=target_entity_id,
                    provider=clean_provider,
                    external_id=clean_ext_id,
                    confidence=1.0,
                    cursor=cursor
                )

            # 4. Link name record to confirmed entity
            self._repo.link_name_record(
                name_record_id=name_record_id,
                entity_id=target_entity_id,
                resolution_source=confirmation_source,
                cursor=cursor
            )

            # 5. Update cached CreatorEntityID on credits
            self._repo.update_credits_entity(name_record_id, target_entity_id, cursor=cursor)

            # 6. Record immutable audit event
            after_state = {
                'name_record_id': name_record_id,
                'creator_entity_id': target_entity_id,
                'provider': clean_provider,
                'external_id': clean_ext_id,
                'resolution_source': confirmation_source,
            }

            audit_id = self._repo.insert_audit_entry(
                action='CONFIRM_PROVIDER_IDENTITY',
                name_record_id=name_record_id,
                creator_entity_id=target_entity_id,
                provider=clean_provider,
                external_id=clean_ext_id,
                provider_display_name=provider_display_name,
                actor=actor,
                confirmation_source=confirmation_source,
                before_state=before_state,
                after_state=after_state,
                cursor=cursor
            )

            conn.commit()

            return {
                'success': True,
                'name_record_id': name_record_id,
                'creator_entity_id': target_entity_id,
                'provider': clean_provider,
                'external_id': clean_ext_id,
                'confirmation_source': confirmation_source,
                'audit_id': audit_id,
            }

        except Exception:
            conn.rollback()
            raise

    # -------------------------------------------------------------------------
    # Reversible Provider Confirmation
    # -------------------------------------------------------------------------

    def reverse_provider_confirmation(self, name_record_id, provider, provider_creator_id,
                                     actor="system", reason=None):
        """
        Conservatively reverse an explicit confirmation.
        Unlinks NameRecordID, restores UNLINKED state, and cleans up orphaned entity/mapping
        only if no other records reference them.
        """
        clean_provider, clean_ext_id = self._validate_common_inputs(name_record_id, provider, provider_creator_id)

        my_db = self._get_db()
        conn = getattr(my_db, 'connection', getattr(my_db, 'conn', None))
        cursor = conn.cursor()

        try:
            # 1. Verify name record exists and is linked
            name_rec = self._repo.get_name_record(name_record_id, cursor=cursor)
            if not name_rec:
                raise NameRecordNotFoundError(f"Name record with ID {name_record_id} not found.")

            entity_id = name_rec.get('creator_entity_id')
            if not entity_id:
                raise TargetNotFoundError(f"Name record {name_record_id} is not currently linked to any entity.")

            # 2. Verify provider mapping
            ext_mapping = self._repo.get_external_id_mapping(clean_provider, clean_ext_id, cursor=cursor)
            if not ext_mapping or ext_mapping.get('creator_entity_id') != entity_id:
                raise TargetNotFoundError(
                    f"Provider mapping {clean_provider}:{clean_ext_id} does not map to entity {entity_id}."
                )

            before_state = {
                'name_record': dict(name_rec),
                'creator_entity_id': entity_id,
                'external_mapping': ext_mapping,
            }

            # 3. Unlink the name record
            self._repo.unlink_name_record(name_record_id, cursor=cursor)
            self._repo.update_credits_entity(name_record_id, None, cursor=cursor)

            # 4. Check if entity or external ID should be cleaned up (only if orphaned)
            other_name_count = self._repo.count_name_records_referencing_entity(
                entity_id, exclude_name_record_id=name_record_id, cursor=cursor
            )
            other_cred_count = self._repo.count_credits_referencing_entity(
                entity_id, exclude_name_record_id=name_record_id, cursor=cursor
            )
            alias_count = self._repo.count_aliases_referencing_entity(entity_id, cursor=cursor)
            all_ext_ids = self._repo.get_external_ids_for_entity(entity_id, cursor=cursor)

            cleaned_up_entity = False
            cleaned_up_ext_id = False

            if other_name_count == 0 and other_cred_count == 0 and alias_count == 0:
                # If no other records reference this entity, delete the mapping and entity
                self._repo.delete_external_id(clean_provider, clean_ext_id, cursor=cursor)
                cleaned_up_ext_id = True

                # If no remaining external IDs, delete entity
                remaining_ext = self._repo.get_external_ids_for_entity(entity_id, cursor=cursor)
                if not remaining_ext:
                    self._repo.delete_entity(entity_id, cursor=cursor)
                    cleaned_up_entity = True

            # 5. Record immutable audit event
            after_state = {
                'name_record_id': name_record_id,
                'creator_entity_id': None,
                'resolution_source': 'unresolved',
                'cleaned_up_entity': cleaned_up_entity,
                'cleaned_up_external_id': cleaned_up_ext_id,
            }

            audit_id = self._repo.insert_audit_entry(
                action='REVERSE_CONFIRMATION',
                name_record_id=name_record_id,
                creator_entity_id=entity_id,
                provider=clean_provider,
                external_id=clean_ext_id,
                actor=actor,
                reason=reason,
                before_state=before_state,
                after_state=after_state,
                cursor=cursor
            )

            conn.commit()

            return {
                'success': True,
                'name_record_id': name_record_id,
                'previous_creator_entity_id': entity_id,
                'provider': clean_provider,
                'external_id': clean_ext_id,
                'cleaned_up_entity': cleaned_up_entity,
                'cleaned_up_external_id': cleaned_up_ext_id,
                'audit_id': audit_id,
            }

        except Exception:
            conn.rollback()
            raise

    # -------------------------------------------------------------------------
    # History & Audit Queries
    # -------------------------------------------------------------------------

    def get_resolution_history(self, name_record_id=None, creator_entity_id=None, limit=100):
        """
        Retrieve chronological resolution audit events.
        """
        return self._repo.get_audit_history(
            name_record_id=name_record_id,
            creator_entity_id=creator_entity_id,
            limit=limit
        )
