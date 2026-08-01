"""Lifecycle service for conservatively-created mandatory rules.

The existing memory write path is intentionally broad (it supports all
audience types for an administrator).  This module is the narrow, audited
entry point used by the automatic rule cockpit: inference may only select the
trusted current Agent and, when available, that Agent's current project.
Broader audiences require an explicit manual request and an administrator.

The implementation is deliberately compatible with the pre-lifecycle store
API.  Newer stores may expose additional rule metadata methods; all calls are
feature-detected and the stable assignment/decision/snapshot interfaces are
used as the fallback.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .governance_engine import GovernanceEngine
from .rule_scope import TARGET_TYPES, canonical_project_ref, normalize_assignment
from .security import FEEDBACK_AUTHORITY
try:  # rolling upgrades: the narrower validator may land after this module
    from .rule_scope import validate_automatic_assignment
except ImportError:  # pragma: no cover - exercised only by pre-lifecycle stores
    def validate_automatic_assignment(value: dict | Any, *, actor_agent_id: str = ""):
        assignment = normalize_assignment(value)
        if assignment.target_type not in AUTO_TARGET_TYPES:
            raise ValueError(
                "automatic assignment cannot broaden target_type; allowed target types are agent and agent_project"
            )
        if not actor_agent_id or assignment.target_id != actor_agent_id:
            raise ValueError("automatic assignment cannot target another agent")
        return assignment
from .schema_v3 import (
    DecisionEvent,
    EffectiveAgentContext,
    MemoryKind,
    MemoryEvent,
    Provenance,
    RuleAssignment,
    RuleMatchFeedback,
    RuleDecision,
    RuleException,
    SharedMemoryRecord,
    SharedMemoryStatus,
    _now_iso,
    stable_hash,
)


AUTO_TARGET_TYPES = frozenset({"agent", "agent_project"})
# v2: only ``not_applicable`` is scope-error evidence.  ``violated`` means the
# rule applied but the agent did not follow it -- that is an execution failure,
# not a proof the scope was too wide.  ``corrected`` needs an explicit
# correction_type before it can drive narrowing.
NARROWING_OUTCOMES = frozenset({"not_applicable"})
FEEDBACK_OUTCOMES = frozenset({
    "followed", "violated", "not_applicable", "corrected", "exception", "ignored",
    "unobserved",
})
# A single feedback event must never change scope.  At least 3 explicit
# scope-error events from at least 2 distinct sessions concentrated in the
# same project are required before automatic narrowing may proceed.
NARROWING_MIN_EVENTS = 3
NARROWING_MIN_SESSIONS = 2
# High-confidence ``followed`` evidence cancels narrowing evidence.
# One effective, high-confidence ``followed`` observation in the same
# agent/project scope is enough to cancel automatic narrowing.  A single
# positive observation is already evidence that the rule may be applicable;
# requiring two would permit a false narrowing after one counterexample.
NARROWING_OPPOSED_THRESHOLD = 1
FEEDBACK_PRODUCER_AUTHORITY = FEEDBACK_AUTHORITY


class RuleCreationService:
    """Create and evolve mandatory rules under a trusted Agent context."""

    def __init__(
        self,
        workspace: str | Path,
        share_group_id: str | None = None,
        *,
        store: Any | None = None,
        engine: GovernanceEngine | None = None,
        is_admin: bool = False,
    ) -> None:
        if not isinstance(workspace, (str, Path)) and hasattr(workspace, "workspace"):
            # Small in-process callers often pass an already-open store as the
            # first positional argument; keep that ergonomic form compatible.
            if store is None:
                store = workspace
            if share_group_id is None:
                share_group_id = getattr(workspace, "group_id", None)
            workspace = getattr(workspace, "workspace")
        self.workspace = Path(workspace).expanduser().resolve()
        self.group_id = str(share_group_id or "default")
        self.store = store or (engine.store if engine is not None else None)
        self.engine = engine or GovernanceEngine(
            self.workspace, self.group_id, store=self.store,
        )
        self.store = self.engine.store
        self.is_admin = bool(is_admin)

    # ------------------------------------------------------------------
    # Context / scope
    # ------------------------------------------------------------------

    @staticmethod
    def _context_dict(context: EffectiveAgentContext | Mapping[str, Any]) -> dict[str, str]:
        if isinstance(context, EffectiveAgentContext):
            return {
                "agent_instance_id": str(context.agent_instance_id or "").strip(),
                "share_group_id": str(context.share_group_id or "").strip(),
                "provider": str(context.provider or "").strip(),
                "project_ref": canonical_project_ref(context.project_ref),
                "runtime_role": str(context.runtime_role or "").strip(),
                "runtime_agent_id": str(context.runtime_agent_id or "").strip(),
                "parent_agent_id": str(context.parent_agent_id or "").strip(),
                "session_id": str(context.session_id or "").strip(),
                "context_hash": str(context.context_hash or "").strip(),
            }
        if isinstance(context, Mapping):
            return {
                "agent_instance_id": str(context.get("agent_instance_id", "") or "").strip(),
                "share_group_id": str(context.get("share_group_id", "") or "").strip(),
                "provider": str(context.get("provider", "") or "").strip(),
                "project_ref": canonical_project_ref(context.get("project_ref", "")),
                "runtime_role": str(context.get("runtime_role", "") or "").strip(),
                "runtime_agent_id": str(context.get("runtime_agent_id", "") or "").strip(),
                "parent_agent_id": str(context.get("parent_agent_id", "") or "").strip(),
                "session_id": str(context.get("session_id", "") or "").strip(),
                "context_hash": str(context.get("context_hash", "") or "").strip(),
            }
        raise ValueError("effective agent context is required")

    def _assignment_hash_for(
        self, memory_id: str, assignments: list[Mapping[str, Any]],
    ) -> str:
        """Hash a proposed assignment revision using Store canonicalization."""
        normalize = getattr(self.store, "_normalize_assignments", None)
        hash_fn = getattr(self.store, "_assignment_hash", None)
        if callable(normalize) and callable(hash_fn):
            try:
                normalized = normalize(memory_id, list(assignments), automatic=False)
                return str(hash_fn(normalized) or "")
            except (TypeError, ValueError, RuntimeError):
                pass
        return stable_hash(
            "rule-assignments",
            json.dumps(list(assignments), ensure_ascii=False, sort_keys=True),
        )

    def _blocked(
        self,
        *,
        action: str,
        reason: str,
        context: Mapping[str, str] | None = None,
        **extra: Any,
    ) -> RuleDecision:
        now = _now_iso()
        decision_id = stable_hash("rule-decision", self.group_id, action, reason, now)
        event = DecisionEvent(
            event_id=decision_id,
            actor=f"agent:{(context or {}).get('agent_instance_id', '') or 'unknown'}",
            action=action,
            target_ids=[str(item) for item in extra.get("target_ids", []) if item],
            reason=reason,
            created_at=now,
        )
        try:
            self.store.append_decision(event)
        except Exception:
            # A validation failure must remain observable even when a read-only
            # compatibility store cannot persist an audit event.
            pass
        assignments = [
            item.to_dict() if hasattr(item, "to_dict") else dict(item)
            for item in extra.get("assignments", [])
        ]
        memory_id = str(extra.get("memory_id", "") or "")
        after = {
            "blocked_reason": reason,
            "body": str(extra.get("body", "") or ""),
            "version_id": str(extra.get("version_id", "") or ""),
            "feedback_id": str(extra.get("feedback_id", "") or ""),
            "receipt_id": str(extra.get("receipt_id", "") or ""),
            "child_rule_id": str(extra.get("child_rule_id", "") or ""),
        }
        decision = RuleDecision(
            decision_id=decision_id,
            actor=f"agent:{(context or {}).get('agent_instance_id', '') or 'unknown'}",
            before={}, after=after, reason=reason,
            confidence=1.0,
            undo_id=str(extra.get("undo_id", "") or ""),
            created_at=now,
            rule_id=memory_id,
            action=action,
            target_ids=[str(item) for item in extra.get("target_ids", []) if item],
            status="blocked",
            memory_id=memory_id,
            parent_rule_id=str(extra.get("parent_rule_id", "") or ""),
            kind=str(extra.get("kind", "") or ""),
            assignments=assignments,
            target_type=str(extra.get("target_type", "") or ""),
            target_id=str(extra.get("target_id", "") or ""),
            project_ref=str(extra.get("project_ref", "") or ""),
            scope_confidence=float(extra.get("scope_confidence", 0.0) or 0.0),
            scope_reason=reason,
            blocked_reason=reason,
            owner_agent_id=str((context or {}).get("agent_instance_id", "") or ""),
        )
        self._persist_structured_decision(decision)
        return decision

    def _persist_structured_decision(self, decision: RuleDecision) -> RuleDecision:
        """Use the lifecycle store model when available; stay compatible with v3.2."""
        append = getattr(self.store, "append_rule_decision", None)
        if callable(append):
            # A successful lifecycle mutation must have a durable structured
            # decision.  Do not swallow persistence failures: callers must
            # fail closed (and atomic store APIs will roll back the mutation).
            return append(decision)
        return decision

    @staticmethod
    def _scope_values(
        context: Mapping[str, str],
        requested_scope: Mapping[str, Any] | Any | None,
        *,
        manual: bool,
        is_admin: bool,
        text: str = "",
    ) -> tuple[dict[str, Any], float, str]:
        """Resolve and validate one assignment without widening authority."""
        agent_id = context["agent_instance_id"]
        project_ref = context["project_ref"]
        if not agent_id:
            raise ValueError("trusted agent_instance_id is required")

        if requested_scope is None:
            # P1: infer the scope from the rule text, not a fixed table.  Only
            # the trusted current agent / agent+project may be selected; broad
            # or ambiguous text falls back to the narrowest trusted scope with
            # a lowered confidence (fallback_used) instead of a confident claim.
            from .rule_scope import infer_scope_from_text
            inference = infer_scope_from_text(
                text, agent_instance_id=agent_id, project_ref=project_ref,
            )
            selected = inference.selected
            target_type = selected.target_type
            target_id = selected.target_id
            selected_project = selected.project_ref if target_type == "agent_project" else ""
            confidence = float(selected.confidence)
            reason = "; ".join(selected.reasons) if selected.reasons else "auto-scope from trusted context"
            if inference.fallback_used:
                reason += " [auto-scope fallback; human confirmation recommended]"
            inferred = {
                "target_type": target_type,
                "target_id": target_id,
                "project_ref": selected_project,
                "effect": "include",
            }
            validate_automatic_assignment(inferred, actor_agent_id=agent_id)
            return inferred, confidence, reason

        if hasattr(requested_scope, "to_dict"):
            raw = requested_scope.to_dict()
        elif isinstance(requested_scope, Mapping):
            raw = dict(requested_scope)
        else:
            raise ValueError("scope must be an object")
        target_type = str(raw.get("target_type", "") or "").strip()
        target_id = str(raw.get("target_id", "") or "").strip()
        selected_project = canonical_project_ref(
            raw.get("project_ref", "")
            or (target_id if target_type == "project" else "")
        )

        if not manual:
            if target_type not in AUTO_TARGET_TYPES:
                raise ValueError(
                    f"automatic rule scope cannot target {target_type or 'unknown'}; "
                    "only agent or agent_project is allowed"
                )
            if target_id != agent_id:
                raise ValueError("automatic rule scope must target the trusted current agent")
            if target_type == "agent_project":
                if not project_ref or selected_project != project_ref:
                    raise ValueError(
                        "automatic agent_project scope must match the trusted current project"
                    )
            elif selected_project:
                raise ValueError("agent scope cannot carry project_ref; use agent_project")
            confidence = 0.94 if target_type == "agent_project" else 0.98
            reason = "explicit self scope accepted within automatic narrow-scope policy"
        else:
            if target_type not in TARGET_TYPES:
                raise ValueError("invalid audience target_type")
            if target_type in {"agent", "agent_project"}:
                if target_id != agent_id and not is_admin:
                    raise ValueError("admin capability required for another agent scope")
            elif not is_admin:
                raise ValueError(
                    "admin capability required for manual system/group/provider/runtime scope"
                )
            confidence = 1.0
            reason = "explicit human-admin scope declaration"

        assignment = {
            "target_type": target_type,
            "target_id": "" if target_type == "project" else target_id,
            "project_ref": selected_project if target_type in {"project", "agent_project"} else "",
            "effect": str(raw.get("effect", "include") or "include"),
        }
        if not manual and assignment["effect"] != "include":
            raise ValueError("automatic rule scope must be an include assignment")
        # Let the canonical store validator provide the final shape/error.
        if not manual:
            validate_automatic_assignment(
                {"memory_id": "pending", **assignment}, actor_agent_id=agent_id,
            )
        else:
            normalize_assignment({"memory_id": "pending", **assignment})
        return assignment, confidence, reason

    def _decision(
        self,
        *,
        action: str,
        status: str,
        result: Mapping[str, Any] | None = None,
        scope: Mapping[str, Any] | None = None,
        scope_confidence: float = 0.0,
        scope_reason: str = "",
        undo_id: str = "",
        parent_rule_id: str = "",
        feedback_id: str = "",
        receipt_id: str = "",
        child_rule_id: str = "",
        blocked_reason: str = "",
        metadata: Mapping[str, Any] | None = None,
        actor: str = "agent:unknown",
        owner_agent_id: str = "",
        before: Any | None = None,
        persist: bool = True,
    ) -> RuleDecision:
        result = result or {}
        record = result.get("record") or result.get("after") or {}
        assignments = result.get("assignments") or []
        if not assignments and scope:
            assignments = [dict(scope)]
        target = assignments[0] if assignments else (dict(scope) if scope else {})
        status_value = status
        decision_id = str(result.get("decision_id", "") or "")
        if not decision_id:
            decision_id = stable_hash(
                "rule-decision", self.group_id, action,
                str(record.get("memory_id", "")), _now_iso(),
            )
        memory_id = str(result.get("memory_id", "") or record.get("memory_id", ""))
        assignments_payload = [
            item.to_dict() if hasattr(item, "to_dict") else dict(item)
            for item in assignments
        ]
        target_type = str(target.get("target_type", "") or "")
        target_id = str(target.get("target_id", "") or "")
        project_ref = str(target.get("project_ref", "") or "")
        decision_confidence = float(scope_confidence) if scope_confidence is not None else 1.0
        after = dict(record)
        after.update({
            "assignments": assignments_payload,
            "version_id": str(result.get("version_id", "") or ""),
            "feedback_id": feedback_id,
            "receipt_id": receipt_id,
            "child_rule_id": child_rule_id,
            "metadata": dict(metadata or {}),
        })
        reason_payload = {
            "scope_reason": scope_reason,
            "scope_confidence": decision_confidence,
            "assignment": target,
            "parent_rule_id": parent_rule_id,
            **dict(metadata or {}),
        }
        decision = RuleDecision(
            decision_id=decision_id,
            actor=actor or "agent:unknown",
            before={} if before is None else before, after=after,
            reason=json.dumps(reason_payload, ensure_ascii=False, separators=(",", ":")),
            confidence=max(0.0, min(1.0, decision_confidence)),
            undo_id=undo_id,
            created_at=_now_iso(),
            rule_id=memory_id,
            action=action,
            status=status_value,
            memory_id=memory_id,
            parent_rule_id=parent_rule_id,
            kind=str(result.get("kind", "") or record.get("kind", "")),
            assignments=assignments_payload,
            target_type=target_type,
            target_id=target_id,
            project_ref=project_ref,
            scope_confidence=decision_confidence,
            scope_reason=scope_reason,
            blocked_reason=blocked_reason or str(result.get("blocked_reason", "") or ""),
            metadata=dict(metadata or {}),
            owner_agent_id=(
                str(owner_agent_id or "").strip()
                or (str(actor).removeprefix("agent:") if str(actor).startswith("agent:") else "")
            ),
        )
        return self._persist_structured_decision(decision) if persist else decision

    # ------------------------------------------------------------------
    # Creation / undo / decision read
    # ------------------------------------------------------------------

    def create_rule_from_text(
        self,
        text: str,
        effective_context: EffectiveAgentContext | Mapping[str, Any],
        *,
        scope: Mapping[str, Any] | Any | None = None,
        requested_scope: Mapping[str, Any] | Any | None = None,
        explicit_scope: Mapping[str, Any] | Any | None = None,
        target_type: str = "",
        target_id: str = "",
        project_ref: str = "",
        manual: bool = False,
        manual_scope: bool | None = None,
        is_admin: bool | None = None,
        kind: str = "",
        kind_override: str = "",
        priority: int = 0,
        idempotency_key: str = "",
        parent_rule_id: str = "",
    ) -> RuleDecision:
        """Classify and persist one mandatory rule with an explainable scope."""
        try:
            context = self._context_dict(effective_context)
        except (TypeError, ValueError) as exc:
            return self._blocked(action="rule_create_auto", reason=str(exc))
        if not context["share_group_id"]:
            return self._blocked(
                action="rule_create_auto",
                reason="trusted share_group_id is required",
                context=context,
            )
        if context["share_group_id"] and context["share_group_id"] != self.group_id:
            return self._blocked(action="rule_create_auto", reason="effective context share_group_id mismatch", context=context)
        body = str(text or "").strip()
        if not body:
            return self._blocked(action="rule_create_auto", reason="text is required", context=context)
        admin = self.is_admin if is_admin is None else bool(is_admin)
        if manual_scope is not None:
            manual = bool(manual_scope)
        requested = requested_scope if requested_scope is not None else scope
        if requested is None:
            requested = explicit_scope
        if requested is None and target_type:
            requested = {
                "target_type": target_type,
                "target_id": target_id,
                "project_ref": project_ref,
                "effect": "include",
            }
        if not kind and kind_override:
            kind = kind_override
        if idempotency_key:
            structured_list = getattr(self.store, "list_rule_decisions", None)
            if callable(structured_list):
                try:
                    for previous in reversed(structured_list()):
                        if previous.action not in {"rule_create_auto", "rule_create_manual"}:
                            continue
                        previous_after = previous.after if isinstance(previous.after, dict) else {}
                        previous_meta = previous_after.get("metadata", {}) if isinstance(previous_after, dict) else {}
                        if previous_meta.get("idempotency_key") == idempotency_key:
                            return previous
                except Exception:
                    pass
        # Keep the complete inference decision in the structured audit record;
        # this is separate from the compact human-facing scope_reason.
        inference_metadata: dict[str, Any] = {}
        if requested is None:
            from .rule_scope import infer_scope_from_text
            inference_metadata["scope_inference"] = infer_scope_from_text(
                body,
                agent_instance_id=context["agent_instance_id"],
                project_ref=context["project_ref"],
            ).to_dict()
        try:
            assignment, confidence, scope_reason = self._scope_values(
                context, requested, manual=bool(manual), is_admin=admin, text=body,
            )
        except (TypeError, ValueError) as exc:
            return self._blocked(
                action="rule_create_auto" if not manual else "rule_create_manual",
                reason=str(exc), context=context,
                target_type=str((requested or {}).get("target_type", "")) if isinstance(requested, Mapping) else "",
                target_id=str((requested or {}).get("target_id", "")) if isinstance(requested, Mapping) else "",
            )

        # A pre-mutation snapshot is the stable, persisted undo token.  Older
        # stores always support this v3 API; a test double may not, so retain a
        # deterministic token and return it as an explainable warning.
        try:
            undo_id = self.store.create_version_snapshot("rule-create:before")
        except Exception:
            undo_id = stable_hash("rule-undo", self.group_id, body, _now_iso())

        now = _now_iso()
        event = MemoryEvent(
            event_id=stable_hash("rule-create-event", self.group_id, context["agent_instance_id"], body, idempotency_key or now),
            agent_instance_id=context["agent_instance_id"],
            share_group_id=self.group_id,
            raw_content=body,
            metadata={
                "rule_creation": "auto" if not manual else "manual",
                "scope_reason": scope_reason,
                "scope_confidence": confidence,
                "parent_rule_id": parent_rule_id,
                "undo_id": undo_id,
                **inference_metadata,
            },
            auto_actions=[],
            created_at=now,
        )
        event.injection_policy = "always"
        event.priority = int(priority)
        event.rule_assignments = [{"memory_id": "pending", **assignment}]
        try:
            result = self.engine.auto_write(
                event,
                kind_override=kind,
                injection_policy="always",
                priority=int(priority),
                rule_assignments=[assignment],
                idempotency_key=idempotency_key,
            )
        except (TypeError, ValueError, RuntimeError) as exc:
            return self._blocked(
                action="rule_create_auto" if not manual else "rule_create_manual",
                reason=str(exc), context=context,
                scope_confidence=confidence, scope_reason=scope_reason,
                undo_id=undo_id, target_type=assignment.get("target_type", ""),
                target_id=assignment.get("target_id", ""),
                project_ref=assignment.get("project_ref", ""),
            )
        if not result.get("ok"):
            return self._blocked(
                action="rule_create_auto" if not manual else "rule_create_manual",
                reason=str(result.get("blocked_reason", "rule creation blocked")),
                context=context, scope_confidence=confidence,
                scope_reason=scope_reason, undo_id=undo_id,
                target_type=assignment.get("target_type", ""),
                target_id=assignment.get("target_id", ""),
                project_ref=assignment.get("project_ref", ""),
            )

        # The atomic Store seam persists the complete lifecycle decision in
        # the same transaction as record/event/assignments.  Return that
        # decision directly; do not append a second decision or risk claiming
        # success after a post-commit write failure.
        persisted_atomic = result.get("decision")
        if persisted_atomic:
            try:
                return RuleDecision.from_dict(
                    persisted_atomic.to_dict()
                    if hasattr(persisted_atomic, "to_dict")
                    else dict(persisted_atomic)
                )
            except (TypeError, ValueError):
                return self._blocked(
                    action="rule_create_auto" if not manual else "rule_create_manual",
                    reason="atomic_rule_decision_decode_failed", context=context,
                    scope_confidence=confidence, scope_reason=scope_reason,
                    undo_id=undo_id,
                    target_type=assignment.get("target_type", ""),
                    target_id=assignment.get("target_id", ""),
                    project_ref=assignment.get("project_ref", ""),
                )

        memory_id = str(result.get("memory_id", "") or (result.get("after") or {}).get("memory_id", ""))
        assignments: list[dict[str, Any]] = []
        if memory_id:
            try:
                assignments = [item.to_dict() for item in self.store.list_rule_assignments(memory_id)]
            except Exception:
                assignments = [dict(assignment)]
        decision_action = "rule_create_auto" if not manual else "rule_create_manual"
        decision_id = stable_hash(
            "rule-decision", self.group_id, decision_action, memory_id, undo_id,
        )
        reason_payload = {
            "scope_confidence": confidence,
            "scope_reason": scope_reason,
            "undo_id": undo_id,
            "parent_rule_id": parent_rule_id,
            "assignment": assignment,
            **inference_metadata,
        }
        try:
            self.store.append_decision(DecisionEvent(
                event_id=decision_id,
                actor=("admin" if manual and admin else f"agent:{context['agent_instance_id']}"),
                action=decision_action,
                target_ids=[item for item in (memory_id, parent_rule_id) if item],
                reason=json.dumps(reason_payload, ensure_ascii=False, separators=(",", ":")),
                created_at=_now_iso(),
            ))
            version_id = self.store.create_version_snapshot(f"{decision_action}:{memory_id}")
        except Exception:
            version_id = str(result.get("version_id", "") or "")
        result = dict(result)
        result["memory_id"] = memory_id
        result["assignments"] = assignments
        result["decision_id"] = decision_id
        result["version_id"] = version_id
        return self._decision(
            action=decision_action, status="created", result=result,
            scope=assignment, scope_confidence=confidence,
            scope_reason=scope_reason, undo_id=undo_id,
            parent_rule_id=parent_rule_id,
            actor=("admin" if manual and admin else f"agent:{context['agent_instance_id']}"),
            before={"undo_id": undo_id, "pre_rule_snapshot": True},
            metadata={
                "manual": bool(manual), "is_admin": admin,
                "idempotency_key": idempotency_key,
                **inference_metadata,
            },
        )

    create_rule = create_rule_from_text

    def _target_undo(
        self,
        decision: RuleDecision,
        *,
        token: str,
        actor: str,
    ) -> RuleDecision | None:
        """Apply one structured inverse; never restore a group snapshot."""
        action = str(getattr(decision, "action", "") or "")
        memory_id = str(decision.memory_id or decision.rule_id or "")
        if not memory_id:
            raise ValueError("structured_decision_required")
        # New atomic writers persist lifecycle metadata on the structured
        # decision itself; older writers nested it under ``after.metadata``.
        # Read both shapes (top-level wins) so revision hashes/provenance are
        # never lost when an inverse is applied.
        nested_metadata = (
            decision.after.get("metadata", {})
            if isinstance(decision.after, dict) else {}
        )
        metadata = dict(nested_metadata) if isinstance(nested_metadata, dict) else {}
        if isinstance(getattr(decision, "metadata", None), dict):
            metadata.update(decision.metadata)
        now = _now_iso()

        def _inverse(status: str = "undone", *, reason: str = "") -> RuleDecision:
            return RuleDecision(
                decision_id=stable_hash("rule-decision-undo", decision.decision_id, actor, now),
                actor=actor,
                before=decision.after,
                after=decision.before,
                reason=reason or f"target-level undo of {decision.decision_id}",
                confidence=decision.confidence,
                undo_id=decision.decision_id,
                created_at=now,
                rule_id=memory_id,
                action="rule_undo",
                target_ids=list(decision.target_ids) or [memory_id],
                status=status,
                memory_id=memory_id,
                parent_rule_id=decision.parent_rule_id,
                scope_confidence=decision.scope_confidence,
                scope_reason="explicit structured inverse",
                blocked_reason="" if status == "undone" else reason,
                metadata={"target_undo": True, "supersedes_token": token, "original_action": action},
                owner_agent_id=(
                    str(actor).removeprefix("agent:")
                    if str(actor).startswith("agent:") else ""
                ),
            )

        if action in {
            "rule_create_auto", "rule_create_manual",
            "rule_propose_auto", "rule_propose_manual",
            "rule_superseded", "rule_conflicted", "rule_quarantined",
        }:
            existing = self.store.get_record(memory_id)
            if existing is None:
                raise ValueError("target_rule_not_found")
            inverse = _inverse()
            metadata = dict(metadata)
            expected_record_hash = str(
                metadata.get("record_revision_hash", "") or ""
            ).strip()
            if not expected_record_hash:
                raise ValueError("structured_inverse_revision_missing")
            mutation_kind = str(metadata.get("mutation_kind", "created") or "created")
            if mutation_kind in {"superseded", "conflicted", "quarantined"}:
                revert_lifecycle = getattr(
                    self.store, "revert_rule_lifecycle_atomic", None,
                )
                if not callable(revert_lifecycle):
                    raise ValueError("atomic_rule_lifecycle_revert_unavailable")
                inverse.metadata.update({
                    "mutation_kind": mutation_kind,
                    "record_revision_hash": expected_record_hash,
                    "old_record_ids": metadata.get("old_record_ids", []),
                    "old_record_hashes": metadata.get("old_record_hashes", {}),
                    "conflict_group_id": metadata.get("conflict_group_id", ""),
                    "quarantine_id": metadata.get("quarantine_id", ""),
                })
                result = revert_lifecycle(
                    decision,
                    expected_record_hash=expected_record_hash,
                    inverse_decision=inverse,
                )
                if isinstance(result, Mapping) and result.get("decision"):
                    persisted = result["decision"]
                    return RuleDecision.from_dict(
                        persisted.to_dict()
                        if hasattr(persisted, "to_dict") else dict(persisted)
                    )
                return inverse
            revert_create = getattr(self.store, "revert_rule_create_atomic", None)
            if not callable(revert_create):
                raise ValueError("atomic_rule_create_revert_unavailable")
            inverse.metadata.update({
                "mutation_kind": mutation_kind,
                "record_revision_hash": expected_record_hash,
                "event_id": str(metadata.get("event_id", "") or ""),
                "added_provenance": metadata.get("added_provenance", []),
            })
            owner = str(
                getattr(decision, "owner_agent_id", "")
                or str(decision.actor or "").removeprefix("agent:")
            )
            result = revert_create(
                memory_id,
                expected_record_hash=expected_record_hash,
                decision=inverse,
                actor_agent_id=owner,
            )
            if isinstance(result, Mapping) and result.get("decision"):
                persisted = result["decision"]
                return RuleDecision.from_dict(
                    persisted.to_dict() if hasattr(persisted, "to_dict") else dict(persisted)
                )
            return inverse
        elif action == "rule_narrow":
            parent_id = str(decision.parent_rule_id or memory_id)
            before_assignments = metadata.get("parent_assignments_before")
            after_assignments = metadata.get("parent_assignments_after")
            if not isinstance(before_assignments, list) or not isinstance(after_assignments, list):
                raise ValueError("structured_narrow_inverse_missing")
            expected_after_hash = str(
                metadata.get("parent_assignments_after_hash", "") or ""
            ).strip()
            if not expected_after_hash:
                raise ValueError("structured_inverse_revision_missing")
            inverse = _inverse()
            apply_narrow = getattr(self.store, "apply_rule_narrow_atomic", None)
            if not callable(apply_narrow):
                raise ValueError("atomic_rule_narrow_revert_unavailable")
            apply_narrow(
                parent_rule_id=parent_id,
                parent_assignments_after=[dict(item) for item in before_assignments],
                expected_parent_assignment_hash=expected_after_hash,
                automatic=True,
                actor_agent_id=str(decision.actor or "").replace("agent:", ""),
                decision=inverse,
            )
            return inverse
        elif action == "rule_exception":
            exception_id = str(metadata.get("exception_id", "") or "")
            if not exception_id:
                child_rule_id = str(decision.child_rule_id or metadata.get("child_rule_id", "") or "")
                if child_rule_id:
                    exception_id = stable_hash("rule-exception", str(decision.parent_rule_id), child_rule_id)
            if not exception_id:
                raise ValueError("structured_exception_inverse_missing")
            after_assignments = metadata.get("parent_assignments_after")
            if not isinstance(after_assignments, list):
                raise ValueError("structured_exception_assignment_delta_missing")
            parent_id = str(decision.parent_rule_id or "")
            expected_after_hash = str(
                metadata.get("parent_assignments_after_hash", "") or ""
            ).strip()
            if not expected_after_hash:
                raise ValueError("structured_inverse_revision_missing")
            inverse = _inverse()
            revert = getattr(self.store, "revert_rule_exception", None)
            if not callable(revert):
                raise ValueError("atomic_rule_exception_revert_unavailable")
            revert(
                exception_id,
                expected_parent_assignment_hash=expected_after_hash,
                decision=inverse,
            )
            return inverse
        else:
            raise ValueError(f"structured_inverse_unsupported:{action or 'unknown'}")

        append = getattr(self.store, "append_rule_decision", None)
        if not callable(append):
            raise ValueError("structured_inverse_persistence_unavailable")
        persisted = append(inverse)
        return persisted if persisted is not None else inverse

    @staticmethod
    def _decision_owned_by(decision: RuleDecision, agent_instance_id: str) -> bool:
        """Exact target ownership check; never use substring/prefix matching."""
        agent_id = str(agent_instance_id or "").strip()
        if not agent_id:
            return False
        owner = str(getattr(decision, "owner_agent_id", "") or "").strip()
        if owner:
            return owner == agent_id
        actor = str(getattr(decision, "actor", "") or "").strip()
        return actor in {agent_id, f"agent:{agent_id}"}

    def undo_rule(
        self,
        undo_id: str,
        effective_context: EffectiveAgentContext | Mapping[str, Any],
        *,
        is_admin: bool | None = None,
    ) -> RuleDecision:
        """Undo one structured lifecycle decision; never restore a group snapshot."""
        context = self._context_dict(effective_context)
        token = str(undo_id or "").strip()
        if not token:
            return self._blocked(action="rule_undo", reason="undo_id is required", context=context)
        admin = self.is_admin if is_admin is None else bool(is_admin)
        if not admin and not context["agent_instance_id"]:
            return self._blocked(
                action="rule_undo",
                reason="trusted agent_instance_id is required for undo",
                context=context,
                undo_id=token,
            )
        structured_original = None
        structured_list = getattr(self.store, "list_rule_decisions", None)
        if callable(structured_list):
            try:
                candidates = structured_list(undo_id=token)
                structured_original = candidates[0] if candidates else None
            except Exception:
                structured_original = None
        if structured_original is None:
            return self._blocked(
                action="rule_undo", reason="structured_decision_required",
                context=context, undo_id=token,
            )
        if not admin and not self._decision_owned_by(
            structured_original, context["agent_instance_id"]
        ):
            return self._blocked(action="rule_undo", reason="undo permission denied", context=context, undo_id=token)
        try:
            return self._target_undo(
                structured_original, token=token,
                actor=("admin" if admin else f"agent:{context['agent_instance_id']}"),
            ) or self._blocked(
                action="rule_undo", reason="target_undo_noop", context=context, undo_id=token,
            )
        except (TypeError, ValueError, RuntimeError) as exc:
            reason = str(exc)
            if reason == "structured_inverse_revision_missing":
                return self._blocked(
                    action="rule_undo", reason=reason,
                    context=context, undo_id=token,
                )
            return self._blocked(
                action="rule_undo", reason=f"target_undo_partial_failure:{reason}",
                context=context, undo_id=token,
            )

    def undo_rule_decision(
        self,
        decision_id: str,
        effective_context: EffectiveAgentContext | Mapping[str, Any] | None = None,
        *,
        is_admin: bool | None = None,
    ) -> RuleDecision:
        """Undo by lifecycle decision id (UI/API friendly alias)."""
        decision = self.read_decision(decision_id)
        if effective_context is None:
            effective_context = EffectiveAgentContext(
                agent_instance_id=os.environ.get("MEMORYGUARD_AGENT_ID", "").strip(),
                share_group_id=self.group_id,
                project_ref=canonical_project_ref(
                    os.environ.get("MEMORYGUARD_PROJECT_CWD") or os.getcwd()
                ),
                provider=os.environ.get("MEMORYGUARD_PROVIDER", "").strip(),
                runtime_role=os.environ.get("MEMORYGUARD_RUNTIME_ROLE", "").strip(),
            )
        context = self._context_dict(effective_context)
        if decision is None:
            return self._blocked(
                action="rule_undo", reason="structured_decision_required",
                context=context, undo_id=str(decision_id or ""),
            )
        admin = self.is_admin if is_admin is None else bool(is_admin)
        if not admin and not self._decision_owned_by(
            decision, context["agent_instance_id"]
        ):
            return self._blocked(
                action="rule_undo", reason="undo permission denied",
                context=context, undo_id=str(decision_id or ""),
            )
        try:
            return self._target_undo(
                decision,
                token=str(decision_id or ""),
                actor=("admin" if admin else f"agent:{context['agent_instance_id']}"),
            ) or self._blocked(
                action="rule_undo", reason="target_undo_noop",
                context=context, undo_id=str(decision_id or ""),
            )
        except (TypeError, ValueError, RuntimeError) as exc:
            reason = str(exc)
            if reason == "structured_inverse_revision_missing":
                return self._blocked(
                    action="rule_undo", reason=reason,
                    context=context, undo_id=str(decision_id or ""),
                )
            return self._blocked(
                action="rule_undo", reason=f"target_undo_partial_failure:{reason}",
                context=context, undo_id=str(decision_id or ""),
            )

    def read_decision(self, decision_id: str) -> RuleDecision | None:
        target = str(decision_id or "").strip()
        if not target:
            return None
        structured_get = getattr(self.store, "get_rule_decision", None)
        if callable(structured_get):
            try:
                structured = structured_get(target)
            except Exception:
                structured = None
            if structured is not None:
                return structured
        try:
            event = next((item for item in self.store.list_decisions() if item.event_id == target), None)
        except Exception:
            event = None
        if event is None:
            return None
        metadata: dict[str, Any] = {}
        try:
            metadata = json.loads(event.reason) if event.reason.startswith("{") else {"reason": event.reason}
        except (TypeError, ValueError):
            metadata = {"reason": event.reason}
        assignment = metadata.get("assignment") if isinstance(metadata, dict) else {}
        memory_id = next(
            (item for item in event.target_ids
             if item and item != metadata.get("parent_rule_id")), "",
        )
        assignments = [assignment] if isinstance(assignment, dict) and assignment else []
        return RuleDecision(
            decision_id=event.event_id,
            actor=event.actor or "agent:unknown",
            before={},
            after={"version_id": self.store.get_active_version_id() or ""},
            reason=event.reason,
            confidence=float(metadata.get("scope_confidence", 1.0) or 1.0),
            undo_id=str(metadata.get("undo_id", "") or ""),
            created_at=event.created_at,
            rule_id=memory_id,
            action=event.action,
            target_ids=list(event.target_ids),
            status="recorded",
            memory_id=memory_id,
            parent_rule_id=str(metadata.get("parent_rule_id", "") or ""),
            assignments=assignments,
            target_type=str((assignment or {}).get("target_type", "")),
            target_id=str((assignment or {}).get("target_id", "")),
            project_ref=str((assignment or {}).get("project_ref", "")),
            scope_confidence=float(metadata.get("scope_confidence", 0.0) or 0.0),
            scope_reason=str(metadata.get("scope_reason", "") or ""),
            blocked_reason="",
        )

    def list_decisions(self, *, action: str | None = None, limit: int | None = None) -> list[RuleDecision]:
        structured_list = getattr(self.store, "list_rule_decisions", None)
        if callable(structured_list):
            try:
                selected = structured_list()
                if action:
                    selected = [item for item in selected if item.action == action]
                if limit is not None:
                    selected = selected[-max(0, int(limit)):]
                return selected
            except Exception:
                pass
        events = self.store.list_decisions()
        selected = [item for item in events if not action or item.action == action]
        if limit is not None:
            selected = selected[-max(0, int(limit)):]
        output: list[RuleDecision] = []
        for item in selected:
            decision = self.read_decision(item.event_id)
            if decision is not None:
                output.append(decision)
        return output

    list_rule_decisions = list_decisions
    read_rule_decision = read_decision
    get_decision = read_decision

    def scope_stats(self, effective_context: EffectiveAgentContext | Mapping[str, Any] | None = None) -> dict[str, Any]:
        context = self._context_dict(effective_context) if effective_context is not None else None
        assignments = self.store.list_rule_assignments()
        counts: dict[str, int] = {}
        active_counts: dict[str, int] = {}
        for item in assignments:
            counts[item.target_type] = counts.get(item.target_type, 0) + 1
            record = self.store.get_record(item.memory_id)
            if record is not None and record.status == SharedMemoryStatus.ACTIVE:
                active_counts[item.target_type] = active_counts.get(item.target_type, 0) + 1
        payload: dict[str, Any] = {
            "share_group_id": self.group_id,
            "assignment_count": len(assignments),
            "by_target_type": counts,
            "active_by_target_type": active_counts,
            "auto_allowed_target_types": sorted(AUTO_TARGET_TYPES),
            "inference_policy": "agent_project when trusted cwd project exists, else agent",
        }
        list_scope_stats = getattr(self.store, "list_rule_scope_stats", None)
        scope_stats_rows: list[Any] = []
        if callable(list_scope_stats):
            try:
                scope_stats_rows = list(list_scope_stats() or [])
                payload["scope_stats"] = [
                    item.to_dict() if hasattr(item, "to_dict") else dict(item)
                    for item in scope_stats_rows
                ]
            except Exception:
                payload["scope_stats"] = []
        # P1: report auto-scope coverage from rule-create decisions instead of
        # dressing assignment counts up as accuracy.  Accuracy itself requires
        # a human-labeled golden set and is never fabricated here.
        decisions: list[Any] = []
        list_decisions = getattr(self.store, "list_rule_decisions", None)
        if callable(list_decisions):
            try:
                decisions = list_decisions()
            except Exception:
                decisions = []
        auto_attempts = [d for d in decisions if str(getattr(d, "action", "") or "") == "rule_create_auto"]
        manual_attempts = [d for d in decisions if str(getattr(d, "action", "") or "") == "rule_create_manual"]
        total_attempts = len(auto_attempts) + len(manual_attempts)
        auto_created = [
            d for d in auto_attempts
            if str(getattr(d, "status", "") or "") == "created"
        ]
        manual_created = [
            d for d in manual_attempts
            if str(getattr(d, "status", "") or "") == "created"
        ]
        created_decisions = auto_created + manual_created
        blocked_decisions = [
            d for d in (auto_attempts + manual_attempts)
            if str(getattr(d, "status", "") or "") == "blocked"
        ]
        fallback_auto = [
            d for d in auto_created
            if "fallback" in str(getattr(d, "scope_reason", "") or getattr(d, "reason", "") or "")
        ]
        corrected_count = 0
        wrong_scope_count = 0
        accepted_count = 0
        def _stat_value(stat: Any, key: str) -> int:
            if isinstance(stat, Mapping):
                return int(stat.get(key, 0) or 0)
            return int(getattr(stat, key, 0) or 0)
        # Runtime scope feedback is meaningful only for rules created by the
        # automatic path.  Blocked decisions never create a rule and
        # unobserved events are excluded by the Store evidence ledger.
        auto_rule_ids = {
            str(getattr(item, "memory_id", "") or getattr(item, "rule_id", ""))
            for item in auto_created
            if str(getattr(item, "memory_id", "") or getattr(item, "rule_id", ""))
        }
        if callable(list_scope_stats):
            try:
                for stat in scope_stats_rows:
                    stat_rule_id = str(
                        stat.get("rule_id", "") if isinstance(stat, Mapping)
                        else getattr(stat, "rule_id", "")
                    )
                    if stat_rule_id not in auto_rule_ids:
                        continue
                    corrected_count += _stat_value(stat, "corrected")
                    wrong_scope_count += _stat_value(stat, "wrong_scope")
                    accepted_count += _stat_value(stat, "accepted")
            except (TypeError, ValueError):
                pass
        observed_total = accepted_count + corrected_count + wrong_scope_count
        fallback_count = len(fallback_auto)
        auto_created_count = len(auto_created)
        successful_scope_decisions = len(auto_created) + len(manual_created)
        def _rate(numerator: int, denominator: int) -> float:
            if denominator <= 0:
                return 0.0
            return max(0.0, min(1.0, float(numerator) / float(denominator)))

        # Optional, explicitly labeled golden-set annotations.  Runtime
        # feedback is never used as a substitute for human-labeled accuracy.
        golden_total = 0
        golden_correct = 0
        for item in auto_created:
            metadata = getattr(item, "metadata", {})
            if not isinstance(metadata, Mapping):
                continue
            expected = metadata.get("golden_expected_scope")
            if expected is None:
                expected = metadata.get("golden_scope")
            if not isinstance(expected, Mapping):
                continue
            actual = metadata.get("assignment")
            if not isinstance(actual, Mapping):
                actual = (getattr(item, "after", {}) or {}).get("assignments", [])
                actual = actual[0] if isinstance(actual, list) and actual else {}
            golden_total += 1
            if (
                str(actual.get("target_type", "")) == str(expected.get("target_type", ""))
                and str(actual.get("target_id", "")) == str(expected.get("target_id", ""))
                and canonical_project_ref(actual.get("project_ref", ""))
                == canonical_project_ref(expected.get("project_ref", ""))
            ):
                golden_correct += 1
        golden_accuracy = _rate(golden_correct, golden_total) if golden_total else None

        metrics = {
            "attempts": total_attempts,
            "created": len(created_decisions),
            "blocked": len(blocked_decisions),
            "manual": len(manual_created),
            "auto_scope_attempts": len(auto_attempts),
            "auto_scope_created": len(auto_created),
            "auto_scope_blocked": sum(
                1 for item in auto_attempts
                if str(getattr(item, "status", "") or "") == "blocked"
            ),
            "manual_scope_attempts": len(manual_attempts),
            "manual_scope_created": len(manual_created),
            "manual_scope_blocked": sum(
                1 for item in manual_attempts
                if str(getattr(item, "status", "") or "") == "blocked"
            ),
            "fallback": fallback_count,
            "corrected": corrected_count,
            "wrong": wrong_scope_count,
            "auto_scope_coverage": _rate(len(auto_created), successful_scope_decisions),
            "auto_scope_accuracy": _rate(accepted_count, observed_total),
            "observed_accuracy": _rate(accepted_count, observed_total),
            "observed_feedback_total": observed_total,
            "accepted": accepted_count,
            "corrected": corrected_count,
            "wrong_scope": wrong_scope_count,
            "fallback_rate": _rate(fallback_count, auto_created_count),
            "under_scoped_rate": _rate(corrected_count, observed_total),
            "over_scoped_rate": _rate(wrong_scope_count, observed_total),
            "manual_correction_rate": _rate(corrected_count, observed_total),
            "golden_scope_accuracy": golden_accuracy,
            "golden_scope_evaluated": golden_total,
        }
        payload["metrics"] = metrics
        payload["scope_metrics"] = dict(metrics)
        payload["auto_scope"] = {
            "auto_created": len(auto_created),
            "manual_created": len(manual_created),
            "auto_scope_coverage": metrics["auto_scope_coverage"],
            "auto_scope_accuracy": metrics["auto_scope_accuracy"],
            "observed_accuracy": metrics["observed_accuracy"],
            "auto_fallback_count": fallback_count,
            "fallback_rate": metrics["fallback_rate"],
            "under_scoped_rate": metrics["under_scoped_rate"],
            "over_scoped_rate": metrics["over_scoped_rate"],
            "manual_correction_rate": metrics["manual_correction_rate"],
            "golden_scope_accuracy": golden_accuracy,
            "golden_scope_evaluated": golden_total,
            "accuracy_note": "accuracy uses reviewed scope outcomes; unreviewed creations are not counted as correct",
        }
        if context is not None:
            payload["effective_context"] = context
            payload["current_agent_assignments"] = sum(
                1 for item in assignments
                if item.target_type in AUTO_TARGET_TYPES and item.target_id == context["agent_instance_id"]
            )
        return payload

    get_scope_stats = scope_stats
    get_rule_scope_stats = scope_stats
    get_rule_auto_scope_metrics = scope_stats

    # ------------------------------------------------------------------
    # Feedback, narrowing and exceptions
    # ------------------------------------------------------------------

    def submit_feedback(
        self,
        receipt_id: str,
        outcome: str,
        actor: str = "",
        evidence: str = "",
        *,
        confidence: float = 1.0,
        effective_context: EffectiveAgentContext | Mapping[str, Any] | None = None,
        idempotency_key: str = "",
        producer: str = "agent",
        actor_id: str | None = None,
    ) -> RuleDecision:
        outcome = str(outcome or "").strip()
        if outcome not in FEEDBACK_OUTCOMES:
            return self._blocked(action="rule_feedback", reason="invalid feedback outcome", receipt_id=receipt_id)
        try:
            confidence_value = float(confidence)
        except (TypeError, ValueError):
            return self._blocked(action="rule_feedback", reason="confidence must be numeric between 0 and 1", receipt_id=receipt_id)
        if not 0 <= confidence_value <= 1:
            return self._blocked(action="rule_feedback", reason="confidence must be between 0 and 1", receipt_id=receipt_id)
        actor_value = str(actor_id if actor_id is not None else actor or "").strip()
        if not actor_value:
            return self._blocked(action="rule_feedback", reason="actor is required", receipt_id=receipt_id)
        feedback_source = str(producer or "agent").strip().casefold()
        if feedback_source not in FEEDBACK_PRODUCER_AUTHORITY:
            return self._blocked(
                action="rule_feedback",
                reason="feedback producer must be one of user|agent|hook",
                receipt_id=receipt_id,
            )

        receipt = self.store.get_rule_match_receipt(str(receipt_id))
        if receipt is None:
            return self._blocked(
                action="rule_feedback", reason="receipt_not_found", receipt_id=receipt_id,
            )
        # An exception with an empty override body must be rejected before any
        # event is written: otherwise the append-only feedback stream and the
        # scope counters are polluted, then ``create_child_exception`` fails and
        # a nonexistent child rule is reported.  The front-end already guards
        # this, but MCP/other API callers must hit the same fail-closed edge.
        if outcome == "exception" and not str(evidence or "").strip():
            return self._blocked(
                action="rule_feedback",
                reason="exception_override_body_required",
                receipt_id=receipt_id,
            )
        # A feedback event is owned by the Agent that produced the receipt, and
        # its evidence is bound to the exact runtime context where that receipt
        # was created.  MCP/host callers cannot use an actor string or a later
        # project switch to move a receipt's evidence to another scope: doing so
        # would let Project B's "not_applicable" mutate Project A's rule.
        #
        # ``receipt_context`` is immutable for every downstream consumer (scope
        # evaluation, auto narrowing, exception split, decision explanation).
        # The caller's ``effective_context`` may only prove *who* submits the
        # feedback; it can never rewrite *where* the receipt was produced.
        receipt_project = canonical_project_ref(
            getattr(receipt, "project_ref", "") or ""
        )
        receipt_context = {
            "agent_instance_id": str(receipt.agent_instance_id or ""),
            "share_group_id": self.group_id,
            "provider": str(receipt.provider or ""),
            "project_ref": receipt_project,
            "runtime_role": str(receipt.runtime_role or ""),
            "runtime_agent_id": "", "parent_agent_id": "",
            "session_id": str(receipt.session_id or ""),
            "context_hash": str(receipt.context_hash or ""),
        }
        if effective_context is not None:
            try:
                context = self._context_dict(effective_context)
            except (TypeError, ValueError) as exc:
                return self._blocked(action="rule_feedback", reason=str(exc), receipt_id=receipt_id)
            if context["share_group_id"] and context["share_group_id"] != self.group_id:
                return self._blocked(
                    action="rule_feedback", reason="feedback share group mismatch",
                    receipt_id=receipt_id,
                )
            if context["agent_instance_id"] != str(receipt.agent_instance_id or ""):
                return self._blocked(
                    action="rule_feedback", reason="feedback agent does not match receipt owner",
                    receipt_id=receipt_id,
                )
            if (
                context["project_ref"]
                and receipt_project
                and context["project_ref"] != receipt_project
            ):
                return self._blocked(
                    action="rule_feedback",
                    reason="feedback_context_project_mismatch",
                    receipt_id=receipt_id,
                )
            if (
                context["provider"]
                and getattr(receipt, "provider", "")
                and context["provider"] != str(receipt.provider or "")
            ):
                return self._blocked(
                    action="rule_feedback",
                    reason="feedback_context_provider_mismatch",
                    receipt_id=receipt_id,
                )
            if (
                context["runtime_role"]
                and getattr(receipt, "runtime_role", "")
                and context["runtime_role"] != str(receipt.runtime_role or "")
            ):
                return self._blocked(
                    action="rule_feedback",
                    reason="feedback_context_runtime_role_mismatch",
                    receipt_id=receipt_id,
                )
            if (
                context["context_hash"]
                and getattr(receipt, "context_hash", "")
                and context["context_hash"] != str(receipt.context_hash or "")
            ):
                return self._blocked(
                    action="rule_feedback",
                    reason="feedback_context_hash_mismatch",
                    receipt_id=receipt_id,
                )
        else:
            context = dict(receipt_context)

        get_effective = getattr(self.store, "get_effective_rule_match_feedback", None)
        if not callable(get_effective):
            get_effective = getattr(self.store, "get_rule_match_feedback_by_receipt", None)
        prior_effective = None
        if callable(get_effective):
            try:
                prior_effective = get_effective(str(receipt_id))
            except (TypeError, ValueError, RuntimeError):
                prior_effective = None
        prior_feedback_id = str(getattr(prior_effective, "feedback_id", "") or "")
        feedback_id = str(idempotency_key or "").strip() or stable_hash(
            "rule-feedback", receipt_id, outcome, feedback_source, actor_value, evidence,
        )
        # An ``unobserved`` feedback event must never be recorded with a
        # positive confidence; the model enforces this in __post_init__.
        feedback = RuleMatchFeedback(
            feedback_id=feedback_id, receipt_id=str(receipt_id), outcome=outcome,
            actor=actor_value, evidence=str(evidence or ""),
            confidence=confidence_value, created_at=_now_iso(),
            source=feedback_source,
            authority=FEEDBACK_PRODUCER_AUTHORITY[feedback_source],
            supersedes_feedback_id=prior_feedback_id,
        )
        try:
            saved = self.store.append_rule_match_feedback(feedback)
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            return self._blocked(action="rule_feedback", reason=str(exc), receipt_id=receipt_id)
        saved_feedback_id = str(getattr(saved, "feedback_id", "") or feedback_id)
        new_effective = None
        if callable(get_effective):
            try:
                new_effective = get_effective(str(receipt_id))
            except (TypeError, ValueError, RuntimeError):
                new_effective = None
        new_effective_id = str(getattr(new_effective, "feedback_id", "") or "")
        became_effective = bool(
            new_effective is not None
            and new_effective_id == saved_feedback_id
            and prior_feedback_id != saved_feedback_id
        )

        # Lower-authority events remain in the append-only stream, but cannot
        # alter counters, narrowing, or exception behavior.
        if not became_effective:
            return self._decision(
                action="rule_feedback", status="recorded", result={
                    "decision_id": stable_hash("rule-feedback-decision", feedback_id),
                    "feedback_id": saved_feedback_id,
                    "receipt_id": receipt_id,
                }, feedback_id=saved_feedback_id, receipt_id=receipt_id,
                scope_confidence=confidence_value,
                metadata={
                    "outcome": outcome, "evidence": evidence,
                    "confidence": confidence_value, "source": feedback_source,
                    "authority": FEEDBACK_PRODUCER_AUTHORITY[feedback_source],
                    "effective": False,
                    "supersedes_feedback_id": prior_feedback_id,
                },
                actor=actor_value,
            )

        # Counters must never count ``unobserved`` as a scope outcome.  Only
        # explicit outcomes update the accuracy ledger.
        if (
            receipt is not None
            and outcome != "unobserved"
            and (
                prior_effective is None
                or prior_effective.outcome != outcome
                or prior_effective.source != feedback_source
            )
        ):
            record_scope = getattr(self.store, "record_rule_scope", None)
            if callable(record_scope):
                # Scope accounting must use the receipt's original context, not
                # the submitter's current project/provider/role.
                context_for_stats = receipt_context
                scope_outcome = {
                    "followed": "accepted",
                    "corrected": "corrected",
                    "violated": "accepted",
                    "exception": "wrong_scope",
                    "not_applicable": "wrong_scope",
                }.get(outcome, "ignored")
                try:
                    record_scope(
                        receipt.memory_id,
                        agent_instance_id=str(receipt.agent_instance_id or context_for_stats.get("agent_instance_id", "")),
                        project_ref=str(
                            context_for_stats.get("project_ref")
                            or receipt.project_ref
                            or ""
                        ),
                        outcome=scope_outcome,
                        receipt_id=str(receipt_id),
                        effective_feedback_id=str(saved_feedback_id),
                    )
                except (TypeError, ValueError, RuntimeError):
                    pass
        base = self._decision(
            action="rule_feedback", status="recorded", result={
                "decision_id": stable_hash("rule-feedback-decision", feedback_id),
                "feedback_id": saved.feedback_id if hasattr(saved, "feedback_id") else feedback_id,
                "receipt_id": receipt_id,
            }, feedback_id=feedback_id, receipt_id=receipt_id,
            scope_confidence=confidence_value,
            metadata={
                "outcome": outcome, "evidence": evidence,
                "confidence": confidence_value, "source": feedback_source,
                "authority": FEEDBACK_PRODUCER_AUTHORITY[feedback_source],
                "effective": True,
                "supersedes_feedback_id": prior_feedback_id,
            },
            actor=actor_value,
        )
        if outcome in NARROWING_OUTCOMES:
            return self._narrow_from_feedback(
                receipt_id, feedback_id, receipt_context, outcome=outcome,
                evidence=evidence, base=base,
            )
        if outcome == "exception":
            return self.create_child_exception(
                receipt_id, receipt_context, evidence=evidence,
                feedback_id=feedback_id, base=base,
            )
        return base

    feedback = submit_feedback
    submit_rule_feedback = submit_feedback

    def _receipt_record(self, receipt_id: str) -> tuple[Any | None, Any | None]:
        receipt = self.store.get_rule_match_receipt(str(receipt_id))
        if receipt is None:
            return None, None
        return receipt, self.store.get_record(receipt.memory_id)

    def _collect_receipt_feedbacks(
        self, receipt_id: str,
    ) -> list[RuleMatchFeedback]:
        listing = getattr(self.store, "list_rule_match_feedbacks", None)
        if callable(listing):
            try:
                return listing(receipt_id=receipt_id)
            except (TypeError, ValueError, RuntimeError):
                pass
        return []

    @staticmethod
    def _evidence_field(item: Any, name: str, default: Any = "") -> Any:
        """Read a store evidence row across rolling API representations."""
        if isinstance(item, Mapping):
            if name in item:
                return item.get(name, default)
            feedback = item.get("feedback")
            receipt = item.get("receipt")
            for nested in (feedback, receipt):
                if isinstance(nested, Mapping) and name in nested:
                    return nested.get(name, default)
        value = getattr(item, name, None)
        if value is not None:
            return value
        for nested_name in ("feedback", "receipt"):
            nested = getattr(item, nested_name, None)
            value = getattr(nested, name, None) if nested is not None else None
            if value is not None:
                return value
        return default

    def _collect_effective_scope_evidence(
        self, *, memory_id: str, agent_instance_id: str, project_ref: str,
    ) -> list[Any]:
        """Collect one effective feedback row per receipt for one scope."""
        listing = getattr(self.store, "list_effective_rule_feedback_evidence", None)
        if callable(listing):
            try:
                rows = list(listing(
                    memory_id=memory_id,
                    agent_instance_id=agent_instance_id,
                    project_ref=canonical_project_ref(project_ref),
                ) or [])
                if rows:
                    return rows
            except (TypeError, ValueError, RuntimeError):
                # Fall through to the compatibility resolver below.
                pass
        rows: list[dict[str, Any]] = []
        try:
            receipts = self.store.list_rule_match_receipts(
                memory_id=memory_id, agent_instance_id=agent_instance_id,
            )
        except (TypeError, ValueError, RuntimeError):
            receipts = []
        get_effective = getattr(self.store, "get_effective_rule_match_feedback", None)
        if not callable(get_effective):
            get_effective = getattr(self.store, "get_rule_match_feedback_by_receipt", None)
        for receipt in receipts:
            if canonical_project_ref(getattr(receipt, "project_ref", "") or "") != canonical_project_ref(project_ref):
                continue
            if not callable(get_effective):
                continue
            try:
                feedback = get_effective(receipt.receipt_id)
            except (TypeError, ValueError, RuntimeError):
                feedback = None
            if feedback is not None:
                rows.append({"feedback": feedback, "receipt": receipt})
        return rows

    def _narrow_evidence_ready(
        self, receipt_id: str,
    ) -> tuple[bool, str, int, int, int]:
        """Check the v2 threshold over all receipts in one rule scope."""
        receipt, parent = self._receipt_record(receipt_id)
        if receipt is None or parent is None:
            return False, "receipt_or_parent_rule_not_found", 0, 0, 0
        project_ref = canonical_project_ref(getattr(receipt, "project_ref", "") or "")
        if not project_ref:
            return False, "no_trusted_project_context", 0, 0, 0
        rows = self._collect_effective_scope_evidence(
            memory_id=parent.memory_id,
            agent_instance_id=str(receipt.agent_instance_id or ""),
            project_ref=project_ref,
        )
        scope_errors = [
            row for row in rows
            if self._evidence_field(row, "outcome") == "not_applicable"
            and float(self._evidence_field(row, "confidence", 0.0) or 0.0) >= 0.7
            and str(self._evidence_field(row, "session_id", "") or "")
        ]
        followed = [
            row for row in rows
            if self._evidence_field(row, "outcome") == "followed"
            and float(self._evidence_field(row, "confidence", 0.0) or 0.0) >= 0.7
        ]
        sessions = {
            str(self._evidence_field(row, "session_id", "") or "")
            for row in scope_errors
            if str(self._evidence_field(row, "session_id", "") or "")
        }
        if len(scope_errors) < NARROWING_MIN_EVENTS:
            return False, "not_applicable_not_enough_evidence", len(scope_errors), len(sessions), len(followed)
        if len(sessions) < NARROWING_MIN_SESSIONS:
            return False, "not_applicable_not_enough_sessions", len(scope_errors), len(sessions), len(followed)
        if len(followed) >= NARROWING_OPPOSED_THRESHOLD:
            return False, "followed_evidence_cancels_narrowing", len(scope_errors), len(sessions), len(followed)
        return True, "ready", len(scope_errors), len(sessions), len(followed)

    def _narrow_from_feedback(
        self,
        receipt_id: str,
        feedback_id: str,
        effective_context: EffectiveAgentContext | Mapping[str, Any] | None,
        *,
        outcome: str,
        evidence: str,
        base: RuleDecision,
    ) -> RuleDecision:
        receipt, parent = self._receipt_record(receipt_id)
        if receipt is None or parent is None:
            return self._blocked(action="rule_narrow", reason="receipt_or_parent_rule_not_found", receipt_id=receipt_id, feedback_id=feedback_id)
        context: Mapping[str, str] = self._context_dict(effective_context) if effective_context is not None else {
            "agent_instance_id": str(receipt.agent_instance_id or ""),
            "share_group_id": self.group_id,
            "provider": receipt.provider or "", "project_ref": receipt.project_ref or "",
            "runtime_role": receipt.runtime_role or "",
            "runtime_agent_id": "", "parent_agent_id": "",
            "session_id": receipt.session_id or "", "context_hash": receipt.context_hash or "",
        }
        if context["agent_instance_id"] != receipt.agent_instance_id:
            return self._blocked(action="rule_narrow", reason="feedback agent does not match receipt agent", receipt_id=receipt_id, feedback_id=feedback_id)
        assignments = self.store.list_rule_assignments(parent.memory_id)
        if not assignments:
            return self._blocked(action="rule_narrow", reason="parent rule has no governed assignment", memory_id=parent.memory_id, receipt_id=receipt_id, feedback_id=feedback_id)
        project_ref = context["project_ref"] or receipt.project_ref
        if not project_ref:
            return self._blocked(action="rule_narrow", reason="no trusted project context for narrowing", memory_id=parent.memory_id, receipt_id=receipt_id, feedback_id=feedback_id)
        ready, reason, error_count, session_count, followed_count = self._narrow_evidence_ready(receipt_id)
        if not ready:
            return self._decision(
                action="rule_narrow", status="pending",
                result={"memory_id": parent.memory_id, "decision_id": stable_hash(
                    "rule-narrow-pending", parent.memory_id, feedback_id, reason,
                )},
                parent_rule_id=parent.memory_id, feedback_id=feedback_id,
                receipt_id=receipt_id, scope_confidence=0.0,
                scope_reason=reason,
                metadata={
                    "evidence_count": error_count,
                    "session_count": session_count,
                    "opposed_followed_count": followed_count,
                    "pending": True,
                },
            )
        # Narrowing is monotone: agent -> agent include + agent_project exclude
        # for the offending project.  Never convert a project-specific
        # assignment back to agent, group, system, provider, or runtime role.
        parent_agent = next((item for item in assignments if item.target_type == "agent" and item.effect == "include" and item.target_id == receipt.agent_instance_id), None)
        if parent_agent is None:
            # If nobody on the parent matches this agent at agent level, it
            # may already be narrowed; a no-op is preferable to widening.
            return self._blocked(action="rule_narrow", reason="no strictly narrower trusted agent_project scope available", memory_id=parent.memory_id, receipt_id=receipt_id, feedback_id=feedback_id)
        # Narrowing only adds the precise parent exclude.  It never creates a
        # child rule: the parent remains the single governed definition.
        before_assignments = [
            item.to_dict() if hasattr(item, "to_dict") else dict(item)
            for item in assignments
        ]
        updated_assignments = list(before_assignments)
        existing_keys = {
            (
                str(item.get("target_type", "")),
                str(item.get("target_id", "")),
                canonical_project_ref(item.get("project_ref", "")),
                str(item.get("effect", "include")),
            )
            for item in updated_assignments
        }
        exclude_key = (
            "agent_project", str(receipt.agent_instance_id),
            canonical_project_ref(project_ref), "exclude",
        )
        if exclude_key not in existing_keys:
            updated_assignments.append({
                "target_type": "agent_project", "target_id": receipt.agent_instance_id,
                "project_ref": project_ref, "effect": "exclude",
            })
        after_assignments = [dict(item) for item in updated_assignments]
        parent_assignments_after_hash = self._assignment_hash_for(
            parent.memory_id, after_assignments,
        )
        decision = self._decision(
            action="rule_narrow", status="created",
            result={
                "memory_id": parent.memory_id,
                "assignments": after_assignments,
                "decision_id": stable_hash("rule-narrow", parent.memory_id, feedback_id, project_ref),
            },
            parent_rule_id=parent.memory_id, feedback_id=feedback_id,
            receipt_id=receipt_id, scope_confidence=1.0,
            scope_reason="cross-session not_applicable evidence reached narrowing threshold",
            before={"assignments": before_assignments},
            metadata={
                "narrowed_parent_rule": parent.memory_id,
                "parent_assignments_before": before_assignments,
                "parent_assignments_after": after_assignments,
                "parent_assignments_after_hash": parent_assignments_after_hash,
                "excluded_project": project_ref,
                "generated_parent_assignment": {
                    "target_type": "agent_project",
                    "target_id": receipt.agent_instance_id,
                    "project_ref": project_ref,
                    "effect": "exclude",
                },
                "evidence_count": error_count,
                "session_count": session_count,
            },
            actor=f"agent:{receipt.agent_instance_id}", persist=False,
        )
        hash_fn = getattr(self.store, "rule_assignment_hash", None)
        expected_hash = (
            str(hash_fn(parent.memory_id) or "")
            if callable(hash_fn)
            else stable_hash(
                "rule-assignments", json.dumps(before_assignments, ensure_ascii=False, sort_keys=True),
            )
        )
        apply_narrow = getattr(self.store, "apply_rule_narrow_atomic", None)
        apply_split = getattr(self.store, "apply_rule_split", None)
        try:
            if callable(apply_narrow):
                applied = apply_narrow(
                    parent_rule_id=parent.memory_id,
                    parent_assignments_after=after_assignments,
                    expected_parent_assignment_hash=expected_hash,
                    automatic=True,
                    actor_agent_id=receipt.agent_instance_id,
                    decision=decision,
                )
                if isinstance(applied, Mapping):
                    persisted = applied.get("decision")
                    if persisted is not None:
                        return persisted
                return decision
            if callable(apply_split):
                applied = apply_split(
                    parent_rule_id=parent.memory_id,
                    expected_parent_assignment_hash=expected_hash,
                    child_record=None,
                    child_assignments=[],
                    parent_assignments_after=after_assignments,
                    exception=None,
                    decision=decision,
                )
                if isinstance(applied, Mapping):
                    persisted = applied.get("decision")
                    if persisted is not None:
                        return persisted
                return decision
            # Compatibility stores without the mutation bundle are allowed to
            # perform this single parent mutation, but never create a child.
            final_assignments = self.store.set_rule_assignments(
                parent.memory_id, after_assignments,
                automatic=True, actor_agent_id=receipt.agent_instance_id,
            )
            decision.after["assignments"] = [
                item.to_dict() if hasattr(item, "to_dict") else dict(item)
                for item in final_assignments
            ]
            return self._persist_structured_decision(decision)
        except (TypeError, ValueError, RuntimeError) as exc:
            return self._blocked(
                action="rule_narrow", reason=f"parent_exclude_update_failed:{exc}",
                memory_id=parent.memory_id, receipt_id=receipt_id, feedback_id=feedback_id,
            )

    def create_child_exception(
        self,
        receipt_id: str,
        effective_context: EffectiveAgentContext | Mapping[str, Any] | None,
        *,
        evidence: str = "",
        feedback_id: str = "",
        base: RuleDecision | None = None,
    ) -> RuleDecision:
        receipt, parent = self._receipt_record(receipt_id)
        if receipt is None or parent is None:
            return self._blocked(action="rule_exception", reason="receipt_or_parent_rule_not_found", receipt_id=receipt_id, feedback_id=feedback_id)
        override_body = str(evidence or "").strip()
        if not override_body:
            return self._blocked(
                action="rule_exception", reason="exception_override_body_required",
                memory_id=getattr(parent, "memory_id", ""), receipt_id=receipt_id,
                feedback_id=feedback_id,
            )
        context = self._context_dict(effective_context) if effective_context is not None else {
            "agent_instance_id": str(receipt.agent_instance_id or ""), "share_group_id": self.group_id,
            "provider": receipt.provider or "", "project_ref": receipt.project_ref or "",
            "runtime_role": receipt.runtime_role or "", "runtime_agent_id": "", "parent_agent_id": "",
            "session_id": receipt.session_id or "", "context_hash": receipt.context_hash or "",
        }
        if context["agent_instance_id"] != receipt.agent_instance_id:
            return self._blocked(action="rule_exception", reason="feedback agent does not match receipt agent", receipt_id=receipt_id, feedback_id=feedback_id)
        # An exception must *actually* narrow the parent.  The child and the
        # parent exclude are persisted by one mutation bundle below.
        project_ref = context["project_ref"] or receipt.project_ref
        parent_assignments = self.store.list_rule_assignments(parent.memory_id)
        for parent_assignment in parent_assignments:
            if parent_assignment.effect != "include":
                continue
            if parent_assignment.target_type == "agent" and parent_assignment.target_id != receipt.agent_instance_id:
                return self._blocked(action="rule_exception", reason="exception scope is outside parent agent audience", memory_id=parent.memory_id, receipt_id=receipt_id, feedback_id=feedback_id)
            if parent_assignment.target_type == "agent_project":
                if (
                    parent_assignment.target_id != receipt.agent_instance_id
                    or parent_assignment.project_ref != project_ref
                ):
                    return self._blocked(action="rule_exception", reason="exception scope is outside parent agent_project audience", memory_id=parent.memory_id, receipt_id=receipt_id, feedback_id=feedback_id)
            if parent_assignment.target_type == "project" and parent_assignment.project_ref != project_ref:
                return self._blocked(action="rule_exception", reason="exception project differs from parent project audience", memory_id=parent.memory_id, receipt_id=receipt_id, feedback_id=feedback_id)
        # A project-scoped exception needs an exclude on the parent for that
        # project.  Without a project context there is no narrower audience to
        # split into, so the exception cannot change actual coverage.
        if project_ref:
            target_type = "agent_project"
            assignment = {
                "target_type": "agent_project", "target_id": receipt.agent_instance_id,
                "project_ref": project_ref, "effect": "include",
            }
        else:
            return self._blocked(
                action="rule_exception",
                reason="exception requires trusted project context to narrow coverage",
                memory_id=parent.memory_id, receipt_id=receipt_id,
                feedback_id=feedback_id,
            )
        # Add the parent exclude for the exception project so the original
        # rule stops applying there.  The child with higher priority then
        # covers the exception context alone.
        before_assignments = [
            item.to_dict() if hasattr(item, "to_dict") else dict(item)
            for item in parent_assignments
        ]
        updated_assignments = list(before_assignments)
        existing_keys = {
            (
                str(item.get("target_type", "")),
                str(item.get("target_id", "")),
                canonical_project_ref(item.get("project_ref", "")),
                str(item.get("effect", "include")),
            )
            for item in updated_assignments
        }
        exclude_key = (
            "agent_project", str(receipt.agent_instance_id),
            canonical_project_ref(project_ref), "exclude",
        )
        exclude_added = exclude_key not in existing_keys
        if exclude_added:
            updated_assignments.append({
                "target_type": "agent_project", "target_id": receipt.agent_instance_id,
                "project_ref": project_ref, "effect": "exclude",
            })
        after_assignments = [dict(item) for item in updated_assignments]
        generated_parent_assignment = {
            "target_type": "agent_project", "target_id": receipt.agent_instance_id,
            "project_ref": project_ref, "effect": "exclude",
        } if exclude_added else {}
        generated_parent_assignment_id = (
            stable_hash(
                "rule-assignment", parent.memory_id, "agent_project",
                receipt.agent_instance_id, canonical_project_ref(project_ref),
                "exclude",
            ) if exclude_added else ""
        )
        child_id = stable_hash(
            "rule-exception-child", self.group_id, parent.memory_id,
            receipt_id, feedback_id, context["agent_instance_id"], project_ref,
        )
        now = _now_iso()
        parent_kind = parent.kind if isinstance(parent.kind, MemoryKind) else MemoryKind.PROCEDURE
        child_record = SharedMemoryRecord(
            memory_id=child_id,
            body=override_body,
            kind=parent_kind,
            status=SharedMemoryStatus.ACTIVE,
            confidence=max(0.8, float(getattr(parent, "confidence", 0.8) or 0.8)),
            provenance=[Provenance(
                source_object_id=f"rule-exception:{receipt_id}",
                locator=f"rule:{parent.memory_id}",
                excerpt_hash=stable_hash(override_body),
                source_revision=now,
            )],
            injection_policy="always",
            priority=int(parent.priority) + 1,
            created_at=now,
            updated_at=now,
            agent_instance_id=context["agent_instance_id"],
        )
        child_assignments = [{"memory_id": child_id, **assignment}]
        normalized_after_hash = ""
        normalize_store = getattr(self.store, "_normalize_assignments", None)
        hash_store = getattr(self.store, "_assignment_hash", None)
        if callable(normalize_store) and callable(hash_store):
            try:
                normalized_after_hash = str(hash_store(
                    normalize_store(parent.memory_id, after_assignments, automatic=False),
                ) or "")
            except (TypeError, ValueError, RuntimeError):
                normalized_after_hash = ""
        parent_assignments_after_hash = normalized_after_hash or self._assignment_hash_for(
            parent.memory_id, after_assignments,
        )
        hash_fn = getattr(self.store, "rule_assignment_hash", None)
        expected_hash = (
            str(hash_fn(parent.memory_id) or "")
            if callable(hash_fn)
            else stable_hash(
                "rule-assignments", json.dumps(before_assignments, ensure_ascii=False, sort_keys=True),
            )
        )
        decision = self._decision(
            action="rule_exception", status="created",
            result={
                "memory_id": child_id,
                "record": child_record.to_dict(),
                "assignments": child_assignments,
                "decision_id": stable_hash("rule-exception", parent.memory_id, child_id, feedback_id),
            },
            parent_rule_id=parent.memory_id, child_rule_id=child_id,
            feedback_id=feedback_id, receipt_id=receipt_id,
            scope=assignment, scope_confidence=1.0,
            scope_reason="explicit exception override for trusted project",
            before={"parent_assignments": before_assignments, "child_status": "absent"},
            metadata={
                "exception": True, "parent_rule_id": parent.memory_id,
                "feedback_id": feedback_id, "receipt_id": receipt_id,
                "parent_assignments_before": before_assignments,
                "parent_assignments_after": after_assignments,
                "parent_assignments_after_hash": parent_assignments_after_hash,
                "excluded_project": project_ref,
                "exception_scope_assignment": {
                    "target_type": "agent_project", "target_id": receipt.agent_instance_id,
                    "project_ref": project_ref, "effect": "exclude",
                },
                "generated_parent_assignment": generated_parent_assignment,
                "generated_parent_assignment_id": generated_parent_assignment_id,
                "generated_parent_assignment_added": exclude_added,
            },
            actor=f"agent:{receipt.agent_instance_id}", persist=False,
        )
        # Local-delta inverse metadata.  Revocation must validate *this*
        # relation's own footprint (the exclude it generated, the child rule it
        # created, and its relation revision), never the parent's whole
        # assignment multiset -- otherwise a sibling exception on another
        # project would block this one (LIFO-only undo).
        generated_hash = ""
        if exclude_added and generated_parent_assignment_id:
            assignment_store = getattr(self.store, "_assignment_hash", None)
            if callable(assignment_store):
                try:
                    generated_hash = str(assignment_store([
                        RuleAssignment(
                            memory_id=parent.memory_id,
                            target_type=generated_parent_assignment.get("target_type", "agent_project"),
                            target_id=generated_parent_assignment.get("target_id", receipt.agent_instance_id),
                            project_ref=generated_parent_assignment.get("project_ref", project_ref),
                            effect=generated_parent_assignment.get("effect", "exclude"),
                        ),
                    ]) or "")
                except (TypeError, ValueError, RuntimeError):
                    generated_hash = ""
        behavior_hash = getattr(self.store, "rule_behavior_hash", None)
        child_behavior_hash = ""
        if callable(behavior_hash):
            try:
                child_behavior_hash = str(behavior_hash(
                    child_record, child_assignments,
                ) or "")
            except (TypeError, ValueError, RuntimeError):
                child_behavior_hash = ""
        exception_relation = RuleException(
            parent_rule=parent.memory_id,
            child_exception=child_id,
            priority=int(parent.priority) + 1,
            reason=override_body,
            rollback={
                "parent_assignments_before": before_assignments,
                "parent_assignments_after": after_assignments,
                "parent_assignments_after_hash": normalized_after_hash or stable_hash(
                    "rule-assignments", json.dumps(after_assignments, ensure_ascii=False, sort_keys=True),
                ),
                "exception_scope_assignment": {
                    "target_type": "agent_project", "target_id": receipt.agent_instance_id,
                    "project_ref": project_ref, "effect": "exclude",
                },
                "generated_parent_assignment": generated_parent_assignment,
                "generated_parent_assignment_id": generated_parent_assignment_id,
                "generated_parent_assignment_added": exclude_added,
                "generated_assignment_hash": generated_hash,
                "child_rule_id": child_id,
                "child_rule_behavior_hash": child_behavior_hash,
                "child_status_before": "absent",
                "relation_revision": now,
                "decision_id": decision.decision_id,
            },
            created_at=now, updated_at=now,
        )
        # Persist the relation identifier in the structured decision so an
        # undo-by-decision call can target the exact exception without
        # reconstructing it from parent/child IDs.
        decision.metadata["exception_id"] = exception_relation.exception_id
        if isinstance(decision.after, dict):
            decision.after.setdefault("metadata", {})["exception_id"] = exception_relation.exception_id
        apply_exception = getattr(self.store, "apply_rule_exception_atomic", None)
        apply_split = getattr(self.store, "apply_rule_split", None)
        try:
            if callable(apply_exception):
                applied = apply_exception(
                    parent_rule_id=parent.memory_id,
                    parent_assignments_before=before_assignments,
                    parent_assignments_after=after_assignments,
                    child_record=child_record,
                    child_assignments=child_assignments,
                    exception_relation=exception_relation,
                    decision=decision,
                    expected_parent_assignment_hash=expected_hash,
                )
            elif callable(apply_split):
                applied = apply_split(
                    parent_rule_id=parent.memory_id,
                    expected_parent_assignment_hash=expected_hash,
                    child_record=child_record,
                    child_assignments=child_assignments,
                    parent_assignments_after=after_assignments,
                    exception=exception_relation,
                    decision=decision,
                )
            else:
                return self._blocked(
                    action="rule_exception", reason="atomic_rule_exception_store_unavailable",
                    memory_id=parent.memory_id, receipt_id=receipt_id, feedback_id=feedback_id,
                )
            if isinstance(applied, Mapping):
                persisted = applied.get("decision")
                if persisted is not None:
                    return persisted
            return decision
        except (TypeError, ValueError, RuntimeError) as exc:
            return self._blocked(
                action="rule_exception", reason=f"atomic_rule_exception_failed:{exc}",
                memory_id=parent.memory_id, receipt_id=receipt_id, feedback_id=feedback_id,
            )

    def create_exception(
        self,
        parent_or_receipt: str,
        context_or_child: EffectiveAgentContext | Mapping[str, Any] | str | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Compatibility dispatcher for receipt-driven or explicit relations."""
        if isinstance(context_or_child, str):
            priority = int(args[0]) if args else int(kwargs.pop("priority", 0))
            reason = str(args[1]) if len(args) > 1 else str(kwargs.pop("reason", ""))
            return self.create_rule_exception(
                parent_or_receipt, context_or_child, priority=priority, reason=reason,
            )
        return self.create_child_exception(
            parent_or_receipt, context_or_child, *args, **kwargs,
        )

    # Explicit parent/child relation helpers used by the localhost cockpit.
    # They are intentionally thin wrappers over the canonical store model;
    # no exclude assignment is synthesized here.
    def create_rule_exception(
        self,
        parent_rule: str,
        child_exception: str,
        priority: int = 0,
        reason: str = "",
    ) -> dict[str, Any]:
        # This low-level relation API is intentionally admin-only.  Normal
        # callers must use receipt-driven ``create_child_exception`` so the
        # parent exclude, child rule, evidence and decision are one atomic
        # mutation.  Relation-only writes are retained for migration/admin
        # tooling but fail closed unless both records and scopes validate.
        if not self.is_admin:
            return {"ok": False, "error": "admin capability required for relation-only exception"}
        if not parent_rule or not child_exception:
            return {"ok": False, "error": "rule_exception_requires_parent_and_child"}
        if parent_rule == child_exception:
            return {"ok": False, "error": "rule_exception_cannot_reference_itself"}
        parent = self.store.get_record(parent_rule)
        child = self.store.get_record(child_exception)
        if parent is None or child is None:
            return {"ok": False, "error": "rule_exception_parent_or_child_not_found"}
        if parent.status != SharedMemoryStatus.ACTIVE or child.status != SharedMemoryStatus.ACTIVE:
            return {"ok": False, "error": "rule_exception_parent_and_child_must_be_active"}
        parent_assignments = self.store.list_rule_assignments(parent_rule)
        child_assignments = self.store.list_rule_assignments(child_exception)
        if not parent_assignments or not child_assignments:
            return {"ok": False, "error": "rule_exception_scope_assignments_required"}

        def _compatible(parent_item: Any, child_item: Any) -> bool:
            if getattr(parent_item, "effect", "include") != "include":
                return False
            if getattr(child_item, "effect", "include") != "include":
                return True
            pt = str(getattr(parent_item, "target_type", ""))
            ct = str(getattr(child_item, "target_type", ""))
            pid = str(getattr(parent_item, "target_id", ""))
            cid = str(getattr(child_item, "target_id", ""))
            pp = canonical_project_ref(getattr(parent_item, "project_ref", ""))
            cp = canonical_project_ref(getattr(child_item, "project_ref", ""))
            if pt == ct:
                return pid == cid and pp == cp
            if pt == "agent" and ct == "agent_project":
                return pid == cid
            if pt == "project" and ct == "agent_project":
                return pp == cp
            if pt in {"group", "system"}:
                return True
            return False

        if any(
            not any(_compatible(parent_item, child_item) for parent_item in parent_assignments)
            for child_item in child_assignments
            if getattr(child_item, "effect", "include") == "include"
        ):
            return {"ok": False, "error": "rule_exception_child_scope_outside_parent"}
        append_exception = getattr(self.store, "append_rule_exception", None)
        if not callable(append_exception):
            return {"ok": False, "error": "rule_exception_store_unavailable"}
        try:
            exception = append_exception(RuleException(
                parent_rule=parent_rule,
                child_exception=child_exception,
                priority=int(priority),
                reason=str(reason or ""),
                rollback={},
                created_at=_now_iso(),
                updated_at=_now_iso(),
            ))
        except (TypeError, ValueError, RuntimeError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, **exception.to_dict()}

    def list_exceptions(self, parent_rule: str = "") -> dict[str, Any]:
        listing = getattr(self.store, "list_rule_exceptions", None)
        if not callable(listing):
            return {"exceptions": [], "total": 0}
        try:
            values = listing(parent_rule=parent_rule or None)
        except (TypeError, ValueError, RuntimeError):
            values = []
        payload = [item.to_dict() if hasattr(item, "to_dict") else dict(item) for item in values]
        return {"exceptions": payload, "total": len(payload)}

    def revoke_exception(
        self,
        exception_id: str,
        effective_context: EffectiveAgentContext | Mapping[str, Any] | None = None,
        *,
        is_admin: bool | None = None,
    ) -> dict[str, Any]:
        # Revoke is a behavioral inverse (restore parent coverage + deactivate
        # child + relation), not a flag-only update.  New stores expose the
        # atomic inverse; legacy stores fail closed instead of claiming success.
        rollback = getattr(self.store, "revert_rule_exception", None)
        if not callable(rollback):
            return {"ok": False, "error": "atomic_rule_exception_revert_unavailable"}
        try:
            getter = getattr(self.store, "get_rule_exception", None)
            current = getter(exception_id) if callable(getter) else None
            if current is None:
                return {"ok": False, "error": "rule_exception_not_found"}
            admin = self.is_admin if is_admin is None else bool(is_admin)
            parent = self.store.get_record(current.parent_rule)
            child = self.store.get_record(current.child_exception)
            if parent is None or child is None:
                return {"ok": False, "error": "rule_exception_parent_or_child_not_found"}
            if not admin:
                if effective_context is None:
                    return {"ok": False, "error": "trusted effective agent context required"}
                context = self._context_dict(effective_context)
                owner = str(child.agent_instance_id or parent.agent_instance_id or "").strip()
                if context["agent_instance_id"] != owner:
                    return {"ok": False, "error": "rule exception revoke permission denied"}
                if context["project_ref"]:
                    child_scopes = self.store.list_rule_assignments(child.memory_id)
                    if child_scopes and not any(
                        canonical_project_ref(item.project_ref) == context["project_ref"]
                        for item in child_scopes
                        if item.effect == "include"
                    ):
                        return {"ok": False, "error": "rule exception revoke scope mismatch"}
            rollback_data = current.rollback if isinstance(current.rollback, Mapping) else {}
            expected_hash = str(
                rollback_data.get("parent_assignments_after_hash", "") or ""
            ).strip()
            if not expected_hash:
                return {"ok": False, "error": "structured_inverse_revision_missing"}
            now = _now_iso()
            owner_agent_id = str(child.agent_instance_id or parent.agent_instance_id or "").strip()
            inverse = RuleDecision(
                decision_id=stable_hash("rule-exception-revoke", exception_id, now),
                actor="admin" if admin else f"agent:{owner_agent_id}",
                before=current.to_dict(),
                after={**current.to_dict(), "active": False},
                reason=f"revoke rule exception {exception_id}",
                confidence=1.0,
                created_at=now,
                rule_id=current.child_exception,
                action="rule_exception_revoke",
                target_ids=[current.parent_rule, current.child_exception],
                status="undone",
                memory_id=current.child_exception,
                parent_rule_id=current.parent_rule,
                child_rule_id=current.child_exception,
                metadata={
                    "exception_id": exception_id,
                    "target_undo": True,
                    "parent_assignments_after_hash": expected_hash,
                },
                owner_agent_id=owner_agent_id,
            )
            exception = rollback(
                exception_id,
                expected_parent_assignment_hash=expected_hash,
                decision=inverse,
            )
        except (TypeError, ValueError, RuntimeError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, **exception.to_dict()}

    revoke_rule_exception = revoke_exception

    def list_rule_match_receipts(self, *, memory_id: str | None = None, agent_instance_id: str | None = None) -> dict[str, Any]:
        listing = getattr(self.store, "list_rule_match_receipts", None)
        if not callable(listing):
            return {"receipts": [], "total": 0}
        try:
            values = listing(memory_id=memory_id, agent_instance_id=agent_instance_id)
        except (TypeError, ValueError, RuntimeError):
            values = []
        payload = [item.to_dict() if hasattr(item, "to_dict") else dict(item) for item in values]
        return {"receipts": payload, "total": len(payload)}

    list_receipts = list_rule_match_receipts


__all__ = [
    "AUTO_TARGET_TYPES", "NARROWING_OUTCOMES", "RuleDecision", "RuleCreationService",
]
