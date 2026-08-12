"""V2 Content Plane storage primitives.

This module owns the canonical text database for V2.  It intentionally keeps
the public surface small: callers write a canonical blob and then attach one
or more source occurrences carrying the authorization context.  The schema
bootstrap is additive because Phase 1 already shipped a compatible subset of
the content tables in :mod:`memoryguard.storage.schema`.

The implementation does not import any V1 business store.  Every writable
connection is selected through :class:`WorkspaceV2Layout`, opened with the
storage database helper and wrapped in the shared transaction helper.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any, Iterable, Mapping
import unicodedata
import uuid

from ..storage.database import execute_sql_script, open_database, open_database_snapshot
from ..storage.layout import WorkspaceV2Layout
from ..storage.schema import initialize_database
from ..storage.transaction import transaction


NORMALIZER_ID = "utf8-nfc-newline-v1"
SCHEMA_VERSION = 3
UNKNOWN_ACL = "__UNKNOWN__"

# ACL values are an explicit contract.  Providers are intentionally open
# ended (registered by deployment) but must be non-empty; absent providers
# become UNKNOWN and never authorize a read.
_ALLOWED_SENSITIVITIES: set[str] = {
    "normal",
    "sensitive",
    "none",
    "low",
    "medium",
    "high",
    "restricted",
}
_ALLOWED_POLICY_CLASSES: set[str] = {"private", "shared", "public"}


def register_acl_values(
    *,
    sensitivities: Iterable[str] = (),
    policy_classes: Iterable[str] = (),
) -> None:
    """Extend ACL enum allowlists before writing/reading content.

    Registration is explicit and process-local; unknown values remain
    unreadable until deployment code opts them in.
    """

    for value in sensitivities:
        text = str(value).strip()
        if not text or text == UNKNOWN_ACL:
            raise ValueError("invalid sensitivity registration")
        _ALLOWED_SENSITIVITIES.add(text)
    for value in policy_classes:
        text = str(value).strip()
        if not text or text == UNKNOWN_ACL:
            raise ValueError("invalid policy class registration")
        _ALLOWED_POLICY_CLASSES.add(text)


class ContentError(RuntimeError):
    """Base class for content-plane failures."""


class ContentCollisionError(ContentError):
    """A canonical hash already exists for different canonical text."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_id(prefix: str, *parts: object) -> str:
    """Return a deterministic, non-sequential V2 identity."""

    payload = "\x1f".join(str(part) for part in (prefix, *parts))
    return f"{prefix}-{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def canonicalize_text(value: str | bytes, *, version: int = 1) -> str:
    """Apply the V1 canonicalizer used by Content Plane blobs.

    Strict UTF-8 decoding, newline normalization and NFC are deliberately the
    only transformations.  Internal whitespace, case and punctuation remain
    part of the content identity.
    """

    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="strict")
    else:
        text = str(value)
    if int(version) != 1:
        raise ValueError(f"unsupported canonicalization version: {version}")
    return unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def acl_digest(values: Mapping[str, Any] | None = None, **kwargs: Any) -> str:
    """Digest only structured ACL values; JSON is not the authorization source."""

    data = dict(values or {})
    data.update(kwargs)
    keys = ("workspace_id", "agent_instance_id", "project_ref", "provider", "share_group_id", "policy_class", "sensitivity")
    return hashlib.sha256(_json({key: str(data.get(key) or "") for key in keys}).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Namespace:
    namespace_id: str
    workspace_id: str
    trust_domain: str
    sensitivity: str
    retention_authority: str
    canonicalization_version: int


@dataclass(frozen=True)
class ContentReadScope:
    """Exact authorization tuple required for content reads.

    A blob has no ACL by itself; access is granted only when an active
    occurrence in the requested namespace matches every scope component.
    Empty strings are valid explicit values.  ``None`` is rejected so callers
    cannot accidentally turn an unknown identity into a wildcard.
    """

    namespace_id: str
    workspace_id: str
    agent_instance_id: str = ""
    project_ref: str = ""
    provider: str = ""
    share_group_id: str = ""
    sensitivity: str = "normal"
    policy_class: str = "private"

    def __post_init__(self) -> None:
        for name in (
            "namespace_id",
            "workspace_id",
            "agent_instance_id",
            "project_ref",
            "provider",
            "share_group_id",
            "sensitivity",
            "policy_class",
        ):
            value = getattr(self, name)
            if value is None:
                raise ValueError(f"{name} must be explicit; None is not a wildcard")
            if not isinstance(value, str):
                object.__setattr__(self, name, str(value))
        if not self.namespace_id or not self.workspace_id:
            raise ValueError("namespace_id and workspace_id are required")


def _scope_is_readable(scope: ContentReadScope | None) -> bool:
    if not isinstance(scope, ContentReadScope):
        return False
    if not scope.provider or scope.provider == UNKNOWN_ACL:
        return False
    if scope.sensitivity not in _ALLOWED_SENSITIVITIES:
        return False
    if scope.policy_class not in _ALLOWED_POLICY_CLASSES:
        return False
    if UNKNOWN_ACL in {
        scope.namespace_id,
        scope.workspace_id,
        scope.agent_instance_id,
        scope.project_ref,
        scope.provider,
        scope.share_group_id,
    }:
        return False
    return True


def _assert_workspace_no_reparse(path: str | Path) -> None:
    """Reject workspace/ancestor symlinks and Windows reparse points.

    ``WorkspaceV2Layout`` resolves paths for ordinary callers; ContentStore
    must inspect the lexical path first so that resolution cannot redirect a
    write into an external tree.
    """

    raw = Path(path).expanduser()
    absolute = Path(os.path.abspath(os.fspath(raw)))
    current = absolute
    while True:
        try:
            exists = current.exists() or current.is_symlink()
        except OSError as exc:
            raise ContentError(f"cannot inspect workspace path: {current}") from exc
        if exists:
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                info = None
            except OSError as exc:
                raise ContentError(f"cannot inspect workspace path: {current}") from exc
            if info is not None and (
                stat.S_ISLNK(info.st_mode)
                or bool(getattr(info, "st_file_attributes", 0) & 0x0400)
            ):
                raise ContentError(
                    f"workspace path cannot contain symlink or reparse point: {current}"
                )
        parent = current.parent
        if parent == current:
            break
        current = parent


@dataclass(frozen=True)
class Blob:
    blob_id: str
    namespace_id: str
    canonical_hash: str
    normalizer_id: str
    text: str
    byte_count: int
    char_count: int


@dataclass(frozen=True)
class Occurrence:
    occurrence_id: str
    source_object_id: str
    occurrence_key: str
    blob_id: str | None
    active: bool


CONTENT_AUX_SCHEMA = """
CREATE TABLE IF NOT EXISTS content_schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_connectors (
    source_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    source_type TEXT NOT NULL,
    external_root_key TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(workspace_id, provider, source_type, external_root_key)
);
CREATE TABLE IF NOT EXISTS conversation_sessions (
    session_id TEXT PRIMARY KEY,
    source_object_id TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    workspace_id TEXT NOT NULL,
    agent_instance_id TEXT NOT NULL DEFAULT '',
    project_ref TEXT NOT NULL DEFAULT '',
    share_group_id TEXT NOT NULL DEFAULT '',
    policy_class TEXT NOT NULL DEFAULT 'private',
    created_at TEXT NOT NULL DEFAULT '',
    imported_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    UNIQUE(source_object_id),
    FOREIGN KEY(source_object_id) REFERENCES source_objects(source_object_id)
);
CREATE TABLE IF NOT EXISTS conversation_summaries (
    summary_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    occurrence_id TEXT,
    summary_kind TEXT NOT NULL DEFAULT 'import',
    summary_hash TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    UNIQUE(session_id, summary_kind),
    FOREIGN KEY(session_id) REFERENCES conversation_sessions(session_id),
    FOREIGN KEY(occurrence_id) REFERENCES content_occurrences(occurrence_id)
);
CREATE TABLE IF NOT EXISTS conversation_observations (
    observation_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL DEFAULT '',
    occurrence_id TEXT,
    observation_type TEXT NOT NULL DEFAULT 'import',
    summary_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES conversation_sessions(session_id),
    FOREIGN KEY(occurrence_id) REFERENCES content_occurrences(occurrence_id)
);
CREATE TABLE IF NOT EXISTS content_evidence_links (
    link_id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    turn_id TEXT NOT NULL DEFAULT '',
    occurrence_id TEXT NOT NULL DEFAULT '',
    blob_id TEXT NOT NULL DEFAULT '',
    source_revision TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'valid',
    created_at TEXT NOT NULL,
    invalidated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS history_mutation_receipts (
    idempotency_key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS content_holds (
    hold_id TEXT PRIMARY KEY,
    blob_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    source_ref TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    created_at TEXT NOT NULL,
    released_at TEXT NOT NULL DEFAULT '',
    UNIQUE(blob_id, reason, source_ref),
    FOREIGN KEY(blob_id) REFERENCES content_blobs(blob_id)
);
CREATE TABLE IF NOT EXISTS content_tombstones (
    tombstone_id TEXT PRIMARY KEY,
    source_object_id TEXT NOT NULL,
    occurrence_id TEXT NOT NULL DEFAULT '',
    blob_id TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL,
    scan_id TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    restored_at TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    UNIQUE(source_object_id, occurrence_id, reason),
    FOREIGN KEY(source_object_id) REFERENCES source_objects(source_object_id)
);
CREATE TABLE IF NOT EXISTS source_sync_state (
    source_id TEXT PRIMARY KEY,
    active_run_id TEXT NOT NULL DEFAULT '',
    owner_id TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'idle',
    cursor TEXT NOT NULL DEFAULT '',
    cursor_digest TEXT NOT NULL DEFAULT '',
    cursor_source_id TEXT NOT NULL DEFAULT '',
    cursor_run_id TEXT NOT NULL DEFAULT '',
    cursor_owner_id TEXT NOT NULL DEFAULT '',
    cursor_revision INTEGER NOT NULL DEFAULT 0,
    cursor_position INTEGER NOT NULL DEFAULT 0,
    cursor_batch_digest TEXT NOT NULL DEFAULT '',
    expected_revision INTEGER NOT NULL DEFAULT 0,
    expected_manifest_digest TEXT NOT NULL DEFAULT '',
    last_complete_scan_id TEXT NOT NULL DEFAULT '',
    manifest_digest TEXT NOT NULL DEFAULT '',
    coverage_digest TEXT NOT NULL DEFAULT '',
    last_started_at TEXT NOT NULL DEFAULT '',
    last_finished_at TEXT NOT NULL DEFAULT '',
    last_error_code TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(source_id) REFERENCES source_connectors(source_id)
);
CREATE TABLE IF NOT EXISTS source_manifest_items (
    source_id TEXT NOT NULL,
    external_object_key TEXT NOT NULL,
    occurrence_key TEXT NOT NULL,
    source_revision TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    last_complete_scan_id TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(source_id, external_object_key, occurrence_key),
    FOREIGN KEY(source_id) REFERENCES source_connectors(source_id)
);
CREATE TABLE IF NOT EXISTS source_manifest_staging (
    run_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    external_object_key TEXT NOT NULL,
    occurrence_key TEXT NOT NULL,
    source_revision TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    coverage_status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(run_id, external_object_key, occurrence_key)
);
CREATE TABLE IF NOT EXISTS source_sync_anomalies (
    source_id TEXT NOT NULL,
    error_fingerprint TEXT NOT NULL,
    error_code TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    resolved_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(source_id, error_fingerprint),
    FOREIGN KEY(source_id) REFERENCES source_connectors(source_id)
);
CREATE TABLE IF NOT EXISTS migration_map (
    map_id TEXT PRIMARY KEY,
    source_db TEXT NOT NULL,
    source_table TEXT NOT NULL,
    source_pk TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL DEFAULT '',
    source_hash TEXT NOT NULL DEFAULT '',
    target_hash TEXT NOT NULL DEFAULT '',
    acl_digest TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'mapped',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_db, source_table, source_pk, target_type)
);
CREATE TABLE IF NOT EXISTS knowledge_records (
    record_id TEXT PRIMARY KEY,
    source_table TEXT NOT NULL,
    source_pk TEXT NOT NULL,
    record_type TEXT NOT NULL,
    content_blob_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    derived_status TEXT NOT NULL DEFAULT 'CANONICAL',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(source_table, source_pk, record_type)
);
CREATE TABLE IF NOT EXISTS knowledge_relations (
    relation_id TEXT PRIMARY KEY,
    source_table TEXT NOT NULL,
    source_pk TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(source_table, source_pk, relation_type)
);
CREATE TABLE IF NOT EXISTS content_acl_anomalies (
    anomaly_id TEXT PRIMARY KEY,
    occurrence_id TEXT NOT NULL,
    field TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(occurrence_id, field),
    FOREIGN KEY(occurrence_id) REFERENCES content_occurrences(occurrence_id)
);
CREATE INDEX IF NOT EXISTS idx_content_holds_blob ON content_holds(blob_id, active);
CREATE INDEX IF NOT EXISTS idx_content_tombstones_occurrence ON content_tombstones(occurrence_id, active);
CREATE INDEX IF NOT EXISTS idx_migration_map_source ON migration_map(source_db, source_table, source_pk);
CREATE INDEX IF NOT EXISTS idx_knowledge_records_source ON knowledge_records(source_table, source_pk);
CREATE INDEX IF NOT EXISTS idx_content_acl_anomalies_occurrence ON content_acl_anomalies(occurrence_id);
"""


_AUX_REQUIRED_TABLES = frozenset(
    {
        "content_schema_meta",
        "source_connectors",
        "conversation_sessions",
        "conversation_summaries",
        "conversation_observations",
        "content_evidence_links",
        "history_mutation_receipts",
        "content_holds",
        "content_tombstones",
        "source_sync_state",
        "source_manifest_items",
        "source_manifest_staging",
        "source_sync_anomalies",
        "migration_map",
        "knowledge_records",
        "knowledge_relations",
        "content_acl_anomalies",
    }
)

_AUX_REQUIRED_COLUMNS = {
    "source_objects": frozenset(
        {"source_id", "object_type", "parent_object_id", "deleted_scan_id"}
    ),
    "content_occurrences": frozenset(
        {"deleted_scan_id", "policy_class", "provider"}
    ),
    "conversation_turns": frozenset(
        {"session_id", "event_key", "content_type", "source_revision"}
    ),
}


class ContentStore:
    """Canonical Content Plane backed by ``WorkspaceV2Layout.content_db``."""

    def __init__(
        self,
        workspace: str | Path | WorkspaceV2Layout,
        *,
        workspace_id: str | None = None,
        trust_domain: str = "workspace",
        sensitivity: str = "normal",
        retention_authority: str = "workspace",
        canonicalization_version: int = 1,
        initialize: bool = True,
    ) -> None:
        if isinstance(workspace, WorkspaceV2Layout):
            self.layout = workspace
        else:
            _assert_workspace_no_reparse(workspace)
            self.layout = WorkspaceV2Layout(Path(workspace))
        self.workspace_id = str(workspace_id or self.layout.workspace)
        self.default_namespace_spec = {
            "workspace_id": self.workspace_id,
            "trust_domain": str(trust_domain),
            "sensitivity": str(sensitivity),
            "retention_authority": str(retention_authority),
            "canonicalization_version": int(canonicalization_version),
        }
        self.db_path = self.layout.content_db
        if initialize:
            self.layout.ensure_dirs()
            # Aux marker preflight must happen before the writable storage
            # bootstrap.  A future/unknown marker therefore cannot trigger a
            # WAL pragma, timestamp update, DDL, or any other target mutation.
            aux_state = self._preflight_aux_schema()
            if aux_state != "current":
                initialize_database(self.db_path, "content", layout=self.layout)
                self._ensure_aux_schema()
            else:
                # Keep ordinary reads/writes sidecar-stable.  Only invoke the
                # additive migration when a legacy marker-2 database is
                # missing one of the Phase-3 proof columns.
                with open_database_snapshot(self.db_path) as conn:
                    state_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(source_sync_state)")}
                    evidence_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(content_evidence_links)")}
                required_state = {"owner_id", "cursor_digest", "cursor_source_id", "cursor_run_id", "cursor_owner_id", "cursor_revision", "cursor_position", "cursor_batch_digest", "expected_revision", "expected_manifest_digest"}
                if not required_state <= state_columns or not {"blob_id", "source_revision"} <= evidence_columns:
                    self._ensure_aux_schema()

    def _preflight_aux_schema(self) -> str:
        """Return ``fresh``/``needs_aux``/``current`` after RO validation.

        Only one aux migration is supported: absent aux schema to
        ``SCHEMA_VERSION``.  Every other marker or incomplete current schema
        fails closed; callers must ship an explicit migration before writing.
        """

        if not self.db_path.is_file():
            return "fresh"
        try:
            with open_database_snapshot(self.db_path) as conn:
                tables = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                if "content_schema_meta" not in tables:
                    # Only a completely absent aux plane may take the one
                    # explicit known migration.  Partial aux objects without
                    # their marker are ambiguous/corrupt and must not be
                    # inferred or completed by CREATE IF NOT EXISTS.
                    partial_aux = sorted(
                        tables & (_AUX_REQUIRED_TABLES - {"content_schema_meta"})
                    )
                    if partial_aux:
                        raise ContentError(
                            "content aux schema marker is missing; refusing partial migration: "
                            + ",".join(partial_aux)
                        )
                    # Let initialize_database perform strict Phase 1 schema
                    # marker preflight before any write.
                    return "needs_aux"
                rows = conn.execute(
                    "SELECT key,value FROM content_schema_meta ORDER BY key"
                ).fetchall()
                if len(rows) != 1 or str(rows[0][0]) != "version":
                    raise ContentError(
                        "content_schema_meta contains unknown or duplicate keys"
                    )
                marker = str(rows[0][1])
                if marker == "2":
                    legacy_required = _AUX_REQUIRED_TABLES - {"history_mutation_receipts"}
                    missing_legacy = sorted(legacy_required - tables)
                    if missing_legacy:
                        raise ContentError(
                            "content schema marker 2 is incomplete: " + ",".join(missing_legacy)
                        )
                    return "upgrade_v3"
                if marker != str(SCHEMA_VERSION):
                    direction = "future" if marker.isdigit() and int(marker) > SCHEMA_VERSION else "unsupported"
                    raise ContentError(
                        f"{direction} content schema version: {marker!r}"
                    )
                missing_tables = sorted(_AUX_REQUIRED_TABLES - tables)
                if missing_tables:
                    raise ContentError(
                        "content schema marker is current but tables are missing: "
                        + ",".join(missing_tables)
                    )
                for table, required in _AUX_REQUIRED_COLUMNS.items():
                    present = {
                        str(row[1])
                        for row in conn.execute(f"PRAGMA table_info({table})")
                    }
                    missing = sorted(required - present)
                    if missing:
                        raise ContentError(
                            f"content schema marker is current but columns are missing in {table}: "
                            + ",".join(missing)
                        )
            # Validate Phase 1 schema marker/user_version read-only as well.
            initialize_database(
                self.db_path, "content", layout=self.layout, readonly=True
            )
            return "current"
        except ContentError:
            raise
        except (sqlite3.Error, OSError, ValueError) as exc:
            raise ContentError(
                f"cannot preflight content schema: {self.db_path}"
            ) from exc

    def _ensure_aux_schema(self) -> None:
        with open_database(self.db_path) as conn:
            with transaction(conn):
                # Phase 1's source_objects and occurrences predate the source
                # connector/sync ledger.  ALTER only when a column is absent;
                # no existing content or identity is rewritten.
                for table, column, ddl in (
                    ("source_objects", "source_id", "ALTER TABLE source_objects ADD COLUMN source_id TEXT NOT NULL DEFAULT ''"),
                    ("source_objects", "object_type", "ALTER TABLE source_objects ADD COLUMN object_type TEXT NOT NULL DEFAULT 'object'"),
                    ("source_objects", "parent_object_id", "ALTER TABLE source_objects ADD COLUMN parent_object_id TEXT NOT NULL DEFAULT ''"),
                    ("source_objects", "deleted_scan_id", "ALTER TABLE source_objects ADD COLUMN deleted_scan_id TEXT NOT NULL DEFAULT ''"),
                    ("content_occurrences", "deleted_scan_id", "ALTER TABLE content_occurrences ADD COLUMN deleted_scan_id TEXT NOT NULL DEFAULT ''"),
                    ("content_occurrences", "policy_class", "ALTER TABLE content_occurrences ADD COLUMN policy_class TEXT NOT NULL DEFAULT 'private'"),
                    ("content_occurrences", "provider", "ALTER TABLE content_occurrences ADD COLUMN provider TEXT NOT NULL DEFAULT ''"),
                    ("conversation_turns", "session_id", "ALTER TABLE conversation_turns ADD COLUMN session_id TEXT NOT NULL DEFAULT ''"),
                    ("conversation_turns", "event_key", "ALTER TABLE conversation_turns ADD COLUMN event_key TEXT NOT NULL DEFAULT ''"),
                    ("conversation_turns", "content_type", "ALTER TABLE conversation_turns ADD COLUMN content_type TEXT NOT NULL DEFAULT 'text'"),
                    ("conversation_turns", "source_revision", "ALTER TABLE conversation_turns ADD COLUMN source_revision TEXT NOT NULL DEFAULT ''"),
                ):
                    cols = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
                    if column not in cols:
                        conn.execute(ddl)
                execute_sql_script(conn, CONTENT_AUX_SCHEMA)
                for table, column, ddl in (
                    ("content_evidence_links", "blob_id", "ALTER TABLE content_evidence_links ADD COLUMN blob_id TEXT NOT NULL DEFAULT ''"),
                    ("content_evidence_links", "source_revision", "ALTER TABLE content_evidence_links ADD COLUMN source_revision TEXT NOT NULL DEFAULT ''"),
                    ("source_sync_state", "owner_id", "ALTER TABLE source_sync_state ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''"),
                    ("source_sync_state", "cursor_digest", "ALTER TABLE source_sync_state ADD COLUMN cursor_digest TEXT NOT NULL DEFAULT ''"),
                    ("source_sync_state", "cursor_source_id", "ALTER TABLE source_sync_state ADD COLUMN cursor_source_id TEXT NOT NULL DEFAULT ''"),
                    ("source_sync_state", "cursor_run_id", "ALTER TABLE source_sync_state ADD COLUMN cursor_run_id TEXT NOT NULL DEFAULT ''"),
                    ("source_sync_state", "cursor_owner_id", "ALTER TABLE source_sync_state ADD COLUMN cursor_owner_id TEXT NOT NULL DEFAULT ''"),
                    ("source_sync_state", "cursor_revision", "ALTER TABLE source_sync_state ADD COLUMN cursor_revision INTEGER NOT NULL DEFAULT 0"),
                    ("source_sync_state", "cursor_position", "ALTER TABLE source_sync_state ADD COLUMN cursor_position INTEGER NOT NULL DEFAULT 0"),
                    ("source_sync_state", "cursor_batch_digest", "ALTER TABLE source_sync_state ADD COLUMN cursor_batch_digest TEXT NOT NULL DEFAULT ''"),
                    ("source_sync_state", "expected_revision", "ALTER TABLE source_sync_state ADD COLUMN expected_revision INTEGER NOT NULL DEFAULT 0"),
                    ("source_sync_state", "expected_manifest_digest", "ALTER TABLE source_sync_state ADD COLUMN expected_manifest_digest TEXT NOT NULL DEFAULT ''"),
                ):
                    cols = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
                    if column not in cols:
                        conn.execute(ddl)
                conn.execute(
                    "INSERT INTO content_schema_meta(key,value) VALUES('version',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(SCHEMA_VERSION),),
                )
                conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_source_objects_source_external ON source_objects(source_id, external_object_key)")

    def connection(self):
        """Yield a configured connection for read-only inspection."""
        return open_database(self.db_path, readonly=True)

    def ensure_namespace(
        self,
        *,
        namespace_id: str | None = None,
        workspace_id: str | None = None,
        trust_domain: str | None = None,
        sensitivity: str | None = None,
        retention_authority: str | None = None,
        canonicalization_version: int | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> Namespace:
        spec = dict(self.default_namespace_spec)
        for key, value in {
            "workspace_id": workspace_id,
            "trust_domain": trust_domain,
            "sensitivity": sensitivity,
            "retention_authority": retention_authority,
            "canonicalization_version": canonicalization_version,
        }.items():
            if value is not None:
                spec[key] = value
        spec["canonicalization_version"] = int(spec["canonicalization_version"])
        namespace_id = namespace_id or stable_id(
            "ns", spec["workspace_id"], spec["trust_domain"], spec["sensitivity"],
            spec["retention_authority"], spec["canonicalization_version"],
        )
        now = _now()
        def apply(local: sqlite3.Connection) -> Namespace:
            local.execute(
                    "INSERT INTO content_namespaces(namespace_id,workspace_id,trust_domain,sensitivity,retention_authority,canonicalization_version,created_at) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(namespace_id) DO NOTHING",
                    (namespace_id, spec["workspace_id"], spec["trust_domain"], spec["sensitivity"], spec["retention_authority"], spec["canonicalization_version"], now),
                )
            row = local.execute("SELECT namespace_id,workspace_id,trust_domain,sensitivity,retention_authority,canonicalization_version FROM content_namespaces WHERE namespace_id=?", (namespace_id,)).fetchone()
            if row is None:
                raise ContentError("namespace creation failed")
            return Namespace(str(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4]), int(row[5]))
        if conn is not None:
            return apply(conn)
        with open_database(self.db_path) as local:
            with transaction(local):
                return apply(local)

    create_namespace = ensure_namespace

    def put_blob(
        self,
        *args: Any,
        text: str | bytes | None = None,
        namespace_id: str | None = None,
        namespace: Namespace | str | None = None,
        language_hint: str = "",
        normalizer_id: str = NORMALIZER_ID,
        conn: sqlite3.Connection | None = None,
    ) -> str | None:
        """Insert or retrieve a namespace-local canonical blob.

        Accepted forms are ``put_blob(text)``, ``put_blob(namespace_id, text)``
        and keyword arguments.  Empty canonical text intentionally creates no
        blob (the caller may keep a migration-map row for the source event).
        """

        if len(args) > 2:
            raise TypeError("put_blob accepts at most namespace_id and text")
        if len(args) == 2:
            namespace_id = str(args[0])
            text = args[1]
        elif len(args) == 1:
            if text is not None:
                namespace_id = str(args[0])
            else:
                text = args[0]
        if namespace is not None:
            namespace_id = namespace.namespace_id if isinstance(namespace, Namespace) else str(namespace)
        ns = self.ensure_namespace(namespace_id=namespace_id, conn=conn)
        if text is None:
            raise TypeError("text is required")
        canonical = canonicalize_text(text, version=ns.canonicalization_version)
        if canonical == "":
            return None
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        blob_id = stable_id("blob", ns.namespace_id, normalizer_id, digest)
        now = _now()
        def apply(local: sqlite3.Connection) -> str:
                row = local.execute(
                    "SELECT blob_id,text FROM content_blobs WHERE namespace_id=? AND normalizer_id=? AND canonical_hash=?",
                    (ns.namespace_id, normalizer_id, digest),
                ).fetchone()
                if row is not None:
                    if str(row[1]) != canonical:
                        raise ContentCollisionError(f"canonical hash collision in namespace {ns.namespace_id}")
                    return str(row[0])
                local.execute(
                    "INSERT INTO content_blobs(blob_id,namespace_id,canonical_hash,normalizer_id,text,byte_count,char_count,language_hint,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (blob_id, ns.namespace_id, digest, normalizer_id, canonical, len(canonical.encode("utf-8")), len(canonical), language_hint, now),
                )
                return blob_id
        if conn is not None:
            return apply(conn)
        with open_database(self.db_path) as local:
            with transaction(local):
                return apply(local)

    upsert_blob = put_blob

    def get_blob(
        self, blob_id: str, scope: ContentReadScope | None = None
    ) -> Blob | None:
        """Read blob only through an exact, occurrence-backed ACL scope.

        Missing IDs, missing scopes, and ACL denials all return ``None``.  No
        branch exposes whether an unscoped ID exists.
        """

        if not _scope_is_readable(scope):
            return None
        with self.connection() as conn:
            row = conn.execute(
                "SELECT b.blob_id,b.namespace_id,b.canonical_hash,b.normalizer_id,"
                "b.text,b.byte_count,b.char_count "
                "FROM content_blobs b JOIN content_occurrences o ON o.blob_id=b.blob_id "
                "WHERE b.blob_id=? AND b.namespace_id=? AND o.active=1 "
                "AND o.workspace_id=? AND o.agent_instance_id=? AND o.project_ref=? "
                "AND o.provider=? AND o.share_group_id=? AND o.sensitivity=? "
                "AND o.policy_class=? LIMIT 1",
                (
                    blob_id,
                    scope.namespace_id,
                    scope.workspace_id,
                    scope.agent_instance_id,
                    scope.project_ref,
                    scope.provider,
                    scope.share_group_id,
                    scope.sensitivity,
                    scope.policy_class,
                ),
            ).fetchone()
        if row is None:
            return None
        return Blob(str(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4]), int(row[5]), int(row[6]))

    def upsert_source_connector(
        self,
        *,
        source_id: str,
        provider: str,
        source_type: str,
        external_root_key: str,
        workspace_id: str | None = None,
        enabled: bool = True,
        conn: sqlite3.Connection | None = None,
    ) -> str:
        now = _now()
        values = (source_id, workspace_id or self.workspace_id, provider, source_type, external_root_key, int(enabled), now, now)
        if conn is not None:
            conn.execute(
                "INSERT INTO source_connectors(source_id,workspace_id,provider,source_type,external_root_key,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(source_id) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at",
                values,
            )
            return source_id
        with open_database(self.db_path) as local:
            with transaction(local):
                return self.upsert_source_connector(source_id=source_id, provider=provider, source_type=source_type, external_root_key=external_root_key, workspace_id=workspace_id, enabled=enabled, conn=local)

    def upsert_occurrence(
        self,
        *,
        source_object_id: str,
        occurrence_key: str,
        blob_id: str | None = None,
        text: str | bytes | None = None,
        namespace_id: str | None = None,
        source_id: str | None = None,
        source_kind: str = "source",
        external_object_key: str | None = None,
        object_type: str = "object",
        source_revision: str = "",
        ordinal: int = 0,
        locator: Mapping[str, Any] | None = None,
        content_role: str = "knowledge",
        sensitivity: str | None = None,
        workspace_id: str | None = None,
        agent_instance_id: str = "",
        project_ref: str = "",
        share_group_id: str = "",
        policy_class: str | None = None,
        provider: str | None = None,
        access_scope: Mapping[str, Any] | None = None,
        active: bool = True,
        conn: sqlite3.Connection | None = None,
    ) -> str:
        if text is not None and blob_id is None:
            blob_id = self.put_blob(text, namespace_id=namespace_id, conn=conn)
        if not source_object_id:
            raise ValueError("source_object_id is required")
        object_key = external_object_key or source_object_id
        now = _now()
        occurrence_id = stable_id("occ", source_object_id, occurrence_key)
        acl_anomalies: list[tuple[str, str, str]] = []
        sensitivity = "" if sensitivity is None else str(sensitivity)
        if not sensitivity:
            sensitivity = UNKNOWN_ACL
            acl_anomalies.append(("sensitivity", sensitivity, "unspecified"))
        elif sensitivity not in _ALLOWED_SENSITIVITIES:
            acl_anomalies.append(("sensitivity", sensitivity, "unsupported_value"))
        policy_class = "" if policy_class is None else str(policy_class)
        if not policy_class:
            policy_class = UNKNOWN_ACL
            acl_anomalies.append(("policy_class", policy_class, "unspecified"))
        elif policy_class not in _ALLOWED_POLICY_CLASSES:
            acl_anomalies.append(("policy_class", policy_class, "unsupported_value"))
        provider = "" if provider is None else str(provider)
        if not provider:
            provider = UNKNOWN_ACL
            acl_anomalies.append(("provider", provider, "unspecified"))
        elif provider == UNKNOWN_ACL:
            acl_anomalies.append(("provider", provider, "unsupported_value"))

        def apply(local: sqlite3.Connection) -> str:
            if source_id:
                local.execute(
                    "INSERT INTO source_connectors(source_id,workspace_id,provider,source_type,external_root_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET updated_at=excluded.updated_at",
                    (source_id, workspace_id or self.workspace_id, source_kind, source_kind, object_key, now, now),
                )
            local.execute(
                "INSERT INTO source_objects(source_object_id,source_kind,external_object_key,title,metadata_json,active,first_seen_at,last_seen_at,source_id,object_type,parent_object_id,deleted_scan_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_object_id) DO UPDATE SET active=1,last_seen_at=excluded.last_seen_at,deleted_scan_id=''",
                (source_object_id, source_kind, object_key, "", "{}", 1, now, now, source_id or "", object_type, "", ""),
            )
            if blob_id is None:
                # Empty content has no Blob by contract.  Keep the source
                # identity in migration_map; occurrences require a Blob FK in
                # the Phase 1 schema, so they are represented by no row.
                return occurrence_id
            local.execute(
                "INSERT INTO content_occurrences(occurrence_id,source_object_id,occurrence_key,blob_id,source_revision,ordinal,locator_json,content_role,sensitivity,workspace_id,agent_instance_id,project_ref,share_group_id,policy_class,provider,access_scope_json,active,first_seen_at,last_seen_at,deleted_scan_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(source_object_id,occurrence_key) DO UPDATE SET blob_id=excluded.blob_id,source_revision=excluded.source_revision,ordinal=excluded.ordinal,locator_json=excluded.locator_json,content_role=excluded.content_role,sensitivity=excluded.sensitivity,workspace_id=excluded.workspace_id,agent_instance_id=excluded.agent_instance_id,project_ref=excluded.project_ref,share_group_id=excluded.share_group_id,policy_class=excluded.policy_class,provider=excluded.provider,access_scope_json=excluded.access_scope_json,active=excluded.active,last_seen_at=excluded.last_seen_at,deleted_scan_id=''",
                (occurrence_id, source_object_id, occurrence_key, blob_id, source_revision, int(ordinal), _json(locator), content_role, sensitivity, workspace_id or self.workspace_id, agent_instance_id, project_ref, share_group_id, policy_class, provider, _json(access_scope), int(active), now, now, ""),
            )
            for field, value, reason in acl_anomalies:
                local.execute(
                    "INSERT INTO content_acl_anomalies(anomaly_id,occurrence_id,field,value,reason,created_at) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(occurrence_id,field) DO UPDATE SET value=excluded.value,reason=excluded.reason,created_at=excluded.created_at",
                    (
                        stable_id("acl-anomaly", occurrence_id, field),
                        occurrence_id,
                        field,
                        value,
                        reason,
                        now,
                    ),
                )
            local.execute("UPDATE content_tombstones SET active=0,restored_at=? WHERE source_object_id=? AND occurrence_id=? AND active=1", (now, source_object_id, occurrence_id))
            return occurrence_id

        if conn is not None:
            return apply(conn)
        with open_database(self.db_path) as local:
            with transaction(local):
                return apply(local)

    def get_occurrence(
        self, occurrence_id: str, scope: ContentReadScope | None = None
    ) -> Occurrence | None:
        """Read occurrence only when every scope component matches exactly."""

        if not _scope_is_readable(scope):
            return None
        with self.connection() as conn:
            row = conn.execute(
                "SELECT o.occurrence_id,o.source_object_id,o.occurrence_key,o.blob_id,o.active "
                "FROM content_occurrences o JOIN content_blobs b ON b.blob_id=o.blob_id "
                "WHERE o.occurrence_id=? AND b.namespace_id=? AND o.active=1 "
                "AND o.workspace_id=? AND o.agent_instance_id=? AND o.project_ref=? "
                "AND o.provider=? AND o.share_group_id=? AND o.sensitivity=? "
                "AND o.policy_class=? LIMIT 1",
                (
                    occurrence_id,
                    scope.namespace_id,
                    scope.workspace_id,
                    scope.agent_instance_id,
                    scope.project_ref,
                    scope.provider,
                    scope.share_group_id,
                    scope.sensitivity,
                    scope.policy_class,
                ),
            ).fetchone()
        if row is None:
            return None
        return Occurrence(str(row[0]), str(row[1]), str(row[2]), str(row[3]), bool(row[4]))

    def hold_blob(self, blob_id: str, *, reason: str, source_ref: str = "", conn: sqlite3.Connection | None = None) -> str:
        hold_id = stable_id("hold", blob_id, reason, source_ref)
        now = _now()
        def apply(local: sqlite3.Connection) -> str:
            local.execute("INSERT INTO content_holds(hold_id,blob_id,reason,source_ref,active,created_at,released_at) VALUES(?,?,?,?,1,?, '') ON CONFLICT(blob_id,reason,source_ref) DO UPDATE SET active=1,released_at=''", (hold_id, blob_id, reason, source_ref, now))
            return hold_id
        if conn is not None:
            return apply(conn)
        with open_database(self.db_path) as local:
            with transaction(local):
                return apply(local)

    def tombstone_occurrence(self, occurrence_id: str, *, reason: str, scan_id: str = "", metadata: Mapping[str, Any] | None = None, conn: sqlite3.Connection | None = None) -> str:
        now = _now()
        tombstone_id = stable_id("tomb", occurrence_id, reason)
        def apply(local: sqlite3.Connection) -> str:
            row = local.execute("SELECT source_object_id,blob_id FROM content_occurrences WHERE occurrence_id=?", (occurrence_id,)).fetchone()
            if row is None:
                raise ContentError(f"unknown occurrence: {occurrence_id}")
            local.execute("UPDATE content_occurrences SET active=0,deleted_scan_id=?,last_seen_at=? WHERE occurrence_id=?", (scan_id, now, occurrence_id))
            local.execute("INSERT INTO content_tombstones(tombstone_id,source_object_id,occurrence_id,blob_id,reason,scan_id,metadata_json,created_at,restored_at,active) VALUES(?,?,?,?,?,?,?,?,?,1) ON CONFLICT(source_object_id,occurrence_id,reason) DO UPDATE SET scan_id=excluded.scan_id,metadata_json=excluded.metadata_json,active=1,restored_at=''", (tombstone_id, row[0], occurrence_id, row[1], reason, scan_id, _json(metadata), now, ""))
            self.hold_blob(str(row[1]), reason=reason, source_ref=occurrence_id, conn=local)
            return tombstone_id
        if conn is not None:
            return apply(conn)
        with open_database(self.db_path) as local:
            with transaction(local):
                return apply(local)

    def list_source_connectors(
        self,
        *,
        workspace_id: str | None = None,
        enabled: bool | None = None,
    ) -> list[dict[str, Any]]:
        """Return reference-only source connector metadata."""
        predicates = ["workspace_id=?"]
        params: list[Any] = [str(workspace_id or self.workspace_id)]
        if enabled is not None:
            predicates.append("enabled=?")
            params.append(1 if enabled else 0)
        with open_database(self.db_path, readonly=True) as conn:
            rows = conn.execute(
                "SELECT source_id,workspace_id,provider,source_type,external_root_key,enabled,created_at,updated_at "
                "FROM source_connectors WHERE " + " AND ".join(predicates) + " ORDER BY source_id",
                tuple(params),
            ).fetchall()
            return [dict(row) for row in rows]

    def set_source_connector_enabled(
        self,
        source_id: str,
        enabled: bool,
        *,
        workspace_id: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        """Toggle one exact source connector without touching source bodies."""
        target_workspace = str(workspace_id or self.workspace_id)
        now = _now()

        def apply(local: sqlite3.Connection) -> bool:
            cur = local.execute(
                "UPDATE source_connectors SET enabled=?,updated_at=? WHERE source_id=? AND workspace_id=?",
                (1 if bool(enabled) else 0, now, str(source_id), target_workspace),
            )
            return int(cur.rowcount or 0) == 1

        if conn is not None:
            return apply(conn)
        with open_database(self.db_path) as local:
            with transaction(local):
                return apply(local)

    def restore_tombstone(
        self,
        tombstone_id: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, str]:
        """Restore one tombstoned occurrence and release its recovery hold.

        The Blob is never rewritten.  Restoring only reactivates the existing
        occurrence/reference and records ``restored_at`` so Content remains the
        sole body authority.
        """
        now = _now()

        def apply(local: sqlite3.Connection) -> dict[str, str]:
            row = local.execute(
                "SELECT occurrence_id,blob_id,reason FROM content_tombstones "
                "WHERE tombstone_id=? AND active=1",
                (str(tombstone_id),),
            ).fetchone()
            if row is None:
                raise ContentError(f"unknown active tombstone: {tombstone_id}")
            occurrence_id, blob_id, reason = str(row[0]), str(row[1]), str(row[2])
            restored = local.execute(
                "UPDATE content_occurrences SET active=1,deleted_scan_id='',last_seen_at=? "
                "WHERE occurrence_id=?",
                (now, occurrence_id),
            )
            local.execute(
                "UPDATE content_tombstones SET active=0,restored_at=? WHERE tombstone_id=?",
                (now, str(tombstone_id)),
            )
            released = local.execute(
                "UPDATE content_holds SET active=0,released_at=? "
                "WHERE blob_id=? AND reason=? AND source_ref=? AND active=1",
                (now, blob_id, reason, occurrence_id),
            )
            return {
                "occurrence_id": occurrence_id,
                "blob_id": blob_id,
                "tombstone_id": str(tombstone_id),
                "restored_occurrence": int(restored.rowcount or 0),
                "released_holds": int(released.rowcount or 0),
            }

        if conn is not None:
            return apply(conn)
        with open_database(self.db_path) as local:
            with transaction(local):
                return apply(local)

    def purge_tombstone(
        self,
        tombstone_id: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, str]:
        """Release recovery metadata without directly deleting a Blob.

        Physical orphan reclamation remains the responsibility of guarded V2
        maintenance.  This operation only removes the recovery hold and marks
        the tombstone non-active, so purge cannot race references in another
        domain.
        """
        now = _now()

        def apply(local: sqlite3.Connection) -> dict[str, str]:
            row = local.execute(
                "SELECT occurrence_id,blob_id,reason FROM content_tombstones "
                "WHERE tombstone_id=?",
                (str(tombstone_id),),
            ).fetchone()
            if row is None:
                raise ContentError(f"unknown tombstone: {tombstone_id}")
            occurrence_id, blob_id, reason = str(row[0]), str(row[1]), str(row[2])
            released = local.execute(
                "UPDATE content_holds SET active=0,released_at=? "
                "WHERE blob_id=? AND reason=? AND source_ref=? AND active=1",
                (now, blob_id, reason, occurrence_id),
            )
            local.execute(
                "UPDATE content_tombstones SET active=0,restored_at=CASE "
                "WHEN restored_at='' THEN ? ELSE restored_at END WHERE tombstone_id=?",
                (now, str(tombstone_id)),
            )
            return {
                "occurrence_id": occurrence_id,
                "blob_id": blob_id,
                "tombstone_id": str(tombstone_id),
                "released_holds": int(released.rowcount or 0),
            }

        if conn is not None:
            return apply(conn)
        with open_database(self.db_path) as local:
            with transaction(local):
                return apply(local)

    def counts(self) -> dict[str, int]:
        tables = ("content_namespaces", "content_blobs", "source_objects", "content_occurrences", "conversation_sessions", "conversation_turns", "conversation_summaries", "conversation_observations", "content_evidence_links", "history_mutation_receipts", "content_holds", "content_tombstones", "migration_map", "knowledge_records", "knowledge_relations", "content_acl_anomalies")
        with open_database(self.db_path, readonly=True) as conn:
            return {table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in tables}

    table_counts = counts

    def integrity_check(self) -> list[str]:
        with open_database(self.db_path, readonly=True) as conn:
            return [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]


__all__ = [
    "Blob", "ContentCollisionError", "ContentError", "ContentReadScope", "ContentStore", "Namespace", "Occurrence", "NORMALIZER_ID", "SCHEMA_VERSION", "UNKNOWN_ACL", "acl_digest", "canonicalize_text", "register_acl_values", "stable_id",
]
