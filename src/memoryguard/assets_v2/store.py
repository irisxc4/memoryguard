"""V2 Asset Registry storage.

The registry is a metadata-only SQLite domain.  It records stable identities,
versions, hashes, relative locations and cross-domain references; it never
copies a file, document body or binary payload.  Public mutations require an
explicit :class:`AssetMutationContext`, and public reads require an exact
read scope so an unauthorized caller cannot distinguish a missing asset from
an existing one.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
import ntpath
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
from typing import Any, Callable, Iterable, Iterator, Mapping
import unicodedata

from ..storage.database import execute_sql_script, open_database
from ..storage.layout import LayoutError, WorkspaceV2Layout
from ..storage.schema import SchemaError, initialize_database
from ..storage.transaction import transaction
from .models import (
    UNKNOWN_ACL,
    Asset,
    AssetAudit,
    AssetHold,
    AssetLocation,
    AssetMigrationMap,
    AssetOutboxEvent,
    AssetReference,
    AssetScope,
    AssetTombstone,
    AssetUnknownLedgerEntry,
    AssetVersion,
)


SCHEMA_VERSION = 1
SCHEMA_MARKER = "memoryguard-v2-phase5-assets"
ASSET_SCHEMA_VERSION = SCHEMA_VERSION
ASSET_SCHEMA_MARKER = SCHEMA_MARKER


class AssetError(RuntimeError):
    """Base class for asset registry failures."""


class AssetSchemaError(AssetError):
    """The assets database is missing or has an unsupported marker."""


class AssetAuthorizationError(AssetError, PermissionError):
    """A caller did not provide an authorized exact context."""


class AssetConflictError(AssetError, ValueError):
    """A stable key was replayed with different immutable data."""


class AssetPathError(AssetError, ValueError):
    """A location is outside its declared root or uses an unsafe path."""


class AssetMigrationError(AssetError):
    """A read-only V1 asset import could not be completed."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    if isinstance(value, (bytes, bytearray, memoryview)):
        payload = bytes(value)
    elif isinstance(value, str):
        payload = value.encode("utf-8")
    else:
        payload = _json(value).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def stable_id(prefix: str, *parts: object) -> str:
    """Derive a stable, non-sequential identifier."""

    payload = "\x1f".join(str(part) for part in (prefix, *parts))
    return f"{prefix}-{_digest(payload)}"


_SENSITIVE_METADATA_KEYS = frozenset(
    {
        # These are complete field names, not fragments.  In particular,
        # ``token_count`` and ``provider_name`` are ordinary metadata and must
        # not be rejected merely because they contain a sensitive word.
        "secret", "secrets", "token", "tokens", "password", "passwd",
        "credential", "credentials", "api", "api_key", "apikey", "api_keys",
        "key", "authority", "authority_id", "owner", "owner_id", "admin",
        "admin_id", "is_admin", "acl", "acl_digest", "acl_hash", "namespace",
        "namespace_id", "scope", "scope_id", "capability", "capability_id",
        "code", "command", "body", "payload", "content", "text", "raw",
        "binary", "bytes", "blob", "transcript", "document", "agent", "agent_id",
        "agent_instance_id", "project", "project_id", "runtime", "runtime_role",
        "group", "group_id", "authorization", "auth", "private_key", "secret_key",
    }
)
_MAX_METADATA_DEPTH = 8
_MAX_METADATA_NODES = 2048
_MAX_METADATA_BYTES = 64 * 1024
_MAX_METADATA_STRING = 8 * 1024


def _metadata_key_tokens(raw_key: Any) -> tuple[str, ...]:
    key = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(raw_key)).casefold()
    return tuple(token for token in re.split(r"[^a-z0-9]+", key) if token)


def _metadata_key_name(raw_key: Any) -> str:
    """Canonicalize one metadata key without substring matching."""

    return "_".join(_metadata_key_tokens(raw_key))


def _metadata_key_forbidden(raw_key: Any) -> bool:
    key = _metadata_key_name(raw_key)
    if not key:
        return True
    # These complete structural fields are deliberately allowed.  ACL fields
    # themselves belong in AssetScope/Asset.acl_digest, never metadata.
    if key in {"content_hash", "content_digest", "provider", "provider_id", "output_hash"}:
        return False
    return key in _SENSITIVE_METADATA_KEYS


