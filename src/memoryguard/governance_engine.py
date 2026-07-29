"""Single write interface for governed shared long-term memory.

GUI, MCP and AutoOrganizer call this engine.  SharedMemoryStore remains the
persistence layer.  The optional idempotency key is a transaction seam: repeat
calls with the same action/key return without applying the mutation twice.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .schema_v3 import (
    ConflictResolution,
    DecisionEvent,
    Provenance,
    QuarantineEntry,
    SharedMemoryRecord,
    SharedMemoryStatus,
    _now_iso,
    stable_hash,
)
from .shared_memory_store import SharedMemoryStore


class GovernanceEngine:
    """Authoritative shared-memory state transition interface."""

    def __init__(
        self,
        workspace: str | Path,
        share_group_id: str,
        *,
        store: SharedMemoryStore | None = None,
    ):
        self.store = store or SharedMemoryStore(workspace, share_group_id)
        self.group_id = share_group_id

    @staticmethod
    def _state(record: SharedMemoryRecord | None) -> dict[str, Any] | None:
        return record.to_dict() if record is not None else None

    @staticmethod
    def _state_hash(state: dict[str, Any] | None) -> str:
        if state is None:
            return ""
        return stable_hash(json.dumps(
            state, ensure_ascii=False, sort_keys=True,
        ))

    def _decision_id(self, action: str, idempotency_key: str) -> str:
        if idempotency_key:
            return stable_hash(
                "governance-idempotency", self.group_id, idempotency_key,
            )
        return stable_hash(
            "governance", self.group_id, action, _now_iso(),
        )

    @staticmethod
    def _request_fingerprint(
        action: str,
        target_ids: list[str],
        payload: dict[str, Any] | None,
    ) -> str:
        return stable_hash(
            action,
            json.dumps(target_ids, ensure_ascii=False),
            json.dumps(
                payload or {},
                ensure_ascii=False,
                sort_keys=True,
            ),
        )

    def _replay(
        self,
        action: str,
        actor: str,
        target_ids: list[str],
        idempotency_key: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if not idempotency_key:
            return None
        decision_id = self._decision_id(action, idempotency_key)
        existing = next(
            (
                item for item in self.store.list_decisions()
                if item.event_id == decision_id
            ),
            None,
        )
        if existing is None:
            return None
        fingerprint = self._request_fingerprint(
            action, target_ids, payload,
        )
        marker = "request_fingerprint="
        previous_fingerprint = ""
        if marker in existing.reason:
            previous_fingerprint = (
                existing.reason.split(marker, 1)[1].split(";", 1)[0]
            )
        current = None
        for target_id in reversed(existing.target_ids):
            current = self.store.get_record(target_id)
            if current is not None:
                break
        if previous_fingerprint != fingerprint:
            return {
                "ok": False,
                "action": action,
                "actor": actor,
                "before": self._state(current),
                "after": self._state(current),
                "decision_id": decision_id,
                "version_id": self.store.get_active_version_id(),
                "blocked_reason": "idempotency_conflict",
                "idempotency_key": idempotency_key,
                "idempotent_replay": False,
            }
        return {
            "ok": True,
            "action": action,
            "actor": actor,
            "before": None,
            "after": self._state(current),
            "decision_id": decision_id,
            "version_id": self.store.get_active_version_id(),
            "blocked_reason": "",
            "idempotency_key": idempotency_key,
            "idempotent_replay": True,
        }

    def _finish(
        self,
        *,
        action: str,
        actor: str,
        target_ids: list[str],
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        reason: str,
        idempotency_key: str = "",
        payload: dict[str, Any] | None = None,
        request_targets: list[str] | None = None,
        snapshot: bool = True,
    ) -> dict[str, Any]:
        decision_id = self._decision_id(action, idempotency_key)
        reason_text = reason
        if idempotency_key:
            fingerprint = self._request_fingerprint(
                action, request_targets or target_ids, payload,
            )
            reason_text += (
                f"; request_fingerprint={fingerprint}; "
                f"idempotency_key={idempotency_key}"
            )
        self.store.append_decision(DecisionEvent(
            event_id=decision_id,
            actor=actor,
            action=action,
            target_ids=target_ids,
            before_hash=self._state_hash(before),
            after_hash=self._state_hash(after),
            reason=reason_text,
            created_at=_now_iso(),
        ))
        version_id = self.store.get_active_version_id()
        if snapshot:
            version_id = self.store.create_version_snapshot(
                f"{action}: {','.join(target_ids)}"
            )
        return {
            "ok": True,
            "action": action,
            "actor": actor,
            "before": before,
            "after": after,
            "decision_id": decision_id,
            "version_id": version_id,
            "blocked_reason": "",
            "idempotency_key": idempotency_key,
            "idempotent_replay": False,
        }

    def _blocked(
        self,
        *,
        action: str,
        actor: str,
        record: SharedMemoryRecord | None,
        reason: str,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        state = self._state(record)
        return {
            "ok": False,
            "action": action,
            "actor": actor,
            "before": state,
            "after": state,
            "decision_id": "",
            "version_id": self.store.get_active_version_id(),
            "blocked_reason": reason,
            "idempotency_key": idempotency_key,
            "idempotent_replay": False,
        }

    @staticmethod
    def _manual_provenance(
        record: SharedMemoryRecord,
        action: str,
    ) -> tuple[list[Provenance], str]:
        now = _now_iso()
        override_id = stable_hash(
            "manual-override", action, record.memory_id, record.body, now,
        )
        provenance = list(record.provenance)
        provenance.append(Provenance(
            source_object_id=f"manual-override:{override_id}",
            locator=f"governance:{action}",
            excerpt_hash=stable_hash(record.body),
            source_revision=now,
        ))
        return provenance, now

    def auto_write(
        self,
        event: Any,
        *,
        kind_override: str = "",
        write_policy: str = "auto_accept",
        enricher_mode: str | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        """Only production entry for automatic shared-memory writes."""
        actor = f"agent:{event.agent_instance_id or 'unknown'}"
        request_targets = [event.agent_instance_id or "unknown"]
        payload = {
            "raw_content": event.raw_content,
            "metadata": event.metadata,
            "kind_override": kind_override,
            "write_policy": write_policy,
            "enricher_mode": enricher_mode or "",
        }
        replay = self._replay(
            "auto_write",
            actor,
            request_targets,
            idempotency_key,
            payload,
        )
        if replay:
            after = replay.get("after") or {}
            replay.update({
                "memory_id": after.get("memory_id", ""),
                "status": after.get("status", ""),
                "kind": after.get("kind", ""),
                "auto_actions": [],
            })
            return replay
        if idempotency_key:
            event.event_id = stable_hash(
                "auto_write_event",
                self.group_id,
                event.agent_instance_id or "",
                idempotency_key,
            )
            event.metadata = dict(event.metadata or {})
            event.metadata["idempotency_key"] = idempotency_key
        self.store.append_event(event)
        from .auto_organizer import AutoOrganizer
        organizer = AutoOrganizer(
            self.store.workspace,
            self.group_id,
            enricher_mode=enricher_mode,
            store=self.store,
            engine=self,
        )
        record, actions = organizer.organize(
            event,
            kind_override=kind_override,
            write_policy=write_policy,
        )
        event.auto_actions = actions
        self.store.update_event(event)
        result = self._finish(
            action="auto_write",
            actor=actor,
            target_ids=[request_targets[0], record.memory_id],
            request_targets=request_targets,
            before=None,
            after=self._state(record),
            reason="automatic governed memory write",
            idempotency_key=idempotency_key,
            payload=payload,
        )
        result.update({
            "memory_id": record.memory_id,
            "status": record.status.value,
            "kind": record.kind.value,
            "auto_actions": actions,
        })
        return result

    def preview_content(
        self,
        content: str,
        *,
        enricher_mode: str | None = "heuristic",
    ) -> dict[str, Any]:
        """Read-only classification/risk preview through private organizer."""
        from .auto_organizer import AutoOrganizer
        organizer = AutoOrganizer(
            self.store.workspace,
            self.group_id,
            enricher_mode=enricher_mode,
            store=self.store,
            engine=self,
        )
        kind = organizer._classify(content)
        confidence = organizer._confidence(content, kind)
        secret = organizer._detect_secret(content)
        return {
            "kind": kind.value,
            "confidence": confidence,
            "secret_pattern": secret,
        }

    def record_governance_decision(
        self,
        *,
        action: str,
        actor: str,
        target_ids: list[str],
        reason: str,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        """Record non-record-specific governance through same interface."""
        payload = {"reason": reason}
        replay = self._replay(
            action, actor, target_ids, idempotency_key, payload,
        )
        if replay:
            return replay
        return self._finish(
            action=action,
            actor=actor,
            target_ids=target_ids,
            before=None,
            after=None,
            reason=reason,
            idempotency_key=idempotency_key,
            payload=payload,
        )

    def agent_update(
        self,
        memory_id: str,
        *,
        actor: str,
        body: str | None = None,
        kind: str | None = None,
        status: str | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        payload = {"body": body, "kind": kind, "status": status}
        replay = self._replay(
            "agent_update", actor, [memory_id], idempotency_key, payload,
        )
        if replay:
            return replay
        record = self.store.get_record(memory_id)
        if record is None:
            return self._blocked(
                action="agent_update", actor=actor, record=None,
                reason="memory_not_found", idempotency_key=idempotency_key,
            )
        if record.locked:
            return self._blocked(
                action="agent_update", actor=actor, record=record,
                reason="manual_override_locked",
                idempotency_key=idempotency_key,
            )
        before = self._state(record)
        now = _now_iso()
        with self.store._tx() as conn:
            if body is not None:
                conn.execute(
                    "UPDATE records SET body=?, canonical_hash=?, updated_at=? "
                    "WHERE memory_id=?",
                    (
                        body,
                        self.store._canonical_hash(body),
                        now,
                        memory_id,
                    ),
                )
            if kind is not None:
                conn.execute(
                    "UPDATE records SET kind=?, updated_at=? WHERE memory_id=?",
                    (kind, now, memory_id),
                )
            if status is not None:
                conn.execute(
                    "UPDATE records SET status=?, updated_at=? WHERE memory_id=?",
                    (status, now, memory_id),
                )
        after = self._state(self.store.get_record(memory_id))
        return self._finish(
            action="agent_update",
            actor=actor,
            target_ids=[memory_id],
            before=before,
            after=after,
            reason="MCP agent governance update",
            idempotency_key=idempotency_key,
            payload=payload,
        )

    def agent_delete(
        self,
        memory_id: str,
        *,
        actor: str,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        replay = self._replay(
            "agent_delete", actor, [memory_id], idempotency_key, {},
        )
        if replay:
            return replay
        record = self.store.get_record(memory_id)
        if record is None:
            return self._blocked(
                action="agent_delete", actor=actor, record=None,
                reason="memory_not_found", idempotency_key=idempotency_key,
            )
        if record.locked:
            return self._blocked(
                action="agent_delete", actor=actor, record=record,
                reason="manual_override_locked",
                idempotency_key=idempotency_key,
            )
        before = self._state(record)
        self.store._update_record_field(
            memory_id, "status", SharedMemoryStatus.DELETED.value,
        )
        after = self._state(self.store.get_record(memory_id))
        return self._finish(
            action="agent_delete",
            actor=actor,
            target_ids=[memory_id],
            before=before,
            after=after,
            reason="MCP agent governance delete",
            idempotency_key=idempotency_key,
            payload={},
        )

    def _human_record_action(
        self,
        memory_id: str,
        action: str,
        *,
        body: str | None = None,
        locked: bool = True,
        status: SharedMemoryStatus | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        payload = {
            "body": body,
            "locked": locked,
            "status": status.value if status else None,
        }
        replay = self._replay(
            action, "user", [memory_id], idempotency_key, payload,
        )
        if replay:
            return replay
        record = self.store.get_record(memory_id)
        if record is None:
            return self._blocked(
                action=action, actor="user", record=None,
                reason="memory_not_found", idempotency_key=idempotency_key,
            )
        before = self._state(record)
        provenance, now = self._manual_provenance(record, action)
        with self.store._tx() as conn:
            if body is not None:
                conn.execute(
                    "UPDATE records SET body=?, canonical_hash=? "
                    "WHERE memory_id=?",
                    (
                        body,
                        self.store._canonical_hash(body),
                        memory_id,
                    ),
                )
            if status is not None:
                conn.execute(
                    "UPDATE records SET status=? WHERE memory_id=?",
                    (status.value, memory_id),
                )
            conn.execute(
                "UPDATE records SET locked=?, provenance=?, updated_at=? "
                "WHERE memory_id=?",
                (
                    1 if locked else 0,
                    json.dumps(
                        [item.to_dict() for item in provenance],
                        ensure_ascii=False,
                    ),
                    now,
                    memory_id,
                ),
            )
        after = self._state(self.store.get_record(memory_id))
        return self._finish(
            action=action,
            actor="user",
            target_ids=[memory_id],
            before=before,
            after=after,
            reason=f"manual override: {action}",
            idempotency_key=idempotency_key,
            payload=payload,
        )

    def human_edit(
        self,
        memory_id: str,
        body: str,
        *,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        return self._human_record_action(
            memory_id, "edit", body=body, idempotency_key=idempotency_key,
        )

    def human_lock(
        self,
        memory_id: str,
        *,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        return self._human_record_action(
            memory_id, "lock", idempotency_key=idempotency_key,
        )

    def human_unlock(
        self,
        memory_id: str,
        *,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        return self._human_record_action(
            memory_id, "unlock", locked=False,
            idempotency_key=idempotency_key,
        )

    def human_delete(
        self,
        memory_id: str,
        *,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        return self._human_record_action(
            memory_id, "delete",
            status=SharedMemoryStatus.DELETED,
            idempotency_key=idempotency_key,
        )

    def human_restore(
        self,
        memory_id: str,
        *,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        replay = self._replay(
            "restore", "user", [memory_id], idempotency_key, {},
        )
        if replay:
            return replay
        record = self.store.get_record(memory_id)
        if record is None:
            return self._blocked(
                action="restore", actor="user", record=None,
                reason="memory_not_found", idempotency_key=idempotency_key,
            )
        before = self._state(record)
        all_records = self.store.list_records()
        lineage = {memory_id}
        changed = True
        while changed:
            changed = False
            for item in all_records:
                if item.memory_id in lineage:
                    continue
                if any(parent in lineage for parent in item.supersedes):
                    lineage.add(item.memory_id)
                    changed = True
        descendants = sorted(
            item.memory_id
            for item in all_records
            if (
                item.memory_id in lineage
                and item.memory_id != memory_id
                and item.status == SharedMemoryStatus.ACTIVE
            )
        )
        provenance, now = self._manual_provenance(record, "restore")
        with self.store._tx() as conn:
            if descendants:
                placeholders = ",".join("?" for _ in descendants)
                conn.execute(
                    f"UPDATE records SET status=?, updated_at=? "
                    f"WHERE memory_id IN ({placeholders})",
                    (
                        SharedMemoryStatus.SHADOWED.value,
                        now,
                        *descendants,
                    ),
                )
            conn.execute(
                "UPDATE records SET status=?, locked=1, provenance=?, "
                "updated_at=? WHERE memory_id=?",
                (
                    SharedMemoryStatus.ACTIVE.value,
                    json.dumps(
                        [item.to_dict() for item in provenance],
                        ensure_ascii=False,
                    ),
                    now,
                    memory_id,
                ),
            )
        after = self._state(self.store.get_record(memory_id))
        return self._finish(
            action="restore",
            actor="user",
            target_ids=[memory_id],
            before=before,
            after=after,
            reason="manual override: restore old; shadow current descendants",
            idempotency_key=idempotency_key,
            payload={},
        )

    def quarantine(
        self,
        memory_id: str,
        *,
        reason: str,
        pattern: str,
        original_content: str,
        actor: str,
        manual_override: bool,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        action = "manual_quarantine" if manual_override else "auto_quarantine"
        payload = {
            "reason": reason,
            "pattern": pattern,
            "original_content_hash": stable_hash(original_content),
            "manual_override": manual_override,
        }
        replay = self._replay(
            action, actor, [memory_id], idempotency_key, payload,
        )
        if replay:
            return replay
        record = self.store.get_record(memory_id)
        if record is None:
            return self._blocked(
                action=action, actor=actor, record=None,
                reason="memory_not_found", idempotency_key=idempotency_key,
            )
        before = self._state(record)
        now = _now_iso()
        provenance = list(record.provenance)
        if manual_override:
            provenance, now = self._manual_provenance(record, "quarantine")
        entry = QuarantineEntry(
            quarantine_id=stable_hash(
                "quar", memory_id, idempotency_key or now,
            ),
            memory_id=memory_id,
            reason=reason,
            detected_pattern=pattern,
            original_content=original_content,
            quarantined_at=now,
        )
        with self.store._tx() as conn:
            conn.execute(
                "UPDATE records SET status=?, locked=?, provenance=?, "
                "updated_at=? WHERE memory_id=?",
                (
                    SharedMemoryStatus.QUARANTINED.value,
                    1 if manual_override else int(record.locked),
                    json.dumps(
                        [item.to_dict() for item in provenance],
                        ensure_ascii=False,
                    ),
                    now,
                    memory_id,
                ),
            )
            self.store._insert_quarantine(conn, entry)
        after = self._state(self.store.get_record(memory_id))
        return self._finish(
            action=action,
            actor=actor,
            target_ids=[memory_id],
            before=before,
            after=after,
            reason=reason,
            idempotency_key=idempotency_key,
            payload=payload,
        )

    def resolve_conflict(
        self,
        group_id: str,
        keep_memory_id: str,
        *,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        replay = self._replay(
            "resolve_conflict",
            "user",
            [group_id, keep_memory_id],
            idempotency_key,
            {"keep_memory_id": keep_memory_id},
        )
        if replay:
            return replay
        group = next(
            (
                item for item in self.store.list_conflicts()
                if item.group_id == group_id
            ),
            None,
        )
        if group is None or keep_memory_id not in group.member_ids:
            return self._blocked(
                action="resolve_conflict", actor="user", record=None,
                reason="conflict_or_keep_memory_not_found",
                idempotency_key=idempotency_key,
            )
        keep = self.store.get_record(keep_memory_id)
        before = self._state(keep)
        if keep is None:
            return self._blocked(
                action="resolve_conflict", actor="user", record=None,
                reason="keep_memory_not_found",
                idempotency_key=idempotency_key,
            )
        provenance, now = self._manual_provenance(
            keep, "resolve_conflict",
        )
        rejected = [
            item for item in group.member_ids if item != keep_memory_id
        ]
        with self.store._tx() as conn:
            for memory_id in rejected:
                other = self.store.get_record(memory_id)
                if other is None:
                    continue
                other_provenance, _ = self._manual_provenance(
                    other, "resolve_conflict_delete",
                )
                conn.execute(
                    "UPDATE records SET status=?, locked=1, provenance=?, "
                    "updated_at=? WHERE memory_id=?",
                    (
                        SharedMemoryStatus.DELETED.value,
                        json.dumps(
                            [item.to_dict() for item in other_provenance],
                            ensure_ascii=False,
                        ),
                        now,
                        memory_id,
                    ),
                )
            conn.execute(
                "UPDATE records SET status=?, locked=1, provenance=?, "
                "conflict_group_id='', updated_at=? WHERE memory_id=?",
                (
                    SharedMemoryStatus.ACTIVE.value,
                    json.dumps(
                        [item.to_dict() for item in provenance],
                        ensure_ascii=False,
                    ),
                    now,
                    keep_memory_id,
                ),
            )
            conn.execute(
                "UPDATE conflicts SET status=?, resolution=? WHERE group_id=?",
                (
                    ConflictResolution.RESOLVED.value,
                    f"keep:{keep_memory_id}",
                    group_id,
                ),
            )
        after = self._state(self.store.get_record(keep_memory_id))
        return self._finish(
            action="resolve_conflict",
            actor="user",
            target_ids=[group_id, keep_memory_id],
            before=before,
            after=after,
            reason=f"manual override: keep {keep_memory_id}",
            idempotency_key=idempotency_key,
            payload={"keep_memory_id": keep_memory_id},
        )

    def resolve_quarantine(
        self,
        quarantine_id: str,
        *,
        resolution: str,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        if resolution not in {"release", "delete"}:
            return self._blocked(
                action="resolve_quarantine",
                actor="user",
                record=None,
                reason="invalid_quarantine_resolution",
                idempotency_key=idempotency_key,
            )
        action = (
            "release_quarantine"
            if resolution == "release"
            else "delete_quarantine"
        )
        entry = next(
            (
                item for item in self.store.list_quarantine()
                if item.quarantine_id == quarantine_id and not item.released
            ),
            None,
        )
        if entry is None:
            return self._blocked(
                action=action, actor="user", record=None,
                reason="quarantine_not_found_or_closed",
                idempotency_key=idempotency_key,
            )
        replay = self._replay(
            action,
            "user",
            [quarantine_id, entry.memory_id],
            idempotency_key,
            {"resolution": resolution},
        )
        if replay:
            return replay
        record = self.store.get_record(entry.memory_id)
        if record is None:
            return self._blocked(
                action=action, actor="user", record=None,
                reason="memory_not_found", idempotency_key=idempotency_key,
            )
        before = self._state(record)
        provenance, now = self._manual_provenance(record, action)
        new_status = (
            SharedMemoryStatus.ACTIVE
            if resolution == "release"
            else SharedMemoryStatus.DELETED
        )
        with self.store._tx() as conn:
            conn.execute(
                "UPDATE records SET status=?, locked=1, provenance=?, "
                "updated_at=? WHERE memory_id=?",
                (
                    new_status.value,
                    json.dumps(
                        [item.to_dict() for item in provenance],
                        ensure_ascii=False,
                    ),
                    now,
                    entry.memory_id,
                ),
            )
            conn.execute(
                "UPDATE quarantine SET released=1 WHERE quarantine_id=?",
                (quarantine_id,),
            )
        after = self._state(self.store.get_record(entry.memory_id))
        return self._finish(
            action=action,
            actor="user",
            target_ids=[quarantine_id, entry.memory_id],
            before=before,
            after=after,
            reason=f"manual quarantine resolution: {resolution}",
            idempotency_key=idempotency_key,
            payload={"resolution": resolution},
        )

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return set(re.findall(
            r"[a-zA-Z_][a-zA-Z0-9_]*|[\u4e00-\u9fff]",
            (text or "").casefold(),
        ))

    def evaluate_auto_write(
        self,
        content: str,
        *,
        threshold: float,
    ) -> dict[str, Any]:
        """Evaluate durable manual overrides before automatic mutation."""
        incoming = self._tokens(content)
        if not incoming:
            return self._blocked(
                action="evaluate_auto_write", actor="auto", record=None,
                reason="empty_content",
            )
        matches: list[tuple[float, SharedMemoryRecord]] = []
        for record in self.store.list_records():
            if not record.locked:
                continue
            existing = self._tokens(record.body)
            if not existing:
                continue
            score = len(incoming & existing) / len(incoming | existing)
            if score >= threshold:
                matches.append((score, record))
        if not matches:
            return {
                "ok": True,
                "action": "evaluate_auto_write",
                "actor": "auto",
                "before": None,
                "after": None,
                "decision_id": "",
                "version_id": self.store.get_active_version_id(),
                "blocked_reason": "",
                "idempotency_key": "",
                "idempotent_replay": False,
                "policy": "allow",
            }
        matches.sort(key=lambda item: (-item[0], item[1].memory_id))
        protected = matches[0][1]
        if protected.status == SharedMemoryStatus.ACTIVE:
            same_content = (
                " ".join(content.casefold().split())
                == " ".join(protected.body.casefold().split())
            )
            policy = (
                "suppress" if same_content else "low_confidence_candidate"
            )
            blocked_reason = (
                "manual_override_locked_identical"
                if same_content
                else "manual_override_locked"
            )
        else:
            policy = "suppress"
            blocked_reason = f"manual_override_{protected.status.value}"
        result = self._blocked(
            action="evaluate_auto_write",
            actor="auto",
            record=protected,
            reason=blocked_reason,
        )
        result["policy"] = policy
        result["protected_id"] = protected.memory_id
        result["protected_status"] = protected.status.value
        return result

    def record_auto_policy(
        self,
        *,
        protected_id: str,
        candidate_id: str = "",
        reason: str,
    ) -> dict[str, Any]:
        protected = self.store.get_record(protected_id)
        candidate = (
            self.store.get_record(candidate_id) if candidate_id else protected
        )
        return self._finish(
            action="manual_override_preserved",
            actor="auto",
            target_ids=[
                item for item in (protected_id, candidate_id) if item
            ],
            before=self._state(protected),
            after=self._state(candidate),
            reason=reason,
            snapshot=False,
        )
