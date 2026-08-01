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
    MemoryEvent,
    RuleMatchFeedback,
    RuleDecision,
    RuleException,
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
NARROWING_OPPOSED_THRESHOLD = 2


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
            }
        raise ValueError("effective agent context is required")

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
        )
        self._persist_structured_decision(decision)
        return decision

    def _persist_structured_decision(self, decision: RuleDecision) -> RuleDecision:
        """Use the lifecycle store model when available; stay compatible with v3.2."""
        append = getattr(self.store, "append_rule_decision", None)
        if callable(append):
            try:
                return append(decision)
            except (TypeError, ValueError, RuntimeError):
                pass
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
        before: Any | None = None,
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
            "scope_confidence": float(scope_confidence),
            "assignment": target,
            "parent_rule_id": parent_rule_id,
            **dict(metadata or {}),
        }
        decision = RuleDecision(
            decision_id=decision_id,
            actor=actor or "agent:unknown",
            before={} if before is None else before, after=after,
            reason=json.dumps(reason_payload, ensure_ascii=False, separators=(",", ":")),
            confidence=max(0.0, min(1.0, float(scope_confidence or 1.0))),
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
            scope_confidence=float(scope_confidence),
            scope_reason=scope_reason,
            blocked_reason=blocked_reason or str(result.get("blocked_reason", "") or ""),
        )
        return self._persist_structured_decision(decision)

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
            metadata={"manual": bool(manual), "is_admin": admin, "idempotency_key": idempotency_key},
        )

    create_rule = create_rule_from_text

    def _target_undo(
        self,
        decision: RuleDecision,
        *,
        token: str,
        actor: str,
    ) -> RuleDecision | None:
        """Target-level inverse operation for one rule decision.

        v2: restores only the record + assignments touched by this decision,
        never the whole shared-memory group.  Returns None when the decision
        does not name a rule or the rule no longer exists.
        """
        if not decision.memory_id and not decision.rule_id:
            return None
        memory_id = decision.memory_id or decision.rule_id
        before = decision.before if isinstance(decision.before, dict) else {}
        before_record = before.get("record") if isinstance(before.get("record"), dict) else before.get("after")
        before_assignments = before.get("assignments")
        get_record = getattr(self.store, "get_record", None)
        if callable(get_record):
            try:
                existing = get_record(memory_id)
            except Exception:
                existing = None
        else:
            existing = None
        if existing is not None:
            # Soft-delete/shadow the rule; preserve history.
            delete = getattr(self.store, "delete", None)
            if callable(delete):
                try:
                    delete(memory_id, actor=actor, manual_override=True)
                except (TypeError, ValueError, RuntimeError):
                    pass
            elif hasattr(self.store, "update_record"):
                from .schema_v3 import SharedMemoryStatus
                try:
                    existing.status = SharedMemoryStatus.DELETED
                    self.store.update_record(existing)
                except (TypeError, ValueError, RuntimeError):
                    pass
        # Restore the exact assignments this decision created (if any).
        if isinstance(before_assignments, list) and before_assignments:
            set_assignments = getattr(self.store, "set_rule_assignments", None)
            if callable(set_assignments):
                try:
                    set_assignments(
                        memory_id, [dict(item) for item in before_assignments],
                        automatic=True, actor_agent_id=str(decision.actor or "").replace("agent:", ""),
                    )
                except (TypeError, ValueError, RuntimeError):
                    pass
        now = _now_iso()
        inverse = RuleDecision(
            decision_id=stable_hash("rule-decision-undo", decision.decision_id, actor, now),
            actor=actor,
            before=decision.after,
            after=decision.before,
            reason=f"target-level undo of {decision.decision_id}",
            confidence=decision.confidence,
            undo_id=decision.decision_id,
            created_at=now,
            rule_id=memory_id,
            action="rule_undo",
            target_ids=[memory_id],
            status="undone",
            memory_id=memory_id,
            parent_rule_id=decision.parent_rule_id,
            scope_confidence=decision.scope_confidence,
            scope_reason="explicit target-level undo",
            blocked_reason="",
            metadata={"target_undo": True, "supersedes_token": token},
        )
        return self._persist_structured_decision(inverse)

    def undo_rule(
        self,
        undo_id: str,
        effective_context: EffectiveAgentContext | Mapping[str, Any],
        *,
        is_admin: bool | None = None,
    ) -> RuleDecision:
        """Rollback the snapshot represented by ``undo_id``."""
        context = self._context_dict(effective_context)
        token = str(undo_id or "").strip()
        if not token:
            return self._blocked(action="rule_undo", reason="undo_id is required", context=context)
        snapshots = []
        try:
            snapshots = self.store.list_version_snapshots()
        except Exception:
            snapshots = []
        snapshot = next((item for item in snapshots if item.get("version_id") == token), None)
        if snapshot is None:
            return self._blocked(action="rule_undo", reason="undo_id not found", context=context, undo_id=token)
        if snapshot.get("share_group_id", self.group_id) != self.group_id:
            return self._blocked(action="rule_undo", reason="undo_id belongs to another share group", context=context, undo_id=token)
        admin = self.is_admin if is_admin is None else bool(is_admin)
        if not admin and not context["agent_instance_id"]:
            return self._blocked(
                action="rule_undo",
                reason="trusted agent_instance_id is required for undo",
                context=context,
                undo_id=token,
            )
        # The snapshot reason is the only persisted owner hint available in
        # legacy stores.  Admins may undo any rule; agents may undo a decision
        # whose rule record or reason names the current agent.
        if not admin:
            reason = str(snapshot.get("reason", ""))
            owner_match = context["agent_instance_id"] and context["agent_instance_id"] in reason
            structured_list = getattr(self.store, "list_rule_decisions", None)
            if callable(structured_list):
                try:
                    owner_match = owner_match or any(
                        context["agent_instance_id"] in str(item.actor or "")
                        for item in structured_list(undo_id=token)
                    )
                except Exception:
                    pass
            if context["agent_instance_id"] and not owner_match:
                return self._blocked(action="rule_undo", reason="undo permission denied", context=context, undo_id=token)
        # v2: daily undo must be a target-level inverse operation, never a
        # whole-share-group rollback.  A shared group can contain rules
        # written by other Agents after this rule was created; rolling back
        # the pre-create snapshot would erase their writes.
        structured_original = None
        structured_list = getattr(self.store, "list_rule_decisions", None)
        if callable(structured_list):
            try:
                candidates = structured_list(undo_id=token)
                structured_original = candidates[0] if candidates else None
            except Exception:
                structured_original = None
        if structured_original is not None:
            try:
                inverse = self._target_undo(
                    structured_original, token=token,
                    actor=("admin" if admin else f"agent:{context['agent_instance_id']}"),
                )
                if inverse is not None:
                    return inverse
            except (TypeError, ValueError, RuntimeError):
                pass
        # No structured decision points to a specific rule: fall back to the
        # versioned snapshot only as a compatibility path for legacy tokens.
        try:
            self.store.rollback_to_version(token)
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            return self._blocked(action="rule_undo", reason=str(exc), context=context, undo_id=token)
        decision_id = stable_hash("rule-undo-decision", self.group_id, token, context["agent_instance_id"], _now_iso())
        try:
            self.store.append_decision(DecisionEvent(
                event_id=decision_id,
                actor=("admin" if admin else f"agent:{context['agent_instance_id']}"),
                action="rule_undo", target_ids=[token],
                reason=f"rollback to pre-rule snapshot {token}", created_at=_now_iso(),
            ))
        except Exception:
            pass
        return self._decision(
            action="rule_undo", status="undone", result={
                "decision_id": decision_id, "version_id": token,
            }, undo_id=token, scope_reason="explicit undo of persisted pre-rule snapshot",
            actor=("admin" if admin else f"agent:{context['agent_instance_id']}"),
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
        token = decision.undo_id if decision is not None and decision.undo_id else str(decision_id or "")
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
        return self.undo_rule(token, effective_context, is_admin=is_admin)

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
        if callable(list_scope_stats):
            try:
                payload["scope_stats"] = [
                    item.to_dict() if hasattr(item, "to_dict") else dict(item)
                    for item in list_scope_stats()
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
        auto_created = [d for d in decisions if str(getattr(d, "action", "") or "") == "rule_create_auto"]
        manual_created = [d for d in decisions if str(getattr(d, "action", "") or "") == "rule_create_manual"]
        total_created = len(auto_created) + len(manual_created)
        fallback_auto = [
            d for d in auto_created
            if "fallback" in str(getattr(d, "scope_reason", "") or getattr(d, "reason", "") or "")
        ]
        payload["auto_scope"] = {
            "auto_created": len(auto_created),
            "manual_created": len(manual_created),
            "auto_scope_coverage": (len(auto_created) / total_created) if total_created else 0.0,
            "auto_fallback_count": len(fallback_auto),
            "accuracy_note": "accuracy requires a human-labeled golden set; assignment count is not accuracy",
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
        actor: str,
        evidence: str = "",
        *,
        
        confidence: float = 1.0,
        effective_context: EffectiveAgentContext | Mapping[str, Any] | None = None,
        idempotency_key: str = "",
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
        if not str(actor or "").strip():
            return self._blocked(action="rule_feedback", reason="actor is required", receipt_id=receipt_id)
        feedback_id = str(idempotency_key or "").strip() or stable_hash(
            "rule-feedback", receipt_id, outcome, actor, evidence,
        )
        # v2: feedback is an append-only event stream.  Derive ``source`` from
        # the actor prefix so the effective-feedback resolver can order
        # user > agent > hook > unobserved correctly.
        actor_value = str(actor).strip()
        actor_lower = actor_value.casefold()
        if actor_lower.startswith("user") or actor_lower == "user":
            feedback_source = "user"
        elif actor_lower.startswith("hook"):
            feedback_source = "hook"
        else:
            feedback_source = "agent"
        # An ``unobserved`` feedback event must never be recorded with a
        # positive confidence; the model enforces this in __post_init__.
        feedback = RuleMatchFeedback(
            feedback_id=feedback_id, receipt_id=str(receipt_id), outcome=outcome,
            actor=actor_value, evidence=str(evidence or ""),
            confidence=confidence_value, created_at=_now_iso(),
            source=feedback_source,
        )
        # Determine the current effective feedback before appending so we can
        # tell whether this new event actually changes the observable outcome.
        lookup_feedback = getattr(self.store, "get_rule_match_feedback_by_receipt", None)
        prior_effective = None
        if callable(lookup_feedback):
            try:
                prior_effective = lookup_feedback(str(receipt_id))
            except Exception:
                prior_effective = None
        try:
            saved = self.store.append_rule_match_feedback(feedback)
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            return self._blocked(action="rule_feedback", reason=str(exc), receipt_id=receipt_id)
        receipt = self.store.get_rule_match_receipt(str(receipt_id))
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
                context_for_stats = self._context_dict(effective_context) if effective_context is not None else {}
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
            },
            actor=actor_value,
        )
        if outcome in NARROWING_OUTCOMES:
            return self._narrow_from_feedback(
                receipt_id, feedback_id, effective_context, outcome=outcome,
                evidence=evidence, base=base,
            )
        if outcome == "exception":
            return self.create_child_exception(
                receipt_id, effective_context, evidence=evidence,
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

    def _narrow_evidence_ready(
        self, receipt_id: str,
    ) -> tuple[bool, str, int, int, int]:
        """Check the v2 narrowing threshold on the argument-supplied list."""
        return False, "not_applicable_not_enough_evidence", 0, 0, 0

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
        }
        if context["agent_instance_id"] != receipt.agent_instance_id:
            return self._blocked(action="rule_narrow", reason="feedback agent does not match receipt agent", receipt_id=receipt_id, feedback_id=feedback_id)
        assignments = self.store.list_rule_assignments(parent.memory_id)
        if not assignments:
            return self._blocked(action="rule_narrow", reason="parent rule has no governed assignment", memory_id=parent.memory_id, receipt_id=receipt_id, feedback_id=feedback_id)
        project_ref = context["project_ref"] or receipt.project_ref
        if not project_ref:
            return self._blocked(action="rule_narrow", reason="no trusted project context for narrowing", memory_id=parent.memory_id, receipt_id=receipt_id, feedback_id=feedback_id)
        # v2: a single feedback event must never change scope.  Require at
        # least 3 explicit ``not_applicable`` events from at least 2 distinct
        # sessions for the same agent/project, with no strong ``followed``
        # evidence cancelling them.
        events = self._collect_receipt_feedbacks(receipt_id)
        scope_error_events = [
            item for item in events
            if item.outcome == "not_applicable" and item.source != "unobserved"
        ]
        followed_events = [
            item for item in events
            if item.outcome == "followed" and item.confidence >= 0.7
        ]
        sessions = {item.actor for item in scope_error_events}
        # Include the current event's session in the count.
        current_session = str(context.get("session_id", "") or receipt.session_id or "")
        sessions.add(
            current_session or f"session:{receipt.agent_instance_id}:{receipt.session_id}"
        )
        if len(scope_error_events) < NARROWING_MIN_EVENTS:
            return self._blocked(action="rule_narrow", reason="not_applicable_not_enough_evidence", memory_id=parent.memory_id, receipt_id=receipt_id, feedback_id=feedback_id, scope_reason=f"needed {NARROWING_MIN_EVENTS}, have {len(scope_error_events)}")
        if len(sessions) < NARROWING_MIN_SESSIONS:
            return self._blocked(action="rule_narrow", reason="not_applicable_not_enough_sessions", memory_id=parent.memory_id, receipt_id=receipt_id, feedback_id=feedback_id, scope_reason=f"needed {NARROWING_MIN_SESSIONS} sessions, have {len(sessions)}")
        if len(followed_events) >= NARROWING_OPPOSED_THRESHOLD:
            return self._blocked(action="rule_narrow", reason="followed_evidence_cancels_narrowing", memory_id=parent.memory_id, receipt_id=receipt_id, feedback_id=feedback_id)
        # Narrowing is monotone: agent -> agent include + agent_project exclude
        # for the offending project.  Never convert a project-specific
        # assignment back to agent, group, system, provider, or runtime role.
        parent_agent = next((item for item in assignments if item.target_type == "agent" and item.effect == "include" and item.target_id == receipt.agent_instance_id), None)
        if parent_agent is None:
            # If nobody on the parent matches this agent at agent level, it
            # may already be narrowed; a no-op is preferable to widening.
            return self._blocked(action="rule_narrow", reason="no strictly narrower trusted agent_project scope available", memory_id=parent.memory_id, receipt_id=receipt_id, feedback_id=feedback_id)
        # Create the child narrower rule (agent_project include).
        child = self.create_rule_from_text(
            evidence.strip() or parent.body,
            EffectiveAgentContext(
                agent_instance_id=context["agent_instance_id"], share_group_id=self.group_id,
                provider=context.get("provider", ""), project_ref=project_ref,
                runtime_role=context.get("runtime_role", ""),
                runtime_agent_id=context.get("runtime_agent_id", ""),
                parent_agent_id=context.get("parent_agent_id", ""),
            ),
            requested_scope={
                "target_type": "agent_project", "target_id": receipt.agent_instance_id,
                "project_ref": project_ref, "effect": "include",
            },
            manual=False,
            priority=parent.priority + 1,
            parent_rule_id=parent.memory_id,
        )
        child.action = "rule_narrow"
        child.parent_rule_id = parent.memory_id
        # The parent rule must stop applying to the offending project while
        # continuing to apply everywhere else.  Replace the agent-include with
        # agent-include + agent_project-exclude for that project.
        updated_assignments = []
        seen_project_exclude = False
        for item in assignments:
            if (
                item.target_type == "agent"
                and item.target_id == receipt.agent_instance_id
                and item.effect == "include"
            ):
                updated_assignments.append({
                    "target_type": "agent", "target_id": receipt.agent_instance_id,
                    "project_ref": "", "effect": "include",
                    "priority_override": item.priority_override,
                })
            elif (
                item.effect == "exclude"
                and item.target_type == "agent_project"
                and item.target_id == receipt.agent_instance_id
                and item.project_ref == project_ref
            ):
                seen_project_exclude = True
            else:
                updated_assignments.append({
                    "target_type": item.target_type, "target_id": item.target_id,
                    "project_ref": item.project_ref, "effect": item.effect,
                    "priority_override": item.priority_override,
                })
        if not seen_project_exclude:
            updated_assignments.append({
                "target_type": "agent_project", "target_id": receipt.agent_instance_id,
                "project_ref": project_ref, "effect": "exclude",
            })
        try:
            final_assignments = self.store.set_rule_assignments(
                parent.memory_id, updated_assignments,
                automatic=True, actor_agent_id=receipt.agent_instance_id,
            )
        except (TypeError, ValueError, RuntimeError) as exc:
            return self._blocked(action="rule_narrow", reason=f"parent_exclude_update_failed:{exc}", memory_id=parent.memory_id, receipt_id=receipt_id, feedback_id=feedback_id)
        after = dict(child.after or {})
        after.update({
            "narrowed_parent_rule": parent.memory_id,
            "parent_assignments_after": [
                item.to_dict() if hasattr(item, "to_dict") else dict(item)
                for item in final_assignments
            ],
            "excluded_project": project_ref,
        })
        child.after = after
        child_decision = self._persist_structured_decision(child)
        return child_decision

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
        context = self._context_dict(effective_context) if effective_context is not None else {
            "agent_instance_id": str(receipt.agent_instance_id or ""), "share_group_id": self.group_id,
            "provider": "", "project_ref": "", "runtime_role": "", "runtime_agent_id": "", "parent_agent_id": "",
        }
        if context["agent_instance_id"] != receipt.agent_instance_id:
            return self._blocked(action="rule_exception", reason="feedback agent does not match receipt agent", receipt_id=receipt_id, feedback_id=feedback_id)
        # v2: an exception must *actually* narrow the parent.  We create a
        # child rule tailored to the exception context and give the parent an
        # exclude assignment for that same context, so the existing
        # include/exclude matcher naturally suppresses the parent there.
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
        child = self.create_rule_from_text(
            evidence.strip() or f"Exception to rule: {parent.body}",
            EffectiveAgentContext(
                agent_instance_id=context["agent_instance_id"], share_group_id=self.group_id,
                provider=context.get("provider", ""), project_ref=project_ref,
                runtime_role=context.get("runtime_role", ""),
                runtime_agent_id=context.get("runtime_agent_id", ""),
                parent_agent_id=context.get("parent_agent_id", ""),
            ),
            requested_scope=assignment,
            manual=False,
            priority=parent.priority + 1,
            parent_rule_id=parent.memory_id,
        )
        child.action = "rule_exception"
        child.parent_rule_id = parent.memory_id
        child.after = dict(child.after or {})
        child.after.update({
            "exception": True,
            "parent_rule_id": parent.memory_id,
            "feedback_id": feedback_id,
            "receipt_id": receipt_id,
        })
        child.reason = json.dumps({
            "exception": True,
            "parent_rule_id": parent.memory_id,
            "feedback_id": feedback_id,
            "receipt_id": receipt_id,
        }, ensure_ascii=False, separators=(",", ":"))
        # Add the parent exclude for the exception project so the original
        # rule stops applying there.  The child with higher priority then
        # covers the exception context alone.
        updated_assignments = []
        seen_exclude = False
        for item in parent_assignments:
            if item.effect == "exclude":
                if (
                    item.target_type == "agent_project"
                    and item.target_id == receipt.agent_instance_id
                    and item.project_ref == project_ref
                ):
                    seen_exclude = True
                    updated_assignments.append({
                        "target_type": item.target_type, "target_id": item.target_id,
                        "project_ref": item.project_ref, "effect": item.effect,
                        "priority_override": item.priority_override,
                    })
                    continue
            else:
                updated_assignments.append({
                    "target_type": item.target_type, "target_id": item.target_id,
                    "project_ref": item.project_ref, "effect": item.effect,
                    "priority_override": item.priority_override,
                })
        if not seen_exclude:
            updated_assignments.append({
                "target_type": "agent_project", "target_id": receipt.agent_instance_id,
                "project_ref": project_ref, "effect": "exclude",
            })
        try:
            final_assignments = self.store.set_rule_assignments(
                parent.memory_id, updated_assignments,
                automatic=True, actor_agent_id=receipt.agent_instance_id,
            )
        except (TypeError, ValueError, RuntimeError) as exc:
            return self._blocked(action="rule_exception", reason=f"parent_exclude_update_failed:{exc}", memory_id=parent.memory_id, receipt_id=receipt_id, feedback_id=feedback_id)
        child.after.update({
            "parent_assignments_after": [
                item.to_dict() if hasattr(item, "to_dict") else dict(item)
                for item in final_assignments
            ],
            "excluded_project": project_ref,
        })
        append_exception = getattr(self.store, "append_rule_exception", None)
        if callable(append_exception) and child.memory_id:
            try:
                append_exception(RuleException(
                    parent_rule=parent.memory_id,
                    child_exception=child.memory_id,
                    priority=parent.priority + 1,
                    reason=evidence or "explicit exception feedback",
                    rollback={"undo_id": child.undo_id},
                    created_at=_now_iso(),
                    updated_at=_now_iso(),
                ))
            except (TypeError, ValueError, RuntimeError):
                pass
        self._persist_structured_decision(child)
        return child

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
        if not parent_rule or not child_exception:
            return {"ok": False, "error": "rule_exception_requires_parent_and_child"}
        if parent_rule == child_exception:
            return {"ok": False, "error": "rule_exception_cannot_reference_itself"}
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

    def revoke_exception(self, exception_id: str) -> dict[str, Any]:
        rollback = getattr(self.store, "rollback_rule_exception", None)
        if not callable(rollback):
            return {"ok": False, "error": "rule_exception_store_unavailable"}
        try:
            exception = rollback(exception_id)
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
