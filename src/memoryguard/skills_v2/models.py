"""Typed contracts for the V2 skill declaration plane.

The skill plane deliberately stores *references* rather than executable
content.  The model layer is kept free of SQLite so callers can validate a
declaration before opening a database (and read-only callers never create
one).  All authorization objects reject ambiguous aliases and Python's
truthiness trap for security-sensitive booleans.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping, Sequence


class SkillError(RuntimeError):
    """Base error raised by the V2 skill contracts."""


class SkillValidationError(SkillError, ValueError):
    """A declaration or reference is malformed."""


class SkillAuthorizationError(SkillError, PermissionError):
    """A skill operation is outside its trusted scope."""


class SkillConflictError(SkillError):
    """An idempotency key or immutable version was reused with new data."""


class SkillSchemaError(SkillError):
    """A skill database is missing, unknown, or from a future version."""


class SkillRuntimeError(SkillError):
    """The shadow runtime is intentionally not executable in V2_BUILDING."""


_ALLOWED_AUTHORITIES = frozenset({
    "manual", "auto", "automatic", "admin", "migration", "system",
})

# A write context is an object-capability.  Keeping the sentinel in this
# module means a JSON mapping or a caller-created dataclass cannot mint write
# authority merely by setting a truthy flag.
_SKILL_CONTEXT_CAPABILITY = object()

# This is an allow-list, rather than a deny-list.  It is intentionally made
# of declaration capabilities only; ``process.exec`` is not present because
# this phase never launches an external process.
ALLOWED_CAPABILITIES = frozenset({
    "skill.read", "skill.invoke", "context.read", "context.write",
    "memory.read", "memory.write", "evidence.read", "assets.read",
    "filesystem.read", "filesystem.write", "network.read", "network.write",
    "model.infer", "stdin.read", "stdout.write",
    # Friendly aliases used by a few existing manifests.
    "read_context", "write_context", "read_memory", "write_memory",
    "read_evidence", "read_assets", "read_filesystem", "write_filesystem",
    "read_network", "write_network", "invoke_model",
})

_FORBIDDEN_KEYS = frozenset({
    "body", "raw", "raw_body", "raw_content", "content", "text", "script",
    "binary", "executable", "source_text", "transcript", "secret", "token",
    "password", "api_key", "apikey", "api-key", "code", "command", "source",
    "credential", "credentials", "private_key", "access_token", "auth_token",
})

_MAX_JSON_BYTES = 64 * 1024
_MAX_STRING_CHARS = 16 * 1024


def _strict_bool(value: Any, field: str) -> bool:
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return value == 1
    if type(value) is str and value in {"0", "1"}:
        return value == "1"
    raise SkillAuthorizationError(f"invalid_skill_context_boolean:{field}")


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _alias_text(value: Mapping[str, Any], field: str, names: Sequence[str]) -> str:
    present = [(name, _text(value[name])) for name in names if name in value and value[name] is not None]
    present = [(name, item) for name, item in present if item]
    if not present:
        return ""
    if len({item for _, item in present}) != 1:
        raise SkillAuthorizationError(f"conflicting_skill_context_alias:{field}")
    return present[0][1]


def _alias_bool(value: Mapping[str, Any], field: str, names: Sequence[str], default: bool = False) -> bool:
    present = [(name, _strict_bool(value[name], field)) for name in names if name in value and value[name] is not None]
    if not present:
        return default
    if len({item for _, item in present}) != 1:
        raise SkillAuthorizationError(f"conflicting_skill_context_alias:{field}")
    return present[0][1]


def _json_safe(value: Any, *, depth: int = 0, key: str = "") -> Any:
    if depth > 8:
        raise SkillValidationError("skill metadata nesting exceeds 8 levels")
    normalized_key = key.casefold().replace("-", "_")
    if normalized_key in {item.replace("-", "_") for item in _FORBIDDEN_KEYS}:
        raise SkillValidationError(f"skill declaration cannot contain raw field: {key}")
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, child in value.items():
            child_key = _text(raw_key)
            if not child_key:
                raise SkillValidationError("skill declaration key cannot be empty")
            result[child_key] = _json_safe(child, depth=depth + 1, key=child_key)
        return result
    if isinstance(value, (list, tuple)):
        return [_json_safe(child, depth=depth + 1) for child in value]
    if isinstance(value, str):
        if len(value) > _MAX_STRING_CHARS:
            raise SkillValidationError("skill metadata string exceeds size limit")
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    # bytes are especially easy to accidentally use for a script body; make
    # the rejection explicit instead of silently converting them.
    raise SkillValidationError("skill declaration must contain JSON values only")


def canonical_json(value: Any) -> str:
    encoded = json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > _MAX_JSON_BYTES:
        raise SkillValidationError("skill metadata exceeds 64 KiB")
    return encoded


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def validate_relative_ref(value: Any, field: str = "entrypoint_ref") -> str:
    ref = _text(value).replace("\\", "/")
    if not ref:
        raise SkillValidationError(f"{field} is required")
    path = PurePosixPath(ref)
    if path.is_absolute() or ref.startswith("/") or ":" in ref.split("/", 1)[0]:
        raise SkillValidationError(f"{field} must be relative")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise SkillValidationError(f"{field} contains an unsafe path component")
    return "/".join(path.parts)


def validate_digest(value: Any, field: str = "digest", *, required: bool = True) -> str:
    digest = _text(value).casefold()
    if not digest and not required:
        return ""
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise SkillValidationError(f"{field} must be a sha256 hex digest")
    return digest


@dataclass(frozen=True)
class SkillEvidenceRef:
    """Reference-only evidence; no evidence body is accepted."""

    evidence_id: str = ""
    source_ref: str = ""
    digest: str = ""
    revision: str = ""
    authority: str = "observed"

    def __post_init__(self) -> None:
        if not _text(self.evidence_id) and not _text(self.source_ref):
            raise SkillValidationError("skill evidence requires evidence_id or source_ref")
        object.__setattr__(self, "evidence_id", _text(self.evidence_id))
        object.__setattr__(self, "source_ref", _text(self.source_ref))
        object.__setattr__(self, "digest", validate_digest(self.digest, "evidence.digest"))
        authority = _text(self.authority).casefold()
        if authority not in {"observed", "audit", "governance", "migration", "system", "legacy"}:
            raise SkillValidationError(f"unknown evidence authority: {authority!r}")
        object.__setattr__(self, "authority", authority)
        object.__setattr__(self, "revision", _text(self.revision))

    @classmethod
    def from_value(cls, value: "SkillEvidenceRef | Mapping[str, Any]") -> "SkillEvidenceRef":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise SkillValidationError("evidence reference must be an object")
        return cls(
            evidence_id=value.get("evidence_id", value.get("id", "")),
            source_ref=value.get("source_ref", value.get("source", "")),
            digest=value.get("digest", ""),
            revision=value.get("revision", value.get("source_revision", "")),
            authority=value.get("authority", "observed"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "source_ref": self.source_ref,
            "digest": self.digest,
            "revision": self.revision,
            "authority": self.authority,
        }

    as_dict = to_dict


@dataclass(frozen=True)
class SkillAssetRef:
    """Reference-only asset link; the asset registry owns the bytes."""

    asset_id: str = ""
    path: str = ""
    digest: str = ""
    asset_kind: str = ""

    def __post_init__(self) -> None:
        if not _text(self.asset_id) and not _text(self.path):
            raise SkillValidationError("asset reference requires asset_id or path")
        object.__setattr__(self, "asset_id", _text(self.asset_id))
        object.__setattr__(self, "path", _text(self.path))
        object.__setattr__(self, "digest", validate_digest(self.digest, "asset.digest"))
        object.__setattr__(self, "asset_kind", _text(self.asset_kind))

    @classmethod
    def from_value(cls, value: "SkillAssetRef | Mapping[str, Any]") -> "SkillAssetRef":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise SkillValidationError("asset reference must be an object")
        return cls(
            asset_id=value.get("asset_id", value.get("id", "")),
            path=value.get("path", ""),
            digest=value.get("digest", ""),
            asset_kind=value.get("asset_kind", value.get("kind", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"asset_id": self.asset_id, "path": self.path, "digest": self.digest, "asset_kind": self.asset_kind}

    as_dict = to_dict


@dataclass(frozen=True)
class SkillCapability:
    capability: str
    authority: str = "manual"
    constraints: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        name = _text(self.capability).casefold()
        if name not in ALLOWED_CAPABILITIES:
            raise SkillValidationError(f"unknown skill capability: {name!r}")
        authority = _text(self.authority).casefold()
        if authority not in _ALLOWED_AUTHORITIES:
            raise SkillValidationError(f"unknown skill capability authority: {authority!r}")
        object.__setattr__(self, "capability", name)
        object.__setattr__(self, "authority", authority)
        object.__setattr__(self, "constraints", MappingProxyType(_json_safe(dict(self.constraints))))

    @classmethod
    def from_value(cls, value: "SkillCapability | str | Mapping[str, Any]") -> "SkillCapability":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            return cls(value)
        if not isinstance(value, Mapping):
            raise SkillValidationError("skill capability must be a string or object")
        return cls(
            capability=value.get("capability", value.get("name", value.get("permission", ""))),
            authority=value.get("authority", "manual"),
            constraints=value.get("constraints", value.get("limits", {})) or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {"capability": self.capability, "authority": self.authority, "constraints": dict(self.constraints)}

    as_dict = to_dict


_MISSING_POLICY_FIELD = object()


@dataclass(frozen=True)
class ExecutionPolicy:
    """Declarative policy only; it is never interpreted as an exec command."""

    capabilities: tuple[str, ...] | object = field(default_factory=lambda: _MISSING_POLICY_FIELD)
    allowed_capabilities: tuple[str, ...] | object = field(default_factory=lambda: _MISSING_POLICY_FIELD)
    authority: str = "manual"
    network: bool = False
    filesystem: str = "none"
    allow_external_process: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        primary_present = self.capabilities is not _MISSING_POLICY_FIELD and self.capabilities is not None
        allowed_present = self.allowed_capabilities is not _MISSING_POLICY_FIELD and self.allowed_capabilities is not None
        primary = tuple(_text(item).casefold() for item in (self.capabilities if primary_present else ()) if _text(item))
        allowed = tuple(_text(item).casefold() for item in (self.allowed_capabilities if allowed_present else ()) if _text(item))
        if primary_present and allowed_present and primary != allowed:
            raise SkillConflictError("execution policy capabilities and allowed_capabilities conflict")
        caps = tuple(dict.fromkeys(primary if primary_present else allowed))
        unknown = sorted(set(caps) - ALLOWED_CAPABILITIES)
        if unknown:
            raise SkillValidationError("unknown skill capability: " + ",".join(unknown))
        authority = _text(self.authority).casefold()
        if authority not in _ALLOWED_AUTHORITIES:
            raise SkillValidationError(f"unknown execution authority: {authority!r}")
        network = _strict_bool(self.network, "network")
        process = _strict_bool(self.allow_external_process, "allow_external_process")
        if process:
            raise SkillValidationError("external process execution is disabled in V2_BUILDING")
        filesystem = _text(self.filesystem).casefold() or "none"
        if filesystem not in {"none", "read", "read_only", "write", "read_write"}:
            raise SkillValidationError(f"unknown filesystem policy: {filesystem!r}")
        object.__setattr__(self, "capabilities", caps)
        object.__setattr__(self, "allowed_capabilities", caps)
        object.__setattr__(self, "authority", authority)
        object.__setattr__(self, "network", network)
        object.__setattr__(self, "allow_external_process", process)
        object.__setattr__(self, "filesystem", filesystem)
        object.__setattr__(self, "metadata", MappingProxyType(_json_safe(dict(self.metadata))))

    @classmethod
    def from_value(cls, value: "ExecutionPolicy | Mapping[str, Any] | None") -> "ExecutionPolicy":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise SkillValidationError("execution_policy must be an object")
        kwargs: dict[str, Any] = {
            "authority": value.get("authority", "manual"),
            "network": value.get("network", False),
            "filesystem": value.get("filesystem", "none"),
            "allow_external_process": value.get("allow_external_process", value.get("process", False)),
            "metadata": value.get("metadata", {}) or {},
        }
        if "capabilities" in value:
            kwargs["capabilities"] = tuple(value.get("capabilities") or ())
        elif "permissions" in value:
            kwargs["capabilities"] = tuple(value.get("permissions") or ())
        if "allowed_capabilities" in value:
            kwargs["allowed_capabilities"] = tuple(value.get("allowed_capabilities") or ())
        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "capabilities": list(self.capabilities),
            "allowed_capabilities": list(self.capabilities),
            "authority": self.authority,
            "network": self.network,
            "filesystem": self.filesystem,
            "allow_external_process": self.allow_external_process,
            "metadata": dict(self.metadata),
        }

    as_dict = to_dict


@dataclass(frozen=True)
class SkillBinding:
    target_type: str
    target_id: str = ""
    project_ref: str = ""
    share_group_id: str = ""
    provider: str = ""
    runtime_role: str = ""
    effect: str = "include"

    def __post_init__(self) -> None:
        target_type = _text(self.target_type).casefold().replace("-", "_")
        aliases = {"agentproject": "agent_project", "project_agent": "agent_project", "global": "system"}
        target_type = aliases.get(target_type, target_type)
        if target_type not in {"agent", "project", "agent_project", "group", "provider", "runtime", "system"}:
            raise SkillValidationError(f"unknown skill binding target: {target_type!r}")
        target_id = _text(self.target_id)
        if target_type != "system" and not target_id:
            raise SkillValidationError("skill binding target_id is required")
        effect = _text(self.effect).casefold() or "include"
        if effect not in {"include", "allow", "exclude", "deny"}:
            raise SkillValidationError(f"unknown skill binding effect: {effect!r}")
        object.__setattr__(self, "target_type", target_type)
        object.__setattr__(self, "target_id", target_id)
        object.__setattr__(self, "project_ref", _text(self.project_ref))
        object.__setattr__(self, "share_group_id", _text(self.share_group_id))
        object.__setattr__(self, "provider", _text(self.provider))
        object.__setattr__(self, "runtime_role", _text(self.runtime_role))
        object.__setattr__(self, "effect", effect)

    @classmethod
    def from_value(cls, value: "SkillBinding | Mapping[str, Any]") -> "SkillBinding":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise SkillValidationError("skill binding must be an object")
        target_type = value.get("target_type", value.get("scope_type", value.get("type", "")))
        target_id = value.get("target_id", value.get("scope_id", value.get("id", "")))
        return cls(
            target_type=target_type,
            target_id=target_id,
            project_ref=value.get("project_ref", value.get("project", "")),
            share_group_id=value.get("share_group_id", value.get("group_id", value.get("group", ""))),
            provider=value.get("provider", ""),
            runtime_role=value.get("runtime_role", value.get("runtime", "")),
            effect=value.get("effect", value.get("mode", "include")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_type": self.target_type,
            "target_id": self.target_id,
            "project_ref": self.project_ref,
            "share_group_id": self.share_group_id,
            "provider": self.provider,
            "runtime_role": self.runtime_role,
            "effect": self.effect,
        }

    as_dict = to_dict


@dataclass(frozen=True)
class SkillReadScope:
    workspace_id: str
    share_group_id: str = ""
    agent_instance_id: str = ""
    project_ref: str = ""
    provider: str = ""
    runtime_role: str = ""
    admin: bool = False

    def __post_init__(self) -> None:
        workspace = _text(self.workspace_id)
        if not workspace:
            raise SkillAuthorizationError("skill read scope requires workspace_id")
        object.__setattr__(self, "workspace_id", workspace)
        for field_name in ("share_group_id", "agent_instance_id", "project_ref", "provider", "runtime_role"):
            object.__setattr__(self, field_name, _text(getattr(self, field_name)))
        object.__setattr__(self, "admin", _strict_bool(self.admin, "admin"))

    @classmethod
    def from_value(cls, value: "SkillReadScope | Mapping[str, Any]") -> "SkillReadScope":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise SkillAuthorizationError("skill read scope must be an object")
        return cls(
            workspace_id=_alias_text(value, "workspace_id", ("workspace",)),
            share_group_id=_alias_text(value, "share_group_id", ("group", "group_id")),
            agent_instance_id=_alias_text(value, "agent_instance_id", ("agent", "agent_id")),
            project_ref=_alias_text(value, "project_ref", ("project", "project_id")),
            provider=_alias_text(value, "provider", ()),
            runtime_role=_alias_text(value, "runtime_role", ("runtime",)),
            admin=_alias_bool(value, "admin", ("admin", "is_admin")),
        )

    @property
    def is_admin(self) -> bool:
        return self.admin

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "share_group_id": self.share_group_id,
            "agent_instance_id": self.agent_instance_id,
            "project_ref": self.project_ref,
            "provider": self.provider,
            "runtime_role": self.runtime_role,
            "admin": self.admin,
        }

    as_dict = to_dict


@dataclass(frozen=True)
class SkillMutationContext(SkillReadScope):
    """Trusted identity used for writes.

    Mapping values are intentionally not accepted by the store.  A caller
    must supply this object (or the explicit :meth:`trusted` constructor),
    which prevents an untrusted public JSON payload from becoming authority.
    """

    actor: str = ""
    authority: str = "manual"
    automatic: bool = False
    _trusted: bool = field(default=False, repr=False, compare=False)
    _identity: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        if type(self._trusted) is not bool:
            raise SkillAuthorizationError("skill mutation context _trusted must be a strict bool")
        if not (self._trusted and self._identity is _SKILL_CONTEXT_CAPABILITY):
            raise SkillAuthorizationError("trusted SkillMutationContext capability is required")
        actor = _text(self.actor)
        if not actor:
            raise SkillAuthorizationError("skill mutation context requires actor")
        authority = _text(self.authority).casefold() or "manual"
        if authority == "automatic":
            authority = "auto"
        if authority not in _ALLOWED_AUTHORITIES:
            raise SkillAuthorizationError(f"unknown skill authority: {authority!r}")
        automatic = _strict_bool(self.automatic, "automatic")
        if automatic and authority not in {"auto", "automatic"}:
            raise SkillAuthorizationError("automatic and authority conflict")
        if authority == "auto" and self.admin:
            raise SkillAuthorizationError("automatic mutations cannot claim admin")
        object.__setattr__(self, "actor", actor)
        object.__setattr__(self, "authority", authority)
        object.__setattr__(self, "automatic", automatic or authority == "auto")
        # Do not coerce or accept a caller-supplied truthy value here.  The
        # identity sentinel above is the actual authority check.
        object.__setattr__(self, "_trusted", self._trusted)

    @classmethod
    def trusted(cls, **kwargs: Any) -> "SkillMutationContext":
        """Construct a trusted context for host integrations/tests."""

        if "_trusted" in kwargs or "_identity" in kwargs:
            raise SkillAuthorizationError("private SkillMutationContext fields cannot be supplied")
        return cls(_trusted=True, _identity=_SKILL_CONTEXT_CAPABILITY, **kwargs)

    @classmethod
    def _from_capability(cls, capability: object, **kwargs: Any) -> "SkillMutationContext":
        """Private factory for migration/runtime code holding the sentinel."""

        if capability is not _SKILL_CONTEXT_CAPABILITY:
            raise SkillAuthorizationError("invalid SkillMutationContext capability")
        return cls(_trusted=True, _identity=capability, **kwargs)

    @classmethod
    def from_value(cls, value: "SkillMutationContext | Mapping[str, Any]") -> "SkillMutationContext":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise SkillAuthorizationError("skill mutation context must be an object")
        # A public mapping is intentionally never upgraded to authority.
        raise SkillAuthorizationError("trusted SkillMutationContext factory is required")

    @property
    def is_automatic(self) -> bool:
        return self.automatic

    def to_dict(self) -> dict[str, Any]:
        result = super().to_dict()
        result.update({"actor": self.actor, "authority": self.authority, "automatic": self.automatic})
        return result


@dataclass(frozen=True)
class SkillDefinition:
    """An immutable skill version declaration."""

    name: str
    namespace: str = "default"
    version: int = 1
    skill_id: str = ""
    description: str = ""
    declaration: Mapping[str, Any] = field(default_factory=dict)
    entrypoint_ref: str = "entrypoint"
    entrypoint_hash: str = ""
    bindings: tuple[SkillBinding, ...] = ()
    capabilities: tuple[SkillCapability, ...] = ()
    evidence_refs: tuple[SkillEvidenceRef, ...] = ()
    asset_refs: tuple[SkillAssetRef, ...] = ()
    execution_policy: ExecutionPolicy | Mapping[str, Any] | None = None
    state: str = "active"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        name = _text(self.name)
        namespace = _text(self.namespace) or "default"
        if not name:
            raise SkillValidationError("skill name is required")
        if not 1 <= int(self.version) <= 2**31 - 1:
            raise SkillValidationError("skill version must be a positive integer")
        ref = validate_relative_ref(self.entrypoint_ref)
        entry_hash = validate_digest(self.entrypoint_hash, "entrypoint_hash")
        bindings = tuple(SkillBinding.from_value(item) for item in self.bindings)
        caps = tuple(SkillCapability.from_value(item) for item in self.capabilities)
        evidence = tuple(SkillEvidenceRef.from_value(item) for item in self.evidence_refs)
        if not evidence:
            raise SkillValidationError("a skill requires at least one evidence reference")
        assets = tuple(SkillAssetRef.from_value(item) for item in self.asset_refs)
        policy = ExecutionPolicy.from_value(self.execution_policy)
        state = _text(self.state).casefold() or "active"
        if state not in {"active", "disabled", "tombstoned"}:
            raise SkillValidationError(f"unknown skill state: {state!r}")
        declaration = _json_safe(dict(self.declaration))
        metadata = _json_safe(dict(self.metadata))
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "namespace", namespace)
        object.__setattr__(self, "version", int(self.version))
        object.__setattr__(self, "skill_id", _text(self.skill_id))
        object.__setattr__(self, "description", _text(self.description))
        object.__setattr__(self, "entrypoint_ref", ref)
        object.__setattr__(self, "entrypoint_hash", entry_hash)
        object.__setattr__(self, "bindings", bindings)
        object.__setattr__(self, "capabilities", caps)
        object.__setattr__(self, "evidence_refs", evidence)
        object.__setattr__(self, "asset_refs", assets)
        object.__setattr__(self, "execution_policy", policy)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "declaration", MappingProxyType(declaration))
        object.__setattr__(self, "metadata", MappingProxyType(metadata))

    @property
    def stable_key(self) -> str:
        return f"{self.namespace}:{self.name}"

    # Compatibility spellings used by manifests and callers.
    @property
    def entrypoint(self) -> str:
        return self.entrypoint_ref

    @property
    def evidence(self) -> tuple[SkillEvidenceRef, ...]:
        return self.evidence_refs

    @property
    def assets(self) -> tuple[SkillAssetRef, ...]:
        return self.asset_refs

    @property
    def policy(self) -> ExecutionPolicy:
        return self.execution_policy  # type: ignore[return-value]

    @property
    def canonical_payload(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "declaration": dict(self.declaration),
            "entrypoint_ref": self.entrypoint_ref,
            "entrypoint_hash": self.entrypoint_hash,
            "bindings": [item.to_dict() for item in self.bindings],
            "capabilities": [item.to_dict() for item in self.capabilities],
            "evidence_refs": [item.to_dict() for item in self.evidence_refs],
            "asset_refs": [item.to_dict() for item in self.asset_refs],
            "execution_policy": self.execution_policy.to_dict(),  # type: ignore[union-attr]
            "metadata": dict(self.metadata),
        }

    @property
    def content_hash(self) -> str:
        return stable_hash(self.canonical_payload)

    @property
    def stable_id(self) -> str:
        return self.skill_id or stable_hash({"namespace": self.namespace, "name": self.name})[:32]

    @property
    def version_id(self) -> str:
        return stable_hash({"skill_id": self.stable_id, "version": self.version, "content_hash": self.content_hash})[:40]

    def to_dict(self) -> dict[str, Any]:
        result = dict(self.canonical_payload)
        result.update({"skill_id": self.stable_id, "version_id": self.version_id, "state": self.state})
        return result

    as_dict = to_dict

    @classmethod
    def from_value(cls, value: "SkillDefinition | Mapping[str, Any]") -> "SkillDefinition":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise SkillValidationError("skill definition must be an object")
        return cls(
            name=value.get("name", ""),
            namespace=value.get("namespace", value.get("provider", "default")),
            version=value.get("version", 1),
            skill_id=value.get("skill_id", value.get("id", "")),
            description=value.get("description", ""),
            declaration=value.get("declaration", value.get("manifest", {})) or {},
            entrypoint_ref=value.get("entrypoint_ref", value.get("entrypoint", "entrypoint")),
            entrypoint_hash=value.get("entrypoint_hash", value.get("hash", "")),
            bindings=tuple(value.get("bindings", value.get("scopes", ())) or ()),
            capabilities=tuple(value.get("capabilities", value.get("permissions", ())) or ()),
            evidence_refs=tuple(value.get("evidence_refs", value.get("evidence", ())) or ()),
            asset_refs=tuple(value.get("asset_refs", value.get("assets", ())) or ()),
            execution_policy=value.get("execution_policy", value.get("policy")),
            state=value.get("state", "active"),
            metadata=value.get("metadata", {}) or {},
        )


@dataclass(frozen=True)
class SkillReceipt:
    receipt_id: str
    operation: str
    skill_id: str
    version_id: str = ""
    idempotency_key: str = ""
    request_hash: str = ""
    status: str = "applied"
    result: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id, "operation": self.operation,
            "skill_id": self.skill_id, "version_id": self.version_id,
            "idempotency_key": self.idempotency_key, "request_hash": self.request_hash,
            "status": self.status, "result": dict(self.result), "created_at": self.created_at,
        }

    as_dict = to_dict


@dataclass(frozen=True)
class SkillDecision:
    decision_id: str
    operation: str
    skill_id: str
    before_hash: str = ""
    after_hash: str = ""
    expected_hash: str = ""
    reason: str = ""
    status: str = "applied"
    created_at: str = ""
    context: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id, "operation": self.operation,
            "skill_id": self.skill_id, "before_hash": self.before_hash,
            "after_hash": self.after_hash, "expected_hash": self.expected_hash,
            "reason": self.reason, "status": self.status, "created_at": self.created_at,
            "context": dict(self.context),
        }

    as_dict = to_dict


@dataclass(frozen=True)
class SkillMutationResult:
    definition: SkillDefinition | None
    receipt: SkillReceipt
    decision: SkillDecision

    @property
    def skill_id(self) -> str:
        return self.receipt.skill_id

    @property
    def version_id(self) -> str:
        return self.receipt.version_id

    def __iter__(self):
        # Compatible with the V2 boundary's ``(value, decision)`` convention.
        yield self.definition
        yield self.decision


@dataclass(frozen=True)
class SkillExecutionReceipt:
    receipt_id: str
    skill_id: str
    version_id: str
    status: str = "blocked"
    reason: str = ""
    requested_capabilities: tuple[str, ...] = ()
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id, "skill_id": self.skill_id,
            "version_id": self.version_id, "status": self.status,
            "reason": self.reason, "requested_capabilities": list(self.requested_capabilities),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class SkillVersion:
    """Read model for one immutable version row."""

    version_id: str
    skill_id: str
    version: int
    content_hash: str
    entrypoint_ref: str
    entrypoint_hash: str
    declaration: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = ""

    @property
    def digest(self) -> str:
        return self.content_hash

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id, "skill_id": self.skill_id,
            "version": int(self.version), "content_hash": self.content_hash,
            "digest": self.content_hash, "entrypoint_ref": self.entrypoint_ref,
            "entrypoint_hash": self.entrypoint_hash, "declaration": dict(self.declaration),
            "created_at": self.created_at,
        }

    as_dict = to_dict


@dataclass(frozen=True)
class SkillAudit:
    audit_id: str
    operation: str
    skill_id: str
    idempotency_key: str = ""
    actor: str = ""
    authority: str = ""
    payload_hash: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "audit_id": self.audit_id, "operation": self.operation,
            "skill_id": self.skill_id, "idempotency_key": self.idempotency_key,
            "actor": self.actor, "authority": self.authority,
            "payload_hash": self.payload_hash, "metadata": dict(self.metadata),
            "created_at": self.created_at,
        }

    as_dict = to_dict


@dataclass(frozen=True)
class SkillOutboxEvent:
    event_id: str
    event_type: str
    aggregate_id: str
    payload_hash: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)
    status: str = "pending"
    attempts: int = 0
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id, "event_type": self.event_type,
            "aggregate_id": self.aggregate_id, "payload_hash": self.payload_hash,
            "payload": dict(self.payload), "status": self.status,
            "attempts": int(self.attempts), "created_at": self.created_at,
        }

    as_dict = to_dict


@dataclass(frozen=True)
class SkillMigrationMap:
    map_id: str
    source_path: str
    source_hash: str
    skill_id: str
    version_id: str
    status: str = "mapped"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "map_id": self.map_id, "source_path": self.source_path,
            "source_hash": self.source_hash, "skill_id": self.skill_id,
            "version_id": self.version_id, "status": self.status,
            "metadata": dict(self.metadata), "created_at": self.created_at,
        }

    as_dict = to_dict


@dataclass(frozen=True)
class SkillUnknownLedgerEntry:
    unknown_id: str
    source_path: str
    field_name: str
    value_hash: str
    details: Mapping[str, Any] = field(default_factory=dict)
    status: str = "BLOCKED"
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "unknown_id": self.unknown_id, "source_path": self.source_path,
            "field_name": self.field_name, "value_hash": self.value_hash,
            "details": dict(self.details), "status": self.status,
            "created_at": self.created_at,
        }

    as_dict = to_dict


# Compatibility spellings used by early V2 design notes and sibling domains.
SkillManifest = SkillDefinition
SkillExecutionPolicy = ExecutionPolicy
SkillScope = SkillReadScope
SkillReadContext = SkillReadScope
SkillVersionRecord = SkillVersion
AuditEntry = SkillAudit
OutboxEvent = SkillOutboxEvent
MigrationMap = SkillMigrationMap
UnknownLedgerEntry = SkillUnknownLedgerEntry


__all__ = [
    "ALLOWED_CAPABILITIES", "ExecutionPolicy", "SkillAssetRef", "SkillAuthorizationError",
    "SkillBinding", "SkillConflictError", "SkillDecision", "SkillDefinition",
    "SkillError", "SkillEvidenceRef", "SkillExecutionReceipt", "SkillMutationContext",
    "SkillMutationResult", "SkillReadScope", "SkillReceipt", "SkillRuntimeError",
    "SkillSchemaError", "SkillValidationError", "SkillVersion", "SkillAudit",
    "SkillOutboxEvent", "SkillMigrationMap", "SkillUnknownLedgerEntry",
    "SkillManifest", "SkillExecutionPolicy", "SkillScope", "SkillReadContext",
    "SkillVersionRecord", "AuditEntry", "OutboxEvent", "MigrationMap",
    "UnknownLedgerEntry", "canonical_json", "stable_hash",
]
