"""Fail-closed native read service for the V2 knowledge surfaces.

The service deliberately keeps the MCP/GUI boundary independent from the
legacy ``KnowledgeStore``.  Books are projected from the V2 Content Plane as
reference-only envelopes.  Candidate rows use an optional, explicitly marked
V2 table in the workspace ``knowledge.db``; the read path never creates or
migrates that table.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Any
from urllib.parse import quote

from ..content.store import ContentReadScope, ContentStore, UNKNOWN_ACL
from ..knowledge_v2.adapter import KnowledgeV2Adapter, _safe_public_text, _safe_summary
from ..storage.layout import WorkspaceV2Layout
from ..storage.schema import SCHEMA_MARKER, SCHEMA_VERSION


KNOWLEDGE_CANDIDATE_SCHEMA_VERSION = 1
KNOWLEDGE_CANDIDATE_META = "knowledge_v2_schema_meta"
KNOWLEDGE_CANDIDATE_TABLE = "knowledge_v2_candidates"

# This is a migration/fixture contract, not an instruction for the read
# service to execute.  A future writer may install it in one transaction.
# Keeping it here lets migration tests create a V2 candidate plane without
# importing the legacy global KnowledgeStore.
KNOWLEDGE_CANDIDATE_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {KNOWLEDGE_CANDIDATE_META} (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS {KNOWLEDGE_CANDIDATE_TABLE} (
    candidate_id TEXT PRIMARY KEY,
    namespace_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    agent_instance_id TEXT NOT NULL,
    project_ref TEXT NOT NULL,
    provider TEXT NOT NULL,
    share_group_id TEXT NOT NULL,
    sensitivity TEXT NOT NULL,
    policy_class TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    reference TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    source_occurrence_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_v2_candidates_scope
    ON {KNOWLEDGE_CANDIDATE_TABLE}(
        namespace_id, workspace_id, agent_instance_id, project_ref,
        provider, share_group_id, sensitivity, policy_class, status
    );
"""

