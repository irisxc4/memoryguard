"""V2 rule-governance contracts (no runtime wiring).

This module is deliberately storage-agnostic.  A caller supplies a port that
owns persistence; the service only validates mutation context, scope and the
audit fields required for a governed rule write.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable


class RuleMutationError(ValueError):
    """Base error for a rejected V2 rule mutation."""


class RuleAuthorizationError(RuleMutationError):
    """The trusted mutation context or target scope is invalid."""


class RuleNotReadyError(RuleMutationError):
    """Raised by a V2 port when its shadow target is not ready."""


def _strict_bool(value: Any, field: str) -> bool:
    """Parse trusted boolean fields without Python's truthiness trap.

    ``bool("false")`` is ``True`` and is unsafe for an authorization bit.
    Accept real booleans and the exact integer/string forms 0 and 1 only.
    """
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return value == 1
    if type(value) is str and value in {"0", "1"}:
        return value == "1"
    raise RuleAuthorizationError(f"invalid_rule_context_boolean:{field}")


def _normal_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _resolve_values(values: list[tuple[str, Any]], field: str) -> str:
    """Resolve aliases, rejecting conflicting non-empty identities."""
    normalized = [(name, _normal_text(value)) for name, value in values]
    present = [(name, value) for name, value in normalized if value]
    if not present:
        return ""
    unique = {value for _, value in present}
    if len(unique) > 1:
        raise RuleAuthorizationError(f"conflicting_rule_context_alias:{field}")
    return present[0][1]


def _mapping_alias(mapping: Mapping[str, Any], field: str, names: tuple[str, ...]) -> Any:
    present = [(name, mapping[name]) for name in names if name in mapping and mapping[name] is not None]
    if not present:
        return None
    # Boolean aliases must be normalized before comparison, otherwise e.g.
    # ``1`` and ``True`` could be treated inconsistently by callers.
    if field in {"admin", "automatic"}:
        values = [(name, _strict_bool(value, field)) for name, value in present]
        if len({value for _, value in values}) > 1:
            raise RuleAuthorizationError(f"conflicting_rule_context_alias:{field}")
        return values[0][1]
    values = [(name, _normal_text(value)) for name, value in present if _normal_text(value)]
    if len({value for _, value in values}) > 1:
        raise RuleAuthorizationError(f"conflicting_rule_context_alias:{field}")
    return values[0][1] if values else ""


@dataclass
class RuleMutationContext:
    """Trusted identity and execution context for one rule mutation.

    The short field names (``agent``, ``project``, ``group``, ``runtime``)
    mirror the governance contract.  Long aliases are accepted for callers
    that use the existing MCP/GUI vocabulary.
    """

    agent: str = ""
    project: str = ""
    group: str = ""
    provider: str = ""
    runtime: str = ""
    admin: bool = False
    automatic: bool = False
    agent_instance_id: str = ""
    project_ref: str = ""
    share_group_id: str = ""
    runtime_role: str = ""
    agent_id: str = ""
    project_id: str = ""
    group_id: str = ""

    def __post_init__(self) -> None:
        self.agent = _resolve_values(
            [("agent", self.agent), ("agent_instance_id", self.agent_instance_id), ("agent_id", self.agent_id)],
            "agent",
        )
        self.project = _resolve_values(
            [("project", self.project), ("project_ref", self.project_ref), ("project_id", self.project_id)],
            "project",
        )
        self.group = _resolve_values(
            [("group", self.group), ("share_group_id", self.share_group_id), ("group_id", self.group_id)],
            "group",
        )
        self.runtime = _resolve_values(
            [("runtime", self.runtime), ("runtime_role", self.runtime_role)],
            "runtime",
        )
        self.provider = _normal_text(self.provider)
        self.agent_instance_id = self.agent
        self.agent_id = self.agent
        self.project_ref = self.project
        self.project_id = self.project
        self.share_group_id = self.group
        self.group_id = self.group
        self.runtime_role = self.runtime
        self.admin = _strict_bool(self.admin, "admin")
        self.automatic = _strict_bool(self.automatic, "automatic")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | "RuleMutationContext") -> "RuleMutationContext":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise RuleAuthorizationError("rule_mutation_context_required")
        fields: dict[str, Any] = {
            "agent": _mapping_alias(value, "agent", ("agent", "agent_instance_id", "agent_id")),
            "project": _mapping_alias(value, "project", ("project", "project_ref", "project_id")),
            "group": _mapping_alias(value, "group", ("group", "share_group_id", "group_id")),
            "runtime": _mapping_alias(value, "runtime", ("runtime", "runtime_role")),
        }
        for field in ("provider",):
            if field in value:
                fields[field] = value[field]
        for field in ("admin", "automatic"):
            alias_names = (field, f"is_{field}")
            parsed = _mapping_alias(value, field, alias_names)
            if parsed is not None:
                fields[field] = parsed
        return cls(**fields)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "project": self.project,
            "group": self.group,
            "provider": self.provider,
            "runtime": self.runtime,
            "admin": self.admin,
            "automatic": self.automatic,
            "agent_instance_id": self.agent,
            "agent_id": self.agent,
            "project_ref": self.project,
            "project_id": self.project,
            "share_group_id": self.group,
            "group_id": self.group,
            "runtime_role": self.runtime,
        }

    def validate(self) -> "RuleMutationContext":
        missing = [name for name in ("agent", "project", "group", "provider", "runtime") if not getattr(self, name)]
        if missing:
            raise RuleAuthorizationError("missing_rule_mutation_context:" + ",".join(missing))
        return self

    def authorize_scope(self, payload: Mapping[str, Any]) -> None:
        """Fail closed for inferred system/group/provider/other-agent scope."""
        if not isinstance(payload, Mapping):
            raise RuleMutationError("rule_payload_required")
        identity_aliases = {
            "agent": ("agent", "agent_id", "agent_instance_id", "actor_agent", "actor_agent_id"),
            "project": ("project", "project_id", "project_ref"),
            "group": ("group", "group_id", "share_group_id"),
            "provider": ("provider",),
            "runtime": ("runtime", "runtime_role"),
        }
        for field, aliases in identity_aliases.items():
            supplied = [_normal_text(payload[name]) for name in aliases if name in payload and payload[name] is not None]
            supplied = [item for item in supplied if item]
            if supplied and any(item != getattr(self, field) for item in supplied):
                raise RuleAuthorizationError("untrusted_identity_argument")
        raw = payload.get("scope", payload.get("audience"))
        scope = raw if isinstance(raw, Mapping) else payload
        target_type = str(scope.get("target_type", scope.get("type", "")) or "").strip().casefold()
        target_id = str(scope.get("target_id", scope.get("id", "")) or "").strip()
        broad = {"system", "group", "provider", "runtime_role", "runtime-role"}
        if target_type in broad and (self.automatic or not self.admin):
            raise RuleAuthorizationError("automatic_scope_expansion_denied" if self.automatic else "admin_scope_required")
        if target_type == "agent" and target_id != self.agent:
            raise RuleAuthorizationError("other_agent_scope_denied")
        if target_type == "project" and target_id and target_id != self.project:
            raise RuleAuthorizationError("other_project_scope_denied")
        if self.automatic:
            if not target_type or target_type not in {"agent", "project"}:
                raise RuleAuthorizationError("automatic_scope_required")
            if not target_id:
                raise RuleAuthorizationError("automatic_scope_required")

    def require_audit(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Require explainable decision, undo and evidence fields."""
        aliases = {
            "decision": ("decision", "decision_id", "action", "outcome"),
            "reason": ("reason",),
            "confidence": ("confidence",),
            "undo_id": ("undo_id",),
            "evidence": ("evidence", "evidence_ref", "evidence_digest"),
        }
        normalized: dict[str, Any] = {}
        missing: list[str] = []
        for field, names in aliases.items():
            value = next((payload.get(name) for name in names if name in payload), None)
            if value is None or (isinstance(value, str) and not value.strip()):
                missing.append(field)
            else:
                normalized[field] = value
        if missing:
            raise RuleMutationError("missing_rule_audit_fields:" + ",".join(missing))
        try:
            confidence = float(normalized["confidence"])
        except (TypeError, ValueError) as exc:
            raise RuleMutationError("invalid_rule_confidence") from exc
        if not 0.0 <= confidence <= 1.0:
            raise RuleMutationError("invalid_rule_confidence")
        normalized["confidence"] = confidence
        return normalized

    def validate_mutation(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self.validate()
        self.authorize_scope(payload)
        audit = self.require_audit(payload)
        return {**dict(payload), **audit, "mutation_context": self.to_dict()}


@runtime_checkable
class RuleGovernancePort(Protocol):
    """Storage/application port injected by the future V2 runtime."""

    def status(self, workspace: str) -> Mapping[str, Any] | bool: ...

    def read(self, operation: str, payload: Mapping[str, Any], *, workspace: str) -> Any: ...

    def mutate(self, operation: str, payload: Mapping[str, Any], *, context: Mapping[str, Any], workspace: str) -> Any: ...


class RuleGovernanceService:
    """Thin contract service; it never constructs a persistence store."""

    def __init__(self, port: RuleGovernancePort, *, workspace: str = "") -> None:
        self.port = port
        self.workspace = str(workspace or "")

    def status(self) -> Mapping[str, Any] | bool:
        return self.port.status(self.workspace)

    def read(self, operation: str, payload: Mapping[str, Any] | None = None) -> Any:
        return self.port.read(str(operation), dict(payload or {}), workspace=self.workspace)

    def mutate(self, operation: str, payload: Mapping[str, Any], context: RuleMutationContext | Mapping[str, Any]) -> Any:
        ctx = RuleMutationContext.from_mapping(context)
        normalized = ctx.validate_mutation(payload)
        return self.port.mutate(str(operation), normalized, context=ctx.to_dict(), workspace=self.workspace)


__all__ = [
    "RuleAuthorizationError", "RuleMutationError", "RuleNotReadyError",
    "RuleMutationContext", "RuleGovernancePort", "RuleGovernanceService",
]