def _validate_metadata(value: Mapping[str, Any] | None, *, _depth: int = 0) -> dict[str, Any]:
    """Validate bounded metadata while rejecting secret/control/body fields."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("asset metadata must be a mapping")
    if _depth > _MAX_METADATA_DEPTH:
        raise ValueError("asset metadata nesting is too deep")
    nodes = [0]

    def walk(raw: Any, depth: int) -> Any:
        if depth > _MAX_METADATA_DEPTH:
            raise ValueError("asset metadata nesting is too deep")
        nodes[0] += 1
        if nodes[0] > _MAX_METADATA_NODES:
            raise ValueError("asset metadata has too many nodes")
        if isinstance(raw, Mapping):
            output: dict[str, Any] = {}
            for raw_key, raw_value in raw.items():
                key = str(raw_key)
                if _metadata_key_forbidden(key):
                    raise ValueError(f"asset metadata field is sensitive or body-like: {key}")
                output[key] = walk(raw_value, depth + 1)
            return output
        if isinstance(raw, (list, tuple)):
            return [walk(item, depth + 1) for item in raw]
        if isinstance(raw, (bytes, bytearray, memoryview)):
            raise ValueError("asset metadata cannot contain binary values")
        if isinstance(raw, str):
            if len(raw) > _MAX_METADATA_STRING:
                raise ValueError("asset metadata string is too large")
            if any(ord(char) < 32 for char in raw):
                raise ValueError("asset metadata contains control characters")
            return raw
        if isinstance(raw, float) and not math.isfinite(raw):
            raise ValueError("asset metadata float is not finite")
        try:
            json.dumps(raw, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("asset metadata value is not JSON-safe") from exc
        return raw

    result = walk(value, _depth)
    encoded = _json(result)
    if len(encoded.encode("utf-8")) > _MAX_METADATA_BYTES:
        raise ValueError("asset metadata is too large")
    return result


def _validate_event_payload(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate bounded operational payloads without treating them as metadata.

    Outbox payloads may contain model-level fields such as ``acl_digest``.
    They are not persisted in an asset ``metadata_json`` column, but still
    require the same JSON/depth/size bounds and must never carry binary data.
    """

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("asset event payload must be a mapping")
    nodes = [0]

    def walk(raw: Any, depth: int) -> Any:
        if depth > _MAX_METADATA_DEPTH:
            raise ValueError("asset event payload nesting is too deep")
        nodes[0] += 1
        if nodes[0] > _MAX_METADATA_NODES:
            raise ValueError("asset event payload has too many nodes")
        if isinstance(raw, Mapping):
            return {str(key): walk(item, depth + 1) for key, item in raw.items()}
        if isinstance(raw, (list, tuple)):
            return [walk(item, depth + 1) for item in raw]
        if isinstance(raw, (bytes, bytearray, memoryview)):
            raise ValueError("asset event payload cannot contain binary values")
        if isinstance(raw, str):
            if len(raw) > _MAX_METADATA_STRING or any(ord(char) < 32 for char in raw):
                raise ValueError("asset event payload string is invalid or too large")
            return raw
        if isinstance(raw, float) and not math.isfinite(raw):
            raise ValueError("asset event payload float is not finite")
        try:
            json.dumps(raw, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("asset event payload value is not JSON-safe") from exc
        return raw

    result = walk(value, 0)
    if len(_json(result).encode("utf-8")) > _MAX_METADATA_BYTES:
        raise ValueError("asset event payload is too large")
    return result


def _decode_json(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    try:
        value = json.loads(raw.decode() if isinstance(raw, bytes) else str(raw))
    except (TypeError, ValueError, UnicodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _assert_no_reparse(path: str | Path) -> Path:
    """Reject symlink/junction/reparse components before resolving a path."""

    raw = Path(path).expanduser()
    absolute = Path(os.path.abspath(os.fspath(raw)))
    components = list(reversed(absolute.parents)) + [absolute]
    for component in components:
        try:
            info = os.lstat(component)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise AssetPathError(f"cannot inspect path component: {component}") from exc
        if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x0400):
            raise AssetPathError(f"path cannot contain a symlink or reparse point: {component}")
    return absolute


_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
)


def normalize_relative_path(value: str | Path) -> str:
    """Normalize and validate a stable, workspace-relative path reference."""

    text = unicodedata.normalize("NFC", str(value))
    if not text or "\x00" in text:
        raise AssetPathError("relative path is empty or contains NUL")
    if "\\" in text or ntpath.isabs(text) or ntpath.splitdrive(text)[0]:
        raise AssetPathError("relative path must use forward-slash separators")
    if text.startswith("/"):
        raise AssetPathError("relative path cannot be absolute")
    pieces = text.split("/")
    for piece in pieces:
        if piece in {"", ".", ".."}:
            raise AssetPathError("relative path contains an empty or traversal component")
        if piece.rstrip(" .") != piece:
            raise AssetPathError("Windows paths cannot end a component with dot/space")
        if piece.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
            raise AssetPathError("relative path uses a Windows reserved component")
        if any(ord(char) < 32 for char in piece):
            raise AssetPathError("relative path contains a control character")
    normalized = PurePosixPath(*pieces).as_posix()
    if normalized in {"", "."} or normalized.startswith("../"):
        raise AssetPathError("relative path escapes its root")
    return normalized


def _safe_scope(value: AssetScope | Mapping[str, Any] | Any, *, namespace_id: str = "") -> AssetScope:
    """Coerce an explicit scope, including a V2MutationContext."""

    if isinstance(value, AssetScope):
        if namespace_id and value.namespace_id != namespace_id and not value.admin:
            raise AssetAuthorizationError("context namespace_id conflicts with target")
        return value
    if hasattr(value, "to_dict") and callable(value.to_dict):
        values = dict(value.to_dict())
    elif isinstance(value, Mapping):
        values = dict(value)
    else:
        raise AssetAuthorizationError("explicit asset context is required")
    if namespace_id and not values.get("namespace_id"):
        values["namespace_id"] = namespace_id
    return AssetScope.from_value(values)


def _scope_readable(scope: AssetScope | None) -> bool:
    if scope is None:
        return False
    fields = (
        scope.namespace_id,
        scope.workspace_id,
        scope.agent_instance_id,
        scope.project_ref,
        scope.provider,
        scope.share_group_id,
        scope.runtime_role,
    )
    return all(value and value != UNKNOWN_ACL for value in fields)


_AUX_TABLES = frozenset(
    {
        "asset_schema_meta",
        "assets",
        "asset_versions",
        "asset_locations",
        "asset_references",
        "asset_holds",
        "asset_tombstones",
        "asset_migration_map",
        "asset_outbox",
        "asset_audit",
        "asset_unknown_ledger",
    }
)


ASSET_AUX_SCHEMA = """
CREATE TABLE IF NOT EXISTS asset_schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assets (
    asset_id TEXT PRIMARY KEY,
    asset_key TEXT NOT NULL,
    asset_kind TEXT NOT NULL,
    namespace_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    agent_instance_id TEXT NOT NULL,
    project_ref TEXT NOT NULL,
    provider TEXT NOT NULL,
    share_group_id TEXT NOT NULL,
    runtime_role TEXT NOT NULL,
    acl_digest TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active'
        CHECK (state IN ('active','tombstoned','deleted','blocked','quarantined')),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(namespace_id, asset_key)
);
CREATE INDEX IF NOT EXISTS idx_assets_acl
    ON assets(namespace_id, workspace_id, agent_instance_id, project_ref,
              provider, share_group_id, runtime_role, state);
CREATE TABLE IF NOT EXISTS asset_versions (
    version_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL,
    version_key TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
    content_hash TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0 CHECK (size_bytes >= 0),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(asset_id, version_key),
    FOREIGN KEY(asset_id) REFERENCES assets(asset_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_asset_versions_hash ON asset_versions(content_hash);
CREATE TABLE IF NOT EXISTS asset_locations (
    location_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL,
    version_id TEXT DEFAULT NULL,
    root_ref TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    content_hash TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0 CHECK (size_bytes >= 0),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(asset_id, version_id, root_ref, relative_path),
    FOREIGN KEY(asset_id) REFERENCES assets(asset_id) ON DELETE CASCADE,
    FOREIGN KEY(version_id) REFERENCES asset_versions(version_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS asset_references (
    reference_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL,
    version_id TEXT DEFAULT NULL,
    reference_kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    target_hash TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(asset_id, version_id, reference_kind, target_id, target_hash),
    FOREIGN KEY(asset_id) REFERENCES assets(asset_id) ON DELETE CASCADE,
    FOREIGN KEY(version_id) REFERENCES asset_versions(version_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS asset_holds (
    hold_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL,
    version_id TEXT DEFAULT NULL,
    reason TEXT NOT NULL,
    source_ref TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    released_at TEXT NOT NULL DEFAULT '',
    UNIQUE(asset_id, version_id, reason, source_ref),
    FOREIGN KEY(asset_id) REFERENCES assets(asset_id) ON DELETE CASCADE,
    FOREIGN KEY(version_id) REFERENCES asset_versions(version_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS asset_tombstones (
    tombstone_id TEXT PRIMARY KEY,
    asset_id TEXT,
    version_id TEXT DEFAULT '',
    reason TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    restored_at TEXT NOT NULL DEFAULT '',
    UNIQUE(asset_id, version_id, reason),
    FOREIGN KEY(asset_id) REFERENCES assets(asset_id) ON DELETE SET NULL,
    FOREIGN KEY(version_id) REFERENCES asset_versions(version_id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS asset_migration_map (
    map_id TEXT PRIMARY KEY,
    source_domain TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    source_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    target_hash TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'mapped'
        CHECK(status IN ('mapped','blocked','conflict')),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_domain, source_ref, source_id)
);
CREATE TABLE IF NOT EXISTS asset_outbox (
    event_id TEXT PRIMARY KEY,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','sent','failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(aggregate_type, aggregate_id, event_type, payload_hash)
);
CREATE TABLE IF NOT EXISTS asset_audit (
    audit_id TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL,
    authority TEXT NOT NULL,
    payload_hash TEXT NOT NULL DEFAULT '',
    context_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(operation, aggregate_type, aggregate_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS asset_unknown_ledger (
    unknown_id TEXT PRIMARY KEY,
    source_domain TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    field TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'BLOCKED'
        CHECK(status IN ('BLOCKED','REVIEWED','RESOLVED')),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(source_domain, source_ref, field, value, reason)
);
CREATE INDEX IF NOT EXISTS idx_asset_holds_active ON asset_holds(asset_id, version_id, active);
CREATE INDEX IF NOT EXISTS idx_asset_tombstones_active ON asset_tombstones(asset_id, version_id, active);
CREATE INDEX IF NOT EXISTS idx_asset_outbox_status ON asset_outbox(status, created_at);
"""


class AssetStore:
    """Metadata-only registry backed by ``WorkspaceV2Layout.assets_db``."""

    def __init__(
        self,
        workspace: str | Path | WorkspaceV2Layout,
        *,
        workspace_id: str | None = None,
        source_workspace: str | Path | None = None,
        readonly: bool = False,
        read_only: bool | None = None,
        initialize: bool = True,
        fault_hook: Callable[[str], Any] | None = None,
        fail_at: str | None = None,
    ) -> None:
        if read_only is not None:
            readonly = bool(read_only)
        if isinstance(workspace, WorkspaceV2Layout):
            # ``WorkspaceV2Layout`` resolves its input path by design.  A
            # caller handing us only the resolved object could therefore
            # conceal a lexical symlink/reparse component.  Require the raw
            # source path and inspect it before accepting the layout.
            if source_workspace is None:
                raise AssetPathError(
                    "source_workspace is required when constructing AssetStore from WorkspaceV2Layout"
                )
            raw_source = Path(source_workspace).expanduser()
            source = _assert_no_reparse(raw_source)
            # Compare both the lexical absolute path and the resolved target.
            # The first comparison prevents a caller from handing us a
            # layout resolved from a symlink while claiming a different raw
            # root; the second permits harmless ``.``/``..`` normalization.
            lexical_layout = Path(os.path.abspath(os.fspath(workspace.workspace)))
            if os.path.normcase(os.fspath(source)) != os.path.normcase(os.fspath(lexical_layout)):
                raise AssetPathError("source_workspace must be the original lexical workspace path")
            if source.resolve(strict=False) != workspace.workspace.resolve(strict=False):
                raise AssetPathError("source_workspace does not match the supplied layout")
            self.source_workspace = source
            self.layout = workspace
        else:
            # Check the lexical path before WorkspaceV2Layout resolves it;
            # otherwise a symlinked workspace would become indistinguishable
            # from its target and writes could escape the requested root.
            source = _assert_no_reparse(Path(workspace))
            if source_workspace is not None:
                supplied = _assert_no_reparse(source_workspace)
                if supplied != source:
                    raise AssetPathError("source_workspace does not match workspace")
            self.source_workspace = source
            self.layout = WorkspaceV2Layout(Path(workspace))
        self.workspace_id = str(workspace_id or self.layout.workspace)
        self.db_path = self.layout.assets_db
        self.readonly = bool(readonly)
        self.fault_hook = fault_hook
        self.fail_at = fail_at
        if not initialize:
            return
        if self.readonly:
            if not self.db_path.is_file():
                raise FileNotFoundError(self.db_path)
            self._preflight_aux()
        else:
            _assert_no_reparse(self.layout.workspace)
            self.layout.ensure_dirs()
            state = self._preflight_aux()
            if state in {"fresh", "needs_aux"}:
                initialize_database(self.db_path, "assets", layout=self.layout)
                self._ensure_aux_schema()

    @property
    def assets_db(self) -> Path:
        return self.db_path

    @property
    def read_only(self) -> bool:
        return self.readonly

    @property
    def schema_marker(self) -> str:
        return SCHEMA_MARKER

    def _preflight_aux(self) -> str:
        if not self.db_path.is_file():
            return "fresh"
        try:
            # This is deliberately read-only and runs before a writable
            # connection.  A future Phase-1 marker therefore cannot be
            # downgraded or mutated by our aux bootstrap.
            initialize_database(self.db_path, "assets", layout=self.layout, readonly=True)
            with open_database(self.db_path, readonly=True) as conn:
                tables = {
                    str(row[0])
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                }
                if "asset_schema_meta" not in tables:
                    partial = sorted((tables & _AUX_TABLES) - {"asset_schema_meta"})
                    if partial:
                        raise AssetSchemaError("asset aux marker is missing; refusing partial schema: " + ",".join(partial))
                    return "needs_aux"
                rows = conn.execute("SELECT key,value FROM asset_schema_meta ORDER BY key").fetchall()
                if len(rows) != 1 or str(rows[0][0]) != "version":
                    raise AssetSchemaError("asset_schema_meta must contain exactly one version row")
                marker = str(rows[0][1])
                if marker != str(SCHEMA_VERSION):
                    direction = "future" if marker.isdigit() and int(marker) > SCHEMA_VERSION else "unsupported"
                    raise AssetSchemaError(f"{direction} asset schema version: {marker!r}")
                missing = sorted(_AUX_TABLES - tables)
                if missing:
                    raise AssetSchemaError("asset schema marker is current but tables are missing: " + ",".join(missing))
            return "current"
        except AssetSchemaError:
            raise
        except (SchemaError, LayoutError, sqlite3.Error, OSError, ValueError) as exc:
            raise AssetSchemaError(f"cannot preflight assets schema: {self.db_path}") from exc

    def _ensure_aux_schema(self) -> None:
        if self.readonly:
            raise AssetAuthorizationError("asset store is read-only")
        with open_database(self.db_path) as conn:
            with transaction(conn):
                execute_sql_script(conn, ASSET_AUX_SCHEMA)
                conn.execute(
                    "INSERT INTO asset_schema_meta(key,value) VALUES('version',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(SCHEMA_VERSION),),
                )

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Open a read-only connection for inspection."""

        if not self.db_path.is_file():
            raise FileNotFoundError(self.db_path)
        with open_database(self.db_path, readonly=True) as conn:
            yield conn

    def _fault(self, step: str) -> None:
        if self.fail_at and self.fail_at == step:
            raise RuntimeError(f"injected asset failure at {step}")
        if self.fault_hook is not None:
            self.fault_hook(step)

    @staticmethod
    def _hash_file(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size

    @staticmethod
    def _row_asset(row: sqlite3.Row | Mapping[str, Any] | None) -> Asset | None:
        if row is None:
            return None
        return Asset(
            asset_id=str(row["asset_id"]),
            asset_key=str(row["asset_key"]),
            asset_kind=str(row["asset_kind"]),
            namespace_id=str(row["namespace_id"]),
            workspace_id=str(row["workspace_id"]),
            agent_instance_id=str(row["agent_instance_id"]),
            project_ref=str(row["project_ref"]),
            provider=str(row["provider"]),
            share_group_id=str(row["share_group_id"]),
            runtime_role=str(row["runtime_role"]),
            state=str(row["state"]),
            metadata=_decode_json(row["metadata_json"]),
            acl_digest=str(row["acl_digest"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _row_version(row: sqlite3.Row | Mapping[str, Any] | None) -> AssetVersion | None:
        if row is None:
            return None
        return AssetVersion(
            version_id=str(row["version_id"] or ""),
            asset_id=str(row["asset_id"]),
            version_key=str(row["version_key"]),
            version=int(row["version"]),
            content_hash=str(row["content_hash"]),
            size_bytes=int(row["size_bytes"]),
            metadata=_decode_json(row["metadata_json"]),
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _row_location(row: sqlite3.Row | Mapping[str, Any] | None) -> AssetLocation | None:
        if row is None:
            return None
        return AssetLocation(
            location_id=str(row["location_id"]),
            asset_id=str(row["asset_id"]),
            version_id=str(row["version_id"]),
            root_ref=str(row["root_ref"]),
            relative_path=str(row["relative_path"]),
            content_hash=str(row["content_hash"]),
            size_bytes=int(row["size_bytes"]),
            metadata=_decode_json(row["metadata_json"]),
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _row_reference(row: sqlite3.Row | Mapping[str, Any] | None) -> AssetReference | None:
        if row is None:
            return None
        return AssetReference(
            reference_id=str(row["reference_id"]),
            asset_id=str(row["asset_id"]),
            version_id=str(row["version_id"]),
            reference_kind=str(row["reference_kind"]),
            target_id=str(row["target_id"]),
            target_hash=str(row["target_hash"]),
            metadata=_decode_json(row["metadata_json"]),
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _row_hold(row: sqlite3.Row | Mapping[str, Any] | None) -> AssetHold | None:
        if row is None:
            return None
        return AssetHold(str(row["hold_id"]), str(row["asset_id"]), str(row["version_id"]), str(row["reason"]), str(row["source_ref"]), bool(row["active"]), str(row["created_at"]), str(row["released_at"]))

    @staticmethod
    def _row_tombstone(row: sqlite3.Row | Mapping[str, Any] | None) -> AssetTombstone | None:
        if row is None:
            return None
        return AssetTombstone(str(row["tombstone_id"]), str(row["asset_id"] or ""), str(row["version_id"] or ""), str(row["reason"]), bool(row["active"]), _decode_json(row["metadata_json"]), str(row["created_at"]), str(row["restored_at"]))

    @staticmethod
    def _row_map(row: sqlite3.Row | Mapping[str, Any] | None) -> AssetMigrationMap | None:
        if row is None:
            return None
        return AssetMigrationMap(str(row["map_id"]), str(row["source_domain"]), str(row["source_ref"]), str(row["source_id"]), str(row["target_type"]), str(row["target_id"]), str(row["target_hash"]), str(row["status"]), _decode_json(row["metadata_json"]), str(row["created_at"]), str(row["updated_at"]))

    @staticmethod
    def _row_outbox(row: sqlite3.Row | Mapping[str, Any] | None) -> AssetOutboxEvent | None:
        if row is None:
            return None
        return AssetOutboxEvent(str(row["event_id"]), str(row["aggregate_type"]), str(row["aggregate_id"]), str(row["event_type"]), str(row["payload_hash"]), _decode_json(row["payload_json"]), str(row["status"]), int(row["attempts"]), str(row["created_at"]), str(row["updated_at"]))

    @staticmethod
    def _row_unknown(row: sqlite3.Row | Mapping[str, Any] | None) -> AssetUnknownLedgerEntry | None:
        if row is None:
            return None
        return AssetUnknownLedgerEntry(str(row["unknown_id"]), str(row["source_domain"]), str(row["source_ref"]), str(row["field"]), str(row["value"]), str(row["reason"]), str(row["status"]), _decode_json(row["metadata_json"]), str(row["created_at"]))

    @staticmethod
    def _acl_values(
        *,
        namespace_id: str,
        workspace_id: str,
        agent_instance_id: str,
        project_ref: str,
        provider: str,
        share_group_id: str,
        runtime_role: str,
    ) -> tuple[str, ...]:
        return tuple(str(value or "") for value in (namespace_id, workspace_id, agent_instance_id, project_ref, provider, share_group_id, runtime_role))

    def _mutation_scope(
        self,
        context: Any,
        *,
        namespace_id: str = "",
    ) -> AssetScope:
        if context is None:
            raise AssetAuthorizationError("explicit V2 asset mutation context required")
        scope = _safe_scope(context, namespace_id=namespace_id)
        if os.path.abspath(os.fspath(Path(scope.workspace_id).expanduser())) != os.path.abspath(os.fspath(self.layout.workspace)):
            raise AssetAuthorizationError("asset context workspace is outside this store")
        if not scope.actor and not scope.admin and scope.authority != "migration":
            raise AssetAuthorizationError("asset mutation actor is required")
        return scope

    @staticmethod
    def _authorize_scope(
        scope: AssetScope,
        *,
        namespace_id: str,
        workspace_id: str,
        agent_instance_id: str,
        project_ref: str,
        provider: str,
        share_group_id: str,
        runtime_role: str,
    ) -> None:
        target = (str(namespace_id), str(workspace_id), str(agent_instance_id), str(project_ref), str(provider), str(share_group_id), str(runtime_role))
        allowed = (scope.namespace_id, scope.workspace_id, scope.agent_instance_id, scope.project_ref, scope.provider, scope.share_group_id, scope.runtime_role)
        if scope.admin:
            if target[1] != scope.workspace_id:
                raise AssetAuthorizationError("admin context workspace mismatch")
            return
        if target != allowed:
            raise AssetAuthorizationError("asset mutation scope is outside context")

    def _target_asset(self, conn: sqlite3.Connection, asset_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM assets WHERE asset_id=?", (str(asset_id),)).fetchone()
        if row is None:
            raise KeyError(f"unknown asset: {asset_id}")
        return row

    @staticmethod
    def _target_version(conn: sqlite3.Connection, version_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT v.*,a.namespace_id,a.workspace_id,a.agent_instance_id,a.project_ref,a.provider,a.share_group_id,a.runtime_role FROM asset_versions v JOIN assets a ON a.asset_id=v.asset_id WHERE v.version_id=?", (str(version_id),)).fetchone()
        if row is None:
            raise KeyError(f"unknown asset version: {version_id}")
        return row

    def _record_unknown(
        self,
        conn: sqlite3.Connection,
        *,
        source_domain: str,
        source_ref: str,
        field: str,
        value: str = "",
        reason: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        metadata_value = _validate_metadata(metadata)
        unknown_id = stable_id("asset-unknown", source_domain, source_ref, field, value, reason)
        conn.execute(
            "INSERT INTO asset_unknown_ledger(unknown_id,source_domain,source_ref,field,value,reason,status,metadata_json,created_at) VALUES(?,?,?,?,? ,?,'BLOCKED',?,?) ON CONFLICT(source_domain,source_ref,field,value,reason) DO NOTHING",
            (unknown_id, str(source_domain), str(source_ref), str(field), str(value), str(reason), _json(metadata_value), _now()),
        )
        return unknown_id

    def _queue_outbox(
        self,
        conn: sqlite3.Connection,
        *,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> AssetOutboxEvent:
        safe_payload = _validate_event_payload(payload)
        payload_hash = _digest(safe_payload)
        event_id = stable_id("asset-event", aggregate_type, aggregate_id, event_type, payload_hash)
        now = _now()
        conn.execute(
            "INSERT INTO asset_outbox(event_id,aggregate_type,aggregate_id,event_type,payload_hash,payload_json,status,attempts,created_at,updated_at) VALUES(?,?,?,?,?,?, 'pending',0,?,?) ON CONFLICT(event_id) DO NOTHING",
            (event_id, str(aggregate_type), str(aggregate_id), str(event_type), payload_hash, _json(safe_payload), now, now),
        )
        row = conn.execute("SELECT * FROM asset_outbox WHERE event_id=?", (event_id,)).fetchone()
        assert row is not None
        if str(row["payload_hash"]) != payload_hash or _decode_json(row["payload_json"]) != safe_payload:
            raise AssetConflictError(f"outbox event conflict: {event_id}")
        return self._row_outbox(row)  # type: ignore[return-value]

    def _record_audit(
        self,
        conn: sqlite3.Connection,
        *,
        operation: str,
        aggregate_type: str,
        aggregate_id: str,
        scope: AssetScope,
        payload: Mapping[str, Any],
        idempotency_key: str = "",
    ) -> AssetAudit:
        safe_payload = _validate_metadata(payload)
        payload_hash = _digest(safe_payload)
        idem = str(idempotency_key or payload_hash)
        audit_id = stable_id("asset-audit", operation, aggregate_type, aggregate_id, idem)
        now = _now()
        conn.execute(
            "INSERT INTO asset_audit(audit_id,operation,aggregate_type,aggregate_id,idempotency_key,actor,authority,payload_hash,context_json,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(audit_id) DO NOTHING",
            (audit_id, str(operation), str(aggregate_type), str(aggregate_id), idem, scope.actor, scope.authority, payload_hash, _json(scope.to_dict()), _json(safe_payload), now),
        )
        row = conn.execute("SELECT * FROM asset_audit WHERE audit_id=?", (audit_id,)).fetchone()
        if row is None or str(row["payload_hash"]) != payload_hash:
            raise AssetConflictError(f"audit conflict: {audit_id}")
        return AssetAudit(audit_id, str(row["operation"]), str(row["aggregate_type"]), str(row["aggregate_id"]), str(row["idempotency_key"]), str(row["actor"]), str(row["authority"]), str(row["payload_hash"]), _decode_json(row["metadata_json"]), str(row["created_at"]))

    def _run_write(self, conn: sqlite3.Connection | None, apply: Callable[[sqlite3.Connection], Any]) -> Any:
        # The optional connection is used by a caller that already owns an
        # outer transaction (e.g. migration).  It must never bypass the
        # store-level read-only contract.
        if self.readonly:
            raise AssetAuthorizationError("asset store is read-only")
        if conn is not None:
            # Borrow an outer migration transaction when present; otherwise
            # make this connection write atomic instead of silently leaving an
            # uncommitted mutation for the caller to accidentally commit.
            with transaction(conn, reuse_existing=True):
                return apply(conn)
        with open_database(self.db_path) as local:
            with transaction(local):
                return apply(local)

    def register_asset(
        self,
        asset_key: str | None = None,
        *,
        asset_id: str = "",
        asset_kind: str = "generic",
        namespace_id: str = "",
        workspace_id: str = "",
        agent_instance_id: str = "",
        project_ref: str = "",
        provider: str = "",
        share_group_id: str = "",
        runtime_role: str = "",
        state: str = "active",
        metadata: Mapping[str, Any] | None = None,
        context: Any | None = None,
        idempotency_key: str = "",
        conn: sqlite3.Connection | None = None,
        **aliases: Any,
    ) -> Asset:
        """Create/replay one asset identity under an explicit context."""

        if context is None:
            context = aliases.pop("scope", None)
        if isinstance(asset_key, Asset):
            value = asset_key
            asset_key = value.asset_key
            asset_id = asset_id or value.asset_id
            asset_kind = asset_kind if asset_kind != "generic" else value.asset_kind
            namespace_id = namespace_id or value.namespace_id
            workspace_id = workspace_id or value.workspace_id
            agent_instance_id = agent_instance_id or value.agent_instance_id
            project_ref = project_ref or value.project_ref
            provider = provider or value.provider
            share_group_id = share_group_id or value.share_group_id
            runtime_role = runtime_role or value.runtime_role
            state = state if state != "active" else value.state
            metadata = metadata if metadata is not None else value.metadata
        elif isinstance(asset_key, Mapping):
            value = dict(asset_key)
            asset_key = value.get("asset_key") or value.get("stable_key") or value.get("external_key") or value.get("asset_id")
            asset_id = asset_id or str(value.get("asset_id") or "")
            asset_kind = asset_kind if asset_kind != "generic" else str(value.get("asset_kind") or "generic")
            namespace_id = namespace_id or str(value.get("namespace_id") or value.get("namespace") or "")
            workspace_id = workspace_id or str(value.get("workspace_id") or value.get("workspace") or "")
            agent_instance_id = agent_instance_id or str(value.get("agent_instance_id") or value.get("agent") or "")
            project_ref = project_ref or str(value.get("project_ref") or value.get("project") or "")
            provider = provider or str(value.get("provider") or "")
            share_group_id = share_group_id or str(value.get("share_group_id") or value.get("group_id") or "")
            runtime_role = runtime_role or str(value.get("runtime_role") or value.get("runtime") or "")
            state = state if state != "active" else str(value.get("state") or value.get("status") or "active")
            metadata = metadata if metadata is not None else value.get("metadata")
        if asset_key is None:
            asset_key = aliases.pop("stable_key", None) or aliases.pop("external_key", None) or asset_id
        if not asset_key:
            raise ValueError("asset_key is required")
        namespace_id = namespace_id or str(aliases.pop("namespace", "") or "")
        workspace_id = workspace_id or str(aliases.pop("workspace", "") or "")
        agent_instance_id = agent_instance_id or str(aliases.pop("agent", "") or "")
        project_ref = project_ref or str(aliases.pop("project", "") or "")
        share_group_id = share_group_id or str(aliases.pop("group_id", "") or "")
        runtime_role = runtime_role or str(aliases.pop("runtime", "") or "")
        acl = aliases.pop("acl", None)
        if isinstance(acl, Mapping):
            namespace_id = namespace_id or str(acl.get("namespace_id") or acl.get("namespace") or "")
            workspace_id = workspace_id or str(acl.get("workspace_id") or acl.get("workspace") or "")
            agent_instance_id = agent_instance_id or str(acl.get("agent_instance_id") or acl.get("agent") or "")
            project_ref = project_ref or str(acl.get("project_ref") or acl.get("project") or "")
            provider = provider or str(acl.get("provider") or "")
            share_group_id = share_group_id or str(acl.get("share_group_id") or acl.get("group_id") or "")
            runtime_role = runtime_role or str(acl.get("runtime_role") or acl.get("runtime") or "")
        scope = self._mutation_scope(context, namespace_id=namespace_id)
        namespace_id = str(namespace_id or scope.namespace_id)
        workspace_id = str(workspace_id or scope.workspace_id)
        agent_instance_id = str(agent_instance_id or scope.agent_instance_id)
        project_ref = str(project_ref or scope.project_ref)
        provider = str(provider or scope.provider)
        share_group_id = str(share_group_id or scope.share_group_id)
        runtime_role = str(runtime_role or scope.runtime_role)
        self._authorize_scope(scope, namespace_id=namespace_id, workspace_id=workspace_id, agent_instance_id=agent_instance_id, project_ref=project_ref, provider=provider, share_group_id=share_group_id, runtime_role=runtime_role)
        if state not in {"active", "tombstoned", "deleted", "blocked", "quarantined"}:
            raise ValueError(f"unsupported asset state: {state!r}")
        if not asset_kind:
            raise ValueError("asset_kind is required")
        # ``path``/``source_path`` are accepted only as metadata references;
        # callers that provide a real file get its hash/size, never its bytes.
        source_path = aliases.pop("source_path", None) or aliases.pop("path", None)
        metadata_input = dict(metadata or {})
        if source_path is not None:
            path_value = _assert_no_reparse(source_path)
            try:
                rel = path_value.resolve(strict=False).relative_to(self.layout.workspace.resolve(strict=False)).as_posix()
            except (ValueError, OSError) as exc:
                raise AssetPathError("asset source path escapes workspace") from exc
            metadata_input.setdefault("relative_path", normalize_relative_path(rel))
            if path_value.is_file():
                digest_value, size_value = self._hash_file(path_value)
                metadata_input.setdefault("content_hash", digest_value)
                metadata_input.setdefault("size_bytes", size_value)
        if aliases.get("content_hash") is not None or aliases.get("digest") is not None:
            metadata_input.setdefault("content_hash", str(aliases.get("content_hash") or aliases.get("digest")))
        if aliases.get("size_bytes") is not None:
            metadata_input.setdefault("size_bytes", int(aliases["size_bytes"]))
        metadata_value = _validate_metadata(metadata_input)
        asset_key = str(asset_key)
        asset_id = str(asset_id or stable_id("asset", namespace_id, asset_key))
        acl = self._acl_values(namespace_id=namespace_id, workspace_id=workspace_id, agent_instance_id=agent_instance_id, project_ref=project_ref, provider=provider, share_group_id=share_group_id, runtime_role=runtime_role)
        acl_hash = _digest({"namespace_id": acl[0], "workspace_id": acl[1], "agent_instance_id": acl[2], "project_ref": acl[3], "provider": acl[4], "share_group_id": acl[5], "runtime_role": acl[6]})
        payload = {"asset_id": asset_id, "asset_key": asset_key, "asset_kind": str(asset_kind), "namespace_id": namespace_id, "workspace_id": workspace_id, "agent_instance_id": agent_instance_id, "project_ref": project_ref, "provider": provider, "share_group_id": share_group_id, "runtime_role": runtime_role, "state": state, "metadata": metadata_value, "acl_digest": acl_hash}

        def apply(local: sqlite3.Connection) -> Asset:
            self._fault("asset.before")
            existing = local.execute("SELECT * FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
            key_existing = local.execute("SELECT * FROM assets WHERE namespace_id=? AND asset_key=?", (namespace_id, asset_key)).fetchone()
            if key_existing is not None and str(key_existing["asset_id"]) != asset_id:
                raise AssetConflictError(f"asset key conflict: {namespace_id}:{asset_key}")
            if existing is not None:
                current = self._row_asset(existing)
                assert current is not None
                current_payload = {"asset_id": current.asset_id, "asset_key": current.asset_key, "asset_kind": current.asset_kind, "namespace_id": current.namespace_id, "workspace_id": current.workspace_id, "agent_instance_id": current.agent_instance_id, "project_ref": current.project_ref, "provider": current.provider, "share_group_id": current.share_group_id, "runtime_role": current.runtime_role, "state": current.state, "metadata": dict(current.metadata), "acl_digest": current.acl_digest}
                if current_payload != payload:
                    raise AssetConflictError(f"asset replay conflicts with immutable payload: {asset_id}")
                return current
            now = _now()
            local.execute("INSERT INTO assets(asset_id,asset_key,asset_kind,namespace_id,workspace_id,agent_instance_id,project_ref,provider,share_group_id,runtime_role,acl_digest,state,metadata_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (asset_id, asset_key, str(asset_kind), namespace_id, workspace_id, agent_instance_id, project_ref, provider, share_group_id, runtime_role, acl_hash, state, _json(metadata_value), now, now))
            # Keep the Phase-1 compatibility index populated with metadata,
            # not file bytes.  The registry tables above remain authoritative.
            registry_digest = str(metadata_value.get("content_hash") or metadata_value.get("digest") or acl_hash)
            registry_path = str(metadata_value.get("relative_path") or metadata_value.get("path") or asset_key)
            local.execute("INSERT INTO asset_registry(asset_id,asset_kind,path,digest,media_type,size_bytes,state,metadata_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(asset_id) DO NOTHING", (asset_id, str(asset_kind), registry_path, registry_digest, str(metadata_value.get("media_type") or ""), int(metadata_value.get("size_bytes") or 0), state if state in {"active", "missing", "quarantined", "deleted"} else "active", _json(metadata_value), now, now))
            for field, value in zip(("namespace_id", "workspace_id", "agent_instance_id", "project_ref", "provider", "share_group_id", "runtime_role"), acl):
                if not value or value == UNKNOWN_ACL:
                    self._record_unknown(local, source_domain="assets", source_ref=asset_id, field=field, value=value, reason="unknown_acl")
            self._queue_outbox(local, aggregate_type="asset", aggregate_id=asset_id, event_type="asset.created", payload={"asset_id": asset_id, "asset_kind": str(asset_kind), "content_hash": registry_digest, "acl_digest": acl_hash})
            # ACL material is model-level data (``Asset.acl_digest``), not
            # audit metadata.  Keep the audit row limited to non-ACL facts.
            self._record_audit(local, operation="asset.create", aggregate_type="asset", aggregate_id=asset_id, scope=scope, payload={"asset_id": asset_id, "asset_key": asset_key}, idempotency_key=idempotency_key)
            self._fault("asset.after")
            row = local.execute("SELECT * FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
            assert row is not None
            return self._row_asset(row)  # type: ignore[return-value]

        return self._run_write(conn, apply)

    create_asset = register_asset
    put_asset = register_asset
    register = register_asset
    put = register_asset

    def register_version(
        self,
        asset_id: str,
        version_key: str | int | None = None,
        *,
        version: int | str | None = None,
        version_id: str = "",
        content_hash: str | None = None,
        digest: str | None = None,
        size_bytes: int = 0,
        source_path: str | Path | None = None,
        metadata: Mapping[str, Any] | None = None,
        context: Any | None = None,
        idempotency_key: str = "",
        conn: sqlite3.Connection | None = None,
        **aliases: Any,
    ) -> AssetVersion:
        if context is None:
            raise AssetAuthorizationError("explicit V2 asset mutation context required")
        if aliases.get("body") is not None or aliases.get("content") is not None or aliases.get("text") is not None:
            raise ValueError("asset versions store hashes only; body/content/text is forbidden")
        if content_hash is None:
            content_hash = digest or aliases.get("hash") or ""
        if source_path is not None and not content_hash:
            path = _assert_no_reparse(source_path)
            if not path.is_file():
                raise FileNotFoundError(path)
            h = hashlib.sha256()
            total = 0
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    h.update(chunk)
                    total += len(chunk)
            content_hash = h.hexdigest()
            size_bytes = total
        content_hash = str(content_hash or "")
        if int(size_bytes) < 0:
            raise ValueError("size_bytes must be >= 0")
        key = str(version_key if version_key is not None else (version if version is not None else "1"))
        try:
            version_number = int(version if version is not None else key)
        except (TypeError, ValueError):
            version_number = 0
        metadata_value = _validate_metadata(metadata)

        def apply(local: sqlite3.Connection) -> AssetVersion:
            asset_row = self._target_asset(local, asset_id)
            scope = self._mutation_scope(context, namespace_id=str(asset_row["namespace_id"]))
            self._authorize_scope(scope, namespace_id=str(asset_row["namespace_id"]), workspace_id=str(asset_row["workspace_id"]), agent_instance_id=str(asset_row["agent_instance_id"]), project_ref=str(asset_row["project_ref"]), provider=str(asset_row["provider"]), share_group_id=str(asset_row["share_group_id"]), runtime_role=str(asset_row["runtime_role"]))
            vid = str(version_id or stable_id("asset-version", asset_id, key))
            existing = local.execute("SELECT * FROM asset_versions WHERE version_id=?", (vid,)).fetchone()
            key_existing = local.execute("SELECT * FROM asset_versions WHERE asset_id=? AND version_key=?", (asset_id, key)).fetchone()
            payload = (asset_id, key, version_number, content_hash, int(size_bytes), metadata_value)
            if key_existing is not None and str(key_existing["version_id"]) != vid:
                raise AssetConflictError(f"asset version key conflict: {asset_id}:{key}")
            if existing is not None:
                current = self._row_version(existing)
                assert current is not None
                if (current.asset_id, current.version_key, current.version, current.content_hash, current.size_bytes, dict(current.metadata)) != payload:
                    raise AssetConflictError(f"asset version replay conflicts: {vid}")
                return current
            now = _now()
            local.execute("INSERT INTO asset_versions(version_id,asset_id,version_key,version,content_hash,size_bytes,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?)", (vid, asset_id, key, version_number, content_hash, int(size_bytes), _json(metadata_value), now))
            self._queue_outbox(local, aggregate_type="asset", aggregate_id=asset_id, event_type="asset.version.created", payload={"asset_id": asset_id, "version_id": vid, "content_hash": content_hash, "size_bytes": int(size_bytes)})
            self._record_audit(local, operation="asset.version.create", aggregate_type="version", aggregate_id=vid, scope=scope, payload={"asset_id": asset_id, "version_id": vid, "content_hash": content_hash}, idempotency_key=idempotency_key)
            row = local.execute("SELECT * FROM asset_versions WHERE version_id=?", (vid,)).fetchone()
            assert row is not None
            return self._row_version(row)  # type: ignore[return-value]

        return self._run_write(conn, apply)

    create_version = register_version
    put_version = register_version

    def register_location(
        self,
        asset_id: str,
        relative_path: str | Path | None = None,
        *,
        version_id: str = "",
        root: str | Path | None = None,
        root_ref: str | None = None,
        path: str | Path | None = None,
        content_hash: str = "",
        digest: str | None = None,
        size_bytes: int = 0,
        metadata: Mapping[str, Any] | None = None,
        location_id: str = "",
        context: Any | None = None,
        idempotency_key: str = "",
        conn: sqlite3.Connection | None = None,
    ) -> AssetLocation:
        if context is None:
            raise AssetAuthorizationError("explicit V2 asset mutation context required")
        if path is not None:
            path_value = _assert_no_reparse(path)
            root_path = _assert_no_reparse(root or self.layout.workspace)
            try:
                derived = path_value.resolve(strict=False).relative_to(root_path.resolve(strict=False)).as_posix()
            except (ValueError, OSError) as exc:
                raise AssetPathError("location path escapes declared root") from exc
            if relative_path is None:
                relative_path = derived
            if path_value.is_file() and not content_hash:
                h = hashlib.sha256()
                total = 0
                with path_value.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        h.update(chunk)
                        total += len(chunk)
                content_hash = h.hexdigest()
                size_bytes = total
        if relative_path is None:
            raise AssetPathError("relative_path is required")
        rel = normalize_relative_path(relative_path)
        if int(size_bytes) < 0:
            raise ValueError("size_bytes must be >= 0")
        metadata_value = _validate_metadata(metadata)
        content_hash = str(content_hash or digest or "")
        supplied_root_ref = str(root_ref or "")
        if supplied_root_ref:
            if "\x00" in supplied_root_ref or Path(supplied_root_ref).is_absolute() or ntpath.isabs(supplied_root_ref):
                raise AssetPathError("root_ref must be a stable relative reference")
            stable_root = unicodedata.normalize("NFC", supplied_root_ref).replace("\\", "/")
            if stable_root != ".":
                stable_root = normalize_relative_path(stable_root)
        else:
            root_path = _assert_no_reparse(root or self.layout.workspace)
            try:
                stable_root = root_path.resolve(strict=False).relative_to(self.layout.workspace.resolve(strict=False)).as_posix() or "."
            except (ValueError, OSError) as exc:
                raise AssetPathError("location root escapes workspace") from exc

        def apply(local: sqlite3.Connection) -> AssetLocation:
            asset_row = self._target_asset(local, asset_id)
            scope = self._mutation_scope(context, namespace_id=str(asset_row["namespace_id"]))
            self._authorize_scope(scope, namespace_id=str(asset_row["namespace_id"]), workspace_id=str(asset_row["workspace_id"]), agent_instance_id=str(asset_row["agent_instance_id"]), project_ref=str(asset_row["project_ref"]), provider=str(asset_row["provider"]), share_group_id=str(asset_row["share_group_id"]), runtime_role=str(asset_row["runtime_role"]))
            if version_id:
                version_row = self._target_version(local, version_id)
                if str(version_row["asset_id"]) != asset_id:
                    raise AssetConflictError("version does not belong to asset")
            lid = str(location_id or stable_id("asset-location", asset_id, version_id, stable_root, rel))
            existing = local.execute("SELECT * FROM asset_locations WHERE location_id=?", (lid,)).fetchone()
            key_existing = local.execute("SELECT * FROM asset_locations WHERE asset_id=? AND version_id=? AND root_ref=? AND relative_path=?", (asset_id, version_id, stable_root, rel)).fetchone()
            payload = (asset_id, version_id, stable_root, rel, content_hash, int(size_bytes), metadata_value)
            if key_existing is not None and str(key_existing["location_id"]) != lid:
                raise AssetConflictError(f"asset location key conflict: {asset_id}:{rel}")
            if existing is not None:
                current = self._row_location(existing)
                assert current is not None
                if (current.asset_id, current.version_id, current.root_ref, current.relative_path, current.content_hash, current.size_bytes, dict(current.metadata)) != payload:
                    raise AssetConflictError(f"asset location replay conflicts: {lid}")
                return current
            now = _now()
            local.execute("INSERT INTO asset_locations(location_id,asset_id,version_id,root_ref,relative_path,content_hash,size_bytes,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (lid, asset_id, version_id or None, stable_root, rel, content_hash, int(size_bytes), _json(metadata_value), now))
            self._queue_outbox(local, aggregate_type="asset", aggregate_id=asset_id, event_type="asset.location.created", payload={"asset_id": asset_id, "version_id": version_id, "location_id": lid, "relative_path": rel, "content_hash": content_hash})
            self._record_audit(local, operation="asset.location.create", aggregate_type="location", aggregate_id=lid, scope=scope, payload={"asset_id": asset_id, "location_id": lid, "relative_path": rel}, idempotency_key=idempotency_key)
            row = local.execute("SELECT * FROM asset_locations WHERE location_id=?", (lid,)).fetchone()
            assert row is not None
            return self._row_location(row)  # type: ignore[return-value]

        return self._run_write(conn, apply)

    create_location = register_location
    put_location = register_location
    add_location = register_location

    def register_reference(
        self,
        asset_id: str,
        reference_kind: str,
        target_id: str,
        *,
        version_id: str = "",
        target_hash: str = "",
        metadata: Mapping[str, Any] | None = None,
        reference_id: str = "",
        context: Any | None = None,
        idempotency_key: str = "",
        conn: sqlite3.Connection | None = None,
    ) -> AssetReference:
        if context is None:
            raise AssetAuthorizationError("explicit V2 asset mutation context required")
        if not reference_kind or not target_id:
            raise ValueError("reference_kind and target_id are required")
        metadata_value = _validate_metadata(metadata)

        def apply(local: sqlite3.Connection) -> AssetReference:
            asset_row = self._target_asset(local, asset_id)
            scope = self._mutation_scope(context, namespace_id=str(asset_row["namespace_id"]))
            self._authorize_scope(scope, namespace_id=str(asset_row["namespace_id"]), workspace_id=str(asset_row["workspace_id"]), agent_instance_id=str(asset_row["agent_instance_id"]), project_ref=str(asset_row["project_ref"]), provider=str(asset_row["provider"]), share_group_id=str(asset_row["share_group_id"]), runtime_role=str(asset_row["runtime_role"]))
            if version_id and str(self._target_version(local, version_id)["asset_id"]) != asset_id:
                raise AssetConflictError("version does not belong to asset")
            rid = str(reference_id or stable_id("asset-reference", asset_id, version_id, reference_kind, target_id, target_hash))
            existing = local.execute("SELECT * FROM asset_references WHERE reference_id=?", (rid,)).fetchone()
            key_existing = local.execute("SELECT * FROM asset_references WHERE asset_id=? AND version_id=? AND reference_kind=? AND target_id=? AND target_hash=?", (asset_id, version_id, str(reference_kind), str(target_id), str(target_hash))).fetchone()
            payload = (asset_id, version_id, str(reference_kind), str(target_id), str(target_hash), metadata_value)
            if key_existing is not None and str(key_existing["reference_id"]) != rid:
                raise AssetConflictError(f"asset reference key conflict: {rid}")
            if existing is not None:
                current = self._row_reference(existing)
                assert current is not None
                if (current.asset_id, current.version_id, current.reference_kind, current.target_id, current.target_hash, dict(current.metadata)) != payload:
                    raise AssetConflictError(f"asset reference replay conflicts: {rid}")
                return current
            now = _now()
            local.execute("INSERT INTO asset_references(reference_id,asset_id,version_id,reference_kind,target_id,target_hash,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?)", (rid, asset_id, version_id or None, str(reference_kind), str(target_id), str(target_hash), _json(metadata_value), now))
            self._queue_outbox(local, aggregate_type="asset", aggregate_id=asset_id, event_type="asset.reference.created", payload={"asset_id": asset_id, "version_id": version_id, "reference_id": rid, "target_id": str(target_id), "target_hash": str(target_hash)})
            self._record_audit(local, operation="asset.reference.create", aggregate_type="reference", aggregate_id=rid, scope=scope, payload={"asset_id": asset_id, "reference_id": rid}, idempotency_key=idempotency_key)
            row = local.execute("SELECT * FROM asset_references WHERE reference_id=?", (rid,)).fetchone()
            assert row is not None
            return self._row_reference(row)  # type: ignore[return-value]

        return self._run_write(conn, apply)

    add_reference = register_reference
    put_reference = register_reference
    create_reference = register_reference

    def add_hold(
        self,
        asset_id: str,
        *,
        version_id: str = "",
        reason: str,
        source_ref: str = "",
        context: Any | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> AssetHold:
        if context is None:
            raise AssetAuthorizationError("explicit V2 asset mutation context required")
        if not reason:
            raise ValueError("hold reason is required")

        def apply(local: sqlite3.Connection) -> AssetHold:
            asset_row = self._target_asset(local, asset_id)
            scope = self._mutation_scope(context, namespace_id=str(asset_row["namespace_id"]))
            self._authorize_scope(scope, namespace_id=str(asset_row["namespace_id"]), workspace_id=str(asset_row["workspace_id"]), agent_instance_id=str(asset_row["agent_instance_id"]), project_ref=str(asset_row["project_ref"]), provider=str(asset_row["provider"]), share_group_id=str(asset_row["share_group_id"]), runtime_role=str(asset_row["runtime_role"]))
            if version_id and str(self._target_version(local, version_id)["asset_id"]) != asset_id:
                raise AssetConflictError("version does not belong to asset")
            hid = stable_id("asset-hold", asset_id, version_id, reason, source_ref)
            now = _now()
            local.execute("INSERT INTO asset_holds(hold_id,asset_id,version_id,reason,source_ref,active,created_at,released_at) VALUES(?,?,?,?,?,1,?,'') ON CONFLICT(asset_id,version_id,reason,source_ref) DO UPDATE SET active=1,released_at=''", (hid, asset_id, version_id or None, str(reason), str(source_ref), now))
            row = local.execute("SELECT * FROM asset_holds WHERE hold_id=?", (hid,)).fetchone()
            assert row is not None
            self._queue_outbox(local, aggregate_type="asset", aggregate_id=asset_id, event_type="asset.hold.created", payload={"asset_id": asset_id, "version_id": version_id, "hold_id": hid, "reason": str(reason), "source_ref": str(source_ref)})
            self._record_audit(local, operation="asset.hold.create", aggregate_type="hold", aggregate_id=hid, scope=scope, payload={"asset_id": asset_id, "version_id": version_id, "reason": str(reason)})
            return self._row_hold(row)  # type: ignore[return-value]

        return self._run_write(conn, apply)

    hold = add_hold
    hold_asset = add_hold
    create_hold = add_hold

    def release_hold(self, hold_id: str, *, context: Any | None = None, conn: sqlite3.Connection | None = None) -> int:
        if context is None:
            raise AssetAuthorizationError("explicit V2 asset mutation context required")
        def apply(local: sqlite3.Connection) -> int:
            row = local.execute("SELECT h.*,a.namespace_id,a.workspace_id,a.agent_instance_id,a.project_ref,a.provider,a.share_group_id,a.runtime_role FROM asset_holds h JOIN assets a ON a.asset_id=h.asset_id WHERE h.hold_id=?", (str(hold_id),)).fetchone()
            if row is None:
                return 0
            scope = self._mutation_scope(context, namespace_id=str(row["namespace_id"]))
            self._authorize_scope(scope, namespace_id=str(row["namespace_id"]), workspace_id=str(row["workspace_id"]), agent_instance_id=str(row["agent_instance_id"]), project_ref=str(row["project_ref"]), provider=str(row["provider"]), share_group_id=str(row["share_group_id"]), runtime_role=str(row["runtime_role"]))
            now = _now()
            cur = local.execute("UPDATE asset_holds SET active=0,released_at=? WHERE hold_id=? AND active=1", (now, str(hold_id)))
            if cur.rowcount:
                self._queue_outbox(local, aggregate_type="asset", aggregate_id=str(row["asset_id"]), event_type="asset.hold.released", payload={"asset_id": str(row["asset_id"]), "hold_id": str(hold_id)})
                self._record_audit(
                    local,
                    operation="asset.hold.release",
                    aggregate_type="hold",
                    aggregate_id=str(hold_id),
                    scope=scope,
                    payload={"asset_id": str(row["asset_id"]), "hold_id": str(hold_id)},
                )
            return int(cur.rowcount)

        return self._run_write(conn, apply)

    def tombstone(
        self,
        asset_id: str,
        *,
        version_id: str = "",
        reason: str = "deleted",
        metadata: Mapping[str, Any] | None = None,
        context: Any | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> AssetTombstone:
        if context is None:
            raise AssetAuthorizationError("explicit V2 asset mutation context required")
        metadata_value = _validate_metadata(metadata)

        def apply(local: sqlite3.Connection) -> AssetTombstone:
            asset_row = self._target_asset(local, asset_id)
            scope = self._mutation_scope(context, namespace_id=str(asset_row["namespace_id"]))
            self._authorize_scope(scope, namespace_id=str(asset_row["namespace_id"]), workspace_id=str(asset_row["workspace_id"]), agent_instance_id=str(asset_row["agent_instance_id"]), project_ref=str(asset_row["project_ref"]), provider=str(asset_row["provider"]), share_group_id=str(asset_row["share_group_id"]), runtime_role=str(asset_row["runtime_role"]))
            if version_id and str(self._target_version(local, version_id)["asset_id"]) != asset_id:
                raise AssetConflictError("version does not belong to asset")
            tomb_id = stable_id("asset-tombstone", asset_id, version_id, reason)
            now = _now()
            existing = local.execute("SELECT * FROM asset_tombstones WHERE tombstone_id=?", (tomb_id,)).fetchone()
            if existing is not None:
                current = self._row_tombstone(existing)
                assert current is not None
                if dict(current.metadata) != metadata_value or current.reason != str(reason):
                    raise AssetConflictError(f"asset tombstone replay conflicts: {tomb_id}")
                return current
            local.execute("INSERT INTO asset_tombstones(tombstone_id,asset_id,version_id,reason,metadata_json,active,created_at,restored_at) VALUES(?,?,?,?,?,1,?, '')", (tomb_id, asset_id, version_id or None, str(reason), _json(metadata_value), now))
            if version_id:
                # Version rows are immutable; tombstone visibility is carried
                # by the parent asset plus the durable tombstone/hold.
                pass
            else:
                local.execute("UPDATE assets SET state='tombstoned',updated_at=? WHERE asset_id=?", (now, asset_id))
            self.add_hold(asset_id, version_id=version_id, reason=f"tombstone:{reason}", source_ref=tomb_id, context=scope, conn=local)
            self._queue_outbox(local, aggregate_type="asset", aggregate_id=asset_id, event_type="asset.tombstoned", payload={"asset_id": asset_id, "version_id": version_id, "tombstone_id": tomb_id, "reason": str(reason)})
            self._record_audit(local, operation="asset.tombstone", aggregate_type="tombstone", aggregate_id=tomb_id, scope=scope, payload={"asset_id": asset_id, "version_id": version_id, "reason": str(reason)})
            row = local.execute("SELECT * FROM asset_tombstones WHERE tombstone_id=?", (tomb_id,)).fetchone()
            assert row is not None
            return self._row_tombstone(row)  # type: ignore[return-value]

        return self._run_write(conn, apply)

    tombstone_asset = tombstone
    delete_asset = tombstone
    delete = tombstone

    def restore(self, asset_id: str, *, version_id: str = "", context: Any | None = None, conn: sqlite3.Connection | None = None) -> int:
        if context is None:
            raise AssetAuthorizationError("explicit V2 asset mutation context required")
        def apply(local: sqlite3.Connection) -> int:
            asset_row = self._target_asset(local, asset_id)
            scope = self._mutation_scope(context, namespace_id=str(asset_row["namespace_id"]))
            self._authorize_scope(scope, namespace_id=str(asset_row["namespace_id"]), workspace_id=str(asset_row["workspace_id"]), agent_instance_id=str(asset_row["agent_instance_id"]), project_ref=str(asset_row["project_ref"]), provider=str(asset_row["provider"]), share_group_id=str(asset_row["share_group_id"]), runtime_role=str(asset_row["runtime_role"]))
            now = _now()
            cur = local.execute("UPDATE asset_tombstones SET active=0,restored_at=? WHERE asset_id=? AND COALESCE(version_id,'')=? AND active=1", (now, asset_id, version_id))
            if not version_id:
                state_cur = local.execute("UPDATE assets SET state='active',updated_at=? WHERE asset_id=? AND state<>'active'", (now, asset_id))
            else:
                state_cur = None
            hold_cur = local.execute("UPDATE asset_holds SET active=0,released_at=? WHERE asset_id=? AND COALESCE(version_id,'')=? AND reason LIKE 'tombstone:%' AND active=1", (now, asset_id, version_id))
            changed = int(cur.rowcount) + int(state_cur.rowcount if state_cur is not None else 0) + int(hold_cur.rowcount)
            if changed:
                event_payload = {"asset_id": asset_id, "version_id": version_id, "restored": changed}
                self._queue_outbox(local, aggregate_type="asset", aggregate_id=asset_id, event_type="asset.restored", payload=event_payload)
                self._record_audit(
                    local,
                    operation="asset.restore",
                    aggregate_type="asset",
                    aggregate_id=asset_id,
                    scope=scope,
                    payload=event_payload,
                )
            return changed

        return self._run_write(conn, apply)

    restore_asset = restore

    def record_migration_map(
        self,
        source_domain: str,
        source_ref: str,
        source_id: str,
        target_type: str,
        target_id: str,
        *,
        target_hash: str = "",
        status: str = "mapped",
        metadata: Mapping[str, Any] | None = None,
        context: Any | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> AssetMigrationMap:
        if context is None:
            raise AssetAuthorizationError("explicit V2 asset mutation context required")
        if status not in {"mapped", "blocked", "conflict"}:
            raise ValueError("invalid migration map status")
        metadata_value = _validate_metadata(metadata)

        def apply(local: sqlite3.Connection) -> AssetMigrationMap:
            # Migration maps do not carry body data.  A migration/admin scope
            # is still required so a random public write cannot impersonate a
            # source authority.
            scope = self._mutation_scope(context, namespace_id=str((metadata_value.get("namespace_id") or "migration")))
            mid = stable_id("asset-map", source_domain, source_ref, source_id)
            existing = local.execute("SELECT * FROM asset_migration_map WHERE map_id=?", (mid,)).fetchone()
            key_existing = local.execute("SELECT * FROM asset_migration_map WHERE source_domain=? AND source_ref=? AND source_id=?", (str(source_domain), str(source_ref), str(source_id))).fetchone()
            payload = (str(source_domain), str(source_ref), str(source_id), str(target_type), str(target_id), str(target_hash), str(status), metadata_value)
            if key_existing is not None and str(key_existing["map_id"]) != mid:
                raise AssetConflictError(f"migration map identity conflict: {mid}")
            if existing is not None:
                current = self._row_map(existing)
                assert current is not None
                if (current.source_domain, current.source_ref, current.source_id, current.target_type, current.target_id, current.target_hash, current.status, dict(current.metadata)) != payload:
                    raise AssetConflictError(f"migration map replay conflicts: {mid}")
                return current
            now = _now()
            local.execute("INSERT INTO asset_migration_map(map_id,source_domain,source_ref,source_id,target_type,target_id,target_hash,status,metadata_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (mid, str(source_domain), str(source_ref), str(source_id), str(target_type), str(target_id), str(target_hash), str(status), _json(metadata_value), now, now))
            if status == "blocked":
                self._record_unknown(local, source_domain=str(source_domain), source_ref=str(source_ref), field="authority", value=str(target_type), reason="unknown_authority", metadata=metadata_value)
            self._queue_outbox(local, aggregate_type="migration", aggregate_id=mid, event_type="asset.migration.mapped", payload={"map_id": mid, "target_id": str(target_id), "target_hash": str(target_hash), "status": str(status)})
            self._record_audit(local, operation="asset.migration.map", aggregate_type="migration_map", aggregate_id=mid, scope=scope, payload={"map_id": mid, "status": str(status)})
            row = local.execute("SELECT * FROM asset_migration_map WHERE map_id=?", (mid,)).fetchone()
            assert row is not None
            return self._row_map(row)  # type: ignore[return-value]

        return self._run_write(conn, apply)

    add_migration_map = record_migration_map
    put_migration_map = record_migration_map

    def get_asset(self, asset_id: str, *, scope: AssetScope | Mapping[str, Any] | None = None, context: Any | None = None) -> Asset | None:
        if scope is None:
            scope = context
        try:
            resolved = _safe_scope(scope) if scope is not None else None
        except (TypeError, ValueError, AssetAuthorizationError):
            return None
        if not _scope_readable(resolved):
            return None
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM assets WHERE asset_id=? AND state='active' AND namespace_id=? AND workspace_id=? AND agent_instance_id=? AND project_ref=? AND provider=? AND share_group_id=? AND runtime_role=? LIMIT 1", (str(asset_id), resolved.namespace_id, resolved.workspace_id, resolved.agent_instance_id, resolved.project_ref, resolved.provider, resolved.share_group_id, resolved.runtime_role)).fetchone()
        return self._row_asset(row)

    read_asset = get_asset

    def get_asset_by_key(self, asset_key: str, *, namespace_id: str = "", scope: AssetScope | Mapping[str, Any] | None = None) -> Asset | None:
        try:
            resolved = _safe_scope(scope, namespace_id=namespace_id) if scope is not None else None
        except (TypeError, ValueError, AssetAuthorizationError):
            return None
        if not _scope_readable(resolved):
            return None
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM assets WHERE asset_key=? AND state='active' AND namespace_id=? AND workspace_id=? AND agent_instance_id=? AND project_ref=? AND provider=? AND share_group_id=? AND runtime_role=? LIMIT 1", (str(asset_key), resolved.namespace_id, resolved.workspace_id, resolved.agent_instance_id, resolved.project_ref, resolved.provider, resolved.share_group_id, resolved.runtime_role)).fetchone()
        return self._row_asset(row)

    def list_assets(self, *, scope: AssetScope | Mapping[str, Any] | None = None, include_deleted: bool = False) -> list[Asset]:
        try:
            resolved = _safe_scope(scope) if scope is not None else None
        except (TypeError, ValueError, AssetAuthorizationError):
            return []
        if not _scope_readable(resolved):
            return []
        state_clause = "state IN ('active','tombstoned','quarantined')" if include_deleted else "state='active'"
        with self.connection() as conn:
            rows = conn.execute(f"SELECT * FROM assets WHERE {state_clause} AND namespace_id=? AND workspace_id=? AND agent_instance_id=? AND project_ref=? AND provider=? AND share_group_id=? AND runtime_role=? ORDER BY asset_id", (resolved.namespace_id, resolved.workspace_id, resolved.agent_instance_id, resolved.project_ref, resolved.provider, resolved.share_group_id, resolved.runtime_role)).fetchall()
        return [item for row in rows if (item := self._row_asset(row)) is not None]

    def get_version(self, version_id: str, *, scope: AssetScope | Mapping[str, Any] | None = None) -> AssetVersion | None:
        try:
            resolved = _safe_scope(scope) if scope is not None else None
        except (TypeError, ValueError, AssetAuthorizationError):
            return None
        if not _scope_readable(resolved):
            return None
        with self.connection() as conn:
            row = conn.execute("SELECT v.* FROM asset_versions v JOIN assets a ON a.asset_id=v.asset_id WHERE v.version_id=? AND a.state='active' AND a.namespace_id=? AND a.workspace_id=? AND a.agent_instance_id=? AND a.project_ref=? AND a.provider=? AND a.share_group_id=? AND a.runtime_role=? LIMIT 1", (str(version_id), resolved.namespace_id, resolved.workspace_id, resolved.agent_instance_id, resolved.project_ref, resolved.provider, resolved.share_group_id, resolved.runtime_role)).fetchone()
        return self._row_version(row)

    read_version = get_version

    def list_versions(self, asset_id: str, *, scope: AssetScope | Mapping[str, Any] | None = None) -> list[AssetVersion]:
        asset = self.get_asset(asset_id, scope=scope)
        if asset is None:
            return []
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM asset_versions WHERE asset_id=? ORDER BY version,version_key", (asset_id,)).fetchall()
        return [item for row in rows if (item := self._row_version(row)) is not None]

    def get_location(self, location_id: str, *, scope: AssetScope | Mapping[str, Any] | None = None) -> AssetLocation | None:
        try:
            resolved = _safe_scope(scope) if scope is not None else None
        except (TypeError, ValueError, AssetAuthorizationError):
            return None
        if not _scope_readable(resolved):
            return None
        with self.connection() as conn:
            row = conn.execute("SELECT l.* FROM asset_locations l JOIN assets a ON a.asset_id=l.asset_id WHERE l.location_id=? AND a.state='active' AND a.namespace_id=? AND a.workspace_id=? AND a.agent_instance_id=? AND a.project_ref=? AND a.provider=? AND a.share_group_id=? AND a.runtime_role=? LIMIT 1", (str(location_id), resolved.namespace_id, resolved.workspace_id, resolved.agent_instance_id, resolved.project_ref, resolved.provider, resolved.share_group_id, resolved.runtime_role)).fetchone()
        return self._row_location(row)

    read_location = get_location

    def get_reference(self, reference_id: str, *, scope: AssetScope | Mapping[str, Any] | None = None) -> AssetReference | None:
        try:
            resolved = _safe_scope(scope) if scope is not None else None
        except (TypeError, ValueError, AssetAuthorizationError):
            return None
        if not _scope_readable(resolved):
            return None
        with self.connection() as conn:
            row = conn.execute("SELECT r.* FROM asset_references r JOIN assets a ON a.asset_id=r.asset_id WHERE r.reference_id=? AND a.state='active' AND a.namespace_id=? AND a.workspace_id=? AND a.agent_instance_id=? AND a.project_ref=? AND a.provider=? AND a.share_group_id=? AND a.runtime_role=? LIMIT 1", (str(reference_id), resolved.namespace_id, resolved.workspace_id, resolved.agent_instance_id, resolved.project_ref, resolved.provider, resolved.share_group_id, resolved.runtime_role)).fetchone()
        return self._row_reference(row)

    read_reference = get_reference

    def list_holds(self, *, asset_id: str | None = None, include_released: bool = False, scope: AssetScope | Mapping[str, Any] | None = None) -> list[AssetHold]:
        try:
            resolved = _safe_scope(scope) if scope is not None else None
        except (TypeError, ValueError, AssetAuthorizationError):
            return []
        if not _scope_readable(resolved):
            return []
        query = "SELECT h.* FROM asset_holds h JOIN assets a ON a.asset_id=h.asset_id WHERE a.namespace_id=? AND a.workspace_id=? AND a.agent_instance_id=? AND a.project_ref=? AND a.provider=? AND a.share_group_id=? AND a.runtime_role=?"
        params: list[Any] = [resolved.namespace_id, resolved.workspace_id, resolved.agent_instance_id, resolved.project_ref, resolved.provider, resolved.share_group_id, resolved.runtime_role]
        if asset_id:
            query += " AND h.asset_id=?"; params.append(str(asset_id))
        if not include_released:
            query += " AND h.active=1"
        query += " ORDER BY h.created_at,h.hold_id"
        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [item for row in rows if (item := self._row_hold(row)) is not None]

    def list_tombstones(self, *, asset_id: str | None = None, include_restored: bool = False, scope: AssetScope | Mapping[str, Any] | None = None) -> list[AssetTombstone]:
        try:
            resolved = _safe_scope(scope) if scope is not None else None
        except (TypeError, ValueError, AssetAuthorizationError):
            return []
        if not _scope_readable(resolved):
            return []
        query = "SELECT t.* FROM asset_tombstones t JOIN assets a ON a.asset_id=t.asset_id WHERE a.namespace_id=? AND a.workspace_id=? AND a.agent_instance_id=? AND a.project_ref=? AND a.provider=? AND a.share_group_id=? AND a.runtime_role=?"
        params: list[Any] = [resolved.namespace_id, resolved.workspace_id, resolved.agent_instance_id, resolved.project_ref, resolved.provider, resolved.share_group_id, resolved.runtime_role]
        if asset_id:
            query += " AND t.asset_id=?"; params.append(str(asset_id))
        if not include_restored:
            query += " AND t.active=1"
        query += " ORDER BY t.created_at,t.tombstone_id"
        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [item for row in rows if (item := self._row_tombstone(row)) is not None]

    def gc(self, *, context: Any | None = None, limit: int | None = None) -> dict[str, Any]:
        """Delete only tombstoned, unreferenced and unheld rows.

        Cross-domain references are intentionally represented by IDs rather
        than foreign keys.  GC therefore computes the complete protected ID
        set (asset, version, location and reference IDs) before deleting and
        removes child rows explicitly instead of relying on ``ON DELETE
        CASCADE`` to hide an unsafe decision.
        """

        scope = self._mutation_scope(context)
        if not scope.admin and scope.authority not in {"system", "migration"}:
            raise AssetAuthorizationError("asset GC requires admin/system context")
        if limit is not None and int(limit) < 0:
            raise ValueError("limit must be >= 0")

        def apply(local: sqlite3.Connection) -> dict[str, Any]:
            query = "SELECT asset_id FROM assets WHERE state IN ('tombstoned','deleted') ORDER BY asset_id"
            if limit is not None:
                query += f" LIMIT {int(limit)}"
            candidates = [str(row[0]) for row in local.execute(query).fetchall()]
            deleted_assets: list[str] = []
            deleted_versions: list[str] = []
            blocked_assets: list[str] = []
            for aid in candidates:
                version_ids = {
                    str(row[0])
                    for row in local.execute(
                        "SELECT version_id FROM asset_versions WHERE asset_id=?", (aid,)
                    ).fetchall()
                }
                location_ids = {
                    str(row[0])
                    for row in local.execute(
                        "SELECT location_id FROM asset_locations WHERE asset_id=?", (aid,)
                    ).fetchall()
                }
                reference_ids = {
                    str(row[0])
                    for row in local.execute(
                        "SELECT reference_id FROM asset_references WHERE asset_id=? OR version_id IN ({})".format(
                            ",".join("?" for _ in version_ids) or "NULL"
                        ),
                        (aid, *sorted(version_ids)),
                    ).fetchall()
                }
                protected_ids = {aid, *version_ids, *location_ids, *reference_ids}
                hold_query = (
                    "SELECT 1 FROM asset_holds WHERE active=1 AND "
                    "(asset_id=? OR version_id IN ({0})) LIMIT 1"
                ).format(",".join("?" for _ in version_ids) or "NULL")
                held = local.execute(hold_query, (aid, *sorted(version_ids))).fetchone() is not None
                ref_query = (
                    "SELECT 1 FROM asset_references WHERE "
                    "asset_id=? OR version_id IN ({0}) OR target_id IN ({1}) LIMIT 1"
                ).format(
                    ",".join("?" for _ in version_ids) or "NULL",
                    ",".join("?" for _ in protected_ids) or "NULL",
                )
                referenced = local.execute(
                    ref_query, (aid, *sorted(version_ids), *sorted(protected_ids))
                ).fetchone() is not None
                if held or referenced:
                    blocked_assets.append(aid)
                    continue

                self._fault("gc.before")
                # Queue evidence before deleting the aggregate.  Both rows
                # are part of this same transaction and deterministic on
                # replay, so a retry cannot duplicate them.
                event_payload = {
                    "asset_id": aid,
                    "version_ids": sorted(version_ids),
                    "deleted": True,
                }
                self._queue_outbox(
                    local,
                    aggregate_type="asset",
                    aggregate_id=aid,
                    event_type="asset.gc.deleted",
                    payload=event_payload,
                )
                self._record_audit(
                    local,
                    operation="asset.gc",
                    aggregate_type="asset",
                    aggregate_id=aid,
                    scope=scope,
                    payload=event_payload,
                )
                # Explicit child cleanup is deliberate: this invariant must
                # remain true even if schema-level cascades are changed.
                local.execute("DELETE FROM asset_references WHERE asset_id=?", (aid,))
                local.execute("DELETE FROM asset_holds WHERE asset_id=?", (aid,))
                local.execute("DELETE FROM asset_tombstones WHERE asset_id=?", (aid,))
                local.execute("DELETE FROM asset_locations WHERE asset_id=?", (aid,))
                local.execute("DELETE FROM asset_versions WHERE asset_id=?", (aid,))
                local.execute("DELETE FROM asset_registry WHERE asset_id=?", (aid,))
                local.execute("DELETE FROM assets WHERE asset_id=?", (aid,))
                deleted_versions.extend(sorted(version_ids))
                deleted_assets.append(aid)
            return {
                "deleted_assets": deleted_assets,
                "deleted_versions": deleted_versions,
                "blocked": len(blocked_assets),
                "blocked_assets": blocked_assets,
            }

        return self._run_write(None, apply)

    collect_garbage = gc

    def pending_outbox(self, *, limit: int = 100) -> list[AssetOutboxEvent]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM asset_outbox WHERE status='pending' ORDER BY created_at,event_id LIMIT ?", (int(limit),)).fetchall()
        return [item for row in rows if (item := self._row_outbox(row)) is not None]

    list_outbox = pending_outbox

    def mark_outbox(self, event_id: str, *, status: str = "sent", context: Any | None = None) -> int:
        if status not in {"pending", "sent", "failed"}:
            raise ValueError("invalid outbox status")
        if self.readonly:
            raise AssetAuthorizationError("asset store is read-only")
        scope = self._mutation_scope(context)
        if not scope.admin and scope.authority not in {"system", "migration"}:
            raise AssetAuthorizationError("outbox acknowledgement requires admin/system context")

        def apply(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "UPDATE asset_outbox SET status=?,attempts=attempts+1,updated_at=? WHERE event_id=?",
                (status, _now(), str(event_id)),
            )
            changed = int(cur.rowcount)
            if changed:
                self._record_audit(
                    conn,
                    operation="asset.outbox.mark",
                    aggregate_type="outbox",
                    aggregate_id=str(event_id),
                    scope=scope,
                    payload={"event_id": str(event_id), "status": status},
                )
            return changed

        return self._run_write(None, apply)

    ack_outbox = mark_outbox

    def list_unknown_ledger(self, *, status: str | None = None) -> list[AssetUnknownLedgerEntry]:
        query = "SELECT * FROM asset_unknown_ledger"
        params: list[Any] = []
        if status:
            query += " WHERE status=?"; params.append(str(status))
        query += " ORDER BY created_at,unknown_id"
        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [item for row in rows if (item := self._row_unknown(row)) is not None]

    unknown_ledger = list_unknown_ledger

    def list_migration_maps(self, *, source_domain: str | None = None) -> list[AssetMigrationMap]:
        query = "SELECT * FROM asset_migration_map"; params: list[Any] = []
        if source_domain is not None:
            query += " WHERE source_domain=?"; params.append(str(source_domain))
        query += " ORDER BY created_at,map_id"
        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [item for row in rows if (item := self._row_map(row)) is not None]

    migration_maps = list_migration_maps

    def counts(self) -> dict[str, int]:
        tables = ("assets", "asset_versions", "asset_locations", "asset_references", "asset_holds", "asset_tombstones", "asset_migration_map", "asset_outbox", "asset_audit", "asset_unknown_ledger")
        with self.connection() as conn:
            return {table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in tables}

    table_counts = counts

    def integrity_check(self) -> list[str]:
        with self.connection() as conn:
            errors = [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall() if str(row[0]).lower() != "ok"]
            errors.extend(f"foreign_key:{tuple(row)}" for row in conn.execute("PRAGMA foreign_key_check").fetchall())
            # Cross-domain references intentionally have no FK; local asset
            # references/versions/locations do and the checks above catch
            # local orphans.  Explicit orphan checks cover nullable version
            # references while retaining the saga boundary.
            for table, column, target in (("asset_locations", "version_id", "asset_versions"), ("asset_references", "version_id", "asset_versions"), ("asset_holds", "version_id", "asset_versions")):
                count = int(conn.execute(f"SELECT COUNT(*) FROM {table} t WHERE t.{column}<>'' AND NOT EXISTS (SELECT 1 FROM {target} x WHERE x.version_id=t.{column})").fetchone()[0])
                if count:
                    errors.append(f"orphan:{table}.{column}:{count}")
            return errors or ["ok"]

    check_integrity = integrity_check

    def status(self) -> dict[str, Any]:
        counts = self.counts()
        return {**counts, "db_path": str(self.db_path), "schema_marker": SCHEMA_MARKER, "readonly": self.readonly}


# Compatibility names used by Phase-5 design notes.
AssetRegistry = AssetStore
AssetRegistryStore = AssetStore


__all__ = [
    "SCHEMA_VERSION",
    "SCHEMA_MARKER",
    "ASSET_SCHEMA_VERSION",
    "ASSET_SCHEMA_MARKER",
    "AssetError",
    "AssetSchemaError",
    "AssetAuthorizationError",
    "AssetConflictError",
    "AssetPathError",
    "AssetMigrationError",
    "AssetStore",
    "AssetRegistry",
    "AssetRegistryStore",
    "stable_id",
    "normalize_relative_path",
]
