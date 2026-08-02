"""Rule Binding: *where* a rule definition applies (P3).

``RuleDefinition`` is shared; ``RuleBinding`` is not.  Every binding carries the
share group and the full audience shape, so a merged definition keeps each
Agent/project boundary exactly as it was.  The merge invariant ``before_bindings
== after_bindings`` lives here, and automatic creation is restricted to the same
narrow audience dimensions the legacy assignment layer already allows.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

from .rule_scope import TARGET_TYPES, canonical_project_ref
from .schema_v3 import _now_iso, stable_hash

# Automatic (merge/sync) bindings may only narrow to the current Agent, at most
# paired with its project.  Never system/group/provider/runtime_role/all_agents.
AUTO_ALLOWED_TARGET_TYPES = {"agent", "agent_project"}
# Sources that must respect the automatic scope boundary.
AUTO_SOURCES = {"auto", "backfill"}
# Created-by values allowed to carry system/broad audiences (human governance).
MANUAL_SOURCES = {"manual", "human", "user", "admin"}
# Migration is a lossless, audited copy of a legacy assignment that P0-P2
# already permitted (group/project/provider/runtime_role/system).  It is not an
# automatic broadening: the binding is only ever built FROM a legacy assignment
# and carries a legacy-assignment hash + migration run id, so it may carry the
# same broad scopes the legacy layer already granted.
MIGRATION_SOURCES = {"migration"}


@dataclass(frozen=True)
class RuleBinding:
    """Audience relation between a Definition and one scope dimension."""
    binding_id: str
    definition_id: str
    share_group_id: str
    target_type: str = "agent"
    target_id: str = ""
    project_ref: str = ""
    provider: str = ""
    runtime_role: str = ""
    effect: str = "include"
    priority: int = 0
    owner_agent_id: str = ""
    created_by: str = "manual"  # auto | backfill | manual | human | user | admin
    authorization: str = ""
    status: str = "active"
    revision: int = 1
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding_id": self.binding_id,
            "definition_id": self.definition_id,
            "share_group_id": self.share_group_id,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "project_ref": self.project_ref,
            "provider": self.provider,
            "runtime_role": self.runtime_role,
            "effect": self.effect,
            "priority": self.priority,
            "owner_agent_id": self.owner_agent_id,
            "created_by": self.created_by,
            "authorization": self.authorization,
            "status": self.status,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuleBinding":
        return cls(
            binding_id=data["binding_id"],
            definition_id=data["definition_id"],
            share_group_id=data.get("share_group_id", ""),
            target_type=data.get("target_type", "agent"),
            target_id=data.get("target_id", ""),
            project_ref=data.get("project_ref", ""),
            provider=data.get("provider", ""),
            runtime_role=data.get("runtime_role", ""),
            effect=data.get("effect", "include"),
            priority=int(data.get("priority", 0) or 0),
            owner_agent_id=data.get("owner_agent_id", ""),
            created_by=data.get("created_by", "manual"),
            authorization=data.get("authorization", ""),
            status=data.get("status", "active"),
            revision=int(data.get("revision", 1) or 1),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )

    def audience_identity(self) -> tuple[Any, ...]:
        """Identity of the permission boundary, independent of definition_id.

        This is what "before == after" compares during a merge: two bindings are
        the same *permission* if every audience dimension matches.  definition_id
        is deliberately excluded — it is the only field a merge may change.
        """
        return (
            self.share_group_id,
            self.target_type,
            canonical_project_ref(self.project_ref),
            self.target_id,
            self.provider.casefold(),
            self.runtime_role.casefold(),
            self.effect,
            self.priority,
        )

    def with_definition(self, definition_id: str) -> "RuleBinding":
        return replace(
            self,
            definition_id=definition_id,
            revision=self.revision + 1,
            updated_at=_now_iso(),
        )


def binding_identity_key(binding: RuleBinding) -> str:
    """Stable string key for a binding's permission boundary."""
    return stable_hash(
        "rule-binding-audience",
        json.dumps(binding.audience_identity(), ensure_ascii=False),
    )


def validate_binding_scope(binding: RuleBinding) -> RuleBinding:
    """Enforce the automatic/broad audience boundary on a binding.

    Mirror of ``validate_automatic_assignment`` at the Definition layer: only
    manual/human governance may create system/group/provider/runtime_role/all-
    agents bindings.  Backfill copies whatever already exists, so its scope is
    inherited from the legacy assignment it converts — it never broadens.
    """
    source = str(binding.created_by or "").casefold()
    if binding.target_type not in TARGET_TYPES:
        raise ValueError(f"invalid binding target_type: {binding.target_type!r}")
    if binding.target_type in {"project", "agent_project"}:
        if not canonical_project_ref(binding.project_ref):
            raise ValueError("binding agent_project requires a valid project_ref")
    if source in AUTO_SOURCES and binding.target_type not in AUTO_ALLOWED_TARGET_TYPES:
        raise ValueError(
            f"automatic binding cannot broaden target_type; "
            f"allowed target types are agent and agent_project"
        )
    if (
        binding.target_type == "system"
        and source not in MANUAL_SOURCES
        and source not in MIGRATION_SOURCES
    ):
        raise ValueError("system binding requires manual governance")
    return replace(
        binding,
        project_ref=canonical_project_ref(binding.project_ref),
    )


def build_binding(
    definition_id: str,
    *,
    share_group_id: str,
    target_type: str = "agent",
    target_id: str = "",
    project_ref: str = "",
    provider: str = "",
    runtime_role: str = "",
    effect: str = "include",
    priority: int = 0,
    owner_agent_id: str = "",
    created_by: str = "manual",
    authorization: str = "",
    binding_id: str = "",
    created_at: str = "",
) -> RuleBinding:
    """Build a validated binding with a deterministic id."""
    binding = RuleBinding(
        binding_id=binding_id or stable_hash(
            "rule-binding", definition_id, share_group_id, target_type,
            target_id, canonical_project_ref(project_ref), provider,
            runtime_role, effect,
        ),
        definition_id=definition_id,
        share_group_id=share_group_id,
        target_type=target_type,
        target_id=target_id,
        project_ref=canonical_project_ref(project_ref),
        provider=provider,
        runtime_role=runtime_role,
        effect=effect,
        priority=priority,
        owner_agent_id=owner_agent_id,
        created_by=created_by,
        authorization=authorization,
        created_at=created_at or _now_iso(),
        updated_at=created_at or _now_iso(),
    )
    return validate_binding_scope(binding)
