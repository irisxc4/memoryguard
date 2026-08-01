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
NARROWING_OUTCOMES = frozenset({"corrected", "violated"})
FEEDBACK_OUTCOMES = frozenset({
    "followed", "violated", "not_applicable", "corrected", "exception", "ignored",
})


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
    ) -> tuple[dict[str, Any], float, str]:
        """Resolve and validate one assignment without widening authority."""
        agent_id = context["agent_instance_id"]
        project_ref = context["project_ref"]
        if not agent_id:
            raise ValueError("trusted agent_instance_id is required")

        if requested_scope is None:
            target_type = "agent_project" if project_ref else "agent"
            target_id = agent_id
            selected_project = project_ref if target_type == "agent_project" else ""
            confidence = 0.96 if selected_project else 0.99
            reason = (
                "inferred from trusted current agent and canonical cwd project"
                if selected_project else
                "inferred from trusted current agent; no canonical project available"
            )
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
                context, requested, manual=bool(manual), is_admin=admin,
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
        structured_original = None
        structured_list = getattr(self.store, "list_rule_decisions", None)
        structured_undo = getattr(self.store, "undo_rule_decision", None)
        if callable(structured_list) and callable(structured_undo):
            try:
                candidates = structured_list(undo_id=token)
                structured_original = candidates[0] if candidates else None
            except Exception:
                structured_original = None
        if structured_original is not None:
            try:
                inverse = structured_undo(
                    structured_original.decision_id,
                    actor=("admin" if admin else f"agent:{context['agent_instance_id']}"),
                )
                inverse.status = "undone"
                inverse.undo_id = token
                inverse.scope_confidence = structured_original.scope_confidence
                inverse.scope_reason = "explicit undo of persisted pre-rule snapshot"
                self._persist_structured_decision(inverse)
                return inverse
            except (TypeError, ValueError, RuntimeError):
                pass
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
        feedback = RuleMatchFeedback(
            feedback_id=feedback_id, receipt_id=str(receipt_id), outcome=outcome,
            actor=str(actor).strip(), evidence=str(evidence or ""),
            confidence=confidence_value, created_at=_now_iso(),
        )
        existing_feedback = None
        lookup_feedback = getattr(self.store, "get_rule_match_feedback_by_receipt", None)
        if callable(lookup_feedback):
            try:
                existing_feedback = lookup_feedback(str(receipt_id))
            except Exception:
                existing_feedback = None
        try:
            saved = existing_feedback or self.store.append_rule_match_feedback(feedback)
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            return self._blocked(action="rule_feedback", reason=str(exc), receipt_id=receipt_id)
        receipt = self.store.get_rule_match_receipt(str(receipt_id))
        if receipt is not None and existing_feedback is None:
            record_scope = getattr(self.store, "record_rule_scope", None)
            if callable(record_scope):
                context_for_stats = self._context_dict(effective_context) if effective_context is not None else {}
                scope_outcome = {
                    "followed": "accepted",
                    "corrected": "corrected",
                    "violated": "corrected",
                    "exception": "wrong_scope",
                    "not_applicable": "wrong_scope",
                }.get(outcome, "ignored")
                try:
                    record_scope(
                        receipt.memory_id,
                        agent_instance_id=str(receipt.agent_instance_id or context_for_stats.get("agent_instance_id", "")),
                        project_ref=str(context_for_stats.get("project_ref", "")),
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
            metadata={"outcome": outcome, "evidence": evidence, "confidence": confidence_value},
            actor=str(actor).strip(),
        )
        if existing_feedback is not None:
            return base
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
            "provider": "", "project_ref": "", "runtime_role": "",
            "runtime_agent_id": "", "parent_agent_id": "",
        }
        if context["agent_instance_id"] != receipt.agent_instance_id:
            return self._blocked(action="rule_narrow", reason="feedback agent does not match receipt agent", receipt_id=receipt_id, feedback_id=feedback_id)
        assignments = self.store.list_rule_assignments(parent.memory_id)
        if not assignments:
            return self._blocked(action="rule_narrow", reason="parent rule has no governed assignment", memory_id=parent.memory_id, receipt_id=receipt_id, feedback_id=feedback_id)
        # Narrowing is monotone: agent -> agent_project only.  Never convert a
        # project-specific assignment back to agent, group, system, provider,
        # or runtime role.  A no-op is preferable to widening.
        parent_agent = next((item for item in assignments if item.target_type == "agent" and item.effect == "include" and item.target_id == receipt.agent_instance_id), None)
        project_ref = context["project_ref"]
        if parent_agent is None or not project_ref:
            return self._blocked(action="rule_narrow", reason="no strictly narrower trusted agent_project scope available", memory_id=parent.memory_id, receipt_id=receipt_id, feedback_id=feedback_id)
        child_assignment = {
            "target_type": "agent_project", "target_id": receipt.agent_instance_id,
            "project_ref": project_ref, "effect": "include",
        }
        return self.create_rule_from_text(
            evidence.strip() or parent.body,
            EffectiveAgentContext(
                agent_instance_id=context["agent_instance_id"], share_group_id=self.group_id,
                provider=context.get("provider", ""), project_ref=project_ref,
                runtime_role=context.get("runtime_role", ""),
                runtime_agent_id=context.get("runtime_agent_id", ""),
                parent_agent_id=context.get("parent_agent_id", ""),
            ),
            requested_scope=child_assignment,
            manual=False,
            parent_rule_id=parent.memory_id,
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
        context = self._context_dict(effective_context) if effective_context is not None else {
            "agent_instance_id": str(receipt.agent_instance_id or ""), "share_group_id": self.group_id,
            "provider": "", "project_ref": "", "runtime_role": "", "runtime_agent_id": "", "parent_agent_id": "",
        }
        if context["agent_instance_id"] != receipt.agent_instance_id:
            return self._blocked(action="rule_exception", reason="feedback agent does not match receipt agent", receipt_id=receipt_id, feedback_id=feedback_id)
        # Exception assignment is always an include on a narrower trusted
        # audience.  We intentionally never add an exclude relation to the
        # parent; parent_rule_id is carried in the decision/provenance metadata.
        project_ref = context["project_ref"]
        target_type = "agent_project" if project_ref else "agent"
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
        assignment = {
            "target_type": target_type, "target_id": receipt.agent_instance_id,
            "project_ref": project_ref if project_ref else "", "effect": "include",
        }
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
        append_exception = getattr(self.store, "append_rule_exception", None)
        if callable(append_exception) and child.memory_id:
            try:
                append_exception(RuleException(
                    parent_rule=parent.memory_id,
                    child_exception=child.memory_id,
                    priority=0,
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
