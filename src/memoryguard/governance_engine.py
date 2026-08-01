"""Single write interface for governed shared long-term memory.

GUI, MCP and AutoOrganizer call this engine.  SharedMemoryStore remains the
persistence layer.  The optional idempotency key is a transaction seam: repeat
calls with the same action/key return without applying the mutation twice.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from .schema_v3 import (
    ConflictGroup,
    ConflictResolution,
    DecisionEvent,
    Provenance,
    QuarantineEntry,
    SharedMemoryRecord,
    SharedMemoryStatus,
    RuleDecision,
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
        injection_policy: str = "relevant",
        priority: int = 0,
        rule_assignments: list[dict[str, Any]] | None = None,
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
            "injection_policy": injection_policy,
            "priority": priority,
            "rule_assignments": rule_assignments or [],
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
        from .schema_v3 import validate_injection_settings
        injection_policy, priority = validate_injection_settings(
            injection_policy, priority,
        )
        # Runtime-only organizer input.  The resulting record persists these
        # fields itself; event metadata is intentionally not used as storage.
        event.injection_policy = injection_policy
        event.priority = priority
        event.rule_assignments = list(rule_assignments or [])

        # Mandatory rules use the planner/atomic store seam.  This preserves
        # the existing enricher + duplicate semantics while ensuring record,
        # event, assignments and structured decision commit together.
        if injection_policy == "always" and rule_assignments:
            from .auto_organizer import AutoOrganizer
            organizer = AutoOrganizer(
                self.store.workspace,
                self.group_id,
                enricher_mode=enricher_mode,
                store=self.store,
                engine=self,
            )
            try:
                planned_record, planned_actions, mutation_kind = organizer.plan_rule_create(
                    event, kind_override=kind_override, write_policy=write_policy,
                )
            except (TypeError, ValueError, RuntimeError) as exc:
                return self._blocked(
                    action="auto_write", actor=actor, record=None,
                    reason=str(exc), idempotency_key=idempotency_key,
                )
            metadata = dict(event.metadata or {})
            target_assignment = (
                rule_assignments[0].to_dict()
                if rule_assignments and hasattr(rule_assignments[0], "to_dict")
                else dict(rule_assignments[0]) if rule_assignments else {}
            )
            if mutation_kind in {"created", "deduplicated"}:
                lifecycle_action = (
                    "rule_create_manual"
                    if metadata.get("rule_creation") == "manual"
                    else "rule_create_auto"
                )
                decision = RuleDecision(
                    decision_id=self._decision_id("auto_write", idempotency_key)
                    if idempotency_key else stable_hash(
                        "rule-create-decision", self.group_id, event.event_id,
                    ),
                    actor=("admin" if lifecycle_action == "rule_create_manual" else actor),
                    owner_agent_id=event.agent_instance_id or "",
                    before={"assignments": []},
                    after={"record": planned_record.to_dict()},
                    reason=str(metadata.get("scope_reason") or "automatic governed memory write"),
                    confidence=float(metadata.get("scope_confidence", planned_record.confidence) or planned_record.confidence),
                    undo_id=str(metadata.get("undo_id", "") or ""),
                    created_at=_now_iso(),
                    rule_id=planned_record.memory_id,
                    action=lifecycle_action,
                    target_ids=[item for item in (event.agent_instance_id, planned_record.memory_id) if item],
                    status="created",
                    memory_id=planned_record.memory_id,
                    kind=planned_record.kind.value,
                    assignments=[target_assignment],
                    target_type=str(target_assignment.get("target_type", "")),
                    target_id=str(target_assignment.get("target_id", "")),
                    project_ref=str(target_assignment.get("project_ref", "")),
                    scope_confidence=float(metadata.get("scope_confidence", planned_record.confidence) or planned_record.confidence),
                    scope_reason=str(metadata.get("scope_reason", "") or ""),
                    body=planned_record.body,
                    metadata=metadata,
                )
                try:
                    atomic = self.store.apply_rule_create_atomic(
                        planned_record,
                        event=event,
                        assignments=list(rule_assignments),
                        decision=decision,
                        automatic=metadata.get("rule_creation") != "manual",
                        actor_agent_id=event.agent_instance_id or "",
                    )
                except (TypeError, ValueError, RuntimeError) as exc:
                    return self._blocked(
                        action="auto_write", actor=actor, record=None,
                        reason=str(exc), idempotency_key=idempotency_key,
                    )
                persisted_decision = atomic.get("decision") if isinstance(atomic, dict) else None
                result = {
                    "ok": True,
                    "action": "auto_write",
                    "actor": actor,
                    "before": None,
                    "after": atomic.get("record") if isinstance(atomic, dict) else planned_record.to_dict(),
                    "decision_id": (
                        persisted_decision.get("decision_id", "")
                        if isinstance(persisted_decision, dict)
                        else getattr(persisted_decision, "decision_id", decision.decision_id)
                    ),
                    "version_id": self.store.get_active_version_id(),
                    "blocked_reason": "",
                    "idempotency_key": idempotency_key,
                    "idempotent_replay": False,
                    "memory_id": atomic.get("memory_id", planned_record.memory_id),
                    "status": (atomic.get("record") or {}).get("status", planned_record.status.value),
                    "kind": (atomic.get("record") or {}).get("kind", planned_record.kind.value),
                    "auto_actions": planned_actions,
                    "record": atomic.get("record", planned_record.to_dict()),
                    "assignments": atomic.get("assignments", []),
                    "mutation_kind": atomic.get("mutation_kind", mutation_kind),
                    "decision": persisted_decision,
                    "event_id": atomic.get("event_id", event.event_id),
                }
                return result
            # Supersede/conflict/quarantine are also planned without writes.
            # Commit the candidate, old-record status/group/quarantine entry,
            # event and structured decision in one Store transaction; never
            # fall back to organizer.organize() after a lifecycle plan.
            if mutation_kind in {"superseded", "conflicted", "quarantined"}:
                action = next(
                    (
                        item for item in planned_actions
                        if isinstance(item, dict)
                        and str(item.get("action", "")) in {
                            "supersede", "conflict", "quarantine",
                        }
                    ),
                    {},
                )
                old_ids = [
                    str(item) for item in (
                        action.get("old_ids")
                        or ([action.get("old_id")] if action.get("old_id") else [])
                        or list(getattr(planned_record, "supersedes", []) or [])
                    ) if str(item)
                ]
                before_records: dict[str, Any] = {}
                for old_id in old_ids:
                    previous = self.store.get_record(old_id)
                    if previous is not None:
                        before_records[old_id] = previous.to_dict()
                conflict_group = None
                if mutation_kind == "conflicted":
                    conflict_group = ConflictGroup(
                        group_id=stable_hash(
                            "auto-conflict", self.group_id,
                            planned_record.memory_id, event.event_id,
                        ),
                        member_ids=[*old_ids, planned_record.memory_id],
                        reason=str(action.get("reason") or "automatic conflict"),
                        created_at=event.created_at or _now_iso(),
                    )
                quarantine_entry = None
                if mutation_kind == "quarantined":
                    pattern = str(
                        action.get("detected_pattern")
                        or action.get("reason")
                        or "sensitive content"
                    )
                    quarantine_entry = QuarantineEntry(
                        quarantine_id=stable_hash(
                            "quarantine", planned_record.memory_id,
                            event.event_id, event.created_at or _now_iso(),
                        ),
                        memory_id=planned_record.memory_id,
                        reason=f"检测到敏感信息: {pattern}",
                        detected_pattern=pattern,
                        original_content=event.raw_content,
                        quarantined_at=event.created_at or _now_iso(),
                    )
                lifecycle_action = f"rule_{mutation_kind}"
                decision = RuleDecision(
                    decision_id=self._decision_id("auto_write", idempotency_key)
                    if idempotency_key else stable_hash(
                        "rule-lifecycle-decision", self.group_id,
                        mutation_kind, event.event_id,
                    ),
                    actor=(
                        "admin"
                        if metadata.get("rule_creation") == "manual"
                        else actor
                    ),
                    owner_agent_id=event.agent_instance_id or "",
                    before={"records": before_records},
                    after={"record": planned_record.to_dict()},
                    reason=str(
                        metadata.get("scope_reason")
                        or action.get("reason")
                        or f"automatic {mutation_kind} lifecycle"
                    ),
                    confidence=float(
                        metadata.get("scope_confidence", planned_record.confidence)
                        or planned_record.confidence
                    ),
                    undo_id=str(metadata.get("undo_id", "") or ""),
                    created_at=_now_iso(),
                    rule_id=planned_record.memory_id,
                    action=lifecycle_action,
                    target_ids=[planned_record.memory_id, *old_ids],
                    status="created",
                    memory_id=planned_record.memory_id,
                    kind=planned_record.kind.value,
                    assignments=[target_assignment],
                    target_type=str(target_assignment.get("target_type", "")),
                    target_id=str(target_assignment.get("target_id", "")),
                    project_ref=str(target_assignment.get("project_ref", "")),
                    scope_confidence=float(
                        metadata.get("scope_confidence", planned_record.confidence)
                        or planned_record.confidence
                    ),
                    scope_reason=str(metadata.get("scope_reason", "") or ""),
                    body=planned_record.body,
                    metadata={
                        **metadata,
                        "mutation_kind": mutation_kind,
                        "old_record_ids": old_ids,
                        "conflict_reason": action.get("reason", ""),
                        "quarantine_reason": action.get("reason", ""),
                    },
                )
                try:
                    atomic = self.store.apply_rule_lifecycle_atomic(
                        planned_record,
                        event,
                        decision=decision,
                        mutation_kind=mutation_kind,
                        assignments=list(rule_assignments),
                        old_record_ids=old_ids,
                        conflict_group=conflict_group,
                        quarantine_entry=quarantine_entry,
                        actor_agent_id=event.agent_instance_id or "",
                        automatic=metadata.get("rule_creation") != "manual",
                    )
                except (TypeError, ValueError, RuntimeError) as exc:
                    return self._blocked(
                        action="auto_write", actor=actor, record=None,
                        reason=str(exc), idempotency_key=idempotency_key,
                    )
                persisted_decision = (
                    atomic.get("decision")
                    if isinstance(atomic, dict) else None
                )
                persisted_dict = (
                    persisted_decision.to_dict()
                    if hasattr(persisted_decision, "to_dict")
                    else persisted_decision
                )
                return {
                    "ok": True,
                    "action": "auto_write",
                    "actor": actor,
                    "before": {"records": before_records},
                    "after": atomic.get("record", planned_record.to_dict()),
                    "decision_id": (
                        persisted_dict.get("decision_id", "")
                        if isinstance(persisted_dict, dict)
                        else decision.decision_id
                    ),
                    "version_id": self.store.get_active_version_id(),
                    "blocked_reason": "",
                    "idempotency_key": idempotency_key,
                    "idempotent_replay": False,
                    "memory_id": atomic.get("memory_id", planned_record.memory_id),
                    "status": atomic.get(
                        "record", planned_record.to_dict()
                    ).get("status", planned_record.status.value),
                    "kind": atomic.get(
                        "record", planned_record.to_dict()
                    ).get("kind", planned_record.kind.value),
                    "auto_actions": planned_actions,
                    "record": atomic.get("record", planned_record.to_dict()),
                    "assignments": atomic.get("assignments", []),
                    "mutation_kind": atomic.get("mutation_kind", mutation_kind),
                    "decision": persisted_dict,
                    "event_id": atomic.get("event_id", event.event_id),
                    "target_ids": atomic.get(
                        "target_ids", [planned_record.memory_id, *old_ids]
                    ),
                    "undo_metadata": atomic.get("undo_metadata", {}),
                }
        self.store.append_event(event)
        from .auto_organizer import AutoOrganizer
        organizer = AutoOrganizer(
            self.store.workspace,
            self.group_id,
            enricher_mode=enricher_mode,
            store=self.store,
            engine=self,
        )
        try:
            record, actions = organizer.organize(
                event,
                kind_override=kind_override,
                write_policy=write_policy,
            )
        except ValueError as exc:
            return self._blocked(
                action="auto_write", actor=actor, record=None,
                reason=str(exc), idempotency_key=idempotency_key,
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
            "record": record.to_dict(),
            "mutation_kind": self._mutation_kind_from_actions(actions, record),
        })
        return result

    @staticmethod
    def _mutation_kind_from_actions(
        actions: list[dict[str, Any]], record: SharedMemoryRecord | None = None,
    ) -> str:
        """Map legacy organizer actions to the explicit lifecycle kind."""
        names = {str(item.get("action", "")) for item in actions if isinstance(item, dict)}
        if "quarantine" in names:
            return "quarantined"
        if "conflict" in names:
            return "conflicted"
        if "supersede" in names:
            return "superseded"
        if "merge_provenance" in names or "semantic_match" in names:
            return "deduplicated"
        return "created"

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
        injection_policy: str | None = None,
        priority: int | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        payload = {
            "body": body, "kind": kind, "status": status,
            "injection_policy": injection_policy, "priority": priority,
        }
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
        prospective = replace(
            record,
            body=body if body is not None else record.body,
            kind=MemoryKind(kind) if kind is not None else record.kind,
            status=SharedMemoryStatus(status) if status is not None else record.status,
            injection_policy=(
                injection_policy if injection_policy is not None
                else record.injection_policy
            ),
            priority=priority if priority is not None else record.priority,
            updated_at=now,
        )
        from .schema_v3 import validate_injection_settings
        try:
            validate_injection_settings(
                prospective.injection_policy, prospective.priority,
            )
        except ValueError as exc:
            return self._blocked(
                action="agent_update", actor=actor, record=record,
                reason=str(exc), idempotency_key=idempotency_key,
            )
        with self.store._tx() as conn:
            try:
                self.store._validate_mandatory_budget(
                    prospective, conn=conn, replacing_id=memory_id,
                )
            except ValueError as exc:
                return self._blocked(
                    action="agent_update", actor=actor, record=record,
                    reason=str(exc), idempotency_key=idempotency_key,
                )
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
            if injection_policy is not None:
                conn.execute(
                    "UPDATE records SET injection_policy=?, updated_at=? WHERE memory_id=?",
                    (injection_policy, now, memory_id),
                )
            if priority is not None:
                conn.execute(
                    "UPDATE records SET priority=?, updated_at=? WHERE memory_id=?",
                    (priority, now, memory_id),
                )
        after = self._state(self.store.get_record(memory_id))
        result = self._finish(
            action="agent_update",
            actor=actor,
            target_ids=[memory_id],
            before=before,
            after=after,
            reason="MCP agent governance update",
            idempotency_key=idempotency_key,
            payload=payload,
        )
        return result

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
        prospective = replace(
            record,
            body=body if body is not None else record.body,
            status=status if status is not None else record.status,
            updated_at=now,
        )
        with self.store._tx() as conn:
            if (
                prospective.status == SharedMemoryStatus.ACTIVE
                and prospective.injection_policy == "always"
            ):
                try:
                    self.store._validate_mandatory_budget(
                        prospective, conn=conn, replacing_id=memory_id,
                    )
                except ValueError as exc:
                    return self._blocked(
                        action=action, actor="user", record=record,
                        reason=str(exc), idempotency_key=idempotency_key,
                    )
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

    def human_set_injection_policy(
        self,
        memory_id: str,
        injection_policy: str,
        priority: int = 0,
        *,
        assignments: list[dict[str, Any]] | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        """Manual policy toggle; unlike agent updates it preserves locked records."""
        from .schema_v3 import validate_injection_settings
        try:
            injection_policy, priority = validate_injection_settings(
                injection_policy, priority,
            )
        except ValueError as exc:
            return self._blocked(
                action="set_injection_policy", actor="user", record=None,
                reason=str(exc), idempotency_key=idempotency_key,
            )
        record = self.store.get_record(memory_id)
        if record is None:
            return self._blocked(
                action="set_injection_policy", actor="user", record=None,
                reason="memory_not_found", idempotency_key=idempotency_key,
            )
        before = self._state(record)
        before_assignments = self.store.list_rule_assignments(memory_id)
        provenance, now = self._manual_provenance(record, "set_injection_policy")
        requested_assignments = assignments or []
        assignment_keys = sorted(
            stable_hash(
                item.get("target_type", ""), item.get("target_id", ""),
                item.get("project_ref", ""), item.get("effect", "include"),
            )
            for item in requested_assignments
        )
        audit_summary = json.dumps({
            "before": {
                "policy": record.injection_policy,
                "priority": record.priority,
                "assignment_keys": sorted(
                    item.assignment_id for item in before_assignments
                ),
            },
            "after": {
                "policy": injection_policy,
                "priority": priority,
                "assignment_keys": assignment_keys,
                "scope_count": len(requested_assignments),
            },
        }, ensure_ascii=False, separators=(",", ":"))[:1200]
        atomic_decision = DecisionEvent(
            event_id=stable_hash(
                "atomic-rule-transition", memory_id, injection_policy,
                str(priority), now,
            ),
            actor="local-admin/gui",
            action="atomic_rule_transition",
            target_ids=[memory_id],
            reason=audit_summary,
            created_at=now,
        )
        try:
            updated, final_assignments = self.store.transition_injection_policy(
                memory_id, injection_policy, priority,
                assignments=assignments or [],
                decision=atomic_decision,
                provenance=provenance,
            )
        except ValueError as exc:
            return self._blocked(
                action="set_injection_policy", actor="user", record=record,
                reason=str(exc), idempotency_key=idempotency_key,
            )
        result = self._finish(
            action="set_injection_policy", actor="user",
            target_ids=[memory_id], before=before,
            after=self._state(updated),
            reason="manual injection policy and audience transition",
            idempotency_key=idempotency_key,
            payload={
                "injection_policy": injection_policy, "priority": priority,
                "assignments": [
                    item.to_dict() for item in final_assignments
                ],
            },
        )
        result["assignments"] = [
            item.to_dict() for item in final_assignments
        ]
        return result

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
            # Validate before shadowing descendants. Returning after a failed
            # check must leave the lineage untouched.
            try:
                self.store._validate_mandatory_budget(
                    replace(
                        record,
                        status=SharedMemoryStatus.ACTIVE,
                        updated_at=now,
                    ),
                    assignments=self.store._list_rule_assignments_conn(
                        conn, memory_id,
                    ),
                    conn=conn,
                    replacing_id=memory_id,
                )
            except ValueError as exc:
                return self._blocked(
                    action="restore", actor="user", record=record,
                    reason=str(exc), idempotency_key=idempotency_key,
                )
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
            try:
                self.store._validate_mandatory_budget(
                    replace(
                        record,
                        status=SharedMemoryStatus.ACTIVE,
                        updated_at=now,
                    ),
                    conn=conn,
                    replacing_id=memory_id,
                )
            except ValueError as exc:
                return self._blocked(
                    action="restore", actor="user", record=record,
                    reason=str(exc), idempotency_key=idempotency_key,
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
            try:
                self.store._validate_mandatory_budget(
                    replace(keep, status=SharedMemoryStatus.ACTIVE, updated_at=now),
                    conn=conn,
                    replacing_id=keep_memory_id,
                )
            except ValueError as exc:
                return self._blocked(
                    action="resolve_conflict", actor="user", record=keep,
                    reason=str(exc), idempotency_key=idempotency_key,
                )
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
            if new_status == SharedMemoryStatus.ACTIVE:
                try:
                    self.store._validate_mandatory_budget(
                        replace(record, status=SharedMemoryStatus.ACTIVE, updated_at=now),
                        conn=conn,
                        replacing_id=entry.memory_id,
                    )
                except ValueError as exc:
                    return self._blocked(
                        action=action, actor="user", record=record,
                        reason=str(exc), idempotency_key=idempotency_key,
                    )
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
        injection_policy: str = "relevant",
        assignments: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Evaluate durable manual overrides before automatic mutation."""
        if assignments is None and hasattr(self, "_incoming_rule_scope"):
            injection_policy, assignments = self._incoming_rule_scope
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
            if not self.store.record_domain_overlaps(
                record, injection_policy, assignments or [],
            ):
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
