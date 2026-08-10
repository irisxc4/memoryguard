"""Value objects for the V2 asset registry.

The asset plane stores identities, hashes and metadata only.  These models are
deliberately boring dataclasses so callers cannot accidentally treat an asset
record as a binary/document transport.  ``AssetScope`` is also the canonical
ACL tuple used by :mod:`memoryguard.assets_v2.store`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


UNKNOWN_ACL = "__UNKNOWN__"


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


@dataclass(frozen=True)
class AssetScope:
    """Exact authorization tuple for one asset read or mutation.

    Empty values are retained as values (never wildcarded); read paths reject
    empty/unknown ACL dimensions.  ``admin`` is a mutation capability and is
    ignored when evaluating a read scope.
    """

    namespace_id: str
    workspace_id: str
    agent_instance_id: str = ""
    project_ref: str = ""
    provider: str = ""
    share_group_id: str = ""
    runtime_role: str = ""
    actor: str = ""
    authority: str = "manual"
    admin: bool = False

    def __post_init__(self) -> None:
        names = (
            "namespace_id",
            "workspace_id",
            "agent_instance_id",
            "project_ref",
            "provider",
            "share_group_id",
            "runtime_role",
            "actor",
            "authority",
        )
        for name in names:
            value = getattr(self, name)
            if value is None:
                raise ValueError(f"{name} must be explicit; None is not a wildcard")
            object.__setattr__(self, name, str(value))
        if not self.namespace_id or not self.workspace_id:
            raise ValueError("namespace_id and workspace_id are required")
        if not self.share_group_id:
            raise ValueError("share_group_id is required")
        if not isinstance(self.admin, bool):
            if isinstance(self.admin, int) and self.admin in (0, 1):
                object.__setattr__(self, "admin", bool(self.admin))
            else:
                raise ValueError("admin must be a boolean or 0/1")
        object.__setattr__(self, "authority", self.authority.casefold() or "manual")

    @classmethod
    def from_value(cls, value: "AssetScope | Mapping[str, Any]", **overrides: Any) -> "AssetScope":
        if isinstance(value, cls):
            if not overrides:
                return value
            values = value.to_dict()
            values.update(overrides)
            return cls(**values)
        if not isinstance(value, Mapping):
            raise TypeError("asset scope must be an AssetScope or mapping")
        aliases = {
            "namespace": "namespace_id",
            "workspace": "workspace_id",
            "agent": "agent_instance_id",
            "project": "project_ref",
            "group_id": "share_group_id",
            "runtime": "runtime_role",
            "is_admin": "admin",
        }
        values: dict[str, Any] = dict(value)
        for alias, canonical in aliases.items():
            if canonical in values and alias in values and str(values[canonical]) != str(values[alias]):
                raise ValueError(f"conflicting asset scope aliases: {canonical}")
            if canonical not in values and alias in values:
                values[canonical] = values[alias]
        values.update(overrides)
        return cls(
            namespace_id=values.get("namespace_id", ""),
            workspace_id=values.get("workspace_id", ""),
            agent_instance_id=values.get("agent_instance_id", ""),
            project_ref=values.get("project_ref", ""),
            provider=values.get("provider", ""),
            share_group_id=values.get("share_group_id", ""),
            runtime_role=values.get("runtime_role", ""),
            actor=values.get("actor", ""),
            authority=values.get("authority", "manual"),
            admin=values.get("admin", False),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "namespace_id": self.namespace_id,
            "workspace_id": self.workspace_id,
            "agent_instance_id": self.agent_instance_id,
            "project_ref": self.project_ref,
            "provider": self.provider,
            "share_group_id": self.share_group_id,
            "runtime_role": self.runtime_role,
            "actor": self.actor,
            "authority": self.authority,
            "admin": bool(self.admin),
        }

    as_dict = to_dict


AssetReadScope = AssetScope
AssetReadContext = AssetScope
AssetMutationContext = AssetScope


@dataclass(frozen=True)
class Asset:
    asset_id: str
    asset_key: str
    asset_kind: str
    namespace_id: str
    workspace_id: str
    agent_instance_id: str
    project_ref: str
    provider: str
    share_group_id: str
    runtime_role: str
    state: str = "active"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    acl_digest: str = ""
    created_at: str = ""
    updated_at: str = ""

    @property
    def status(self) -> str:
        return self.state

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "asset_key": self.asset_key,
            "asset_kind": self.asset_kind,
            "namespace_id": self.namespace_id,
            "workspace_id": self.workspace_id,
            "agent_instance_id": self.agent_instance_id,
            "project_ref": self.project_ref,
            "provider": self.provider,
            "share_group_id": self.share_group_id,
            "runtime_role": self.runtime_role,
            "state": self.state,
            "status": self.state,
            "metadata": _mapping(self.metadata),
            "acl_digest": self.acl_digest,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    as_dict = to_dict


@dataclass(frozen=True)
class AssetVersion:
    version_id: str
    asset_id: str
    version_key: str
    version: int
    content_hash: str
    size_bytes: int
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = ""

    @property
    def digest(self) -> str:
        return self.content_hash

    @property
    def hash(self) -> str:
        return self.content_hash

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "asset_id": self.asset_id,
            "version_key": self.version_key,
            "version": self.version,
            "content_hash": self.content_hash,
            "digest": self.content_hash,
            "size_bytes": self.size_bytes,
            "metadata": _mapping(self.metadata),
            "created_at": self.created_at,
        }

    as_dict = to_dict


@dataclass(frozen=True)
class AssetLocation:
    location_id: str
    asset_id: str
    version_id: str
    root_ref: str
    relative_path: str
    content_hash: str
    size_bytes: int
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = ""

    @property
    def path(self) -> str:
        return self.relative_path

    @property
    def digest(self) -> str:
        return self.content_hash

    def to_dict(self) -> dict[str, Any]:
        return {
            "location_id": self.location_id,
            "asset_id": self.asset_id,
            "version_id": self.version_id,
            "root_ref": self.root_ref,
            "relative_path": self.relative_path,
            "path": self.relative_path,
            "content_hash": self.content_hash,
            "digest": self.content_hash,
            "size_bytes": self.size_bytes,
            "metadata": _mapping(self.metadata),
            "created_at": self.created_at,
        }

    as_dict = to_dict


@dataclass(frozen=True)
class AssetReference:
    reference_id: str
    asset_id: str
    version_id: str
    reference_kind: str
    target_id: str
    target_hash: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = ""

    @property
    def ref_type(self) -> str:
        return self.reference_kind

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference_id": self.reference_id,
            "asset_id": self.asset_id,
            "version_id": self.version_id,
            "reference_kind": self.reference_kind,
            "ref_type": self.reference_kind,
            "target_id": self.target_id,
            "target_hash": self.target_hash,
            "metadata": _mapping(self.metadata),
            "created_at": self.created_at,
        }

    as_dict = to_dict


@dataclass(frozen=True)
class AssetHold:
    hold_id: str
    asset_id: str
    version_id: str
    reason: str
    source_ref: str
    active: bool
    created_at: str
    released_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "hold_id": self.hold_id,
            "asset_id": self.asset_id,
            "version_id": self.version_id,
            "reason": self.reason,
            "source_ref": self.source_ref,
            "active": bool(self.active),
            "created_at": self.created_at,
            "released_at": self.released_at,
        }

    as_dict = to_dict


@dataclass(frozen=True)
class AssetTombstone:
    tombstone_id: str
    asset_id: str
    version_id: str
    reason: str
    active: bool
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = ""
    restored_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tombstone_id": self.tombstone_id,
            "asset_id": self.asset_id,
            "version_id": self.version_id,
            "reason": self.reason,
            "active": bool(self.active),
            "metadata": _mapping(self.metadata),
            "created_at": self.created_at,
            "restored_at": self.restored_at,
        }

    as_dict = to_dict


@dataclass(frozen=True)
class AssetMigrationMap:
    map_id: str
    source_domain: str
    source_ref: str
    source_id: str
    target_type: str
    target_id: str
    target_hash: str
    status: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "map_id": self.map_id,
            "source_domain": self.source_domain,
            "source_ref": self.source_ref,
            "source_id": self.source_id,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "target_hash": self.target_hash,
            "status": self.status,
            "metadata": _mapping(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    as_dict = to_dict


@dataclass(frozen=True)
class AssetOutboxEvent:
    event_id: str
    aggregate_type: str
    aggregate_id: str
    event_type: str
    payload_hash: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    status: str = "pending"
    attempts: int = 0
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "event_type": self.event_type,
            "payload_hash": self.payload_hash,
            "payload": _mapping(self.payload),
            "status": self.status,
            "attempts": self.attempts,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    as_dict = to_dict


@dataclass(frozen=True)
class AssetAudit:
    audit_id: str
    operation: str
    aggregate_type: str
    aggregate_id: str
    idempotency_key: str
    actor: str
    authority: str
    payload_hash: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "audit_id": self.audit_id,
            "operation": self.operation,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "idempotency_key": self.idempotency_key,
            "actor": self.actor,
            "authority": self.authority,
            "payload_hash": self.payload_hash,
            "metadata": _mapping(self.metadata),
            "created_at": self.created_at,
        }

    as_dict = to_dict


@dataclass(frozen=True)
class AssetUnknownLedgerEntry:
    unknown_id: str
    source_domain: str
    source_ref: str
    field: str
    value: str
    reason: str
    status: str = "BLOCKED"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "unknown_id": self.unknown_id,
            "source_domain": self.source_domain,
            "source_ref": self.source_ref,
            "field": self.field,
            "value": self.value,
            "reason": self.reason,
            "status": self.status,
            "metadata": _mapping(self.metadata),
            "created_at": self.created_at,
        }

    as_dict = to_dict


# Compatibility spellings used by early V2 design notes.
AssetVersionRecord = AssetVersion
AssetLocationRecord = AssetLocation
AssetReferenceRecord = AssetReference
Hold = AssetHold
Tombstone = AssetTombstone
MigrationMap = AssetMigrationMap
OutboxEvent = AssetOutboxEvent
AuditEntry = AssetAudit
UnknownLedgerEntry = AssetUnknownLedgerEntry
Version = AssetVersion
Location = AssetLocation
Reference = AssetReference
Audit = AssetAudit
Outbox = AssetOutboxEvent
UnknownLedger = AssetUnknownLedgerEntry
AssetACL = AssetScope
ReadContext = AssetReadContext
MutationContext = AssetMutationContext


__all__ = [
    "UNKNOWN_ACL",
    "AssetScope",
    "AssetReadScope",
    "AssetReadContext",
    "AssetMutationContext",
    "Asset",
    "AssetVersion",
    "AssetLocation",
    "AssetReference",
    "AssetHold",
    "AssetTombstone",
    "AssetMigrationMap",
    "AssetOutboxEvent",
    "AssetAudit",
    "AssetUnknownLedgerEntry",
    "AssetVersionRecord",
    "AssetLocationRecord",
    "AssetReferenceRecord",
    "Hold",
    "Tombstone",
    "MigrationMap",
    "OutboxEvent",
    "AuditEntry",
    "UnknownLedgerEntry",
    "Version",
    "Location",
    "Reference",
    "Audit",
    "Outbox",
    "UnknownLedger",
    "AssetACL",
    "ReadContext",
    "MutationContext",
]
