"""Storage-neutral rule-exception scope planning shared by V1/V2 services.

The planner owns only audience semantics.  It does not import a store, mutate a
rule, or inspect untrusted browser identity.  Callers provide already trusted
Agent/project identifiers and the parent rule's current binding DTOs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .rule_scope import canonical_project_ref


class RuleExceptionPlanError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = str(code or "rule_exception_scope_invalid")
        super().__init__(self.code)


@dataclass(frozen=True)
class RuleExceptionScopePlan:
    agent_instance_id: str
    project_ref: str
    child_binding: Mapping[str, Any]
    parent_exclude: Mapping[str, Any]
    exclude_required: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_instance_id": self.agent_instance_id,
            "project_ref": self.project_ref,
            "child_binding": dict(self.child_binding),
            "parent_exclude": dict(self.parent_exclude),
            "exclude_required": bool(self.exclude_required),
        }


def _value(item: Any, key: str, default: Any = "") -> Any:
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def plan_rule_exception_scope(
    parent_bindings: Sequence[Any],
    *,
    agent_instance_id: str,
    project_ref: str,
) -> RuleExceptionScopePlan:
    """Validate a narrower current-Agent/current-project exception audience."""

    agent = str(agent_instance_id or "").strip()
    project = canonical_project_ref(project_ref)
    if not agent:
        raise RuleExceptionPlanError("rule_exception_agent_required")
    if not project:
        raise RuleExceptionPlanError("rule_exception_project_required")

    includes = [item for item in parent_bindings if str(_value(item, "status", "active") or "active") == "active" and str(_value(item, "effect", "include") or "include") == "include"]
    if not includes:
        raise RuleExceptionPlanError("rule_exception_parent_audience_required")

    for item in includes:
        target_type = str(_value(item, "target_type", "") or "").strip().casefold()
        target_id = str(_value(item, "target_id", "") or "").strip()
        parent_project = canonical_project_ref(_value(item, "project_ref", ""))
        if target_type == "agent" and target_id != agent:
            raise RuleExceptionPlanError("rule_exception_outside_parent_agent")
        if target_type == "agent_project" and (target_id != agent or parent_project != project):
            raise RuleExceptionPlanError("rule_exception_outside_parent_agent_project")
        if target_type == "project" and parent_project != project:
            raise RuleExceptionPlanError("rule_exception_outside_parent_project")
        # group/system/provider/runtime_role parents are broader than the
        # current Agent+Project and therefore can be narrowed safely.

    child = {
        "target_type": "agent_project",
        "target_id": agent,
        "project_ref": project,
        "effect": "include",
    }
    exclude = {
        "target_type": "agent_project",
        "target_id": agent,
        "project_ref": project,
        "effect": "exclude",
    }
    exclude_required = not any(
        str(_value(item, "status", "active") or "active") == "active"
        and str(_value(item, "target_type", "") or "").strip().casefold() == "agent_project"
        and str(_value(item, "target_id", "") or "").strip() == agent
        and canonical_project_ref(_value(item, "project_ref", "")) == project
        and str(_value(item, "effect", "include") or "include") == "exclude"
        for item in parent_bindings
    )
    return RuleExceptionScopePlan(
        agent_instance_id=agent,
        project_ref=project,
        child_binding=child,
        parent_exclude=exclude,
        exclude_required=exclude_required,
    )


__all__ = [
    "RuleExceptionPlanError", "RuleExceptionScopePlan",
    "plan_rule_exception_scope",
]
