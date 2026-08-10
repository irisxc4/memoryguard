"""Immutable contracts for Phase 7 maintenance.

The maintenance plane is deliberately a control-plane ledger.  It records
trusted scope, audit epochs and decisions; it never accepts a body, blob,
secret or executable control payload.  All values are normalised before they
are used as stable identities so retries cannot manufacture a second job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import math
from types import MappingProxyType
from typing import Any, Mapping


UNKNOWN_ACL = "__UNKNOWN__"
SCHEMA_VERSION = 1
SCHEMA_MARKER = "memoryguard-v2-phase7-maintenance"


class MaintenanceError(RuntimeError):
    """Base error for the fail-closed maintenance contract."""


class MaintenanceAuthorizationError(MaintenanceError, PermissionError):
    """A context is missing, untrusted, or outside this workspace."""


class MaintenanceSchemaError(MaintenanceError):
    """The maintenance ledger has an unsupported or incomplete schema."""


class MaintenanceConflictError(MaintenanceError, ValueError):
    """A CAS, idempotency, or stable-key conflict was detected."""


class MaintenanceLeaseError(MaintenanceError):
    """A maintenance lease is missing, expired, or owned by another actor."""


class MaintenanceJobState(str, Enum):
    PLANNED = "PLANNED"
    AUDITING = "AUDITING"
    READY = "READY"
    ACTIVE = "ACTIVE"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class MaintenanceOperation(str, Enum):
    AUDIT = "audit"
    REPORT = "report"
    SWEEP = "sweep"
    COMPACT = "compact"


class EpochState(str, Enum):
    OPEN = "OPEN"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class CandidateState(str, Enum):
    MARKED = "MARKED"
    CONFIRMED = "CONFIRMED"
    DELETING = "DELETING"
    BLOCKED = "BLOCKED"
    SWEPT = "SWEPT"


def _text(value: Any, field_name: str, *, required: bool = False) -> str:
    if value is None:
        raise ValueError(f"{field_name} must be explicit")
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{field_name} is required")
    if "\x00" in value:
        raise ValueError(f"{field_name} contains NUL")
    return value


def _strict_bool(value: Any, field_name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field_name} must be bool")
    return value


def _strict_int(value: Any, field_name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field_name} must be int >= {minimum}")
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(v) for v in value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite float is not allowed")
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError("binary values are not allowed in maintenance metadata")
    return value


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    return value


_DENY_METADATA_KEYS = frozenset({
    "body", "content", "text", "raw", "payload", "blob", "document", "transcript",
    "secret", "secrets", "token", "tokens", "password", "credential", "credentials",
    "api_key", "apikey", "private_key", "command", "code", "control", "control_payload",
    "authority", "admin", "acl", "scope", "authorization", "owner", "actor", "agent",
    "project", "share_group", "capability",
})

_METADATA_MAX_DEPTH = 8
_METADATA_MAX_NODES = 512
_METADATA_MAX_STRING = 4096
_METADATA_MAX_JSON_BYTES = 64 * 1024


def _validate_safe_metadata(value: Any, *, depth: int = 0, nodes: list[int] | None = None) -> None:
    if nodes is None:
        nodes = [0]
    if depth > _METADATA_MAX_DEPTH:
        raise ValueError("maintenance metadata is too deeply nested")
    nodes[0] += 1
    if nodes[0] > _METADATA_MAX_NODES:
        raise ValueError("maintenance metadata has too many nodes")
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().casefold().replace("-", "_")
            if normalized in _DENY_METADATA_KEYS:
                raise ValueError(f"maintenance metadata field is forbidden: {key}")
            if len(str(key)) > _METADATA_MAX_STRING:
                raise ValueError("maintenance metadata key is too large")
            _validate_safe_metadata(child, depth=depth + 1, nodes=nodes)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for child in value:
            _validate_safe_metadata(child, depth=depth + 1, nodes=nodes)
    elif isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError("maintenance metadata cannot contain binary values")
    elif isinstance(value, str) and len(value) > _METADATA_MAX_STRING:
        raise ValueError("maintenance metadata string is too large")
    elif not isinstance(value, (str, int, float, bool)) and value is not None:
        try:
            json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("maintenance metadata must be JSON-compatible") from exc


def stable_digest(value: Any) -> str:
    """Return a deterministic SHA-256 digest for JSON-safe control values."""

    encoded = json.dumps(_plain(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > _METADATA_MAX_JSON_BYTES:
        raise ValueError("maintenance metadata JSON is too large")
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def stable_id(prefix: str, *parts: object) -> str:
    prefix = _text(prefix, "prefix", required=True)
    payload = "\x1f".join(str(item) for item in (prefix, *parts))
    return f"{prefix}-{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


_SCOPE_FIELDS = frozenset({
    "workspace_id", "agent_instance_id", "project_ref", "provider",
    "share_group_id", "runtime_role", "trusted_context",
})
_ALIASES = {
    "workspace": "workspace_id", "agent": "agent_instance_id", "project": "project_ref",
    "group_id": "share_group_id", "group": "share_group_id", "runtime": "runtime_role",
    "trusted": "trusted_context", "is_trusted": "trusted_context",
}


def _scope_values(value: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(value)
    unknown = set(data) - _SCOPE_FIELDS - set(_ALIASES)
    if unknown:
        raise ValueError(f"unknown maintenance ACL/control field(s): {', '.join(sorted(map(str, unknown)))}")
    for alias, canonical in _ALIASES.items():
        if alias in data:
            if canonical in data and data[canonical] != data[alias]:
                raise ValueError(f"conflicting maintenance scope aliases: {canonical}")
            data.setdefault(canonical, data[alias])
    return {field: data.get(field, "" if field != "trusted_context" else False) for field in _SCOPE_FIELDS}


@dataclass(frozen=True, slots=True)
class MaintenanceScope:
    """Exact ACL tuple; empty dimensions are values, never wildcards."""

    workspace_id: str
    agent_instance_id: str = ""
    project_ref: str = ""
    provider: str = ""
    share_group_id: str = ""
    runtime_role: str = ""
    trusted_context: bool = False

    def __post_init__(self) -> None:
        for field_name in ("workspace_id", "agent_instance_id", "project_ref", "provider", "share_group_id", "runtime_role"):
            object.__setattr__(self, field_name, _text(getattr(self, field_name), field_name, required=field_name == "workspace_id"))
        object.__setattr__(self, "trusted_context", _strict_bool(self.trusted_context, "trusted_context"))
        if UNKNOWN_ACL in self.to_dict().values():
            raise ValueError("unknown ACL dimensions are not valid")

    @classmethod
    def from_value(cls, value: "MaintenanceScope | Mapping[str, Any]") -> "MaintenanceScope":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("maintenance scope must be MaintenanceScope or mapping")
        return cls(**_scope_values(value))

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "agent_instance_id": self.agent_instance_id,
            "project_ref": self.project_ref,
            "provider": self.provider,
            "share_group_id": self.share_group_id,
            "runtime_role": self.runtime_role,
            "trusted_context": self.trusted_context,
        }

    as_dict = to_dict

    @property
    def digest(self) -> str:
        return stable_digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class MaintenanceContext:
    """A trusted execution context, including optional generation/lease pins."""

    scope: MaintenanceScope
    actor_id: str = ""
    maintenance_lease_id: str = ""
    expected_generation: int | None = None
    trusted_context: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.scope, MaintenanceScope):
            raise TypeError("scope must be MaintenanceScope")
        object.__setattr__(self, "actor_id", _text(self.actor_id, "actor_id", required=True))
        object.__setattr__(self, "maintenance_lease_id", _text(self.maintenance_lease_id, "maintenance_lease_id"))
        object.__setattr__(self, "trusted_context", _strict_bool(self.trusted_context, "trusted_context"))
        if self.expected_generation is not None:
            object.__setattr__(self, "expected_generation", _strict_int(self.expected_generation, "expected_generation"))
        if self.trusted_context is not True or self.scope.trusted_context is not True:
            raise ValueError("trusted MaintenanceContext and trusted scope are required")

    @classmethod
    def trusted(
        cls,
        scope: MaintenanceScope | Mapping[str, Any],
        *,
        actor_id: str,
        maintenance_lease_id: str = "",
        expected_generation: int | None = None,
    ) -> "MaintenanceContext":
        """Explicit host-side constructor for a trusted context."""

        resolved = MaintenanceScope.from_value(scope)
        if not resolved.trusted_context:
            raise MaintenanceAuthorizationError("trusted scope is required")
        return cls(resolved, actor_id=actor_id, maintenance_lease_id=maintenance_lease_id, expected_generation=expected_generation, trusted_context=True)

    @classmethod
    def from_scope(
        cls,
        scope: MaintenanceScope | Mapping[str, Any],
        *,
        actor_id: str,
        maintenance_lease_id: str = "",
        expected_generation: int | None = None,
    ) -> "MaintenanceContext":
        return cls.trusted(scope, actor_id=actor_id, maintenance_lease_id=maintenance_lease_id, expected_generation=expected_generation)

    @classmethod
    def from_value(cls, value: "MaintenanceContext | Mapping[str, Any]") -> "MaintenanceContext":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("maintenance context must be MaintenanceContext or mapping")
        allowed = {"scope", "actor_id", "maintenance_lease_id", "expected_generation", "trusted_context", "actor", "lease_id"}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"unknown maintenance context field(s): {', '.join(sorted(map(str, unknown)))}")
        scope = value.get("scope")
        if scope is None:
            scope = {key: value[key] for key in _SCOPE_FIELDS if key in value}
        return cls(
            scope=MaintenanceScope.from_value(scope),
            actor_id=value.get("actor_id", value.get("actor", "")),
            maintenance_lease_id=value.get("maintenance_lease_id", value.get("lease_id", "")),
            expected_generation=value.get("expected_generation"),
            trusted_context=value.get("trusted_context", False),
        )

    @property
    def workspace_id(self) -> str:
        return self.scope.workspace_id

    @property
    def digest(self) -> str:
        return stable_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope.to_dict(), "actor_id": self.actor_id,
            "maintenance_lease_id": self.maintenance_lease_id,
            "expected_generation": self.expected_generation,
            "trusted_context": self.trusted_context,
        }


@dataclass(frozen=True, slots=True)
class MaintenanceJob:
    job_id: str
    request_key: str
    operation: MaintenanceOperation
    state: MaintenanceJobState = MaintenanceJobState.PLANNED
    dry_run: bool = True
    expected_generation: int | None = None
    context_digest: str = ""
    created_at: str = ""
    updated_at: str = ""
    error_code: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "job_id", _text(self.job_id, "job_id", required=True))
        object.__setattr__(self, "request_key", _text(self.request_key, "request_key", required=True))
        operation = self.operation if isinstance(self.operation, MaintenanceOperation) else MaintenanceOperation(str(self.operation).casefold())
        state = self.state if isinstance(self.state, MaintenanceJobState) else MaintenanceJobState(str(self.state).upper())
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "dry_run", _strict_bool(self.dry_run, "dry_run"))
        if self.expected_generation is not None:
            object.__setattr__(self, "expected_generation", _strict_int(self.expected_generation, "expected_generation"))
        for name in ("context_digest", "created_at", "updated_at", "error_code"):
            object.__setattr__(self, name, _text(getattr(self, name), name))

    @property
    def idempotency_digest(self) -> str:
        return stable_digest({"request_key": self.request_key, "operation": self.operation.value, "dry_run": self.dry_run, "expected_generation": self.expected_generation, "context_digest": self.context_digest})

    @property
    def idempotency_key(self) -> str:
        return self.request_key

    @property
    def request_digest(self) -> str:
        return self.idempotency_digest

    def to_dict(self) -> dict[str, Any]:
        return {"job_id": self.job_id, "request_key": self.request_key, "operation": self.operation.value, "state": self.state.value, "dry_run": self.dry_run, "expected_generation": self.expected_generation, "context_digest": self.context_digest, "created_at": self.created_at, "updated_at": self.updated_at, "error_code": self.error_code}


@dataclass(frozen=True, slots=True)
class MaintenanceEpoch:
    epoch_id: str
    job_id: str
    epoch_number: int
    state: EpochState = EpochState.OPEN
    reference_digest: str = ""
    complete: bool = False
    created_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "epoch_id", _text(self.epoch_id, "epoch_id", required=True))
        object.__setattr__(self, "job_id", _text(self.job_id, "job_id", required=True))
        object.__setattr__(self, "epoch_number", _strict_int(self.epoch_number, "epoch_number", minimum=1))
        state = self.state if isinstance(self.state, EpochState) else EpochState(str(self.state).upper())
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "reference_digest", _text(self.reference_digest, "reference_digest"))
        object.__setattr__(self, "complete", _strict_bool(self.complete, "complete"))
        object.__setattr__(self, "created_at", _text(self.created_at, "created_at"))

    def to_dict(self) -> dict[str, Any]:
        return {"epoch_id": self.epoch_id, "job_id": self.job_id, "epoch_number": self.epoch_number, "state": self.state.value, "reference_digest": self.reference_digest, "complete": self.complete, "created_at": self.created_at}


@dataclass(frozen=True, slots=True)
class MaintenanceCandidate:
    candidate_id: str
    epoch_id: str
    blob_id: str
    reference_digest: str
    hold_digest: str = ""
    state: CandidateState = CandidateState.MARKED
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        for name in ("candidate_id", "epoch_id", "blob_id", "reference_digest", "hold_digest", "created_at", "updated_at"):
            object.__setattr__(self, name, _text(getattr(self, name), name, required=name in {"candidate_id", "epoch_id", "blob_id", "reference_digest"}))
        state = self.state if isinstance(self.state, CandidateState) else CandidateState(str(self.state).upper())
        object.__setattr__(self, "state", state)

    @property
    def digest(self) -> str:
        return stable_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {"candidate_id": self.candidate_id, "epoch_id": self.epoch_id, "blob_id": self.blob_id, "reference_digest": self.reference_digest, "hold_digest": self.hold_digest, "state": self.state.value, "created_at": self.created_at, "updated_at": self.updated_at}


@dataclass(frozen=True, slots=True)
class MaintenanceReport:
    report_id: str
    job_id: str
    status: str
    counts: Mapping[str, int] = field(default_factory=dict)
    safety: Mapping[str, Any] = field(default_factory=dict)
    digest: str = ""
    created_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "report_id", _text(self.report_id, "report_id", required=True))
        object.__setattr__(self, "job_id", _text(self.job_id, "job_id", required=True))
        object.__setattr__(self, "status", _text(self.status, "status", required=True))
        if not isinstance(self.counts, Mapping) or not isinstance(self.safety, Mapping):
            raise ValueError("report counts and safety must be mappings")
        _validate_safe_metadata(self.safety)
        counts: dict[str, int] = {}
        if len(self.counts) > _METADATA_MAX_NODES:
            raise ValueError("report counts have too many entries")
        for key, value in self.counts.items():
            count_key = _text(str(key), "count key", required=True)
            normalized = count_key.casefold().replace("-", "_")
            if normalized in _DENY_METADATA_KEYS:
                raise ValueError(f"report count field is forbidden: {key}")
            if len(count_key) > _METADATA_MAX_STRING:
                raise ValueError("report count key is too large")
            counts[count_key] = _strict_int(value, f"count[{key}]")
        object.__setattr__(self, "counts", MappingProxyType(counts))
        object.__setattr__(self, "safety", _freeze(self.safety))
        object.__setattr__(self, "digest", _text(self.digest, "digest"))
        object.__setattr__(self, "created_at", _text(self.created_at, "created_at"))

    @property
    def stable_digest(self) -> str:
        return self.digest or stable_digest({"report_id": self.report_id, "job_id": self.job_id, "status": self.status, "counts": self.counts, "safety": self.safety})

    def to_dict(self) -> dict[str, Any]:
        return {"report_id": self.report_id, "job_id": self.job_id, "status": self.status, "counts": _plain(self.counts), "safety": _plain(self.safety), "digest": self.digest, "created_at": self.created_at}


@dataclass(frozen=True, slots=True)
class MaintenanceLease:
    lease_id: str
    owner_id: str
    scope_digest: str
    expires_at: str
    acquired_at: str = ""
    released_at: str = ""
    active: bool = True

    def __post_init__(self) -> None:
        for name in ("lease_id", "owner_id", "scope_digest", "expires_at", "acquired_at", "released_at"):
            object.__setattr__(self, name, _text(getattr(self, name), name, required=name in {"lease_id", "owner_id", "scope_digest", "expires_at"}))
        object.__setattr__(self, "active", _strict_bool(self.active, "active"))

    def to_dict(self) -> dict[str, Any]:
        return {"lease_id": self.lease_id, "owner_id": self.owner_id, "scope_digest": self.scope_digest, "expires_at": self.expires_at, "acquired_at": self.acquired_at, "released_at": self.released_at, "active": self.active}


@dataclass(frozen=True, slots=True)
class MaintenanceReceipt:
    """Immutable idempotency receipt returned by a job request."""

    receipt_id: str
    request_key: str
    operation: str
    request_digest: str
    job_id: str
    result_digest: str = ""
    status: str = "created"
    created_at: str = ""

    def __post_init__(self) -> None:
        for name in ("receipt_id", "request_key", "operation", "request_digest", "job_id", "result_digest", "status", "created_at"):
            object.__setattr__(self, name, _text(getattr(self, name), name, required=name in {"receipt_id", "request_key", "operation", "request_digest", "job_id"}))

    @property
    def idempotency_key(self) -> str:
        return self.request_key

    def to_dict(self) -> dict[str, Any]:
        return {"receipt_id": self.receipt_id, "request_key": self.request_key, "operation": self.operation, "request_digest": self.request_digest, "job_id": self.job_id, "result_digest": self.result_digest, "status": self.status, "created_at": self.created_at}


@dataclass(frozen=True, slots=True)
class MaintenanceLedgerEntry:
    ledger_id: str
    event_type: str
    job_id: str
    epoch_id: str = ""
    candidate_id: str = ""
    detail_digest: str = ""
    created_at: str = ""

    def __post_init__(self) -> None:
        for name in ("ledger_id", "event_type", "job_id", "epoch_id", "candidate_id", "detail_digest", "created_at"):
            object.__setattr__(self, name, _text(getattr(self, name), name, required=name in {"ledger_id", "event_type", "job_id"}))

    def to_dict(self) -> dict[str, Any]:
        return {"ledger_id": self.ledger_id, "event_type": self.event_type, "job_id": self.job_id, "epoch_id": self.epoch_id, "candidate_id": self.candidate_id, "detail_digest": self.detail_digest, "created_at": self.created_at}


MaintenanceReadScope = MaintenanceScope
MaintenanceMutationContext = MaintenanceContext
