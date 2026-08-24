"""
Creator Identity Resolution Data Access Layer (Phase C4.8).

Provides low-level transactional SQLite operations for candidate rejections,
entity mapping, external provider IDs, and immutable resolution audit logs.
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from mylar import db, logger


class IdentityRepository:
    """
    Data repository for creator identity resolution and audit tables.
    """

    def __init__(self, db_conn=None):
        self._db = db_conn

    def _get_db(self):
        if self._db:
            return self._db
        return db.DBConnection()

    def _get_cursor(self, cursor=None):
        """
        Get an active SQLite cursor. Returns (cursor, should_close_connection).
        """
        if cursor is not None:
            return cursor, False
        my_db = self._get_db()
        if hasattr(my_db, 'cursor') and callable(getattr(my_db, 'cursor')):
            return my_db.cursor(), False
        conn = getattr(my_db, 'connection', getattr(my_db, 'conn', None))
        if conn and hasattr(conn, 'cursor'):
            return conn.cursor(), False
        raise RuntimeError("Unable to obtain database cursor from DBConnection.")

    # -------------------------------------------------------------------------
    # Candidate Rejections
    # -------------------------------------------------------------------------

    def get_active_rejection(self, name_record_id, provider, external_id, cursor=None):
        cur, _ = self._get_cursor(cursor)
        cur.execute(
            """
            SELECT RejectionID, NameRecordID, Provider, ExternalID, ProviderDisplayName,
                   Status, RejectedBy, RejectedAt, ReversedBy, ReversedAt, Reason, ReversalReason
            FROM ext_creator_candidate_rejections
            WHERE NameRecordID = ? AND Provider = ? AND ExternalID = ? AND Status = 'active'
            """,
            (int(name_record_id), str(provider), str(external_id))
        )
        row = cur.fetchone()
        if not row:
            return None
        return self._row_to_rejection_dict(row)

    def get_rejection_by_id(self, rejection_id, cursor=None):
        cur, _ = self._get_cursor(cursor)
        cur.execute(
            """
            SELECT RejectionID, NameRecordID, Provider, ExternalID, ProviderDisplayName,
                   Status, RejectedBy, RejectedAt, ReversedBy, ReversedAt, Reason, ReversalReason
            FROM ext_creator_candidate_rejections
            WHERE RejectionID = ?
            """,
            (int(rejection_id),)
        )
        row = cur.fetchone()
        if not row:
            return None
        return self._row_to_rejection_dict(row)

    def get_any_rejection(self, name_record_id, provider, external_id, cursor=None):
        cur, _ = self._get_cursor(cursor)
        cur.execute(
            """
            SELECT RejectionID, NameRecordID, Provider, ExternalID, ProviderDisplayName,
                   Status, RejectedBy, RejectedAt, ReversedBy, ReversedAt, Reason, ReversalReason
            FROM ext_creator_candidate_rejections
            WHERE NameRecordID = ? AND Provider = ? AND ExternalID = ?
            ORDER BY RejectionID DESC LIMIT 1
            """,
            (int(name_record_id), str(provider), str(external_id))
        )
        row = cur.fetchone()
        if not row:
            return None
        return self._row_to_rejection_dict(row)

    def insert_rejection(self, name_record_id, provider, external_id, provider_display_name=None,
                         reason=None, actor="system", cursor=None):
        cur, _ = self._get_cursor(cursor)
        cur.execute(
            """
            INSERT INTO ext_creator_candidate_rejections
            (NameRecordID, Provider, ExternalID, ProviderDisplayName, Status, RejectedBy, RejectedAt, Reason)
            VALUES (?, ?, ?, ?, 'active', ?, CURRENT_TIMESTAMP, ?)
            """,
            (int(name_record_id), str(provider), str(external_id),
             str(provider_display_name) if provider_display_name else None,
             str(actor), str(reason) if reason else None)
        )
        return cur.lastrowid

    def reactivate_rejection(self, rejection_id, reason=None, actor="system", provider_display_name=None, cursor=None):
        cur, _ = self._get_cursor(cursor)
        cur.execute(
            """
            UPDATE ext_creator_candidate_rejections
            SET Status = 'active', RejectedBy = ?, RejectedAt = CURRENT_TIMESTAMP,
                Reason = ?, ProviderDisplayName = COALESCE(?, ProviderDisplayName),
                ReversedBy = NULL, ReversedAt = NULL, ReversalReason = NULL
            WHERE RejectionID = ?
            """,
            (str(actor), str(reason) if reason else None,
             str(provider_display_name) if provider_display_name else None,
             int(rejection_id))
        )
        return cur.rowcount > 0

    def reverse_rejection(self, rejection_id, actor="system", reason=None, cursor=None):
        cur, _ = self._get_cursor(cursor)
        cur.execute(
            """
            UPDATE ext_creator_candidate_rejections
            SET Status = 'reversed', ReversedBy = ?, ReversedAt = CURRENT_TIMESTAMP, ReversalReason = ?
            WHERE RejectionID = ? AND Status = 'active'
            """,
            (str(actor), str(reason) if reason else None, int(rejection_id))
        )
        return cur.rowcount > 0

    def supersede_rejection(self, rejection_id, actor="system", reason=None, cursor=None):
        cur, _ = self._get_cursor(cursor)
        cur.execute(
            """
            UPDATE ext_creator_candidate_rejections
            SET Status = 'superseded', ReversedBy = ?, ReversedAt = CURRENT_TIMESTAMP, ReversalReason = ?
            WHERE RejectionID = ? AND Status = 'active'
            """,
            (str(actor), str(reason) if reason else "Superseded by explicit transfer", int(rejection_id))
        )
        return cur.rowcount > 0

    def restore_superseded_rejection(self, rejection_id, actor="system", reason=None, cursor=None):
        cur, _ = self._get_cursor(cursor)
        cur.execute(
            """
            UPDATE ext_creator_candidate_rejections
            SET Status = 'active', ReversedBy = NULL, ReversedAt = NULL, ReversalReason = NULL,
                Reason = COALESCE(?, Reason)
            WHERE RejectionID = ? AND Status = 'superseded'
            """,
            (str(reason) if reason else None, int(rejection_id))
        )
        return cur.rowcount > 0

    def _row_to_rejection_dict(self, row):
        return {
            'rejection_id': row[0],
            'name_record_id': row[1],
            'provider': row[2],
            'external_id': row[3],
            'provider_display_name': row[4],
            'status': row[5],
            'rejected_by': row[6],
            'rejected_at': row[7],
            'reversed_by': row[8],
            'reversed_at': row[9],
            'reason': row[10],
            'reversal_reason': row[11],
        }

    # -------------------------------------------------------------------------
    # Name Records
    # -------------------------------------------------------------------------

    def get_name_record(self, name_record_id, cursor=None):
        cur, _ = self._get_cursor(cursor)
        cur.execute(
            """
            SELECT NameRecordID, RawName, NormalizedName, NameSlug, CreatorEntityID,
                   ResolutionSource, CreatedAt, UpdatedAt
            FROM ext_creator_name_records
            WHERE NameRecordID = ?
            """,
            (int(name_record_id),)
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            'name_record_id': row[0],
            'raw_name': row[1],
            'normalized_name': row[2],
            'name_slug': row[3],
            'creator_entity_id': row[4],
            'resolution_source': row[5],
            'created_at': row[6],
            'updated_at': row[7],
        }

    def get_name_records_for_entity(self, entity_id, cursor=None):
        cur, _ = self._get_cursor(cursor)
        cur.execute(
            """
            SELECT NameRecordID, RawName, NormalizedName, NameSlug, CreatorEntityID,
                   ResolutionSource, CreatedAt, UpdatedAt
            FROM ext_creator_name_records
            WHERE CreatorEntityID = ?
            ORDER BY NameRecordID ASC
            """,
            (int(entity_id),)
        )
        rows = cur.fetchall()
        return [
            {
                'name_record_id': r[0],
                'raw_name': r[1],
                'normalized_name': r[2],
                'name_slug': r[3],
                'creator_entity_id': r[4],
                'resolution_source': r[5],
                'created_at': r[6],
                'updated_at': r[7],
            }
            for r in rows
        ]

    def link_name_record(self, name_record_id, entity_id, resolution_source="explicit_user", cursor=None):
        cur, _ = self._get_cursor(cursor)
        cur.execute(
            """
            UPDATE ext_creator_name_records
            SET CreatorEntityID = ?, ResolutionSource = ?, UpdatedAt = CURRENT_TIMESTAMP
            WHERE NameRecordID = ?
            """,
            (int(entity_id), str(resolution_source), int(name_record_id))
        )
        return cur.rowcount > 0

    def unlink_name_record(self, name_record_id, cursor=None):
        cur, _ = self._get_cursor(cursor)
        cur.execute(
            """
            UPDATE ext_creator_name_records
            SET CreatorEntityID = NULL, ResolutionSource = 'unresolved', UpdatedAt = CURRENT_TIMESTAMP
            WHERE NameRecordID = ?
            """,
            (int(name_record_id),)
        )
        return cur.rowcount > 0

    def update_credits_entity(self, name_record_id, entity_id=None, cursor=None):
        cur, _ = self._get_cursor(cursor)
        cur.execute(
            """
            UPDATE ext_creator_credits
            SET CreatorEntityID = ?
            WHERE NameRecordID = ?
            """,
            (int(entity_id) if entity_id is not None else None, int(name_record_id))
        )
        return cur.rowcount

    # -------------------------------------------------------------------------
    # Entities
    # -------------------------------------------------------------------------

    def get_entity_by_id(self, entity_id, cursor=None):
        cur, _ = self._get_cursor(cursor)
        cur.execute(
            """
            SELECT CreatorEntityID, DisplayName, NormalizedName, EntitySlug, Notes, CreatedAt, UpdatedAt
            FROM ext_creator_entities
            WHERE CreatorEntityID = ?
            """,
            (int(entity_id),)
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            'creator_entity_id': row[0],
            'display_name': row[1],
            'normalized_name': row[2],
            'entity_slug': row[3],
            'notes': row[4],
            'created_at': row[5],
            'updated_at': row[6],
        }

    def get_entity_by_slug(self, slug, cursor=None):
        cur, _ = self._get_cursor(cursor)
        cur.execute(
            """
            SELECT CreatorEntityID, DisplayName, NormalizedName, EntitySlug, Notes, CreatedAt, UpdatedAt
            FROM ext_creator_entities
            WHERE EntitySlug = ?
            """,
            (str(slug),)
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            'creator_entity_id': row[0],
            'display_name': row[1],
            'normalized_name': row[2],
            'entity_slug': row[3],
            'notes': row[4],
            'created_at': row[5],
            'updated_at': row[6],
        }

    def create_entity(self, display_name, normalized_name, entity_slug, notes=None, cursor=None):
        cur, _ = self._get_cursor(cursor)
        cur.execute(
            """
            INSERT INTO ext_creator_entities
            (DisplayName, NormalizedName, EntitySlug, Notes, CreatedAt, UpdatedAt)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (str(display_name), str(normalized_name), str(entity_slug), str(notes) if notes else None)
        )
        return cur.lastrowid

    def delete_entity(self, entity_id, cursor=None):
        cur, _ = self._get_cursor(cursor)
        cur.execute(
            "DELETE FROM ext_creator_entities WHERE CreatorEntityID = ?",
            (int(entity_id),)
        )
        return cur.rowcount > 0

    def count_name_records_referencing_entity(self, entity_id, exclude_name_record_id=None, cursor=None):
        cur, _ = self._get_cursor(cursor)
        if exclude_name_record_id is not None:
            cur.execute(
                "SELECT COUNT(*) FROM ext_creator_name_records WHERE CreatorEntityID = ? AND NameRecordID != ?",
                (int(entity_id), int(exclude_name_record_id))
            )
        else:
            cur.execute(
                "SELECT COUNT(*) FROM ext_creator_name_records WHERE CreatorEntityID = ?",
                (int(entity_id),)
            )
        row = cur.fetchone()
        return row[0] if row else 0

    def count_credits_referencing_entity(self, entity_id, exclude_name_record_id=None, cursor=None):
        cur, _ = self._get_cursor(cursor)
        if exclude_name_record_id is not None:
            cur.execute(
                "SELECT COUNT(*) FROM ext_creator_credits WHERE CreatorEntityID = ? AND NameRecordID != ?",
                (int(entity_id), int(exclude_name_record_id))
            )
        else:
            cur.execute(
                "SELECT COUNT(*) FROM ext_creator_credits WHERE CreatorEntityID = ?",
                (int(entity_id),)
            )
        row = cur.fetchone()
        return row[0] if row else 0

    def count_aliases_referencing_entity(self, entity_id, cursor=None):
        cur, _ = self._get_cursor(cursor)
        cur.execute(
            "SELECT COUNT(*) FROM ext_creator_aliases WHERE CreatorEntityID = ?",
            (int(entity_id),)
        )
        row = cur.fetchone()
        return row[0] if row else 0

    # -------------------------------------------------------------------------
    # External ID Mappings
    # -------------------------------------------------------------------------

    def get_external_id_mapping(self, provider, external_id, cursor=None):
        cur, _ = self._get_cursor(cursor)
        cur.execute(
            """
            SELECT ExternalMappingID, CreatorEntityID, Provider, ExternalID, ExternalURL,
                   SourceVersion, ObservedAt, Confidence
            FROM ext_creator_external_ids
            WHERE Provider = ? AND ExternalID = ?
            """,
            (str(provider), str(external_id))
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            'external_mapping_id': row[0],
            'creator_entity_id': row[1],
            'provider': row[2],
            'external_id': row[3],
            'external_url': row[4],
            'source_version': row[5],
            'observed_at': row[6],
            'confidence': row[7],
        }

    def get_external_ids_for_entity(self, entity_id, cursor=None):
        cur, _ = self._get_cursor(cursor)
        cur.execute(
            """
            SELECT ExternalMappingID, CreatorEntityID, Provider, ExternalID, ExternalURL,
                   SourceVersion, ObservedAt, Confidence
            FROM ext_creator_external_ids
            WHERE CreatorEntityID = ?
            """,
            (int(entity_id),)
        )
        rows = cur.fetchall()
        return [
            {
                'external_mapping_id': r[0],
                'creator_entity_id': r[1],
                'provider': r[2],
                'external_id': r[3],
                'external_url': r[4],
                'source_version': r[5],
                'observed_at': r[6],
                'confidence': r[7],
            }
            for r in rows
        ]

    def insert_external_id(self, entity_id, provider, external_id, external_url=None,
                           source_version=None, confidence=1.0, cursor=None):
        cur, _ = self._get_cursor(cursor)
        cur.execute(
            """
            INSERT INTO ext_creator_external_ids
            (CreatorEntityID, Provider, ExternalID, ExternalURL, SourceVersion, ObservedAt, Confidence)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
            """,
            (int(entity_id), str(provider), str(external_id),
             str(external_url) if external_url else None,
             str(source_version) if source_version else None,
             float(confidence))
        )
        return cur.lastrowid

    def delete_external_id(self, provider, external_id, cursor=None):
        cur, _ = self._get_cursor(cursor)
        cur.execute(
            "DELETE FROM ext_creator_external_ids WHERE Provider = ? AND ExternalID = ?",
            (str(provider), str(external_id))
        )
        return cur.rowcount > 0

    def reassign_external_id(self, provider, external_id, new_entity_id, cursor=None):
        cur, _ = self._get_cursor(cursor)
        cur.execute(
            """
            UPDATE ext_creator_external_ids
            SET CreatorEntityID = ?
            WHERE Provider = ? AND ExternalID = ?
            """,
            (int(new_entity_id), str(provider), str(external_id))
        )
        return cur.rowcount > 0

    # -------------------------------------------------------------------------
    # Immutable Resolution Audit Log
    # -------------------------------------------------------------------------

    def insert_audit_entry(self, action, name_record_id=None, creator_entity_id=None,
                           provider=None, external_id=None, provider_display_name=None,
                           actor="system", reason=None, confirmation_source=None,
                           before_state=None, after_state=None, cursor=None):
        cur, _ = self._get_cursor(cursor)
        before_json = json.dumps(before_state, sort_keys=True) if before_state is not None else None
        after_json = json.dumps(after_state, sort_keys=True) if after_state is not None else None

        cur.execute(
            """
            INSERT INTO ext_creator_resolution_audit
            (Action, NameRecordID, CreatorEntityID, Provider, ExternalID, ProviderDisplayName,
             Actor, Reason, ConfirmationSource, BeforeStateJson, AfterStateJson, CreatedAt)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (str(action),
             int(name_record_id) if name_record_id is not None else None,
             int(creator_entity_id) if creator_entity_id is not None else None,
             str(provider) if provider else None,
             str(external_id) if external_id else None,
             str(provider_display_name) if provider_display_name else None,
             str(actor),
             str(reason) if reason else None,
             str(confirmation_source) if confirmation_source else None,
             before_json,
             after_json)
        )
        return cur.lastrowid

    def get_audit_history(self, name_record_id=None, creator_entity_id=None, limit=100, cursor=None):
        cur, _ = self._get_cursor(cursor)
        conditions = []
        params = []
        if name_record_id is not None:
            conditions.append("NameRecordID = ?")
            params.append(int(name_record_id))
        if creator_entity_id is not None:
            conditions.append("CreatorEntityID = ?")
            params.append(int(creator_entity_id))

        query = """
            SELECT AuditID, Action, NameRecordID, CreatorEntityID, Provider, ExternalID,
                   ProviderDisplayName, Actor, Reason, ConfirmationSource, BeforeStateJson,
                   AfterStateJson, CreatedAt
            FROM ext_creator_resolution_audit
        """
        if conditions:
            query += " WHERE " + " OR ".join(conditions)
        query += " ORDER BY AuditID DESC"
        if limit:
            query += f" LIMIT {int(limit)}"

        cur.execute(query, tuple(params))
        rows = cur.fetchall()
        history = []
        for r in rows:
            before_parsed = None
            after_parsed = None
            if r[10]:
                try:
                    before_parsed = json.loads(r[10])
                except Exception:
                    before_parsed = r[10]
            if r[11]:
                try:
                    after_parsed = json.loads(r[11])
                except Exception:
                    after_parsed = r[11]

            history.append({
                'audit_id': r[0],
                'action': r[1],
                'name_record_id': r[2],
                'creator_entity_id': r[3],
                'provider': r[4],
                'external_id': r[5],
                'provider_display_name': r[6],
                'actor': r[7],
                'reason': r[8],
                'confirmation_source': r[9],
                'before_state': before_parsed,
                'after_state': after_parsed,
                'created_at': r[12],
            })
        return history

    def get_latest_transfer_audit(self, name_record_id, provider=None, external_id=None, cursor=None):
        """
        Get the most recent TRANSFER_PROVIDER_MAPPING audit entry for a given record (and optional pairing).
        """
        cur, _ = self._get_cursor(cursor)
        query = """
            SELECT AuditID, Action, NameRecordID, CreatorEntityID, Provider, ExternalID,
                   ProviderDisplayName, Actor, Reason, ConfirmationSource, BeforeStateJson,
                   AfterStateJson, CreatedAt
            FROM ext_creator_resolution_audit
            WHERE Action = 'TRANSFER_PROVIDER_MAPPING'
              AND NameRecordID = ?
        """
        params = [int(name_record_id)]
        if provider:
            query += " AND Provider = ?"
            params.append(str(provider))
        if external_id:
            query += " AND ExternalID = ?"
            params.append(str(external_id))
        query += " ORDER BY AuditID DESC LIMIT 1"
        cur.execute(query, tuple(params))
        r = cur.fetchone()
        if not r:
            return None

        before_parsed = None
        after_parsed = None
        if r[10]:
            try:
                before_parsed = json.loads(r[10])
            except Exception:
                before_parsed = r[10]
        if r[11]:
            try:
                after_parsed = json.loads(r[11])
            except Exception:
                after_parsed = r[11]

        return {
            'audit_id': r[0],
            'action': r[1],
            'name_record_id': r[2],
            'creator_entity_id': r[3],
            'provider': r[4],
            'external_id': r[5],
            'provider_display_name': r[6],
            'actor': r[7],
            'reason': r[8],
            'confirmation_source': r[9],
            'before_state': before_parsed,
            'after_state': after_parsed,
            'created_at': r[12],
        }

    def derive_pairing_state(self, name_record_id: int, provider: Optional[str] = None, external_id: Optional[str] = None, cursor=None) -> Dict[str, Any]:
        """
        Shared canonical server-side state derivation for an exact (NameRecordID, Provider, ExternalID) tuple.
        Returns a dictionary containing the derived state, explanation, and entity metadata.
        """
        cur, _ = self._get_cursor(cursor)
        name_rec = self.get_name_record(name_record_id, cursor=cur)
        if not name_rec:
            return {
                'state': 'unresolved',
                'explanation': f"NameRecordID #{name_record_id} does not exist.",
                'local_entity_id': None,
                'local_display_name': None,
                'provider_entity_id': None,
                'provider_display_name': None,
                'is_transferred': False,
                'is_confirmed': False,
                'is_rejected': False,
                'is_conflicted': False,
                'active_rejection': None,
                'latest_transfer_audit': None,
                'latest_reverse_audit': None,
            }

        local_entity_id = name_rec.get('creator_entity_id')
        local_entity = self.get_entity_by_id(local_entity_id, cursor=cur) if local_entity_id else None
        local_display_name = local_entity.get('display_name') if local_entity else None
        raw_name = name_rec.get('raw_name') or 'Local credit'

        clean_prov = provider.strip().lower() if provider and str(provider).strip() else None
        clean_ext_id = str(external_id).strip() if external_id and str(external_id).strip() else None

        # If provider/external_id not passed, but local entity has external IDs, grab primary
        local_ext_ids = self.get_external_ids_for_entity(local_entity_id, cursor=cur) if local_entity_id else []
        if not clean_prov and not clean_ext_id and local_ext_ids:
            clean_prov = local_ext_ids[0]['provider'].strip().lower()
            clean_ext_id = str(local_ext_ids[0]['external_id']).strip()

        ext_map = None
        provider_entity_id = None
        provider_entity = None
        if clean_prov and clean_ext_id:
            ext_map = self.get_external_id_mapping(clean_prov, clean_ext_id, cursor=cur)
            if ext_map:
                provider_entity_id = ext_map.get('creator_entity_id')
                provider_entity = self.get_entity_by_id(provider_entity_id, cursor=cur)

        active_rej = None
        if clean_prov and clean_ext_id:
            active_rej = self.get_active_rejection(name_record_id, clean_prov, clean_ext_id, cursor=cur)
        else:
            cur.execute("""
                SELECT RejectionID, Provider, ExternalID, Reason, ProviderDisplayName, RejectedAt
                FROM ext_creator_candidate_rejections
                WHERE NameRecordID = ? AND Status = 'active'
                ORDER BY RejectionID DESC LIMIT 1
            """, (name_record_id,))
            r_row = cur.fetchone()
            if r_row:
                active_rej = {
                    'rejection_id': r_row[0],
                    'provider': r_row[1],
                    'external_id': r_row[2],
                    'reason': r_row[3],
                    'provider_display_name': r_row[4],
                    'created_at': r_row[5]
                }

        # Query all audit rows for this name record
        cur.execute("""
            SELECT AuditID, Action, Provider, ExternalID, ProviderDisplayName, Actor, Reason, CreatedAt
            FROM ext_creator_resolution_audit
            WHERE NameRecordID = ?
            ORDER BY AuditID DESC
        """, (name_record_id,))
        audit_rows = cur.fetchall()
        total_events = len(audit_rows)

        latest_transfer_audit = None
        latest_reverse_transfer_audit = None
        has_conflict_audit = False
        for row in audit_rows:
            r_id, r_action, r_prov, r_ext, r_name, r_actor, r_reason, r_time = row
            if r_action == 'LEGACY_EXTERNAL_ID_COLLISION' or (r_reason and ('collision' in r_reason.lower() or 'conflict' in r_reason.lower())):
                has_conflict_audit = True
            if r_action == 'TRANSFER_PROVIDER_MAPPING':
                if (not clean_prov or (r_prov and r_prov.lower() == clean_prov)) and (not clean_ext_id or (r_ext and str(r_ext) == clean_ext_id)):
                    if not latest_transfer_audit:
                        latest_transfer_audit = {'audit_id': r_id, 'action': r_action, 'provider': r_prov, 'external_id': r_ext, 'created_at': r_time}
            elif r_action == 'REVERSE_TRANSFER_PROVIDER_MAPPING':
                if (not clean_prov or (r_prov and r_prov.lower() == clean_prov)) and (not clean_ext_id or (r_ext and str(r_ext) == clean_ext_id)):
                    if not latest_reverse_transfer_audit:
                        latest_reverse_transfer_audit = {'audit_id': r_id, 'action': r_action, 'provider': r_prov, 'external_id': r_ext, 'created_at': r_time}

        has_unreversed_transfer = False
        if latest_transfer_audit:
            if not latest_reverse_transfer_audit or latest_transfer_audit['audit_id'] > latest_reverse_transfer_audit['audit_id']:
                has_unreversed_transfer = True

        # State Determination
        state = 'unresolved'
        prov_lbl = (clean_prov or 'PROVIDER').upper()
        ext_lbl = clean_ext_id or ''
        ext_str = f" [{prov_lbl} ID: {ext_lbl}]" if clean_prov and clean_ext_id else ""
        provider_disp = provider_entity.get('display_name') if provider_entity else (f"Entity #{provider_entity_id}" if provider_entity_id else None)

        if has_unreversed_transfer and local_entity_id and provider_entity_id and local_entity_id == provider_entity_id:
            state = 'transferred'
            explanation = f"Confirmed through an explicit provider-mapping transfer to '{local_display_name or 'local entity'}'{ext_str}."
        elif active_rej is not None:
            state = 'rejected'
            r_prov_str = (active_rej['provider'] or 'PROVIDER').upper()
            explanation = f"Local credit '{raw_name}' has an active rejection suppressing {r_prov_str} ID: {active_rej['external_id']}."
        elif clean_prov and clean_ext_id and local_entity_id and provider_entity_id and local_entity_id == provider_entity_id:
            state = 'confirmed'
            explanation = f"Local credit '{raw_name}' is currently confirmed as '{local_display_name or 'local entity'}'{ext_str}."
        elif clean_prov and clean_ext_id and provider_entity_id and provider_entity_id != local_entity_id:
            state = 'conflicted'
            if latest_reverse_transfer_audit and (not latest_transfer_audit or latest_reverse_transfer_audit['audit_id'] > latest_transfer_audit['audit_id']):
                explanation = f"Transfer was reversed; provider mapping [{prov_lbl} ID: {ext_lbl}] again maps to competing entity '{provider_disp}' (Entity #{provider_entity_id})."
            else:
                explanation = f"Direct resolution is blocked because provider [{prov_lbl} ID: {ext_lbl}] is currently mapped to competing entity '{provider_disp}'."
        elif has_conflict_audit:
            state = 'conflicted'
            explanation = f"Local credit '{raw_name}' has an unresolved external ID collision recorded in history."
        elif local_entity_id:
            state = 'confirmed'
            explanation = f"Local credit '{raw_name}' is currently confirmed as '{local_display_name or 'local entity'}'{ext_str}."
        elif total_events > 0:
            state = 'unresolved'
            explanation = f"Local credit '{raw_name}' is currently unlinked. {total_events} decision event{'s' if total_events != 1 else ''} on record."
        else:
            state = 'unresolved'
            explanation = f"Local credit '{raw_name}' is currently unlinked with no decision history on record."

        return {
            'state': state,
            'explanation': explanation,
            'local_entity_id': local_entity_id,
            'local_display_name': local_display_name,
            'provider_entity_id': provider_entity_id,
            'provider_display_name': provider_disp,
            'is_transferred': (state == 'transferred'),
            'is_confirmed': (state == 'confirmed'),
            'is_rejected': (state == 'rejected'),
            'is_conflicted': (state == 'conflicted'),
            'active_rejection': active_rej,
            'latest_transfer_audit': latest_transfer_audit,
            'latest_reverse_audit': latest_reverse_transfer_audit,
        }