_CONTENT_REQUIRED_TABLES = frozenset(
    {
        "content_namespaces",
        "content_blobs",
        "source_objects",
        "content_occurrences",
        "conversation_turns",
    }
)
_CONTENT_REQUIRED_COLUMNS = {
    "content_namespaces": frozenset({"namespace_id", "workspace_id"}),
    "content_blobs": frozenset({"blob_id", "namespace_id", "canonical_hash"}),
    "source_objects": frozenset({"source_object_id", "title", "object_type"}),
    "content_occurrences": frozenset(
        {
            "occurrence_id",
            "source_object_id",
            "blob_id",
            "locator_json",
            "content_role",
            "workspace_id",
            "agent_instance_id",
            "project_ref",
            "provider",
            "share_group_id",
            "sensitivity",
            "policy_class",
            "active",
        }
    ),
}
_CONTENT_AUX_REQUIRED_TABLES = frozenset(
    {
        "content_schema_meta",
        "source_connectors",
        "conversation_sessions",
        "conversation_summaries",
        "conversation_observations",
        "content_evidence_links",
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
_CONTENT_AUX_REQUIRED_COLUMNS = {
    "source_objects": frozenset({"source_id", "object_type", "parent_object_id", "deleted_scan_id"}),
    "content_occurrences": frozenset({"deleted_scan_id", "policy_class", "provider"}),
    "conversation_turns": frozenset({"session_id", "event_key", "content_type", "source_revision"}),
    "content_evidence_links": frozenset({"blob_id", "source_revision"}),
    "source_sync_state": frozenset(
        {
            "owner_id",
            "cursor_digest",
            "cursor_source_id",
            "cursor_run_id",
            "cursor_owner_id",
            "cursor_revision",
            "cursor_position",
            "cursor_batch_digest",
            "expected_revision",
            "expected_manifest_digest",
        }
    ),
}
_KNOWLEDGE_REQUIRED_TABLES = frozenset({"schema_meta", "knowledge_assets", "knowledge_documents"})
_CANDIDATE_REQUIRED_COLUMNS = frozenset(
    {
        "candidate_id",
        "namespace_id",
        "workspace_id",
        "agent_instance_id",
        "project_ref",
        "provider",
        "share_group_id",
        "sensitivity",
        "policy_class",
        "status",
        "summary",
        "reference",
        "content_hash",
        "source_occurrence_id",
    }
)


class KnowledgeV2ServiceError(RuntimeError):
    """Stable public code; no filesystem, SQL, or exception detail leaks."""

    def __init__(self, code: str) -> None:
        self.code = str(code or "knowledge_v2_unavailable")
        super().__init__(self.code)


class KnowledgeV2SchemaError(KnowledgeV2ServiceError):
    """The existing V2 database is missing or has an unsupported schema."""


class KnowledgeV2Unavailable(KnowledgeV2ServiceError):
    """The requested V2 read plane is not installed or cannot be opened."""


@contextmanager
def _immutable_ro_connection(path: Path):
    """Open SQLite without creating a WAL/SHM sidecar on a cold database."""

    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    wal = Path(str(resolved) + "-wal")
    query = "mode=ro" if wal.exists() else "mode=ro&immutable=1"
    uri = "file:" + quote(str(resolved), safe="/:\\") + "?" + query
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        yield conn
    finally:
        conn.close()


class _ImmutableContentStore(ContentStore):
    """ContentStore identity with an immutable read-only connection factory."""

    @contextmanager
    def connection(self):  # type: ignore[override]
        with _immutable_ro_connection(self.db_path) as conn:
            yield conn


def _safe_limit(value: Any, default: int = 100) -> int:
    if isinstance(value, bool):
        raise KnowledgeV2ServiceError("invalid_limit")
    try:
        return max(1, min(500, int(value)))
    except (TypeError, ValueError) as exc:
        raise KnowledgeV2ServiceError("invalid_limit") from exc


def _valid_scope(scope: ContentReadScope | None) -> bool:
    if not isinstance(scope, ContentReadScope):
        return False
    values = (
        scope.namespace_id,
        scope.workspace_id,
        scope.agent_instance_id,
        scope.project_ref,
        scope.provider,
        scope.share_group_id,
        scope.sensitivity,
        scope.policy_class,
    )
    return all(
        isinstance(value, str)
        and value == value.strip()
        and bool(value)
        and value != UNKNOWN_ACL
        for value in values
    )


def _scope_or_raise(scope: ContentReadScope | None) -> ContentReadScope:
    if not _valid_scope(scope):
        raise KnowledgeV2ServiceError("knowledge_scope_required")
    assert scope is not None
    return scope


def _error(service: str, code: str) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "BLOCKED",
        "service": service,
        "code": str(code),
        "error": str(code),
    }


class KnowledgeV2ReadonlyService:
    """Reference-only V2 book/candidate service.

    ``initialize=False`` is intentional: constructing the service and every
    operation are side-effect free.  Schema validation is performed through
    SQLite ``mode=ro`` handles before any query is issued.
    """

    service_name = "knowledge_v2"

    def __init__(self, workspace: str | Path) -> None:
        self.layout = WorkspaceV2Layout(Path(workspace))
        self.workspace = self.layout.workspace

    @property
    def content_db(self) -> Path:
        return self.layout.content_db

    @property
    def knowledge_db(self) -> Path:
        return self.layout.knowledge_db

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
        try:
            return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error as exc:
            raise KnowledgeV2SchemaError("content_schema_unavailable") from exc

    @staticmethod
    def _validate_phase1_marker(
        conn: sqlite3.Connection,
        domain: str,
        required_tables: frozenset[str],
    ) -> set[str]:
        """Validate Phase-1 metadata without ``initialize_database``.

        ``initialize_database(..., readonly=True)`` performs a harmless
        metadata read but SQLite may materialize an empty WAL sidecar on some
        Windows builds.  Native read surfaces must keep the target entirely
        sidecar-stable, so this service performs the equivalent checks on one
        explicit ``mode=ro`` connection.
        """

        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if not (required_tables | {"schema_meta"}) <= tables:
            raise KnowledgeV2SchemaError(f"{domain}_schema_incomplete")
        rows = conn.execute(
            "SELECT domain,version,marker FROM schema_meta ORDER BY domain"
        ).fetchall()
        if len(rows) != 1:
            raise KnowledgeV2SchemaError(f"{domain}_schema_unavailable")
        row_domain, row_version, row_marker = str(rows[0][0]), rows[0][1], str(rows[0][2])
        if row_domain != domain or row_marker != SCHEMA_MARKER:
            raise KnowledgeV2SchemaError(f"{domain}_schema_unavailable")
        try:
            version = int(row_version)
        except (TypeError, ValueError) as exc:
            raise KnowledgeV2SchemaError(f"{domain}_schema_unavailable") from exc
        if version != SCHEMA_VERSION:
            code = f"{domain}_schema_future" if version > SCHEMA_VERSION else f"{domain}_schema_unsupported"
            raise KnowledgeV2SchemaError(code)
        try:
            user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        except (TypeError, ValueError, sqlite3.Error) as exc:
            raise KnowledgeV2SchemaError(f"{domain}_schema_unavailable") from exc
        if user_version != SCHEMA_VERSION:
            code = f"{domain}_schema_future" if user_version > SCHEMA_VERSION else f"{domain}_schema_unsupported"
            raise KnowledgeV2SchemaError(code)
        return tables

    def _content_adapter(self) -> KnowledgeV2Adapter:
        if not self.content_db.is_file():
            raise KnowledgeV2Unavailable("content_db_missing")
        store = _ImmutableContentStore(self.workspace, initialize=False)
        try:
            with store.connection() as conn:
                tables = self._validate_phase1_marker(
                    conn, "content", _CONTENT_AUX_REQUIRED_TABLES | _CONTENT_REQUIRED_TABLES
                )
                meta_rows = conn.execute(
                    "SELECT key,value FROM content_schema_meta ORDER BY key"
                ).fetchall()
                if len(meta_rows) != 1 or str(meta_rows[0][0]) != "version":
                    raise KnowledgeV2SchemaError("content_schema_unavailable")
                aux_version = str(meta_rows[0][1])
                if aux_version != "2":
                    code = "content_schema_future" if aux_version.isdigit() and int(aux_version) > 2 else "content_schema_unsupported"
                    raise KnowledgeV2SchemaError(code)
                if not _CONTENT_REQUIRED_TABLES <= tables:
                    raise KnowledgeV2SchemaError("content_schema_incomplete")
                if not _CONTENT_AUX_REQUIRED_TABLES <= tables:
                    raise KnowledgeV2SchemaError("content_schema_incomplete")
                for table, required in _CONTENT_REQUIRED_COLUMNS.items():
                    if not required <= self._table_columns(conn, table):
                        raise KnowledgeV2SchemaError("content_schema_incomplete")
                for table, required in _CONTENT_AUX_REQUIRED_COLUMNS.items():
                    if not required <= self._table_columns(conn, table):
                        raise KnowledgeV2SchemaError("content_schema_incomplete")
        except KnowledgeV2ServiceError:
            raise
        except FileNotFoundError as exc:
            raise KnowledgeV2Unavailable("content_db_missing") from exc
        except Exception as exc:  # schema marker/future/SQLite details stay private
            raise KnowledgeV2SchemaError("content_schema_unavailable") from exc
        return KnowledgeV2Adapter(store)

    def book(
        self,
        scope: ContentReadScope | None,
        *,
        book_id: str | None = None,
        query: str = "",
        limit: int = 100,
    ) -> tuple[dict[str, str], ...]:
        """Return ACL-filtered reference envelopes; never blob bodies."""

        checked = _scope_or_raise(scope)
        adapter = self._content_adapter()
        return adapter.read(
            checked,
            query=str(query or "").strip(),
            limit=_safe_limit(limit),
            occurrence_id=str(book_id).strip() if book_id else None,
        )

    # Name used by future native-port wiring.
    knowledge_book = book

    def _candidate_schema(self) -> None:
        path = self.knowledge_db
        if not path.is_file():
            raise KnowledgeV2Unavailable("candidate_db_missing")
        try:
            # Validate the standard V2 knowledge marker without opening a
            # writable handle.  This rejects legacy/global KnowledgeStore DBs.
            with _immutable_ro_connection(path) as conn:
                tables = self._validate_phase1_marker(
                    conn, "knowledge", _KNOWLEDGE_REQUIRED_TABLES
                )
                if KNOWLEDGE_CANDIDATE_META not in tables or KNOWLEDGE_CANDIDATE_TABLE not in tables:
                    raise KnowledgeV2Unavailable("candidate_schema_unavailable")
                rows = conn.execute(
                    f"SELECT key,value FROM {KNOWLEDGE_CANDIDATE_META} ORDER BY key"
                ).fetchall()
                if len(rows) != 1 or str(rows[0][0]) != "version":
                    raise KnowledgeV2SchemaError("candidate_schema_incomplete")
                marker = str(rows[0][1])
                if marker != str(KNOWLEDGE_CANDIDATE_SCHEMA_VERSION):
                    code = "candidate_schema_future" if marker.isdigit() and int(marker) > KNOWLEDGE_CANDIDATE_SCHEMA_VERSION else "candidate_schema_unsupported"
                    raise KnowledgeV2SchemaError(code)
                if not _CANDIDATE_REQUIRED_COLUMNS <= self._table_columns(conn, KNOWLEDGE_CANDIDATE_TABLE):
                    raise KnowledgeV2SchemaError("candidate_schema_incomplete")
        except KnowledgeV2ServiceError:
            raise
        except FileNotFoundError as exc:
            raise KnowledgeV2Unavailable("candidate_db_missing") from exc
        except Exception as exc:
            raise KnowledgeV2SchemaError("candidate_schema_unavailable") from exc

    def candidates(
        self,
        scope: ContentReadScope | None,
        *,
        status: str = "pending",
        query: str = "",
        limit: int = 100,
    ) -> tuple[dict[str, str], ...]:
        """Read only the optional V2 candidate plane under exact ACL scope."""

        checked = _scope_or_raise(scope)
        self._candidate_schema()
        status_text = str(status or "pending").strip()
        if not status_text or len(status_text) > 64 or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for ch in status_text):
            raise KnowledgeV2ServiceError("invalid_candidate_status")
        query_text = str(query or "").strip()
        params: list[Any] = [
            checked.namespace_id,
            checked.workspace_id,
            checked.agent_instance_id,
            checked.project_ref,
            checked.provider,
            checked.share_group_id,
            checked.sensitivity,
            checked.policy_class,
            status_text,
        ]
        predicates = [
            "namespace_id=?",
            "workspace_id=?",
            "agent_instance_id=?",
            "project_ref=?",
            "provider=?",
            "share_group_id=?",
            "sensitivity=?",
            "policy_class=?",
            "status=?",
        ]
        if query_text:
            predicates.append("(summary LIKE ? OR reference LIKE ?)")
            like = f"%{query_text}%"
            params.extend((like, like))
        params.append(_safe_limit(limit))
        sql = (
            "SELECT candidate_id,summary,reference,content_hash,status "
            f"FROM {KNOWLEDGE_CANDIDATE_TABLE} WHERE "
            + " AND ".join(predicates)
            + " ORDER BY candidate_id LIMIT ?"
        )
        try:
            with _immutable_ro_connection(self.knowledge_db) as conn:
                rows = conn.execute(sql, params).fetchall()
        except Exception as exc:
            raise KnowledgeV2Unavailable("candidate_read_unavailable") from exc
        return tuple(
            {
                # Candidate metadata is a second reference-only surface.  It
                # must use the same reject-on-match policy as book rows; a
                # path/body/secret value in a legacy candidate row is omitted
                # rather than partially redacted into a misleading label.
                "candidate_id": _safe_public_text(row[0]),
                "summary": _safe_summary({"summary": row[1]}),
                "ref": _safe_public_text(row[2]),
                "hash": _safe_public_text(row[3]),
                "status": _safe_public_text(row[4], limit=64),
                "trust": "reference_only",
            }
            for row in rows
        )

    knowledge_candidates = candidates

    def dispatch(
        self,
        name: str,
        payload: Mapping[str, Any] | None = None,
        *,
        scope: ContentReadScope | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        """Return a stable native-service envelope for future port wiring."""

        payload_map = dict(payload or {})
        operation = str(name or "").strip().casefold()
        is_book = operation in {"book", "knowledge_book", "memoryguard_knowledge_book"}
        if is_book:
            handler = self.book
            service = "knowledge_book"
        elif operation in {"candidates", "knowledge_candidates", "memoryguard_knowledge_candidates"}:
            handler = self.candidates
            service = "knowledge_candidates"
        else:
            return _error(self.service_name, "unknown_knowledge_operation")
        try:
            if is_book:
                rows = handler(
                    scope,
                    book_id=payload_map.get("book_id"),
                    query=payload_map.get("query", ""),
                    limit=payload_map.get("limit", 100),
                )
            else:
                rows = handler(
                    scope,
                    status=payload_map.get("status", "pending"),
                    query=payload_map.get("query", ""),
                    limit=payload_map.get("limit", 100),
                )
        except KnowledgeV2ServiceError as exc:
            return _error(service, exc.code)
        return {
            "ok": True,
            "status": "READY",
            "service": service,
            "references": list(rows),
            "total": len(rows),
            "trust": "reference_only",
        }

    call = dispatch


KnowledgeV2NativeService = KnowledgeV2ReadonlyService


__all__ = [
    "KNOWLEDGE_CANDIDATE_META",
    "KNOWLEDGE_CANDIDATE_SCHEMA",
    "KNOWLEDGE_CANDIDATE_SCHEMA_VERSION",
    "KNOWLEDGE_CANDIDATE_TABLE",
    "KnowledgeV2NativeService",
    "KnowledgeV2ReadonlyService",
    "KnowledgeV2SchemaError",
    "KnowledgeV2ServiceError",
    "KnowledgeV2Unavailable",
]
