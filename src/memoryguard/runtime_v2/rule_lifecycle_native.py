"""Native V2 lifecycle mutations for mandatory rules.

The legacy MCP lifecycle used ``SharedMemoryStore`` as both rule source and
mutation ledger.  V2 keeps rule facts in ``rules/rules.db`` instead.  This
service is deliberately small: create a governed Definition+Binding, persist
receipt feedback as body-free evidence, and undo by compensating state rather
than deleting immutable history.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from ..governance_v2.rules import RuleMutationContext, RuleAuthorizationError
from ..rule_definition import build_definition
from ..rule_exception_core import RuleExceptionPlanError, plan_rule_exception_scope
from ..rule_scope import canonical_project_ref
from ..rules.v2_store import RuleV2Store, stable_digest
from ..security import feedback_authority
from .native_ports import NativeContextError, resolve_native_transport_context


class NativeRuleLifecycleError(ValueError):
    """Stable fail-closed lifecycle error exposed through the native port."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return list(parsed) if isinstance(parsed, list) else []
    return []


def _digest_text(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


class NativeRuleLifecycleService:
    """Rule create/feedback/undo over one canonical V2 rules database."""

    service_name = "rule_lifecycle"
    _OUTCOMES = frozenset({
        "followed", "violated", "not_applicable", "corrected", "exception", "ignored",
    })

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.store = RuleV2Store(self.workspace)

    def _settle_canonical(self, group: str) -> dict[str, Any]:
        from ..rule_reconciliation import settle_native_canonical_snapshot

        return settle_native_canonical_snapshot(
            self.workspace, group, store=self.store,
        )

    def _context(self, raw: Any, *, automatic: bool) -> tuple[Any, RuleMutationContext]:
        try:
            authority = resolve_native_transport_context(raw)
        except NativeContextError as exc:
            raise NativeRuleLifecycleError("trusted_context_capability_required") from exc
        if Path(authority.workspace_id).expanduser().resolve() != self.workspace:
            raise NativeRuleLifecycleError("context_workspace_mismatch")
        try:
            context = RuleMutationContext(
                agent=authority.agent_instance_id,
                project=authority.project_ref,
                group=authority.share_group_id,
                provider=authority.provider,
                runtime=authority.runtime_role,
                admin=bool(authority.admin),
                automatic=bool(automatic),
            ).validate()
        except (RuleAuthorizationError, ValueError) as exc:
            raise NativeRuleLifecycleError(str(exc)) from exc
        return authority, context

    @staticmethod
    def _scope(payload: Mapping[str, Any], context: RuleMutationContext, *, manual: bool) -> dict[str, Any]:
        requested = payload.get("scope")
        if requested is None:
            scope = {"target_type": "agent", "target_id": context.agent}
        elif not isinstance(requested, Mapping):
            raise NativeRuleLifecycleError("invalid_rule_scope")
        else:
            scope = {str(key): value for key, value in requested.items()}
        target_type = str(scope.get("target_type", scope.get("type", "")) or "").strip().casefold()
        target_id = str(scope.get("target_id", scope.get("id", "")) or "").strip()
        if not target_type:
            target_type = "agent"
            target_id = target_id or context.agent
        requested_project = str(scope.get("project_ref") or "").strip()
        if requested_project and canonical_project_ref(requested_project) != canonical_project_ref(context.project):
            # ``target_id`` is normalized to the trusted project below for
            # authorization.  Compare the caller's explicit project first so
            # that normalization cannot turn a foreign-project request into a
            # valid local project scope.
            raise NativeRuleLifecycleError("other_project_scope_denied")
        auth_scope = dict(scope)
        auth_scope["target_type"] = "project" if target_type == "agent_project" else target_type
        if target_type in {"project", "agent_project"}:
            auth_scope["target_id"] = context.project
        elif target_type == "agent":
            auth_scope["target_id"] = target_id or context.agent
        try:
            # ``automatic`` is already encoded in the trusted RuleMutationContext.
            context.authorize_scope({"scope": auth_scope})
        except RuleAuthorizationError as exc:
            raise NativeRuleLifecycleError(str(exc)) from exc
        if target_type == "project":
            # Automatic project scope remains tied to the current Agent in the
            # V2 binding model; it is not a project-wide broadcast.
            target_type = "agent_project"
            target_id = context.agent
        if target_type == "agent_project":
            target_id = context.agent
        if target_type == "agent":
            target_id = target_id or context.agent
        if target_type == "group":
            target_id = target_id or context.group
        if target_type == "provider":
            target_id = target_id or context.provider
        if target_type in {"runtime", "runtime-role"}:
            target_type = "runtime_role"
        if target_type == "runtime_role":
            target_id = target_id or context.runtime
        return {
            "target_type": target_type,
            "target_id": target_id,
            "project_ref": context.project if target_type in {"project", "agent_project"} else str(scope.get("project_ref") or ""),
            "provider": context.provider if target_type == "provider" else str(scope.get("provider") or ""),
            "runtime_role": context.runtime if target_type == "runtime_role" else str(scope.get("runtime_role") or ""),
            "effect": str(scope.get("effect") or "include"),
            "manual": bool(manual),
        }

    def _fingerprint(self, operation: str, payload: Mapping[str, Any], context: RuleMutationContext) -> str:
        clean = {str(key): value for key, value in payload.items() if str(key) not in {
            "agent", "agent_id", "agent_instance_id", "project", "project_id", "project_ref",
            "group", "group_id", "share_group_id", "provider", "runtime", "runtime_role",
        }}
        if "evidence" in clean:
            clean["evidence_digest"] = _digest_text(clean.pop("evidence"))
        return stable_digest({"operation": operation, "payload": clean, "scope": context.to_dict()})

    def _request_key(self, operation: str, payload: Mapping[str, Any], context: RuleMutationContext) -> tuple[str, str]:
        fingerprint = self._fingerprint(operation, payload, context)
        explicit = str(payload.get("idempotency_key") or "").strip()
        key = explicit or f"derived:{operation}:{fingerprint}"
        return key, fingerprint

    def _replay(self, operation: str, payload: Mapping[str, Any], context: RuleMutationContext) -> tuple[str, str, dict[str, Any] | None]:
        key, fingerprint = self._request_key(operation, payload, context)
        fence = self.store.get_idempotency_fence(context.group, key)
        if fence is None:
            return key, fingerprint, None
        if str(fence.get("request_fingerprint") or "") != fingerprint:
            raise NativeRuleLifecycleError("idempotency_conflict")
        decision_id = str(fence.get("decision_id") or "")
        decision = self.store.get_decision(decision_id) if decision_id else None
        if decision is None:
            raise NativeRuleLifecycleError("idempotency_replay_incomplete")
        return key, fingerprint, {"ok": True, "status": "replayed", "idempotent_replay": True, "decision": decision}

    def _record_fence(self, *, key: str, fingerprint: str, context: RuleMutationContext, decision_id: str, memory_id: str = "") -> None:
        self.store.record_idempotency_fence({
            "fence_id": stable_digest(("native-v2-rule-lifecycle", context.group, key)),
            "key": key,
            "request_fingerprint": fingerprint,
            "memory_id": memory_id,
            "event_id": decision_id,
            "decision_id": decision_id,
            "created_at": _now(),
            "share_group_id": context.group,
            "source_ref": "native-v2-rule-lifecycle",
        })

    @staticmethod
    def _decision_result(decision: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
        return {"ok": True, "status": "committed", "decision": dict(decision), **extra}

    def create_auto(self, payload: Mapping[str, Any], *, context: Any) -> dict[str, Any]:
        body = str(payload.get("text") or "").strip()
        if not body:
            raise NativeRuleLifecycleError("rule_text_required")
        manual = bool(payload.get("manual", False))
        authority, trusted = self._context(context, automatic=not manual)
        scope = self._scope(payload, trusted, manual=manual)
        try:
            priority = int(payload.get("priority", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise NativeRuleLifecycleError("invalid_rule_priority") from exc
        if priority < -100 or priority > 100:
            raise NativeRuleLifecycleError("invalid_rule_priority")
        key, fingerprint, replay = self._replay("rule_create_auto", payload, trusted)
        if replay is not None:
            # A prior write may have committed before snapshot publication.
            # Idempotent replay is also the repair path for that narrow crash
            # window; never report success while canonical reads remain stale.
            self._settle_canonical(trusted.group)
            return replay
        kind = str(payload.get("kind") or "procedure")
        now = _now()
        definition = build_definition(body, kind=kind, created_at=now)
        binding_id = stable_digest((
            "native-v2-rule-binding", definition.definition_id, trusted.group,
            scope["target_type"], scope["target_id"], scope["project_ref"],
            scope["provider"], scope["runtime_role"], scope["effect"], priority,
        ))
        decision_id = stable_digest(("native-v2-rule-create", trusted.group, key, fingerprint))
        undo_id = stable_digest(("native-v2-rule-undo", decision_id))
        created_by = "admin" if manual and trusted.admin else ("manual" if manual else "auto")
        with self.store.transaction():
            previous_definition = self.store.get_definition(definition.definition_id)
            previous_binding = next((item for item in self.store.list_bindings(definition_id=definition.definition_id) if item.binding_id == binding_id), None)
            persisted = self.store.upsert_definition(definition)
            binding = self.store.upsert_binding({
                "binding_id": binding_id,
                "definition_id": persisted.definition_id,
                "share_group_id": trusted.group,
                "target_type": scope["target_type"],
                "target_id": scope["target_id"],
                "project_ref": scope["project_ref"],
                "provider": scope["provider"],
                "runtime_role": scope["runtime_role"],
                "effect": scope["effect"],
                "priority": priority,
                "owner_agent_id": trusted.agent,
                "created_by": created_by,
                "authorization": f"native:{authority.session_source}:{authority.session_id}",
                "status": "active",
                "revision": (previous_binding.revision + 1 if previous_binding and previous_binding.status != "active" else (previous_binding.revision if previous_binding else 1)),
                "created_at": previous_binding.created_at if previous_binding else now,
                "updated_at": now,
            })
            before = {
                "definition_status": previous_definition.status if previous_definition else "missing",
                "binding_status": previous_binding.status if previous_binding else "missing",
            }
            after = {
                "definition_id": persisted.definition_id,
                "definition_status": persisted.status,
                "binding_id": binding.binding_id,
                "binding_status": binding.status,
                "scope": scope,
                "priority": priority,
            }
            self.store.record_decision({
                "decision_id": decision_id,
                "actor": trusted.agent,
                "owner_agent_id": trusted.agent,
                "rule_id": persisted.definition_id,
                "action": "rule_create_auto" if not manual else "rule_create_manual",
                "before_hash": stable_digest(before),
                "after_hash": stable_digest(after),
                "before_json": _json(before),
                "after_json": _json(after),
                "reason": "native V2 mandatory rule creation",
                "confidence": 1.0,
                "undo_id": undo_id,
                "target_ids_json": _json([binding.binding_id]),
                "metadata_json": _json({"idempotency_key": key, "manual": manual}),
                "source_ref": "native-v2:mcp:rule_create_auto",
                "created_at": now,
            })
            self._record_fence(key=key, fingerprint=fingerprint, context=trusted, decision_id=decision_id, memory_id=persisted.definition_id)
            source_id = "decision:" + decision_id
            source_ref = "native-v2:mcp:rule_create_auto:" + decision_id
            self.store.upsert_source_link(
                source_kind="native-rule-lifecycle",
                share_group_id=trusted.group,
                memory_id=source_id,
                source_ref=source_ref,
                original_definition_id=persisted.definition_id,
                canonical_definition_id=persisted.definition_id,
                status="active",
                created_at=now,
                updated_at=now,
            )
            self.store.record_evidence_ref({
                "evidence_id": stable_digest(("native-rule-lifecycle", decision_id)),
                "definition_id": persisted.definition_id,
                "source_rule_id": source_id,
                "share_group_id": trusted.group,
                "evidence_ref": source_ref,
                "content_digest": persisted.semantic_hash,
                "authority": "governed-lifecycle",
                "status": "active",
                "created_at": now,
            })
            self._settle_canonical(trusted.group)
        decision = self.store.get_decision(decision_id) or {"decision_id": decision_id}
        return self._decision_result(decision, definition_id=definition.definition_id, binding_id=binding_id, undo_id=undo_id)

    def feedback(self, payload: Mapping[str, Any], *, context: Any) -> dict[str, Any]:
        authority, trusted = self._context(context, automatic=False)
        receipt_id = str(payload.get("receipt_id") or "").strip()
        outcome = str(payload.get("outcome") or "").strip().casefold()
        if not receipt_id:
            raise NativeRuleLifecycleError("receipt_id_required")
        if outcome not in self._OUTCOMES:
            raise NativeRuleLifecycleError("invalid_rule_feedback_outcome")
        try:
            confidence = float(payload.get("confidence", 1.0) if payload.get("confidence") is not None else 1.0)
        except (TypeError, ValueError) as exc:
            raise NativeRuleLifecycleError("invalid_rule_confidence") from exc
        if not 0.0 <= confidence <= 1.0:
            raise NativeRuleLifecycleError("invalid_rule_confidence")
        receipt = self.store.get_receipt(receipt_id)
        if receipt is None:
            raise NativeRuleLifecycleError("rule_receipt_not_found")
        if str(receipt.get("share_group_id") or "") != trusted.group:
            raise NativeRuleLifecycleError("rule_receipt_scope_mismatch")
        receipt_agent = str(receipt.get("agent_instance_id") or "")
        receipt_project = str(receipt.get("project_ref") or "")
        if receipt_agent and receipt_agent != trusted.agent and not trusted.admin:
            raise NativeRuleLifecycleError("rule_receipt_owner_mismatch")
        if receipt_project and receipt_project != trusted.project and not trusted.admin:
            raise NativeRuleLifecycleError("rule_receipt_project_mismatch")
        evidence = str(payload.get("evidence") or "")
        evidence_digest = _digest_text(evidence) if evidence else ""
        # Feedback is always retained as an audit row, but only a trusted host
        # or transport session may affect the effective evidence projection.
        # This preserves the old cross-session narrowing rule without letting
        # an untrusted direct/MCP caller narrow a binding.
        trusted_session = bool(
            authority.session_trusted is True
            and bool(authority.session_id)
            and authority.session_source.casefold() in {"host", "transport"}
        )
        clean_payload = dict(payload)
        clean_payload["evidence"] = evidence_digest
        key, fingerprint, replay = self._replay("rule_feedback", clean_payload, trusted)
        if replay is not None:
            return replay
        definition_id = str(receipt.get("definition_id") or "")
        if not definition_id or self.store.get_definition(definition_id) is None:
            raise NativeRuleLifecycleError("rule_receipt_definition_missing")
        now = _now()
        feedback_id = stable_digest(("native-v2-rule-feedback", trusted.group, receipt_id, key, fingerprint))
        decision_id = stable_digest(("native-v2-rule-feedback-decision", feedback_id))
        undo_id = stable_digest(("native-v2-rule-feedback-undo", decision_id))
        producer = "user" if trusted.admin else "agent"
        producer_authority = feedback_authority(producer)
        metadata = {
            "producer": producer,
            "evidence_present": bool(evidence),
            "evidence_digest": evidence_digest,
            "session_id": authority.session_id,
            "session_source": authority.session_source,
            "confidence": confidence,
        }
        with self.store.transaction():
            self.store.record_feedback({
                "feedback_id": feedback_id,
                "receipt_id": receipt_id,
                "definition_id": definition_id,
                "outcome": outcome,
                "authority": producer_authority,
                "evidence_digest": evidence_digest,
                "metadata_json": _json(metadata),
                "created_at": now,
            })
            polarity = "positive" if outcome == "followed" else "negative"
            self.store.record_evidence_contribution({
                "contribution_id": stable_digest(("native-v2-feedback-evidence", feedback_id)),
                "definition_id": definition_id,
                # One receipt is one independent observation.  Session ID is
                # intentionally not part of this key: the same matched rule
                # replayed in another trusted session must collapse to one
                # effective winner, while distinct receipts remain distinct
                # cross-session evidence.
                "independence_key": stable_digest(("native-v2-feedback", trusted.group, definition_id, receipt_id)),
                "kind": "feedback",
                "polarity": polarity,
                "authority": producer_authority,
                "confidence": confidence,
                "observed_at": now,
                "active": 1,
                "receipt_id": receipt_id,
                "feedback_id": feedback_id,
                "source_evidence_id": "",
                "source_memory_id": str(receipt.get("source_rule_id") or ""),
                "source_ids_json": _json([receipt_id]),
                "metadata_json": _json({
                    "outcome": outcome,
                    "evidence_digest": evidence_digest,
                    "session_id": authority.session_id,
                    "session_source": authority.session_source,
                    "session_trusted": trusted_session,
                }),
                "created_at": now,
                "updated_at": now,
            })
            effective = None
            if trusted_session:
                effective = self.store.rebuild_evidence_effective(
                    definition_id=definition_id,
                    independence_key=stable_digest(("native-v2-feedback", trusted.group, definition_id, receipt_id)),
                    kind="feedback",
                    updated_at=now,
                )
                self.store.rebuild_effective_feedback_projection(receipt_id, updated_at=now)
            after = {"feedback_id": feedback_id, "receipt_id": receipt_id, "outcome": outcome, "definition_id": definition_id}
            self.store.record_decision({
                "decision_id": decision_id,
                "actor": trusted.agent,
                "owner_agent_id": trusted.agent,
                "rule_id": definition_id,
                "action": "rule_feedback",
                "before_hash": stable_digest({"receipt_id": receipt_id}),
                "after_hash": stable_digest(after),
                "before_json": _json({"receipt_id": receipt_id}),
                "after_json": _json(after),
                "reason": "native V2 rule feedback",
                "confidence": confidence,
                "undo_id": undo_id,
                "target_ids_json": _json([feedback_id]),
                "metadata_json": _json({"idempotency_key": key, "evidence_digest": evidence_digest}),
                "source_ref": "native-v2:mcp:rule_feedback",
                "created_at": now,
            })
            self._record_fence(key=key, fingerprint=fingerprint, context=trusted, decision_id=decision_id, memory_id=definition_id)
        decision = self.store.get_decision(decision_id) or {"decision_id": decision_id}
        return self._decision_result(
            decision,
            feedback_id=feedback_id,
            receipt_id=receipt_id,
            outcome=outcome,
            undo_id=undo_id,
            effective_evidence_id=str((effective or {}).get("effective_id") or ""),
            trusted_session=trusted_session,
        )

    def undo(self, payload: Mapping[str, Any], *, context: Any) -> dict[str, Any]:
        _authority, trusted = self._context(context, automatic=False)
        decision_id = str(payload.get("decision_id") or "").strip()
        undo_id = str(payload.get("undo_id") or "").strip()
        if not decision_id and not undo_id:
            raise NativeRuleLifecycleError("rule_undo_target_required")
        original = self.store.get_decision(decision_id) if decision_id else self.store.get_decision_by_undo(undo_id)
        if original is None:
            raise NativeRuleLifecycleError("rule_decision_not_found")
        owner = str(original.get("owner_agent_id") or original.get("actor") or "")
        if owner and owner != trusted.agent and not trusted.admin:
            raise NativeRuleLifecycleError("rule_undo_owner_mismatch")
        action = str(original.get("action") or "")
        if action.startswith("rule_undo"):
            raise NativeRuleLifecycleError("rule_undo_recursive_denied")
        clean_payload = {"decision_id": str(original.get("decision_id") or ""), "undo_id": str(original.get("undo_id") or ""), "idempotency_key": payload.get("idempotency_key", "")}
        key, fingerprint, replay = self._replay("rule_undo", clean_payload, trusted)
        if replay is not None:
            self._settle_canonical(trusted.group)
            return replay
        now = _now()
        original_after = _object(original.get("after_json"))
        compensation: dict[str, Any] = {"original_decision_id": str(original.get("decision_id") or ""), "original_action": action}
        new_decision_id = stable_digest(("native-v2-rule-undo", trusted.group, key, fingerprint))
        with self.store.transaction():
            if action in {"rule_create_auto", "rule_create_manual"}:
                binding_id = str(original_after.get("binding_id") or "")
                definition_id = str(original_after.get("definition_id") or original.get("rule_id") or "")
                if not binding_id or not definition_id:
                    raise NativeRuleLifecycleError("rule_undo_snapshot_incomplete")
                binding = next((item for item in self.store.list_bindings(definition_id=definition_id) if item.binding_id == binding_id), None)
                if binding is None:
                    raise NativeRuleLifecycleError("rule_undo_binding_missing")
                if binding.status != "inactive":
                    self.store.upsert_binding({**binding.to_dict(), "status": "inactive", "revision": binding.revision + 1, "updated_at": now})
                active = self.store.list_bindings(definition_id=definition_id, status="active")
                definition = self.store.get_definition(definition_id)
                if definition is not None and not active and definition.status != "inactive":
                    self.store.upsert_definition(replace(definition, status="inactive", revision=definition.revision + 1, updated_at=now))
                compensation.update({"definition_id": definition_id, "binding_id": binding_id, "binding_status": "inactive", "definition_status": "active" if active else "inactive"})
            elif action == "rule_feedback":
                feedback_id = str(original_after.get("feedback_id") or "")
                receipt_id = str(original_after.get("receipt_id") or "")
                definition_id = str(original_after.get("definition_id") or original.get("rule_id") or "")
                if not feedback_id or not receipt_id or not definition_id:
                    raise NativeRuleLifecycleError("rule_undo_snapshot_incomplete")
                contribution_id = stable_digest(("native-v2-feedback-evidence", feedback_id))
                contribution = next(
                    (
                        item for item in self.store.list_evidence_contributions()
                        if str(item.get("contribution_id") or "") == contribution_id
                        or str(item.get("feedback_id") or "") == feedback_id
                    ),
                    None,
                )
                if contribution is not None:
                    contribution_id = str(contribution.get("contribution_id") or contribution_id)
                    self.store.deactivate_evidence_contribution(contribution_id, updated_at=now)
                    self.store.rebuild_evidence_effective(
                        definition_id=str(contribution.get("definition_id") or definition_id),
                        independence_key=str(contribution.get("independence_key") or ""),
                        kind=str(contribution.get("kind") or "feedback"),
                        updated_at=now,
                    )
                compensating_id = stable_digest(("native-v2-feedback-compensation", feedback_id, new_decision_id))
                self.store.record_feedback({
                    "feedback_id": compensating_id,
                    "receipt_id": receipt_id,
                    "definition_id": definition_id,
                    "outcome": "ignored",
                    "authority": feedback_authority("user" if trusted.admin else "agent"),
                    "evidence_digest": "",
                    "metadata_json": _json({"compensates_feedback_id": feedback_id, "original_decision_id": str(original.get("decision_id") or "")}),
                    "created_at": now,
                })
                self.store.rebuild_effective_feedback_projection(receipt_id, updated_at=now)
                compensation.update({"feedback_id": feedback_id, "compensating_feedback_id": compensating_id, "receipt_id": receipt_id, "definition_id": definition_id})
            else:
                raise NativeRuleLifecycleError("rule_undo_action_unsupported")
            self.store.record_decision({
                "decision_id": new_decision_id,
                "actor": trusted.agent,
                "owner_agent_id": trusted.agent,
                "rule_id": str(original.get("rule_id") or ""),
                "action": "rule_undo",
                "before_hash": stable_digest(original_after),
                "after_hash": stable_digest(compensation),
                "before_json": _json(original_after),
                "after_json": _json(compensation),
                "reason": "native V2 compensating undo",
                "confidence": 1.0,
                "undo_id": stable_digest(("native-v2-rule-undo-compensation", new_decision_id)),
                "target_ids_json": _json(_list(original.get("target_ids_json"))),
                "metadata_json": _json({"idempotency_key": key, "compensates": str(original.get("decision_id") or "")}),
                "source_ref": "native-v2:mcp:rule_undo",
                "created_at": now,
            })
            self._record_fence(key=key, fingerprint=fingerprint, context=trusted, decision_id=new_decision_id, memory_id=str(original.get("rule_id") or ""))
            self._settle_canonical(trusted.group)
        decision = self.store.get_decision(new_decision_id) or {"decision_id": new_decision_id}
        return self._decision_result(decision, compensation=compensation)

    def _resolve_definition_id(self, value: str, group: str) -> str:
        candidate = str(value or "").strip()
        if not candidate:
            raise NativeRuleLifecycleError("parent_rule_required")
        if self.store.get_definition(candidate) is not None:
            return candidate
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT canonical_definition_id FROM rule_source_links "
                "WHERE share_group_id=? AND (memory_id=? OR canonical_definition_id=?) "
                "ORDER BY updated_at DESC LIMIT 1",
                (group, candidate, candidate),
            ).fetchone()
        definition_id = str(row[0] or "") if row is not None else ""
        if not definition_id or self.store.get_definition(definition_id) is None:
            raise NativeRuleLifecycleError("parent_rule_not_found")
        return definition_id

    def create_exception(self, payload: Mapping[str, Any], *, context: Any) -> dict[str, Any]:
        authority, trusted = self._context(context, automatic=False)
        parent_value = str(payload.get("parent_rule") or payload.get("parent_rule_id") or "").strip()
        child_text = str(payload.get("child_rule") or payload.get("child_exception") or payload.get("text") or "").strip()
        if not child_text:
            raise NativeRuleLifecycleError("child_exception_required")
        try:
            priority = int(payload.get("priority", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise NativeRuleLifecycleError("invalid_rule_priority") from exc
        if priority < -100 or priority > 100:
            raise NativeRuleLifecycleError("invalid_rule_priority")
        reason = str(payload.get("reason") or "native V2 rule exception").strip()[:1024]
        parent_id = self._resolve_definition_id(parent_value, trusted.group)
        parent = self.store.get_definition(parent_id)
        if parent is None or parent.status != "active":
            raise NativeRuleLifecycleError("parent_rule_not_active")
        parent_bindings = self.store.list_bindings(
            definition_id=parent_id,
            share_group_id=trusted.group,
            status="active",
        )
        try:
            plan = plan_rule_exception_scope(
                parent_bindings,
                agent_instance_id=trusted.agent,
                project_ref=trusted.project,
            )
        except RuleExceptionPlanError as exc:
            raise NativeRuleLifecycleError(exc.code) from exc

        clean_payload = {
            "parent_rule_id": parent_id,
            "child_text_digest": _digest_text(child_text),
            "priority": priority,
            "reason_digest": _digest_text(reason),
        }
        key, fingerprint, replay = self._replay("rule_exception_create", clean_payload, trusted)
        if replay is not None:
            self._settle_canonical(trusted.group)
            decision = replay.get("decision") or {}
            try:
                after = _object(decision.get("after_json"))
            except Exception:
                after = {}
            return {**replay, "exception_id": str(after.get("exception_id") or ""), "child_definition_id": str(after.get("child_definition_id") or "")}

        now = _now()
        child = build_definition(child_text, kind=parent.rule_kind, created_at=now)
        if child.definition_id == parent_id:
            raise NativeRuleLifecycleError("exception_cannot_equal_parent")
        child_binding_id = stable_digest((
            "native-v2-rule-exception-child", child.definition_id, trusted.group,
            plan.child_binding["target_type"], plan.child_binding["target_id"],
            plan.child_binding["project_ref"], priority,
        ))
        exclude_binding_id = stable_digest((
            "native-v2-rule-exception-parent-exclude", parent_id, trusted.group,
            plan.parent_exclude["target_type"], plan.parent_exclude["target_id"],
            plan.parent_exclude["project_ref"],
        )) if plan.exclude_required else ""
        exception_id = stable_digest((
            "native-v2-rule-exception", parent_id, child.definition_id,
            trusted.group, trusted.agent, trusted.project,
        ))
        decision_id = stable_digest(("native-v2-rule-exception-decision", trusted.group, key, fingerprint))
        undo_id = stable_digest(("native-v2-rule-exception-undo", decision_id))
        evidence_id = stable_digest(("native-v2-rule-exception-evidence", exception_id, _digest_text(child_text)))
        source_ref = f"native-v2:rule-exception:{exception_id}"

        previous_child = self.store.get_definition(child.definition_id)
        previous_child_binding = next(
            (item for item in self.store.list_bindings(definition_id=child.definition_id, share_group_id=trusted.group) if item.binding_id == child_binding_id),
            None,
        )
        previous_exclude = next(
            (item for item in parent_bindings if item.binding_id == exclude_binding_id),
            None,
        ) if exclude_binding_id else None
        before = {
            "child_definition_status": previous_child.status if previous_child else "missing",
            "child_binding_status": previous_child_binding.status if previous_child_binding else "missing",
            "parent_exclude_status": previous_exclude.status if previous_exclude else "missing",
        }
        rollback = {
            "child_definition_id": child.definition_id,
            "child_binding_id": child_binding_id,
            "child_definition_previous_status": before["child_definition_status"],
            "child_binding_previous_status": before["child_binding_status"],
            "parent_exclude_binding_id": exclude_binding_id,
            "parent_exclude_previous_status": before["parent_exclude_status"],
            "generated_parent_exclude": bool(plan.exclude_required),
        }
        with self.store.transaction():
            persisted_child = self.store.upsert_definition(child)
            child_binding = self.store.upsert_binding({
                "binding_id": child_binding_id,
                "definition_id": persisted_child.definition_id,
                "share_group_id": trusted.group,
                **dict(plan.child_binding),
                "priority": priority,
                "owner_agent_id": trusted.agent,
                "created_by": "admin" if trusted.admin else "manual",
                "authorization": f"native:{authority.session_source}:{authority.session_id}",
                "status": "active",
                "revision": previous_child_binding.revision + 1 if previous_child_binding and previous_child_binding.status != "active" else (previous_child_binding.revision if previous_child_binding else 1),
                "created_at": previous_child_binding.created_at if previous_child_binding else now,
                "updated_at": now,
            })
            parent_exclude = None
            if plan.exclude_required:
                parent_exclude = self.store.upsert_binding({
                    "binding_id": exclude_binding_id,
                    "definition_id": parent_id,
                    "share_group_id": trusted.group,
                    **dict(plan.parent_exclude),
                    "priority": priority,
                    "owner_agent_id": trusted.agent,
                    "created_by": "admin" if trusted.admin else "manual",
                    "authorization": f"native:{authority.session_source}:{authority.session_id}",
                    "status": "active",
                    "revision": previous_exclude.revision + 1 if previous_exclude and previous_exclude.status != "active" else (previous_exclude.revision if previous_exclude else 1),
                    "created_at": previous_exclude.created_at if previous_exclude else now,
                    "updated_at": now,
                })
            self.store.upsert_exception({
                "exception_id": exception_id,
                "parent_rule_id": parent_id,
                "child_exception_id": child.definition_id,
                "parent_rule": parent.canonical_text,
                "child_exception": child.canonical_text,
                "priority": priority,
                "reason": reason,
                "rollback_json": _json(rollback),
                "active": 1,
                "source_ref": source_ref,
                "created_at": now,
                "updated_at": now,
            })
            after = {
                "exception_id": exception_id,
                "parent_rule_id": parent_id,
                "child_definition_id": child.definition_id,
                "child_binding_id": child_binding.binding_id,
                "parent_exclude_binding_id": parent_exclude.binding_id if parent_exclude else "",
                "scope": plan.to_dict(),
            }
            self.store.record_decision({
                "decision_id": decision_id,
                "actor": trusted.agent,
                "owner_agent_id": trusted.agent,
                "rule_id": parent_id,
                "action": "rule_exception_create",
                "before_hash": stable_digest(before),
                "after_hash": stable_digest(after),
                "before_json": _json(before),
                "after_json": _json(after),
                "reason": reason,
                "confidence": 1.0,
                "undo_id": undo_id,
                "target_ids_json": _json([exception_id, child.definition_id, child_binding_id, exclude_binding_id]),
                "metadata_json": _json({"idempotency_key": key}),
                "source_ref": source_ref,
                "created_at": now,
            })
            self.store.append_evidence_outbox({
                "event_id": stable_digest(("rule-exception-evidence-event", exception_id)),
                "migration_id": "native-v2",
                "evidence_id": evidence_id,
                "definition_id": child.definition_id,
                "evidence_ref": source_ref,
                "content_digest": _digest_text(child_text),
                "polarity": "positive",
                "source_kind": "rule_exception",
                "source_group_id": trusted.group,
                "payload_json": _json({"exception_id": exception_id, "parent_rule_id": parent_id}),
                "created_at": now,
                "consumed_at": now,
            })
            self.store.append_domain_outbox({
                "event_id": stable_digest(("rule-exception-domain-event", exception_id)),
                "migration_id": "native-v2",
                "event_type": "rule_exception_created",
                "source_kind": "rule_exception",
                "source_group_id": trusted.group,
                "source_ref": source_ref,
                "payload_digest": stable_digest(after),
                "payload_json": _json({"exception_id": exception_id, "definition_id": child.definition_id}),
                "created_at": now,
                "consumed_at": now,
            })
            source_id = "exception:" + exception_id
            self.store.upsert_source_link(
                source_kind="rule_exception",
                share_group_id=trusted.group,
                memory_id=source_id,
                source_ref=source_ref,
                original_definition_id=child.definition_id,
                canonical_definition_id=child.definition_id,
                status="active",
                created_at=now,
                updated_at=now,
            )
            self.store.record_evidence_ref({
                "evidence_id": evidence_id,
                "definition_id": child.definition_id,
                "source_rule_id": source_id,
                "share_group_id": trusted.group,
                "evidence_ref": source_ref,
                "content_digest": child.semantic_hash,
                "authority": "governed-lifecycle",
                "status": "active",
                "created_at": now,
            })
            self._record_fence(key=key, fingerprint=fingerprint, context=trusted, decision_id=decision_id, memory_id=parent_id)
            self._settle_canonical(trusted.group)
        decision = self.store.get_decision(decision_id) or {"decision_id": decision_id}
        return self._decision_result(
            decision,
            exception_id=exception_id,
            child_definition_id=child.definition_id,
            child_binding_id=child_binding_id,
            parent_exclude_binding_id=exclude_binding_id,
            undo_id=undo_id,
        )

    def revoke_exception(self, payload: Mapping[str, Any], *, context: Any) -> dict[str, Any]:
        _authority, trusted = self._context(context, automatic=False)
        exception_id = str(payload.get("exception_id") or "").strip()
        if not exception_id:
            raise NativeRuleLifecycleError("exception_id_required")
        clean_payload = {"exception_id": exception_id}
        key, fingerprint, replay = self._replay("rule_exception_revoke", clean_payload, trusted)
        if replay is not None:
            self._settle_canonical(trusted.group)
            return {**replay, "exception_id": exception_id}
        with self.store.transaction() as conn:
            row = conn.execute("SELECT * FROM rule_exceptions WHERE exception_id=?", (exception_id,)).fetchone()
        if row is None:
            raise NativeRuleLifecycleError("rule_exception_not_found")
        item = {str(key): row[key] for key in row.keys()}
        if int(item.get("active") or 0) != 1:
            raise NativeRuleLifecycleError("rule_exception_not_active")
        parent_id = str(item.get("parent_rule_id") or "")
        allowed = self.store.list_bindings(definition_id=parent_id, share_group_id=trusted.group)
        if not allowed:
            raise NativeRuleLifecycleError("rule_exception_scope_mismatch")
        try:
            rollback = _object(item.get("rollback_json"))
        except Exception as exc:
            raise NativeRuleLifecycleError("rule_exception_rollback_corrupt") from exc
        child_definition_id = str(rollback.get("child_definition_id") or item.get("child_exception_id") or "")
        child_binding_id = str(rollback.get("child_binding_id") or "")
        exclude_binding_id = str(rollback.get("parent_exclude_binding_id") or "")
        now = _now()
        decision_id = stable_digest(("native-v2-rule-exception-revoke", trusted.group, key, fingerprint))
        after = {
            "exception_id": exception_id,
            "child_binding_status": "inactive",
            "parent_exclude_status": "inactive" if bool(rollback.get("generated_parent_exclude")) else "preserved",
        }
        with self.store.transaction() as conn:
            child_binding = next((item for item in self.store.list_bindings(definition_id=child_definition_id) if item.binding_id == child_binding_id), None)
            if child_binding is not None and child_binding.status == "active":
                self.store.upsert_binding({**child_binding.to_dict(), "status": "inactive", "revision": child_binding.revision + 1, "updated_at": now})
            if bool(rollback.get("generated_parent_exclude")) and exclude_binding_id:
                exclude = next((item for item in self.store.list_bindings(definition_id=parent_id) if item.binding_id == exclude_binding_id), None)
                if exclude is not None and exclude.status == "active":
                    self.store.upsert_binding({**exclude.to_dict(), "status": "inactive", "revision": exclude.revision + 1, "updated_at": now})
            child_def = self.store.get_definition(child_definition_id)
            if child_def is not None and not self.store.list_bindings(definition_id=child_definition_id, status="active"):
                self.store.upsert_definition(replace(child_def, status="inactive", revision=child_def.revision + 1, updated_at=now))
            conn.execute("UPDATE rule_exceptions SET active=0,updated_at=? WHERE exception_id=? AND active=1", (now, exception_id))
            self.store.record_decision({
                "decision_id": decision_id,
                "actor": trusted.agent,
                "owner_agent_id": trusted.agent,
                "rule_id": parent_id,
                "action": "rule_exception_revoke",
                "before_hash": stable_digest(item),
                "after_hash": stable_digest(after),
                "before_json": _json({"exception_id": exception_id, "active": 1}),
                "after_json": _json(after),
                "reason": "native V2 rule exception revoke",
                "confidence": 1.0,
                "undo_id": stable_digest(("native-v2-rule-exception-revoke-undo", decision_id)),
                "target_ids_json": _json([exception_id, child_binding_id, exclude_binding_id]),
                "metadata_json": _json({"idempotency_key": key}),
                "source_ref": f"native-v2:rule-exception:{exception_id}",
                "created_at": now,
            })
            self.store.append_domain_outbox({
                "event_id": stable_digest(("rule-exception-revoked-event", exception_id, decision_id)),
                "migration_id": "native-v2",
                "event_type": "rule_exception_revoked",
                "source_kind": "rule_exception",
                "source_group_id": trusted.group,
                "source_ref": f"native-v2:rule-exception:{exception_id}",
                "payload_digest": stable_digest(after),
                "payload_json": _json({"exception_id": exception_id}),
                "created_at": now,
                "consumed_at": now,
            })
            self._record_fence(key=key, fingerprint=fingerprint, context=trusted, decision_id=decision_id, memory_id=parent_id)
            self._settle_canonical(trusted.group)
        decision = self.store.get_decision(decision_id) or {"decision_id": decision_id}
        return self._decision_result(decision, exception_id=exception_id, revoked=True)

    def decision_read(self, payload: Mapping[str, Any], *, context: Any) -> dict[str, Any]:
        _authority, trusted = self._context(context, automatic=False)
        decision_id = str(payload.get("decision_id") or "").strip()
        if not decision_id:
            raise NativeRuleLifecycleError("decision_id_required")
        decision = self.store.get_decision(decision_id)
        if decision is None:
            raise NativeRuleLifecycleError("rule_decision_not_found")
        owner = str(decision.get("owner_agent_id") or decision.get("actor") or "")
        if owner and owner != trusted.agent and not trusted.admin:
            raise NativeRuleLifecycleError("rule_decision_owner_mismatch")
        return {"decision": decision}

    def scope_stats(self, payload: Mapping[str, Any], *, context: Any) -> dict[str, Any]:
        del payload
        _authority, trusted = self._context(context, automatic=False)
        bindings = self.store.list_bindings(share_group_id=trusted.group)
        visible = [item for item in bindings if item.owner_agent_id in {"", trusted.agent} or trusted.admin]
        by_target: dict[str, int] = {}
        for item in visible:
            by_target[item.target_type] = by_target.get(item.target_type, 0) + 1
        return {
            "share_group_id": trusted.group,
            "agent_instance_id": trusted.agent,
            "active": sum(1 for item in visible if item.status == "active"),
            "inactive": sum(1 for item in visible if item.status != "active"),
            "by_target_type": by_target,
            "automatic_scope_policy": ["agent", "agent_project"],
        }

    def dispatch(self, operation: str, payload: Mapping[str, Any] | None = None, *, context: Any = None, **_: Any) -> dict[str, Any]:
        handlers = {
            "rule_create_auto": self.create_auto,
            "memoryguard_rule_create_auto": self.create_auto,
            "rule_feedback": self.feedback,
            "memoryguard_rule_feedback": self.feedback,
            "rule_undo": self.undo,
            "memoryguard_rule_undo": self.undo,
            "rule_decision_read": self.decision_read,
            "memoryguard_rule_decision_read": self.decision_read,
            "rule_scope_stats": self.scope_stats,
            "memoryguard_rule_scope_stats": self.scope_stats,
            "rule_exception_create": self.create_exception,
            "rule_exception_revoke": self.revoke_exception,
        }
        handler = handlers.get(str(operation or ""))
        if handler is None:
            return {"ok": False, "status": "error", "code": "unknown_rule_lifecycle_operation"}
        try:
            return handler(dict(payload or {}), context=context)
        except NativeRuleLifecycleError as exc:
            return {"ok": False, "status": "error", "code": exc.code}
        except (RuleAuthorizationError, ValueError):
            # Value text can contain paths or source details; collapse any
            # unclassified validation failure at this boundary.
            return {"ok": False, "status": "error", "code": "rule_lifecycle_validation_failed"}

    call = dispatch


__all__ = ["NativeRuleLifecycleError", "NativeRuleLifecycleService"]
