"""Rule-audience validation and deterministic matching.

This deliberately has no database dependency so the same matcher is used by
MCP, hooks, bootstrap and future GUI governance.
"""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
from typing import Iterable

from .schema_v3 import EffectiveAgentContext, RuleAssignment


TARGET_TYPES = {
    "agent", "group", "project", "agent_project", "provider",
    "runtime_role", "system",
}
EFFECTS = {"include", "exclude"}


def canonical_project_ref(value: str | os.PathLike[str] | None) -> str:
    """Canonical project identity shared by rules and raw history.

    Only filesystem-looking values are resolved.  Legacy logical labels stay
    labels, while Windows spelling/slash aliases collapse to one stable key.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    filesystem_ref = (
        "/" in text or "\\" in text or bool(os.path.isabs(text))
        or (len(text) >= 2 and text[1] == ":")
    )
    if not filesystem_ref:
        return text
    try:
        resolved = Path(text).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return ""
    normalized = os.path.normcase(str(resolved)).replace("\\", "/")
    if normalized == "/" or (
        len(normalized) == 3
        and normalized[1:] == ":/"
    ):
        return normalized
    return normalized.rstrip("/")


def normalize_assignment(value: dict | RuleAssignment) -> RuleAssignment:
    if isinstance(value, RuleAssignment):
        assignment = value
    elif isinstance(value, dict):
        assignment = RuleAssignment(
            memory_id=str(value.get("memory_id", "")),
            target_type=str(value.get("target_type", "")),
            target_id=str(value.get("target_id", "")),
            project_ref=str(value.get("project_ref", "")),
            effect=str(value.get("effect", "include")),
            priority_override=value.get("priority_override"),
        )
    else:
        raise ValueError("audience assignment must be an object")
    if assignment.target_type not in TARGET_TYPES:
        raise ValueError("invalid audience target_type")
    if assignment.effect not in EFFECTS:
        raise ValueError("invalid audience effect")
    if assignment.target_type in {"project", "agent_project"}:
        raw_project_ref = (
            assignment.project_ref
            or (
                assignment.target_id
                if assignment.target_type == "project" else ""
            )
        )
        project_ref = canonical_project_ref(raw_project_ref)
        if not project_ref:
            raise ValueError(
                f"audience {assignment.target_type} requires a valid project_ref"
            )
        assignment = replace(
            assignment,
            target_id=(
                "" if assignment.target_type == "project"
                else assignment.target_id
            ),
            project_ref=project_ref,
        )
    elif assignment.project_ref:
        raise ValueError(
            f"audience {assignment.target_type} cannot carry project_ref; "
            "use agent_project for Agent + project scope"
        )
    if assignment.target_type in {"agent", "provider", "runtime_role"} and not assignment.target_id:
        raise ValueError(f"audience {assignment.target_type} requires target_id")
    if assignment.target_type == "agent_project" and (not assignment.target_id or not assignment.project_ref):
        raise ValueError("audience agent_project requires target_id and project_ref")
    if assignment.priority_override is not None and (
        isinstance(assignment.priority_override, bool)
        or not isinstance(assignment.priority_override, int)
        or not -100 <= assignment.priority_override <= 100
    ):
        raise ValueError("audience priority_override must be an integer between -100 and 100")
    return assignment


def assignment_matches(assignment: RuleAssignment, context: EffectiveAgentContext) -> bool:
    """Return whether audience shape applies; exclude precedence is caller-owned."""
    try:
        assignment = normalize_assignment(assignment)
    except ValueError:
        return False
    kind = assignment.target_type
    context_project_ref = canonical_project_ref(context.project_ref)
    if kind == "system":
        return True
    if kind == "group":
        return not assignment.target_id or assignment.target_id == context.share_group_id
    if kind == "agent":
        return assignment.target_id == context.agent_instance_id
    if kind == "project":
        return (
            bool(context_project_ref)
            and assignment.project_ref == context_project_ref
        )
    if kind == "agent_project":
        return (assignment.target_id == context.agent_instance_id
                and bool(context_project_ref)
                and assignment.project_ref == context_project_ref)
    if kind == "provider":
        return bool(context.provider) and assignment.target_id.casefold() == context.provider.casefold()
    if kind == "runtime_role":
        # Unknown runtime role must not be guessed into a role-scoped rule.
        return bool(context.runtime_role) and assignment.target_id.casefold() == context.runtime_role.casefold()
    return False


def effective_assignments(
    assignments: Iterable[RuleAssignment], context: EffectiveAgentContext,
) -> tuple[list[RuleAssignment], list[RuleAssignment]]:
    matched = [item for item in assignments if assignment_matches(item, context)]
    excludes = [item for item in matched if item.effect == "exclude"]
    includes = [item for item in matched if item.effect == "include"]
    return includes, excludes


def can_manage_assignment(assignment: RuleAssignment, *, actor_agent_id: str, is_admin: bool) -> bool:
    """Non-admins may manage only their own agent or agent-project audience."""
    try:
        assignment = normalize_assignment(assignment)
    except ValueError:
        return False
    if is_admin:
        return True
    return (
        assignment.target_type in {"agent", "agent_project"}
        and assignment.target_id == actor_agent_id
    )
