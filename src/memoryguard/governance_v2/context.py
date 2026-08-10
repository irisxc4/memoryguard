"""Explicit mutation authorization for the V2 fact/evidence planes."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Mapping


class V2GovernanceError(RuntimeError):
    """Base error for fail-closed V2 governance decisions."""


class V2ScopeError(V2GovernanceError, PermissionError):
    """The requested mutation would cross an authorized scope."""


class V2ContextError(V2GovernanceError, ValueError):
    """A mutation context is missing or malformed."""


_AUTHORITIES = frozenset({"manual", "auto", "admin", "migration", "system"})


def _strict_bool(value: Any, *, name: str) -> bool:
    """Accept only a real bool or integer 0/1; reject string truthiness."""

    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise V2ContextError(f"{name} must be a boolean or 0/1")


def _alias_text(value: Mapping[str, Any], canonical: str, aliases: tuple[str, ...]) -> str:
    present = [(key, value[key]) for key in (canonical, *aliases) if key in value and value[key] is not None]
    if not present:
        return ""
    normalized = [str(item or "") for _, item in present]
    if len(set(normalized)) != 1:
        raise V2ContextError(f"conflicting mutation context aliases: {canonical}")
    return normalized[0]


@dataclass(frozen=True)
class V2MutationContext:
    """Caller-owned identity and scope for one V2 mutation.

    The context is an authorization assertion, not a defaulting hint.  A
    non-admin caller cannot omit a dimension and thereby write another
    agent/project/provider/runtime scope.
    """

    workspace_id: str
    share_group_id: str
    agent_instance_id: str = ""
    project_ref: str = ""
    provider: str = ""
    runtime_role: str = ""
    actor: str = ""
    admin: bool = False
    authority: str = "manual"

    def __post_init__(self) -> None:
        values = {
            "workspace_id": str(self.workspace_id or ""),
            "share_group_id": str(self.share_group_id or ""),
            "agent_instance_id": str(self.agent_instance_id or ""),
            "project_ref": str(self.project_ref or ""),
            "provider": str(self.provider or ""),
            "runtime_role": str(self.runtime_role or ""),
            "actor": str(self.actor or ""),
            "authority": str(self.authority or "manual").casefold(),
        }
        if not values["workspace_id"]:
            raise V2ContextError("workspace_id is required")
        if not values["share_group_id"]:
            raise V2ContextError("share_group_id is required")
        if not values["actor"]:
            raise V2ContextError("actor is required")
        if values["authority"] not in _AUTHORITIES:
            raise V2ContextError(f"unsupported mutation authority: {values['authority']!r}")
        admin = _strict_bool(self.admin, name="admin")
        if values["authority"] == "auto" and admin:
            raise V2ContextError("automatic mutations cannot claim admin")
        for key, value in values.items():
            object.__setattr__(self, key, value)
        object.__setattr__(self, "admin", admin)

    @classmethod
    def from_value(cls, value: "V2MutationContext | Mapping[str, Any]") -> "V2MutationContext":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise V2ContextError("mutation context must be an object")
        admin_values = [(key, value[key]) for key in ("admin", "is_admin") if key in value and value[key] is not None]
        if not admin_values:
            admin = False
        else:
            parsed = [_strict_bool(item, name="admin") for _, item in admin_values]
            if len(set(parsed)) != 1:
                raise V2ContextError("conflicting mutation context aliases: admin")
            admin = parsed[0]
        authority = _alias_text(value, "authority", ()) or "manual"
        if "automatic" in value and value.get("automatic") is not None:
            automatic = _strict_bool(value.get("automatic"), name="automatic")
            expected = "auto" if automatic else "manual"
            if "authority" not in value:
                authority = expected
            elif authority.casefold() != expected:
                raise V2ContextError("automatic and authority conflict")
        return cls(
            workspace_id=_alias_text(value, "workspace_id", ("workspace",)),
            share_group_id=_alias_text(value, "share_group_id", ("group_id",)),
            agent_instance_id=_alias_text(value, "agent_instance_id", ("agent",)),
            project_ref=_alias_text(value, "project_ref", ("project",)),
            provider=_alias_text(value, "provider", ()),
            runtime_role=_alias_text(value, "runtime_role", ("runtime",)),
            actor=_alias_text(value, "actor", ()),
            admin=admin,
            authority=authority,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "share_group_id": self.share_group_id,
            "agent_instance_id": self.agent_instance_id,
            "project_ref": self.project_ref,
            "provider": self.provider,
            "runtime_role": self.runtime_role,
            "actor": self.actor,
            "admin": bool(self.admin),
            "authority": self.authority,
        }

    as_dict = to_dict

    def check_workspace(self, current: str | Path) -> None:
        requested = os.path.abspath(os.fspath(Path(self.workspace_id).expanduser()))
        actual = os.path.abspath(os.fspath(Path(current).expanduser()))
        if requested != actual:
            raise V2ScopeError("mutation workspace_id does not match store workspace")

    def check_scope(
        self,
        *,
        workspace_id: str | Path,
        share_group_id: str,
        agent_instance_id: str = "",
        project_ref: str = "",
        provider: str = "",
        runtime_role: str = "",
    ) -> None:
        self.check_workspace(workspace_id)
        if str(share_group_id or "") != self.share_group_id:
            raise V2ScopeError("mutation share_group_id is outside context")
        if self.admin:
            return
        dimensions = (
            ("agent_instance_id", agent_instance_id, self.agent_instance_id),
            ("project_ref", project_ref, self.project_ref),
            ("provider", provider, self.provider),
            ("runtime_role", runtime_role, self.runtime_role),
        )
        for name, target, allowed in dimensions:
            target_text = str(target or "")
            allowed_text = str(allowed or "")
            if target_text != allowed_text:
                raise V2ScopeError(f"mutation {name} is outside context")

    @property
    def is_automatic(self) -> bool:
        return self.authority == "auto"


# Identity-only capability.  Migration adapters import this private object;
# callers cannot forge it by constructing a mapping or copying its fields.
_MIGRATION_CAPABILITY = object()


def _require_migration_capability(capability: object) -> None:
    if capability is not _MIGRATION_CAPABILITY:
        raise V2ScopeError("invalid migration governance capability")


__all__ = [
    "V2ContextError",
    "V2GovernanceError",
    "V2MutationContext",
    "V2ScopeError",
]
