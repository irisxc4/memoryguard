"""Minimal V2 Phase-1 schemas and migration bootstrap.

The tables here are durable storage contracts, not service implementations.
Business services may add indexes or derived tables in later migrations, but
the phase-1 marker and foreign-key boundaries remain stable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from .database import connect_database, execute_sql_script, open_database
from .layout import LayoutError, WorkspaceV2Layout
from .transaction import transaction


SCHEMA_VERSION = 1
SCHEMA_MARKER = "memoryguard-v2-phase1"


class SchemaError(RuntimeError):
    """A database is not a supported, intact V2 schema."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


COMMON_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    domain TEXT PRIMARY KEY,
    version INTEGER NOT NULL CHECK (version >= 1),
    marker TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


RUNTIME_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_runs (
    run_id TEXT PRIMARY KEY,
    task_type TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued','running','succeeded','failed','cancelled')),
    requested_by TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    error_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_nodes (
    node_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    parent_node_id TEXT DEFAULT NULL,
    node_type TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending','running','succeeded','failed','skipped')),
    input_json TEXT NOT NULL DEFAULT '{}',
    output_json TEXT NOT NULL DEFAULT '{}',
    error_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES task_runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY (parent_node_id) REFERENCES task_nodes(node_id)
);
CREATE INDEX IF NOT EXISTS idx_task_nodes_run ON task_nodes(run_id);
CREATE TABLE IF NOT EXISTS tool_refs (
    tool_ref_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    node_id TEXT DEFAULT NULL,
    provider TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    request_digest TEXT NOT NULL DEFAULT '',
    response_digest TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES task_runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY (node_id) REFERENCES task_nodes(node_id)
);
"""


MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_atoms (
    atom_id TEXT PRIMARY KEY,
    canonical_hash TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    body_ref TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','contested','unsupported','superseded','deleted')),
    injection_policy TEXT NOT NULL DEFAULT 'relevant'
        CHECK (injection_policy IN ('relevant','always')),
    priority INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


RULES_SCHEMA = """
CREATE TABLE IF NOT EXISTS rule_definitions (
    definition_id TEXT PRIMARY KEY,
    rule_key TEXT NOT NULL UNIQUE,
    text TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active'
        CHECK (state IN ('active','disabled','deleted','superseded')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rule_bindings (
    binding_id TEXT PRIMARY KEY,
    definition_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL DEFAULT '',
    project_ref TEXT NOT NULL DEFAULT '',
    effect TEXT NOT NULL DEFAULT 'include'
        CHECK (effect IN ('include','exclude')),
    priority INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (definition_id, target_type, target_id, project_ref),
    FOREIGN KEY (definition_id) REFERENCES rule_definitions(definition_id)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_rule_bindings_target
    ON rule_bindings(target_type, target_id, project_ref);
"""


EVIDENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    evidence_type TEXT NOT NULL,
    source_ref TEXT NOT NULL DEFAULT '',
    source_revision TEXT NOT NULL DEFAULT '',
    digest TEXT NOT NULL DEFAULT '',
    authority TEXT NOT NULL DEFAULT 'observed',
    status TEXT NOT NULL DEFAULT 'valid'
        CHECK (status IN ('valid','stale','superseded','source_deleted','invalidated')),
    observed_at TEXT NOT NULL,
    invalidated_at TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS evidence_links (
    link_id TEXT PRIMARY KEY,
    evidence_id TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    relation TEXT NOT NULL DEFAULT 'supports',
    created_at TEXT NOT NULL,
    UNIQUE (evidence_id, subject_type, subject_id, relation),
    FOREIGN KEY (evidence_id) REFERENCES evidence(evidence_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_evidence_links_subject
    ON evidence_links(subject_type, subject_id);
"""


CONTENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS content_namespaces (
    namespace_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    trust_domain TEXT NOT NULL,
    sensitivity TEXT NOT NULL,
    retention_authority TEXT NOT NULL,
    canonicalization_version INTEGER NOT NULL CHECK (canonicalization_version >= 1),
    created_at TEXT NOT NULL,
    UNIQUE (workspace_id, trust_domain, sensitivity, retention_authority,
            canonicalization_version)
);
CREATE TABLE IF NOT EXISTS content_blobs (
    blob_id TEXT PRIMARY KEY,
    namespace_id TEXT NOT NULL,
    canonical_hash TEXT NOT NULL,
    normalizer_id TEXT NOT NULL,
    text TEXT NOT NULL,
    byte_count INTEGER NOT NULL CHECK (byte_count >= 0),
    char_count INTEGER NOT NULL CHECK (char_count >= 0),
    language_hint TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (namespace_id, normalizer_id, canonical_hash),
    FOREIGN KEY (namespace_id) REFERENCES content_namespaces(namespace_id)
);
CREATE TABLE IF NOT EXISTS raw_content (
    raw_content_id TEXT PRIMARY KEY,
    blob_id TEXT NOT NULL,
    source_ref TEXT NOT NULL DEFAULT '',
    content_kind TEXT NOT NULL DEFAULT 'document',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (blob_id) REFERENCES content_blobs(blob_id)
);
CREATE TABLE IF NOT EXISTS source_objects (
    source_object_id TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL,
    external_object_key TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE (source_kind, external_object_key)
);
CREATE TABLE IF NOT EXISTS content_occurrences (
    occurrence_id TEXT PRIMARY KEY,
    source_object_id TEXT NOT NULL,
    occurrence_key TEXT NOT NULL,
    blob_id TEXT NOT NULL,
    source_revision TEXT NOT NULL DEFAULT '',
    ordinal INTEGER NOT NULL DEFAULT 0 CHECK (ordinal >= 0),
    locator_json TEXT NOT NULL DEFAULT '{}',
    content_role TEXT NOT NULL DEFAULT 'knowledge',
    sensitivity TEXT NOT NULL DEFAULT 'normal',
    workspace_id TEXT NOT NULL,
    agent_instance_id TEXT NOT NULL DEFAULT '',
    project_ref TEXT NOT NULL DEFAULT '',
    share_group_id TEXT NOT NULL DEFAULT '',
    policy_class TEXT NOT NULL DEFAULT 'private',
    access_scope_json TEXT NOT NULL DEFAULT '{}',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE (source_object_id, occurrence_key),
    FOREIGN KEY (source_object_id) REFERENCES source_objects(source_object_id),
    FOREIGN KEY (blob_id) REFERENCES content_blobs(blob_id)
);
CREATE TABLE IF NOT EXISTS conversation_turns (
    turn_id TEXT PRIMARY KEY,
    occurrence_id TEXT NOT NULL,
    session_ref TEXT NOT NULL,
    role TEXT NOT NULL,
    ordinal INTEGER NOT NULL DEFAULT 0 CHECK (ordinal >= 0),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (session_ref, ordinal),
    FOREIGN KEY (occurrence_id) REFERENCES content_occurrences(occurrence_id)
);
CREATE INDEX IF NOT EXISTS idx_content_occurrences_scope
    ON content_occurrences(workspace_id, agent_instance_id, project_ref,
                           share_group_id, policy_class, active);
"""


KNOWLEDGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_assets (
    asset_id TEXT PRIMARY KEY,
    asset_type TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    source_ref TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','deleted','restoring','purged')),
    policy_class TEXT NOT NULL DEFAULT 'private',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_documents (
    document_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL,
    path TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (asset_id, path),
    FOREIGN KEY (asset_id) REFERENCES knowledge_assets(asset_id) ON DELETE CASCADE
);
"""


CODEGRAPH_SCHEMA = """
CREATE TABLE IF NOT EXISTS codegraph_nodes (
    node_id TEXT PRIMARY KEY,
    node_kind TEXT NOT NULL,
    stable_key TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL DEFAULT '',
    source_ref TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS codegraph_edges (
    edge_id TEXT PRIMARY KEY,
    from_node_id TEXT NOT NULL,
    to_node_id TEXT NOT NULL,
    edge_kind TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (from_node_id, to_node_id, edge_kind),
    FOREIGN KEY (from_node_id) REFERENCES codegraph_nodes(node_id) ON DELETE CASCADE,
    FOREIGN KEY (to_node_id) REFERENCES codegraph_nodes(node_id) ON DELETE CASCADE,
    CHECK (from_node_id <> to_node_id OR edge_kind = 'self')
);
CREATE INDEX IF NOT EXISTS idx_codegraph_edges_to ON codegraph_edges(to_node_id);
"""


ASSETS_SCHEMA = """
CREATE TABLE IF NOT EXISTS asset_registry (
    asset_id TEXT PRIMARY KEY,
    asset_kind TEXT NOT NULL,
    path TEXT NOT NULL,
    digest TEXT NOT NULL,
    media_type TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0 CHECK (size_bytes >= 0),
    state TEXT NOT NULL DEFAULT 'active'
        CHECK (state IN ('active','missing','quarantined','deleted')),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (asset_kind, path, digest)
);
"""


SCENARIO_SCHEMA = """
CREATE TABLE IF NOT EXISTS scenario_projections (
    projection_id TEXT PRIMARY KEY,
    scenario_key TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0),
    source_digest TEXT NOT NULL DEFAULT '',
    projection_digest TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'building'
        CHECK (status IN ('building','ready','failed','stale')),
    payload_json TEXT NOT NULL DEFAULT '{}',
    error_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (scenario_key, generation)
);
"""


PROFILE_SCHEMA = """
CREATE TABLE IF NOT EXISTS profile_projections (
    projection_id TEXT PRIMARY KEY,
    profile_key TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0),
    source_digest TEXT NOT NULL DEFAULT '',
    projection_digest TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'building'
        CHECK (status IN ('building','ready','failed','stale')),
    payload_json TEXT NOT NULL DEFAULT '{}',
    error_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (profile_key, generation)
);
"""


SYSTEM_SCHEMA = """
CREATE TABLE IF NOT EXISTS manifest (
    manifest_id TEXT PRIMARY KEY,
    state TEXT NOT NULL
        CHECK (state IN ('V1_ACTIVE','V2_BUILDING','V2_READY','V2_ACTIVE')),
    generation INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0),
    migration_id TEXT NOT NULL DEFAULT '',
    source_digest TEXT NOT NULL DEFAULT '',
    target_digest TEXT NOT NULL DEFAULT '',
    manifest_digest TEXT NOT NULL DEFAULT '',
    digests_json TEXT NOT NULL DEFAULT '{}',
    errors_json TEXT NOT NULL DEFAULT '{}',
    last_error TEXT NOT NULL DEFAULT '',
    workspace_source_pointer TEXT NOT NULL DEFAULT '',
    global_source_pointer TEXT NOT NULL DEFAULT '',
    data_home_root TEXT NOT NULL DEFAULT '',
    checkpoints_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS migration_ledger (
    transition_id TEXT PRIMARY KEY,
    migration_id TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 0),
    source_digest TEXT NOT NULL DEFAULT '',
    target_digest TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('started','completed','failed')),
    error_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT '',
    UNIQUE (migration_id, generation, to_state)
);
CREATE TABLE IF NOT EXISTS outbox_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    last_sequence INTEGER NOT NULL DEFAULT 0 CHECK (last_sequence >= 0),
    updated_at TEXT NOT NULL
);
"""


DOMAIN_SCHEMAS: Mapping[str, str] = {
    "runtime": RUNTIME_SCHEMA,
    "memory": MEMORY_SCHEMA,
    "rules": RULES_SCHEMA,
    "evidence": EVIDENCE_SCHEMA,
    "content": CONTENT_SCHEMA,
    "knowledge": KNOWLEDGE_SCHEMA,
    "codegraph": CODEGRAPH_SCHEMA,
    "assets": ASSETS_SCHEMA,
    "projection.scenario": SCENARIO_SCHEMA,
    "projection.profile": PROFILE_SCHEMA,
    "system": SYSTEM_SCHEMA,
}


def _schema_key(domain: str, path: Path | None = None) -> str:
    if domain == "projection" and path is not None:
        return "projection.profile" if path.name == "profile.db" else "projection.scenario"
    if domain not in DOMAIN_SCHEMAS:
        raise ValueError(f"unknown V2 schema domain: {domain!r}")
    return domain


def _marker_domain(domain: str, path: Path | None = None) -> str:
    key = _schema_key(domain, path)
    return key


def _schema_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _check_schema_meta(
    conn: sqlite3.Connection,
    domain: str,
    *,
    path: Path | None = None,
    allow_fresh: bool = True,
) -> bool:
    """Validate schema metadata before any DDL or upsert can run.

    ``True`` means the database is a fresh, empty SQLite file.  Existing
    databases must carry exactly one supported marker row; an absent,
    mismatched, old, or future marker is a hard error rather than an upgrade
    opportunity.  This is deliberately stricter than ``CREATE IF NOT EXISTS``
    because a V2 writer must never downgrade a database it does not understand.
    """

    key = _schema_key(domain, path)
    tables = _schema_tables(conn)
    if "schema_meta" not in tables:
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if tables or user_version:
            raise SchemaError(
                f"V2 schema metadata is missing for {key!r}; refusing to infer or migrate"
            )
        if not allow_fresh:
            raise SchemaError(f"fresh schema is not allowed for {key!r}")
        return True

    try:
        rows = conn.execute(
            "SELECT domain, version, marker FROM schema_meta"
        ).fetchall()
    except sqlite3.Error as exc:
        raise SchemaError(f"cannot read V2 schema metadata for {key!r}") from exc
    if not rows:
        raise SchemaError(f"schema metadata row is missing for {key!r}")

    expected_rows = 0
    for row in rows:
        try:
            row_domain = str(row[0])
            row_version = int(row[1])
            row_marker = str(row[2])
        except (TypeError, ValueError, IndexError) as exc:
            raise SchemaError(f"malformed V2 schema metadata for {key!r}") from exc
        if row_domain != key:
            raise SchemaError(
                f"schema metadata domain mismatch: expected {key!r}, got {row_domain!r}"
            )
        if row_marker != SCHEMA_MARKER:
            raise SchemaError(
                f"schema marker mismatch for {key!r}: {row_marker!r}"
            )
        if row_version != SCHEMA_VERSION:
            direction = "future" if row_version > SCHEMA_VERSION else "unsupported"
            raise SchemaError(
                f"{direction} V2 schema version for {key!r}: {row_version}"
            )
        expected_rows += 1
    if expected_rows != 1:
        raise SchemaError(f"schema metadata must contain one row for {key!r}")

    user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if user_version != SCHEMA_VERSION:
        direction = "future" if user_version > SCHEMA_VERSION else "unsupported"
        raise SchemaError(
            f"{direction} SQLite user_version for {key!r}: {user_version}"
        )
    return False


def _validate_schema_file(path: Path, domain: str, *, immutable: bool = False) -> None:
    """Read-only preflight used before opening an existing DB for writes."""

    if not path.is_file():
        return
    try:
        with open_database(path, readonly=True, immutable=immutable) as conn:
            _check_schema_meta(conn, domain, path=path, allow_fresh=True)
    except SchemaError:
        raise
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise SchemaError(f"cannot inspect existing V2 database: {path}") from exc


def _apply_schema(
    conn: sqlite3.Connection,
    domain: str,
    *,
    path: Path | None = None,
    now: str | None = None,
) -> None:
    now = now or _now()
    key = _schema_key(domain, path)
    _check_schema_meta(conn, key, path=path)
    execute_sql_script(conn, COMMON_SCHEMA + DOMAIN_SCHEMAS[key])
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    marker_domain = _marker_domain(domain, path)
    conn.execute(
        "INSERT INTO schema_meta(domain, version, marker, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(domain) DO UPDATE SET version=excluded.version, marker=excluded.marker, "
        "updated_at=excluded.updated_at",
        (marker_domain, SCHEMA_VERSION, SCHEMA_MARKER, now),
    )
    if key == "system":
        conn.execute(
            "INSERT INTO manifest(manifest_id, state, generation, created_at, updated_at) "
            "VALUES ('workspace', 'V1_ACTIVE', 0, ?, ?) "
            "ON CONFLICT(manifest_id) DO NOTHING",
            (now, now),
        )
        conn.execute(
            "INSERT INTO outbox_checkpoints(checkpoint_id, domain, last_sequence, updated_at) "
            "VALUES ('system', 'system', 0, ?) ON CONFLICT(checkpoint_id) DO NOTHING",
            (now,),
        )


def initialize_database(
    path: str | Path,
    domain: str,
    *,
    layout: WorkspaceV2Layout | None = None,
    readonly: bool = False,
    read_only: bool | None = None,
) -> dict[str, Any]:
    """Create/migrate one domain, or inspect it without mutation in RO mode."""

    if read_only is not None:
        readonly = bool(read_only)
    db_path = Path(path).expanduser().resolve()
    schema_key = _schema_key(domain, db_path)
    if not readonly:
        if layout is None:
            raise LayoutError(
                "V2 schema writes require a WorkspaceV2Layout; "
                "use connect_database for low-level standalone SQLite"
            )
        layout.assert_database_path(db_path, domain)
    if readonly:
        with open_database(db_path, readonly=True) as conn:
            _check_schema_meta(conn, schema_key, path=db_path, allow_fresh=False)
            row = conn.execute(
                "SELECT domain, version, marker, updated_at FROM schema_meta WHERE domain=?",
                (_marker_domain(domain, db_path),),
            ).fetchone()
            return dict(row) if row is not None else {}
    # Preflight an existing file through a read-only URI.  In particular, a
    # future-version probe must not run the writable connection's WAL pragma or
    # any DDL before failing closed.
    _validate_schema_file(db_path, schema_key)
    conn = connect_database(db_path, readonly=False)
    try:
        with transaction(conn):
            _apply_schema(conn, schema_key, path=db_path)
        row = conn.execute(
            "SELECT domain, version, marker, updated_at FROM schema_meta WHERE domain=?",
            (_marker_domain(schema_key, db_path),),
        ).fetchone()
        return dict(row) if row is not None else {}
    finally:
        conn.close()


def initialize_domain(
    layout: WorkspaceV2Layout,
    domain: str,
    *,
    readonly: bool = False,
    read_only: bool | None = None,
) -> tuple[dict[str, Any], ...]:
    """Initialize all database files belonging to one layout domain."""

    if read_only is not None:
        readonly = bool(read_only)
    paths = layout.db_paths(domain)
    assert isinstance(paths, tuple)
    if not readonly:
        layout.ensure_dirs()
    return tuple(
        initialize_database(path, domain, layout=layout, readonly=readonly)
        for path in paths
    )


def initialize_all(
    layout: WorkspaceV2Layout,
    *,
    readonly: bool = False,
    read_only: bool | None = None,
) -> dict[str, tuple[dict[str, Any], ...]]:
    """Initialize every Phase-1 database and return its schema markers."""

    if read_only is not None:
        readonly = bool(read_only)
    if not readonly:
        layout.ensure_dirs()
    result: dict[str, tuple[dict[str, Any], ...]] = {}
    for domain in layout.DOMAINS:
        result[domain] = initialize_domain(layout, domain, readonly=readonly)
    return result


# Compatibility aliases from the initial Phase-1 integration draft.
initialize = initialize_all
bootstrap = initialize_all
