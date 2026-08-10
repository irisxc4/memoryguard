"""Data contracts for the persistent V2 CodeGraph plane.

The graph is deliberately a metadata graph.  A source file is represented by
its workspace-relative path and a content digest; symbols carry signatures,
hashes and line ranges only.  No model in this module has a field for source
text.  This makes accidentally copying a file body into ``codegraph.db`` a
type/API error rather than a convention callers may forget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Mapping
import re


CODEGRAPH_DB_NAME = "codegraph.db"
CODEGRAPH_SCHEMA_VERSION = 1
CODEGRAPH_SCHEMA_MARKER = "memoryguard-v2-phase5-codegraph"
UNKNOWN = "__UNKNOWN__"

# Metadata accepted by the graph plane is deliberately small and bounded.
# Hash/digest fields are structural identities and are explicitly allowed;
# arbitrary ``source``/``body``/credential fields are not.
METADATA_MAX_DEPTH = 8
METADATA_MAX_BYTES = 64 * 1024
_METADATA_DENY_TOKENS = frozenset(
    {
        "secret",
        "token",
        "password",
        "apikey",
        "api_key",
        "code",
        "command",
        "body",
        "raw",
        "source",
        "authority",
        "owner",
        "admin",
        "acl",
        "scope",
        "capability",
    }
)
_METADATA_HASH_FIELDS = frozenset(
    {
        "hash",
        "digest",
        "content_hash",
        "source_hash",
        "file_hash",
        "symbol_hash",
        "canonical_hash",
        "payload_hash",
    }
)
_METADATA_STRUCTURAL_FIELDS = frozenset(
    {
        "path",
        "relative_path",
        "file_path",
        "file_id",
        "source_id",
        "source_revision",
        "revision_id",
        "symbol_id",
        "edge_id",
        "from_id",
        "to_id",
        "from",
        "to",
        "source_id",
        "target_id",
        "name",
        "label",
        "kind",
        "symbol_kind",
        "signature",
        "signature_text",
        "line_start",
        "line_end",
        "start_line",
        "end_line",
        "relation",
        "edge_kind",
        "type",
        "weight",
        "active",
        "language",
        "source_digest",
        "content_role",
        "object_type",
    }
)


def _metadata_key_forbidden(key: Any) -> bool:
    text = str(key).strip().lower()
    if text in _METADATA_HASH_FIELDS or text in _METADATA_STRUCTURAL_FIELDS:
        return False
    # Keep this token based so ``source_id`` remains a structural identity,
    # while ``source``/``source_text`` and credential compounds are blocked.
    tokens = {token for token in re.split(r"[^a-z0-9]+", text) if token}
    normalized = text.replace("-", "_")
    return normalized in _METADATA_DENY_TOKENS or bool(tokens & _METADATA_DENY_TOKENS)


def validate_metadata(value: Any, *, path: str = "", depth: int = 0) -> None:
    """Reject sensitive/unbounded metadata before any graph write.

    The function intentionally raises instead of redacting: silently dropping
    a nested field would make the persisted projection differ from the caller's
    authenticated input and could leave a partial write behind.
    """

    if depth > METADATA_MAX_DEPTH:
        raise CodeGraphError(f"metadata nesting exceeds {METADATA_MAX_DEPTH}")
    if isinstance(value, Mapping):
        for key, child in value.items():
            if _metadata_key_forbidden(key):
                raise CodeGraphError(f"forbidden codegraph metadata field: {key}")
            validate_metadata(child, path=f"{path}.{key}" if path else str(key), depth=depth + 1)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for index, child in enumerate(value):
            validate_metadata(child, path=f"{path}[{index}]", depth=depth + 1)
    elif isinstance(value, bytes) and len(value) > METADATA_MAX_BYTES:
        raise CodeGraphError("codegraph metadata value is too large")
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise CodeGraphError("codegraph metadata is not JSON-compatible") from exc
    if len(encoded) > METADATA_MAX_BYTES:
        raise CodeGraphError("codegraph metadata exceeds size limit")


class CodeGraphError(RuntimeError):
    """Base class for CodeGraph failures."""


class CodeGraphScopeError(CodeGraphError):
    """The caller did not provide a trusted, exact ACL scope."""


class CodeGraphPathError(CodeGraphError):
    """A source path is absolute, escapes the workspace, or is a reparse path."""


class CodeGraphSchemaError(CodeGraphError):
    """The persistent graph has an unknown/incomplete schema marker."""


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def stable_digest(value: Any) -> str:
    """Stable SHA-256 digest for JSON-compatible metadata."""

    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def stable_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join(str(item) for item in (prefix, *parts))
    return f"{prefix}-{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _strict_bool(value: Any, *, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be bool")
    return value


@dataclass(frozen=True)
class CodeGraphScope:
    """Exact workspace/agent/project/provider/group/runtime ACL tuple.

    ``trusted_context`` is explicit so a public adapter cannot accidentally
    treat a plain mapping or an unknown identity as an authorization context.
    Empty values are exact values, never wildcards.  ``UNKNOWN`` is rejected
    by the store for both reads and writes.
    """

    workspace_id: str
    agent_instance_id: str = ""
    project_ref: str = ""
    provider: str = ""
    share_group_id: str = ""
    runtime_role: str = ""
    trusted_context: bool = True

    def __post_init__(self) -> None:
        for name in (
            "workspace_id",
            "agent_instance_id",
            "project_ref",
            "provider",
            "share_group_id",
            "runtime_role",
        ):
            value = _text(getattr(self, name))
            object.__setattr__(self, name, value)
            if not value and name == "workspace_id":
                raise ValueError("workspace_id is required")
        if not isinstance(self.trusted_context, bool):
            raise ValueError("trusted_context must be bool")

    @classmethod
    def from_value(cls, value: "CodeGraphScope | Mapping[str, Any]") -> "CodeGraphScope":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("scope must be CodeGraphScope or mapping")
        data = dict(value)
        aliases = {
            "workspace": "workspace_id",
            "agent": "agent_instance_id",
            "project": "project_ref",
            "group": "share_group_id",
            "group_id": "share_group_id",
            "provider_id": "provider",
            "runtime": "runtime_role",
            "trusted": "trusted_context",
        }
        for alias, canonical in aliases.items():
            if alias not in data:
                continue
            if canonical == "trusted_context":
                alias_value = _strict_bool(data[alias], field_name=alias)
                if canonical in data and _strict_bool(data[canonical], field_name=canonical) != alias_value:
                    raise ValueError(f"conflicting scope alias: {alias}/{canonical}")
            elif canonical in data and str(data[canonical]) != str(data[alias]):
                raise ValueError(f"conflicting scope alias: {alias}/{canonical}")
            data.setdefault(canonical, data[alias])
        trusted_value = data.get("trusted_context", True)
        trusted_value = _strict_bool(trusted_value, field_name="trusted_context")
        return cls(
            workspace_id=_text(data.get("workspace_id")),
            agent_instance_id=_text(data.get("agent_instance_id")),
            project_ref=_text(data.get("project_ref")),
            provider=_text(data.get("provider")),
            share_group_id=_text(data.get("share_group_id")),
            runtime_role=_text(data.get("runtime_role")),
            trusted_context=trusted_value,
        )

    def as_tuple(self) -> tuple[str, ...]:
        return (
            self.workspace_id,
            self.agent_instance_id,
            self.project_ref,
            self.provider,
            self.share_group_id,
            self.runtime_role,
        )

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

    as_mapping = to_dict

    @property
    def digest(self) -> str:
        return stable_digest(self.as_tuple())

    def matches(self, candidate: Mapping[str, Any]) -> bool:
        aliases = {
            "workspace_id": ("workspace_id", "workspace"),
            "agent_instance_id": ("agent_instance_id", "agent"),
            "project_ref": ("project_ref", "project"),
            "provider": ("provider",),
            "share_group_id": ("share_group_id", "group_id", "group"),
            "runtime_role": ("runtime_role", "runtime"),
        }
        for field_name, names in aliases.items():
            values = [_text(candidate.get(name)) for name in names if name in candidate]
            if not values or any(value != getattr(self, field_name) for value in values):
                return False
        return True


@dataclass(frozen=True)
class SourceFile:
    source_id: str
    path: str
    content_hash: str
    scope: CodeGraphScope
    source_revision: str = ""
    language: str = ""
    active: bool = True
    revision_id: str = ""
    file_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _text(self.source_id))
        object.__setattr__(self, "path", _text(self.path))
        object.__setattr__(self, "content_hash", _text(self.content_hash))
        object.__setattr__(self, "source_revision", _text(self.source_revision))
        object.__setattr__(self, "language", _text(self.language))
        object.__setattr__(self, "revision_id", _text(self.revision_id))
        object.__setattr__(self, "file_id", _text(self.file_id) or stable_id("file", self.scope.digest, self.path))
        if not self.path:
            raise ValueError("source file path is required")
        if not self.content_hash:
            raise ValueError("source file content_hash is required")

    @property
    def stable_key(self) -> str:
        return f"{self.scope.digest}:{self.path}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_id": self.file_id,
            "source_id": self.source_id,
            "path": self.path,
            "content_hash": self.content_hash,
            "source_revision": self.source_revision,
            "language": self.language,
            "active": self.active,
            "revision_id": self.revision_id,
            "scope": self.scope.to_dict(),
        }


@dataclass(frozen=True)
class Symbol:
    symbol_id: str
    file_id: str
    name: str
    kind: str = "symbol"
    signature: str = ""
    symbol_hash: str = ""
    line_start: int = 0
    line_end: int = 0
    scope: CodeGraphScope | None = None
    revision_id: str = ""
    active: bool = True

    def __post_init__(self) -> None:
        for name in ("symbol_id", "file_id", "name", "kind", "signature", "symbol_hash", "revision_id"):
            object.__setattr__(self, name, _text(getattr(self, name)))
        if not self.symbol_id or not self.file_id or not self.name:
            raise ValueError("symbol_id, file_id and name are required")
        if int(self.line_start) < 0 or int(self.line_end) < 0:
            raise ValueError("symbol line range must be non-negative")
        if self.line_end and self.line_start and self.line_end < self.line_start:
            raise ValueError("symbol line_end must be >= line_start")
        object.__setattr__(self, "line_start", int(self.line_start))
        object.__setattr__(self, "line_end", int(self.line_end))

    @property
    def signature_hash(self) -> str:
        return self.symbol_hash or stable_digest({"name": self.name, "kind": self.kind, "signature": self.signature})

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol_id": self.symbol_id,
            "file_id": self.file_id,
            "name": self.name,
            "kind": self.kind,
            "signature": self.signature,
            "symbol_hash": self.symbol_hash,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "revision_id": self.revision_id,
            "active": self.active,
            "scope": self.scope.to_dict() if self.scope else None,
        }


@dataclass(frozen=True)
class Edge:
    edge_id: str
    from_id: str
    to_id: str
    relation: str = "related"
    scope: CodeGraphScope | None = None
    revision_id: str = ""
    weight: float = 1.0
    active: bool = True

    def __post_init__(self) -> None:
        for name in ("edge_id", "from_id", "to_id", "relation", "revision_id"):
            object.__setattr__(self, name, _text(getattr(self, name)))
        if not self.edge_id or not self.from_id or not self.to_id:
            raise ValueError("edge_id, from_id and to_id are required")
        object.__setattr__(self, "weight", float(self.weight))

    @property
    def edge_kind(self) -> str:
        return self.relation

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "from_id": self.from_id,
            "to_id": self.to_id,
            "relation": self.relation,
            "weight": self.weight,
            "revision_id": self.revision_id,
            "active": self.active,
            "scope": self.scope.to_dict() if self.scope else None,
        }


@dataclass(frozen=True)
class Revision:
    revision_id: str
    file_id: str
    content_hash: str
    source_revision: str = ""
    revision_number: int = 1
    created_at: str = ""
    active: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "file_id": self.file_id,
            "content_hash": self.content_hash,
            "source_revision": self.source_revision,
            "revision_number": self.revision_number,
            "created_at": self.created_at,
            "active": self.active,
        }


@dataclass(frozen=True)
class AffectedQuery:
    query_id: str
    scope: CodeGraphScope
    start_id: str
    depth: int
    limit: int
    result_ids: tuple[str, ...] = ()
    digest: str = ""
    created_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "start_id", _text(self.start_id))
        object.__setattr__(self, "depth", max(0, int(self.depth)))
        object.__setattr__(self, "limit", max(1, int(self.limit)))
        object.__setattr__(self, "result_ids", tuple(str(value) for value in self.result_ids))

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "scope": self.scope.to_dict(),
            "start_id": self.start_id,
            "depth": self.depth,
            "limit": self.limit,
            "result_ids": list(self.result_ids),
            "digest": self.digest,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class Checkpoint:
    checkpoint_id: str
    scope: CodeGraphScope
    domain: str
    sequence: int = 0
    digest: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "scope": self.scope.to_dict(),
            "domain": self.domain,
            "sequence": self.sequence,
            "digest": self.digest,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class OutboxEvent:
    event_id: str
    scope: CodeGraphScope
    event_type: str
    aggregate_id: str
    payload_hash: str
    sequence: int = 0
    status: str = "pending"
    attempts: int = 0
    error: str = ""
    created_at: str = ""
    projected_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "scope": self.scope.to_dict(),
            "event_type": self.event_type,
            "aggregate_id": self.aggregate_id,
            "payload_hash": self.payload_hash,
            "sequence": self.sequence,
            "status": self.status,
            "attempts": self.attempts,
            "error": self.error,
            "created_at": self.created_at,
            "projected_at": self.projected_at,
        }


@dataclass(frozen=True)
class UnknownLedgerEntry:
    ledger_id: str
    source_ref: str
    code: str
    detail: str = ""
    status: str = "BLOCKED"
    source_hash: str = ""
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ledger_id": self.ledger_id,
            "source_ref": self.source_ref,
            "code": self.code,
            "detail": self.detail,
            "status": self.status,
            "source_hash": self.source_hash,
            "created_at": self.created_at,
        }


__all__ = [
    "AffectedQuery",
    "Checkpoint",
    "CODEGRAPH_DB_NAME",
    "CODEGRAPH_SCHEMA_MARKER",
    "CODEGRAPH_SCHEMA_VERSION",
    "CodeGraphError",
    "CodeGraphPathError",
    "CodeGraphSchemaError",
    "CodeGraphScope",
    "CodeGraphScopeError",
    "Edge",
    "OutboxEvent",
    "Revision",
    "SourceFile",
    "Symbol",
    "UNKNOWN",
    "UnknownLedgerEntry",
    "stable_digest",
    "stable_id",
    "validate_metadata",
]
