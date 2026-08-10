"""Persistent, metadata-only V2 CodeGraph store.

This store is intentionally boring SQLite.  It reuses the repository's V2
layout, connection and transaction helpers, but owns a separate graph schema
with immutable revisions and a small projection outbox.  Source text is never
accepted by the public API and no table has a source-body column.
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
from typing import Any, Iterable, Iterator, Mapping, Sequence

from ..storage.database import execute_sql_script, open_database
from ..storage.layout import WorkspaceV2Layout
from ..storage.schema import initialize_database
from ..storage.transaction import transaction
from .models import (
    AffectedQuery,
    CODEGRAPH_SCHEMA_MARKER,
    CODEGRAPH_SCHEMA_VERSION,
    CodeGraphError,
    CodeGraphPathError,
    CodeGraphSchemaError,
    CodeGraphScope,
    CodeGraphScopeError,
    Edge,
    OutboxEvent,
    Revision,
    SourceFile,
    Symbol,
    UNKNOWN,
    UnknownLedgerEntry,
    stable_digest,
    stable_id,
    validate_metadata,
)


_AUX_TABLES = frozenset(
    {
        "codegraph_schema_meta",
        "graph_scopes",
        "source_files",
        "revisions",
        "symbols",
        "edges",
        "affected_queries",
        "checkpoints",
        "outbox",
        "migration_map",
        "unknown_ledger",
        "source_tombstones",
    }
)

CODEGRAPH_AUX_SCHEMA = """
CREATE TABLE IF NOT EXISTS codegraph_schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS graph_scopes (
    scope_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    agent_instance_id TEXT NOT NULL DEFAULT '',
    project_ref TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    share_group_id TEXT NOT NULL DEFAULT '',
    runtime_role TEXT NOT NULL DEFAULT '',
    trusted_context INTEGER NOT NULL DEFAULT 1 CHECK (trusted_context IN (0,1)),
    created_at TEXT NOT NULL,
    UNIQUE(workspace_id, agent_instance_id, project_ref, provider, share_group_id, runtime_role)
);
CREATE TABLE IF NOT EXISTS source_files (
    file_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL,
    source_id TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    source_revision TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    revision_id TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(scope_id, path),
    FOREIGN KEY(scope_id) REFERENCES graph_scopes(scope_id)
);
CREATE TABLE IF NOT EXISTS revisions (
    revision_id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    source_revision TEXT NOT NULL DEFAULT '',
    revision_number INTEGER NOT NULL CHECK(revision_number >= 1),
    created_at TEXT NOT NULL,
    UNIQUE(file_id, content_hash, source_revision),
    FOREIGN KEY(file_id) REFERENCES source_files(file_id),
    FOREIGN KEY(scope_id) REFERENCES graph_scopes(scope_id)
);
CREATE TABLE IF NOT EXISTS symbols (
    symbol_id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    revision_id TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'symbol',
    signature TEXT NOT NULL DEFAULT '',
    symbol_hash TEXT NOT NULL DEFAULT '',
    line_start INTEGER NOT NULL DEFAULT 0 CHECK(line_start >= 0),
    line_end INTEGER NOT NULL DEFAULT 0 CHECK(line_end >= 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    UNIQUE(file_id, revision_id, name, kind, symbol_hash, line_start, line_end),
    FOREIGN KEY(file_id) REFERENCES source_files(file_id),
    FOREIGN KEY(scope_id) REFERENCES graph_scopes(scope_id),
    FOREIGN KEY(revision_id) REFERENCES revisions(revision_id)
);
CREATE TABLE IF NOT EXISTS edges (
    edge_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL,
    revision_id TEXT NOT NULL DEFAULT '',
    from_id TEXT NOT NULL,
    to_id TEXT NOT NULL,
    relation TEXT NOT NULL DEFAULT 'related',
    weight REAL NOT NULL DEFAULT 1.0,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    UNIQUE(scope_id, revision_id, from_id, to_id, relation),
    FOREIGN KEY(scope_id) REFERENCES graph_scopes(scope_id),
    FOREIGN KEY(revision_id) REFERENCES revisions(revision_id)
);
CREATE INDEX IF NOT EXISTS idx_codegraph_edges_to ON edges(scope_id, to_id, active);
CREATE INDEX IF NOT EXISTS idx_codegraph_edges_from ON edges(scope_id, from_id, active);
CREATE TABLE IF NOT EXISTS affected_queries (
    query_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL,
    start_id TEXT NOT NULL,
    depth INTEGER NOT NULL CHECK(depth >= 0),
    result_limit INTEGER NOT NULL CHECK(result_limit >= 1),
    relation_filter TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '[]',
    result_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(scope_id, start_id, depth, result_limit, relation_filter, result_digest),
    FOREIGN KEY(scope_id) REFERENCES graph_scopes(scope_id)
);
CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL,
    domain TEXT NOT NULL,
    sequence INTEGER NOT NULL DEFAULT 0 CHECK(sequence >= 0),
    digest TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    UNIQUE(scope_id, domain),
    FOREIGN KEY(scope_id) REFERENCES graph_scopes(scope_id)
);
CREATE TABLE IF NOT EXISTS outbox (
    event_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL,
    sequence INTEGER NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','projected','failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    projected_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(scope_id) REFERENCES graph_scopes(scope_id)
);
CREATE INDEX IF NOT EXISTS idx_codegraph_outbox_pending ON outbox(status, sequence);
CREATE TABLE IF NOT EXISTS migration_map (
    map_id TEXT PRIMARY KEY,
    source_db TEXT NOT NULL,
    source_table TEXT NOT NULL,
    source_pk TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    target_id TEXT NOT NULL DEFAULT '',
    target_type TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'mapped',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_db, source_table, source_pk, target_type)
);
CREATE TABLE IF NOT EXISTS unknown_ledger (
    ledger_id TEXT PRIMARY KEY,
    source_ref TEXT NOT NULL,
    code TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'BLOCKED',
    source_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(source_ref, code, detail)
);
CREATE TABLE IF NOT EXISTS source_tombstones (
    tombstone_id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    revision_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(file_id, reason),
    FOREIGN KEY(file_id) REFERENCES source_files(file_id),
    FOREIGN KEY(scope_id) REFERENCES graph_scopes(scope_id)
);
-- Compatibility read aliases; callers must still use CodeGraphStore writes.
CREATE VIEW IF NOT EXISTS codegraph_outbox AS SELECT * FROM outbox;
CREATE VIEW IF NOT EXISTS codegraph_checkpoints AS SELECT * FROM checkpoints;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _assert_no_reparse(path: str | Path) -> None:
    raw = Path(path).expanduser()
    current = Path(os.path.abspath(os.fspath(raw)))
    while True:
        try:
            exists = current.exists() or current.is_symlink()
        except OSError as exc:
            raise CodeGraphPathError(f"cannot inspect workspace path: {current}") from exc
        if exists:
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                info = None
            except OSError as exc:
                raise CodeGraphPathError(f"cannot inspect workspace path: {current}") from exc
            if info is not None and (stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x0400)):
                raise CodeGraphPathError(f"workspace path cannot contain symlink or reparse point: {current}")
        if current.parent == current:
            return
        current = current.parent


def normalize_relative_path(value: str | Path) -> str:
    """Normalize a source reference while rejecting absolute/traversal paths."""

    raw = str(value).strip().replace("\\", "/")
    if not raw or "\x00" in raw or any(ord(char) < 32 for char in raw):
        raise CodeGraphPathError("source path is empty or contains control characters")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:/", raw) or raw.startswith("//"):
        raise CodeGraphPathError("source path must be workspace-relative")
    path = PurePosixPath(raw)
    parts = [part for part in path.parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise CodeGraphPathError("source path escapes workspace")
    normalized = "/".join(parts)
    if normalized.startswith("../") or normalized == "..":
        raise CodeGraphPathError("source path escapes workspace")
    return normalized


def _safe_path_under_workspace(workspace: Path, relative_path: str) -> None:
    candidate = workspace / Path(relative_path.replace("/", os.sep))
    current = candidate
    while current != workspace and current.parent != current:
        if current.exists() or current.is_symlink():
            try:
                info = os.lstat(current)
            except OSError as exc:
                raise CodeGraphPathError(f"cannot inspect source path: {current}") from exc
            if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x0400):
                raise CodeGraphPathError(f"source path cannot contain symlink or reparse point: {current}")
        current = current.parent
    if current != workspace:
        raise CodeGraphPathError("source path is outside workspace")


def _scope_row(scope: CodeGraphScope) -> tuple[str, ...]:
    return (scope.workspace_id, scope.agent_instance_id, scope.project_ref, scope.provider, scope.share_group_id, scope.runtime_role)


class CodeGraphStore:
    """SQLite-backed CodeGraph with exact ACL and immutable revisions."""

    SCHEMA_VERSION = CODEGRAPH_SCHEMA_VERSION
    SCHEMA_MARKER = CODEGRAPH_SCHEMA_MARKER

    def __init__(
        self,
        workspace: str | Path | WorkspaceV2Layout,
        *,
        workspace_id: str | None = None,
        initialize: bool = True,
        source_workspace: str | Path | None = None,
    ) -> None:
        if isinstance(workspace, WorkspaceV2Layout):
            raw = source_workspace
            if raw is not None:
                _assert_no_reparse(raw)
            self.layout = workspace
        else:
            _assert_no_reparse(workspace)
            self.layout = WorkspaceV2Layout(Path(workspace))
        self.workspace = self.layout.workspace
        self.workspace_id = str(workspace_id or self.workspace)
        self.db_path = self.layout.codegraph_db
        if initialize:
            self.layout.ensure_dirs()
            state = self._preflight()
            if state != "current":
                initialize_database(self.db_path, "codegraph", layout=self.layout)
                self._ensure_aux_schema()

    def _preflight(self) -> str:
        if not self.db_path.is_file():
            return "fresh"
        try:
            with open_database(self.db_path, readonly=True) as conn:
                tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                aux = tables & _AUX_TABLES
                if "codegraph_schema_meta" not in tables:
                    if aux:
                        raise CodeGraphSchemaError("codegraph aux schema marker is missing")
                    return "needs_aux"
                rows = conn.execute("SELECT key,value FROM codegraph_schema_meta ORDER BY key").fetchall()
                if len(rows) != 1 or str(rows[0][0]) != "version" or str(rows[0][1]) != str(CODEGRAPH_SCHEMA_VERSION):
                    raise CodeGraphSchemaError("unsupported codegraph schema marker")
                missing = sorted(_AUX_TABLES - {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} - {"codegraph_schema_meta"})
                # Views are intentionally not part of the required write schema.
                if missing:
                    raise CodeGraphSchemaError("codegraph schema is incomplete: " + ",".join(missing))
            initialize_database(self.db_path, "codegraph", layout=self.layout, readonly=True)
            return "current"
        except CodeGraphError:
            raise
        except (sqlite3.Error, OSError, ValueError) as exc:
            raise CodeGraphSchemaError(f"cannot inspect codegraph database: {self.db_path}") from exc

    def _ensure_aux_schema(self) -> None:
        with open_database(self.db_path) as conn:
            with transaction(conn):
                execute_sql_script(conn, CODEGRAPH_AUX_SCHEMA)
                conn.execute(
                    "INSERT INTO codegraph_schema_meta(key,value) VALUES('version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(CODEGRAPH_SCHEMA_VERSION),),
                )

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a physically read-only connection for inspection."""

        with open_database(self.db_path, readonly=True) as conn:
            yield conn

    def _scope(self, scope: CodeGraphScope | Mapping[str, Any] | None, *, write: bool = False) -> CodeGraphScope:
        if scope is None:
            raise CodeGraphScopeError("explicit trusted CodeGraphScope is required")
        # A mapping is useful for exact read predicates, but it is not a
        # mutation capability.  Callers must carry the private, trusted
        # CodeGraphScope object for every write path.
        if write and not isinstance(scope, CodeGraphScope):
            raise CodeGraphScopeError("codegraph writes require a trusted CodeGraphScope capability")
        checked = CodeGraphScope.from_value(scope)
        if not checked.trusted_context:
            raise CodeGraphScopeError("untrusted context cannot access codegraph")
        if checked.workspace_id != self.workspace_id:
            raise CodeGraphScopeError("workspace scope mismatch")
        if any(value == UNKNOWN for value in checked.as_tuple()):
            raise CodeGraphScopeError("unknown ACL scope is blocked")
        if write and not checked.trusted_context:
            raise CodeGraphScopeError("trusted context required for codegraph writes")
        return checked

    @staticmethod
    def _scope_id(scope: CodeGraphScope) -> str:
        return stable_id("scope", *scope.as_tuple())

    def _ensure_scope(self, conn: sqlite3.Connection, scope: CodeGraphScope) -> str:
        scope_id = self._scope_id(scope)
        now = _now()
        conn.execute(
            "INSERT INTO graph_scopes(scope_id,workspace_id,agent_instance_id,project_ref,provider,share_group_id,runtime_role,trusted_context,created_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(scope_id) DO NOTHING",
            (scope_id, *_scope_row(scope), int(scope.trusted_context), now),
        )
        return scope_id

    def _scope_from_row(self, row: sqlite3.Row) -> CodeGraphScope:
        return CodeGraphScope(
            workspace_id=str(row["workspace_id"]),
            agent_instance_id=str(row["agent_instance_id"]),
            project_ref=str(row["project_ref"]),
            provider=str(row["provider"]),
            share_group_id=str(row["share_group_id"]),
            runtime_role=str(row["runtime_role"]),
            trusted_context=bool(row["trusted_context"]),
        )

    def _check_source_path(self, path: str | Path) -> str:
        normalized = normalize_relative_path(path)
        _safe_path_under_workspace(self.workspace, normalized)
        return normalized

    @staticmethod
    def _coerce_symbol(value: Symbol | Mapping[str, Any], *, file_id: str, scope: CodeGraphScope, revision_id: str) -> Symbol:
        if isinstance(value, Symbol):
            if value.file_id != file_id:
                raise CodeGraphError("symbol belongs to another source file")
            if value.scope is not None and value.scope.as_tuple() != scope.as_tuple():
                raise CodeGraphScopeError("symbol ACL scope mismatch")
            if value.revision_id and value.revision_id != revision_id:
                raise CodeGraphError("symbol revision does not match source file revision")
            return replace(value, scope=scope, revision_id=revision_id)
        if not isinstance(value, Mapping):
            raise TypeError("symbol must be Symbol or mapping")
        validate_metadata(value)
        supplied_revision = str(value.get("revision_id") or "")
        if supplied_revision and supplied_revision != revision_id:
            raise CodeGraphError("symbol revision does not match source file revision")
        symbol_id = str(value.get("symbol_id") or value.get("id") or stable_id("symbol", file_id, value.get("name", ""), value.get("kind", "symbol"), value.get("signature", ""), value.get("line_start", 0), value.get("line_end", 0)))
        return Symbol(
            symbol_id=symbol_id,
            file_id=file_id,
            name=str(value.get("name") or value.get("label") or ""),
            kind=str(value.get("kind") or value.get("symbol_kind") or "symbol"),
            signature=str(value.get("signature") or value.get("signature_text") or ""),
            symbol_hash=str(value.get("symbol_hash") or value.get("hash") or ""),
            line_start=int(value.get("line_start") or value.get("start_line") or 0),
            line_end=int(value.get("line_end") or value.get("end_line") or 0),
            scope=scope,
            revision_id=str(value.get("revision_id") or revision_id),
            active=bool(value.get("active", True)),
        )

    @staticmethod
    def _coerce_edge(value: Edge | Mapping[str, Any], *, scope: CodeGraphScope, revision_id: str) -> Edge:
        if isinstance(value, Edge):
            if value.scope is not None and value.scope.as_tuple() != scope.as_tuple():
                raise CodeGraphScopeError("edge ACL scope mismatch")
            if value.revision_id and revision_id and value.revision_id != revision_id:
                raise CodeGraphError("edge revision does not match requested revision")
            return replace(value, scope=scope, revision_id=value.revision_id or revision_id)
        if not isinstance(value, Mapping):
            raise TypeError("edge must be Edge or mapping")
        validate_metadata(value)
        from_id = str(value.get("from_id") or value.get("from") or value.get("source") or "")
        to_id = str(value.get("to_id") or value.get("to") or value.get("target") or "")
        relation = str(value.get("relation") or value.get("edge_kind") or value.get("type") or "related")
        edge_id = str(value.get("edge_id") or value.get("id") or stable_id("edge", scope.digest, revision_id, from_id, to_id, relation))
        return Edge(
            edge_id=edge_id,
            from_id=from_id,
            to_id=to_id,
            relation=relation,
            scope=scope,
            revision_id=str(value.get("revision_id") or revision_id),
            weight=float(value.get("weight", 1.0)),
            active=bool(value.get("active", True)),
        )

    def upsert_source_file(
        self,
        path: str | Path | SourceFile | Mapping[str, Any],
        content_hash: str | None = None,
        *,
        scope: CodeGraphScope | Mapping[str, Any] | None = None,
        source_revision: str = "",
        language: str = "",
        source_id: str = "",
        active: bool = True,
        symbols: Sequence[Symbol | Mapping[str, Any]] = (),
        edges: Sequence[Edge | Mapping[str, Any]] = (),
        fail_at: str | None = None,
    ) -> SourceFile:
        """Insert one file head and immutable revision idempotently."""

        checked_scope = self._scope(scope, write=True)
        if isinstance(path, SourceFile):
            source = path
            if source.scope.as_tuple() != checked_scope.as_tuple():
                raise CodeGraphScopeError("source file ACL scope mismatch")
            path_value = source.path
            content_hash = source.content_hash
            source_revision = source.source_revision
            language = source.language
            source_id = source.source_id
            active = source.active
        elif isinstance(path, Mapping):
            data = path
            validate_metadata(data)
            path_value = str(data.get("path") or data.get("relative_path") or "")
            content_hash = str(data.get("content_hash") or data.get("hash") or content_hash or "")
            source_revision = str(data.get("source_revision") or source_revision)
            language = str(data.get("language") or language)
            source_id = str(data.get("source_id") or source_id)
            active = bool(data.get("active", active))
            symbols = tuple(data.get("symbols") or symbols)
            edges = tuple(data.get("edges") or edges)
        else:
            path_value = str(path)
        normalized_path = self._check_source_path(path_value)
        digest = str(content_hash or "").strip()
        if not digest:
            raise ValueError("content_hash is required; source text is not accepted")
        source_revision = str(source_revision or "")
        language = str(language or "")
        source_id = str(source_id or "")
        scope_id = self._scope_id(checked_scope)
        file_id = stable_id("file", scope_id, normalized_path)
        now = _now()

        with open_database(self.db_path) as conn:
            with transaction(conn):
                self._ensure_scope(conn, checked_scope)
                existing = conn.execute("SELECT * FROM source_files WHERE file_id=?", (file_id,)).fetchone()
                if existing is not None and str(existing["scope_id"]) != scope_id:
                    raise CodeGraphScopeError("source file scope collision")
                unchanged = existing is not None and str(existing["content_hash"]) == digest and str(existing["source_revision"]) == source_revision and str(existing["language"]) == language and bool(existing["active"]) == bool(active)
                revision_id = str(existing["revision_id"] or "") if unchanged else stable_id("revision", file_id, digest, source_revision)
                revision_number = int(existing["revision_number"]) if False else 0
                if not unchanged:
                    revision_row = conn.execute("SELECT revision_id,revision_number,content_hash,source_revision FROM revisions WHERE revision_id=?", (revision_id,)).fetchone()
                    # ``revisions.file_id`` is a real FK.  Establish the file
                    # head first (with an empty head pointer), then append the
                    # immutable revision and finally point the head at it.
                    conn.execute(
                        "INSERT INTO source_files(file_id,scope_id,source_id,path,content_hash,source_revision,language,revision_id,active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(file_id) DO UPDATE SET source_id=excluded.source_id,path=excluded.path,content_hash=excluded.content_hash,source_revision=excluded.source_revision,language=excluded.language,active=excluded.active,updated_at=excluded.updated_at",
                        (file_id, scope_id, source_id, normalized_path, digest, source_revision, language, "", int(active), now, now),
                    )
                    if revision_row is not None:
                        if str(revision_row["content_hash"]) != digest or str(revision_row["source_revision"]) != source_revision:
                            raise CodeGraphError("immutable revision identity collision")
                        revision_number = int(revision_row["revision_number"])
                    else:
                        max_row = conn.execute("SELECT COALESCE(MAX(revision_number),0) FROM revisions WHERE file_id=?", (file_id,)).fetchone()
                        revision_number = int(max_row[0]) + 1
                        conn.execute("INSERT INTO revisions(revision_id,file_id,scope_id,content_hash,source_revision,revision_number,created_at) VALUES(?,?,?,?,?,?,?)", (revision_id, file_id, scope_id, digest, source_revision, revision_number, now))
                    conn.execute("UPDATE source_files SET revision_id=?,updated_at=? WHERE file_id=?", (revision_id, now, file_id))
                    if fail_at == "after_file":
                        raise CodeGraphError("injected codegraph failure after file")
                    for symbol_value in symbols:
                        symbol = self._coerce_symbol(symbol_value, file_id=file_id, scope=checked_scope, revision_id=revision_id)
                        self._insert_symbol(conn, symbol, scope_id, now)
                    for edge_value in edges:
                        edge = self._coerce_edge(edge_value, scope=checked_scope, revision_id=revision_id)
                        self._insert_edge(conn, edge, scope_id, now)
                    self._append_outbox_conn(conn, checked_scope, "source_file.upsert" if active else "source_file.tombstone", file_id, stable_digest({"file_id": file_id, "revision_id": revision_id, "content_hash": digest, "active": bool(active)}), now)
                row = conn.execute("SELECT f.*,r.revision_number FROM source_files f LEFT JOIN revisions r ON r.revision_id=f.revision_id WHERE f.file_id=?", (file_id,)).fetchone()
                if row is None:
                    raise CodeGraphError("source file insert failed")
                return SourceFile(
                    source_id=str(row["source_id"]),
                    path=str(row["path"]),
                    content_hash=str(row["content_hash"]),
                    scope=checked_scope,
                    source_revision=str(row["source_revision"]),
                    language=str(row["language"]),
                    active=bool(row["active"]),
                    revision_id=str(row["revision_id"]),
                    file_id=str(row["file_id"]),
                )

    put_source_file = upsert_source_file
    upsert_file = upsert_source_file
    put_file = upsert_source_file

    def _insert_symbol(self, conn: sqlite3.Connection, symbol: Symbol, scope_id: str, now: str) -> str:
        revision = conn.execute(
            "SELECT file_id,scope_id FROM revisions WHERE revision_id=?",
            (symbol.revision_id,),
        ).fetchone()
        if revision is None or str(revision[0]) != symbol.file_id or str(revision[1]) != scope_id:
            raise CodeGraphError("symbol revision is not owned by its source file and scope")
        source = conn.execute(
            "SELECT scope_id FROM source_files WHERE file_id=?",
            (symbol.file_id,),
        ).fetchone()
        if source is None or str(source[0]) != scope_id:
            raise CodeGraphScopeError("symbol source file scope mismatch")
        symbol_hash = symbol.signature_hash
        existing = conn.execute("SELECT file_id,scope_id,revision_id,name,kind,signature,symbol_hash,line_start,line_end,active FROM symbols WHERE symbol_id=?", (symbol.symbol_id,)).fetchone()
        values = (symbol.file_id, scope_id, symbol.revision_id, symbol.name, symbol.kind, symbol.signature, symbol_hash, symbol.line_start, symbol.line_end, int(symbol.active))
        if existing is not None:
            if tuple(str(item) for item in existing) != tuple(str(item) for item in values):
                raise CodeGraphError("immutable symbol identity collision")
            return symbol.symbol_id
        conn.execute("INSERT INTO symbols(symbol_id,file_id,scope_id,revision_id,name,kind,signature,symbol_hash,line_start,line_end,active,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (symbol.symbol_id, symbol.file_id, scope_id, symbol.revision_id, symbol.name, symbol.kind, symbol.signature, symbol_hash, symbol.line_start, symbol.line_end, int(symbol.active), now))
        return symbol.symbol_id

    def put_symbols(self, file_id: str, symbols: Sequence[Symbol | Mapping[str, Any]], *, scope: CodeGraphScope | Mapping[str, Any] | None = None, revision_id: str = "") -> tuple[Symbol, ...]:
        checked_scope = self._scope(scope, write=True)
        scope_id = self._scope_id(checked_scope)
        now = _now()
        with open_database(self.db_path) as conn:
            with transaction(conn):
                self._ensure_scope(conn, checked_scope)
                file = conn.execute("SELECT revision_id FROM source_files WHERE file_id=? AND scope_id=?", (str(file_id), scope_id)).fetchone()
                if file is None:
                    raise CodeGraphError("unknown source file")
                selected_revision = str(revision_id or file[0] or "")
                revision = conn.execute("SELECT file_id,scope_id FROM revisions WHERE revision_id=?", (selected_revision,)).fetchone()
                if revision is None or str(revision[0]) != str(file_id) or str(revision[1]) != scope_id:
                    raise CodeGraphError("unknown source file revision")
                output: list[Symbol] = []
                for value in symbols:
                    symbol = self._coerce_symbol(value, file_id=str(file_id), scope=checked_scope, revision_id=selected_revision)
                    self._insert_symbol(conn, symbol, scope_id, now)
                    output.append(symbol)
                if output:
                    self._append_outbox_conn(conn, checked_scope, "symbols.upsert", str(file_id), stable_digest([item.to_dict() for item in output]), now)
                return tuple(output)

    put_symbol_batch = put_symbols

    def _insert_edge(self, conn: sqlite3.Connection, edge: Edge, scope_id: str, now: str) -> str:
        # Endpoints must be metadata identities in this scope; no arbitrary
        # cross-tenant edge can be smuggled into the graph.
        revision = conn.execute("SELECT scope_id FROM revisions WHERE revision_id=?", (edge.revision_id,)).fetchone()
        if revision is None or str(revision[0]) != scope_id:
            raise CodeGraphError("edge revision is not present in the requested scope")
        from_row = conn.execute("SELECT symbol_id,scope_id,revision_id FROM symbols WHERE symbol_id=? AND scope_id=? AND active=1", (edge.from_id, scope_id)).fetchone()
        to_row = conn.execute("SELECT symbol_id,scope_id,revision_id FROM symbols WHERE symbol_id=? AND scope_id=? AND active=1", (edge.to_id, scope_id)).fetchone()
        # Self-loops still need a real endpoint; checking the two lookups
        # separately prevents the old count shortcut from creating orphans.
        if from_row is None or to_row is None:
            raise CodeGraphError("edge endpoint is not a symbol in the requested scope")
        existing = conn.execute("SELECT scope_id,revision_id,from_id,to_id,relation,weight,active FROM edges WHERE edge_id=?", (edge.edge_id,)).fetchone()
        values = (scope_id, edge.revision_id, edge.from_id, edge.to_id, edge.relation, edge.weight, int(edge.active))
        if existing is not None:
            if tuple(str(item) for item in existing) != tuple(str(item) for item in values):
                raise CodeGraphError("immutable edge identity collision")
            return edge.edge_id
        conn.execute("INSERT INTO edges(edge_id,scope_id,revision_id,from_id,to_id,relation,weight,active,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (edge.edge_id, scope_id, edge.revision_id, edge.from_id, edge.to_id, edge.relation, edge.weight, int(edge.active), now))
        return edge.edge_id

    def put_edges(self, edges: Sequence[Edge | Mapping[str, Any]], *, scope: CodeGraphScope | Mapping[str, Any] | None = None, revision_id: str = "") -> tuple[Edge, ...]:
        checked_scope = self._scope(scope, write=True)
        scope_id = self._scope_id(checked_scope)
        now = _now()
        with open_database(self.db_path) as conn:
            with transaction(conn):
                self._ensure_scope(conn, checked_scope)
                output: list[Edge] = []
                for value in edges:
                    edge = self._coerce_edge(value, scope=checked_scope, revision_id=revision_id or (value.revision_id if isinstance(value, Edge) else str(value.get("revision_id") or "")))
                    if not edge.revision_id:
                        revision_rows = conn.execute("SELECT DISTINCT revision_id FROM symbols WHERE symbol_id IN (?,?) AND scope_id=? AND active=1 ORDER BY revision_id", (edge.from_id, edge.to_id, scope_id)).fetchall()
                        if len(revision_rows) == 1 and str(revision_rows[0][0]):
                            edge = replace(edge, revision_id=str(revision_rows[0][0]))
                        else:
                            raise CodeGraphError("edge revision is required and endpoints must agree")
                    self._insert_edge(conn, edge, scope_id, now)
                    output.append(edge)
                if output:
                    self._append_outbox_conn(conn, checked_scope, "edges.upsert", stable_id("edge-batch", checked_scope.digest, *[item.edge_id for item in output]), stable_digest([item.to_dict() for item in output]), now)
                return tuple(output)

    put_edge_batch = put_edges

    def get_source_file(self, file_id: str, *, scope: CodeGraphScope | Mapping[str, Any] | None = None) -> SourceFile | None:
        checked_scope = self._scope(scope)
        scope_id = self._scope_id(checked_scope)
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM source_files WHERE file_id=? AND scope_id=?", (str(file_id), scope_id)).fetchone()
        if row is None:
            return None
        return SourceFile(source_id=str(row["source_id"]), path=str(row["path"]), content_hash=str(row["content_hash"]), scope=checked_scope, source_revision=str(row["source_revision"]), language=str(row["language"]), active=bool(row["active"]), revision_id=str(row["revision_id"]), file_id=str(row["file_id"]))

    read_source_file = get_source_file

    def list_source_files(self, *, scope: CodeGraphScope | Mapping[str, Any] | None = None, active_only: bool = True) -> tuple[SourceFile, ...]:
        checked_scope = self._scope(scope)
        scope_id = self._scope_id(checked_scope)
        sql = "SELECT * FROM source_files WHERE scope_id=?" + (" AND active=1" if active_only else "") + " ORDER BY path,file_id"
        with self.connection() as conn:
            rows = conn.execute(sql, (scope_id,)).fetchall()
        return tuple(SourceFile(source_id=str(row["source_id"]), path=str(row["path"]), content_hash=str(row["content_hash"]), scope=checked_scope, source_revision=str(row["source_revision"]), language=str(row["language"]), active=bool(row["active"]), revision_id=str(row["revision_id"]), file_id=str(row["file_id"])) for row in rows)

    list_files = list_source_files

    def get_symbols(self, file_id: str, *, scope: CodeGraphScope | Mapping[str, Any] | None = None, revision_id: str = "") -> tuple[Symbol, ...]:
        checked_scope = self._scope(scope)
        scope_id = self._scope_id(checked_scope)
        sql = "SELECT * FROM symbols WHERE file_id=? AND scope_id=? AND active=1"
        params: list[Any] = [str(file_id), scope_id]
        if revision_id:
            sql += " AND revision_id=?"; params.append(str(revision_id))
        else:
            sql += " AND revision_id=(SELECT revision_id FROM source_files WHERE file_id=? AND scope_id=?)"
            params.extend((str(file_id), scope_id))
        sql += " ORDER BY line_start,line_end,name,symbol_id"
        with self.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return tuple(Symbol(symbol_id=str(row["symbol_id"]), file_id=str(row["file_id"]), name=str(row["name"]), kind=str(row["kind"]), signature=str(row["signature"]), symbol_hash=str(row["symbol_hash"]), line_start=int(row["line_start"]), line_end=int(row["line_end"]), scope=checked_scope, revision_id=str(row["revision_id"]), active=bool(row["active"])) for row in rows)

    list_symbols = get_symbols

    def list_revisions(self, file_id: str, *, scope: CodeGraphScope | Mapping[str, Any] | None = None) -> tuple[Revision, ...]:
        checked_scope = self._scope(scope)
        scope_id = self._scope_id(checked_scope)
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM revisions WHERE file_id=? AND scope_id=? ORDER BY revision_number,revision_id", (str(file_id), scope_id)).fetchall()
        return tuple(Revision(str(row["revision_id"]), str(row["file_id"]), str(row["content_hash"]), str(row["source_revision"]), int(row["revision_number"]), str(row["created_at"]), True) for row in rows)

    revisions = list_revisions

    def get_revision(self, revision_id: str, *, scope: CodeGraphScope | Mapping[str, Any] | None = None) -> Revision | None:
        checked_scope = self._scope(scope)
        scope_id = self._scope_id(checked_scope)
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM revisions WHERE revision_id=? AND scope_id=?", (str(revision_id), scope_id)).fetchone()
        if row is None:
            return None
        return Revision(str(row["revision_id"]), str(row["file_id"]), str(row["content_hash"]), str(row["source_revision"]), int(row["revision_number"]), str(row["created_at"]), True)

    read_revision = get_revision

    def list_edges(self, *, scope: CodeGraphScope | Mapping[str, Any] | None = None, revision_id: str = "", relation: str = "") -> tuple[Edge, ...]:
        checked_scope = self._scope(scope)
        scope_id = self._scope_id(checked_scope)
        sql = "SELECT * FROM edges WHERE scope_id=? AND active=1"; params: list[Any] = [scope_id]
        if revision_id:
            sql += " AND revision_id=?"; params.append(str(revision_id))
        if relation:
            sql += " AND relation=?"; params.append(str(relation))
        sql += " ORDER BY from_id,to_id,relation,edge_id"
        with self.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return tuple(Edge(str(row["edge_id"]), str(row["from_id"]), str(row["to_id"]), str(row["relation"]), checked_scope, str(row["revision_id"]), float(row["weight"]), bool(row["active"])) for row in rows)

    edges = list_edges

    def tombstone_source_file(self, path_or_file_id: str, *, scope: CodeGraphScope | Mapping[str, Any] | None = None, reason: str = "deleted") -> str:
        checked_scope = self._scope(scope, write=True)
        scope_id = self._scope_id(checked_scope)
        now = _now()
        with open_database(self.db_path) as conn:
            with transaction(conn):
                self._ensure_scope(conn, checked_scope)
                if "/" in str(path_or_file_id) or "\\" in str(path_or_file_id):
                    path = self._check_source_path(path_or_file_id)
                    row = conn.execute("SELECT file_id,revision_id FROM source_files WHERE scope_id=? AND path=?", (scope_id, path)).fetchone()
                else:
                    row = conn.execute("SELECT file_id,revision_id FROM source_files WHERE scope_id=? AND file_id=?", (scope_id, str(path_or_file_id))).fetchone()
                if row is None:
                    # Deleting an unknown path is an existence-neutral no-op;
                    # callers receive a stable tombstone identity.
                    return stable_id("tombstone", scope_id, str(path_or_file_id), reason)
                file_id, revision_id = str(row[0]), str(row[1])
                tombstone_id = stable_id("tombstone", file_id, reason)
                conn.execute("UPDATE source_files SET active=0,updated_at=? WHERE file_id=?", (now, file_id))
                conn.execute("INSERT INTO source_tombstones(tombstone_id,file_id,scope_id,reason,revision_id,created_at) VALUES(?,?,?,?,?,?) ON CONFLICT(file_id,reason) DO NOTHING", (tombstone_id, file_id, scope_id, str(reason), revision_id, now))
                self._append_outbox_conn(conn, checked_scope, "source_file.tombstone", file_id, stable_digest({"file_id": file_id, "revision_id": revision_id, "reason": str(reason)}), now)
                return tombstone_id

    delete_source_file = tombstone_source_file
    tombstone = tombstone_source_file

    def affected_query(self, start_id: str, *, scope: CodeGraphScope | Mapping[str, Any] | None = None, depth: int = 2, limit: int = 100, relation: str = "") -> AffectedQuery:
        checked_scope = self._scope(scope)
        scope_id = self._scope_id(checked_scope)
        bounded_depth = min(max(0, int(depth)), 32)
        bounded_limit = min(max(1, int(limit)), 10_000)
        relation_filter = str(relation or "")
        with self.connection() as conn:
            known = conn.execute("SELECT 1 FROM symbols WHERE symbol_id=? AND scope_id=? AND active=1", (str(start_id), scope_id)).fetchone()
            if known is None:
                raise CodeGraphError("affected query start_id is not a symbol in the requested scope")
            visited: set[str] = {str(start_id)}
            frontier = [str(start_id)]
            for _level in range(bounded_depth):
                if not frontier or len(visited) - 1 >= bounded_limit:
                    break
                placeholders = ",".join("?" for _ in frontier)
                sql = f"SELECT from_id,to_id,relation FROM edges WHERE scope_id=? AND active=1 AND to_id IN ({placeholders})"
                params: list[Any] = [scope_id, *frontier]
                if relation_filter:
                    sql += " AND relation=?"; params.append(relation_filter)
                sql += " ORDER BY from_id,to_id,relation,edge_id"
                next_frontier: list[str] = []
                for row in conn.execute(sql, params).fetchall():
                    candidate = str(row[0])
                    if candidate in visited:
                        continue
                    visited.add(candidate); next_frontier.append(candidate)
                    if len(visited) - 1 >= bounded_limit:
                        break
                frontier = sorted(next_frontier)
        result_ids = tuple(sorted(visited - {str(start_id)}))[:bounded_limit]
        result_digest = stable_digest(result_ids)
        query_id = stable_id("affected", scope_id, start_id, bounded_depth, bounded_limit, relation_filter, result_digest)
        now = _now()
        with open_database(self.db_path) as conn:
            with transaction(conn):
                conn.execute("INSERT INTO affected_queries(query_id,scope_id,start_id,depth,result_limit,relation_filter,result_json,result_digest,created_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(query_id) DO NOTHING", (query_id, scope_id, str(start_id), bounded_depth, bounded_limit, relation_filter, _json(list(result_ids)), result_digest, now))
        return AffectedQuery(query_id=query_id, scope=checked_scope, start_id=str(start_id), depth=bounded_depth, limit=bounded_limit, result_ids=result_ids, digest=result_digest, created_at=now)

    query_affected = affected_query

    def affected(self, start_id: str, *, scope: CodeGraphScope | Mapping[str, Any] | None = None, depth: int = 2, limit: int = 100, relation: str = "") -> list[str]:
        return list(self.affected_query(start_id, scope=scope, depth=depth, limit=limit, relation=relation).result_ids)

    find_affected = affected

    def _append_outbox_conn(self, conn: sqlite3.Connection, scope: CodeGraphScope, event_type: str, aggregate_id: str, payload_hash: str, now: str) -> str:
        scope_id = self._scope_id(scope)
        event_id = stable_id("codegraph-event", scope_id, event_type, aggregate_id, payload_hash)
        row = conn.execute("SELECT event_id FROM outbox WHERE event_id=?", (event_id,)).fetchone()
        if row is not None:
            return event_id
        sequence = int(conn.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM outbox").fetchone()[0])
        conn.execute("INSERT INTO outbox(event_id,scope_id,sequence,event_type,aggregate_id,payload_hash,status,attempts,error,created_at,projected_at) VALUES(?,?,?,?,?,?, 'pending',0,'',?, '')", (event_id, scope_id, sequence, str(event_type), str(aggregate_id), str(payload_hash), now))
        return event_id

    def append_outbox(self, event_type: str, aggregate_id: str, *, payload_hash: str = "", scope: CodeGraphScope | Mapping[str, Any] | None = None) -> OutboxEvent:
        checked_scope = self._scope(scope, write=True)
        now = _now()
        with open_database(self.db_path) as conn:
            with transaction(conn):
                self._ensure_scope(conn, checked_scope)
                event_id = self._append_outbox_conn(conn, checked_scope, event_type, aggregate_id, payload_hash, now)
                row = conn.execute("SELECT * FROM outbox WHERE event_id=?", (event_id,)).fetchone()
                assert row is not None
                return self._row_outbox(row, checked_scope)

    enqueue_outbox = append_outbox

    def _row_outbox(self, row: sqlite3.Row, scope: CodeGraphScope | None = None) -> OutboxEvent:
        if scope is None:
            scope = CodeGraphScope(workspace_id=str(row["workspace_id"]) if "workspace_id" in row.keys() else self.workspace_id)
        return OutboxEvent(event_id=str(row["event_id"]), scope=scope, event_type=str(row["event_type"]), aggregate_id=str(row["aggregate_id"]), payload_hash=str(row["payload_hash"]), sequence=int(row["sequence"]), status=str(row["status"]), attempts=int(row["attempts"]), error=str(row["error"]), created_at=str(row["created_at"]), projected_at=str(row["projected_at"]))

    def pending_outbox(self, *, scope: CodeGraphScope | Mapping[str, Any] | None = None, limit: int | None = None) -> tuple[OutboxEvent, ...]:
        checked_scope = self._scope(scope)
        scope_id = self._scope_id(checked_scope)
        sql = "SELECT * FROM outbox WHERE scope_id=? AND status='pending' ORDER BY sequence"
        params: list[Any] = [scope_id]
        if limit is not None:
            sql += " LIMIT ?"; params.append(max(0, int(limit)))
        with self.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return tuple(self._row_outbox(row, checked_scope) for row in rows)

    list_pending_outbox = pending_outbox

    def drain_outbox(self, *, scope: CodeGraphScope | Mapping[str, Any] | None = None, projector: Any | None = None, limit: int | None = None) -> dict[str, int]:
        checked_scope = self._scope(scope, write=True)
        events = self.pending_outbox(scope=checked_scope, limit=limit)
        projected = failed = 0
        for event in events:
            try:
                if projector is not None:
                    callback = getattr(projector, "project", projector)
                    callback(event)
                with open_database(self.db_path) as conn:
                    with transaction(conn):
                        conn.execute("UPDATE outbox SET status='projected',attempts=attempts+1,projected_at=?,error='' WHERE event_id=? AND status='pending'", (_now(), event.event_id))
                projected += 1
            except Exception as exc:
                with open_database(self.db_path) as conn:
                    with transaction(conn):
                        conn.execute("UPDATE outbox SET status='failed',attempts=attempts+1,error=? WHERE event_id=?", (f"{type(exc).__name__}:{exc}", event.event_id))
                failed += 1
        return {"projected": projected, "failed": failed, "pending": len(self.pending_outbox(scope=checked_scope))}

    project_outbox = drain_outbox

    def get_checkpoint(self, domain: str = "codegraph", *, scope: CodeGraphScope | Mapping[str, Any] | None = None) -> Checkpoint:
        checked_scope = self._scope(scope)
        scope_id = self._scope_id(checked_scope)
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM checkpoints WHERE scope_id=? AND domain=?", (scope_id, str(domain))).fetchone()
        if row is None:
            return Checkpoint(stable_id("checkpoint", scope_id, domain), checked_scope, str(domain), 0, "", "")
        return Checkpoint(str(row["checkpoint_id"]), checked_scope, str(row["domain"]), int(row["sequence"]), str(row["digest"]), str(row["updated_at"]))

    checkpoint = get_checkpoint

    def save_checkpoint(self, domain: str, sequence: int, *, digest: str = "", scope: CodeGraphScope | Mapping[str, Any] | None = None) -> Checkpoint:
        checked_scope = self._scope(scope, write=True)
        scope_id = self._scope_id(checked_scope)
        sequence = max(0, int(sequence)); now = _now()
        checkpoint_id = stable_id("checkpoint", scope_id, domain)
        with open_database(self.db_path) as conn:
            with transaction(conn):
                self._ensure_scope(conn, checked_scope)
                current = conn.execute("SELECT sequence,digest FROM checkpoints WHERE scope_id=? AND domain=?", (scope_id, str(domain))).fetchone()
                if current is not None and int(current[0]) == sequence and str(current[1]) != str(digest):
                    raise CodeGraphError("checkpoint digest is immutable at a sequence")
                conn.execute("INSERT INTO checkpoints(checkpoint_id,scope_id,domain,sequence,digest,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(scope_id,domain) DO UPDATE SET sequence=CASE WHEN excluded.sequence>checkpoints.sequence THEN excluded.sequence ELSE checkpoints.sequence END,digest=CASE WHEN excluded.sequence>=checkpoints.sequence THEN excluded.digest ELSE checkpoints.digest END,updated_at=CASE WHEN excluded.sequence>=checkpoints.sequence THEN excluded.updated_at ELSE checkpoints.updated_at END", (checkpoint_id, scope_id, str(domain), sequence, str(digest), now))
                row = conn.execute("SELECT * FROM checkpoints WHERE checkpoint_id=?", (checkpoint_id,)).fetchone()
                assert row is not None
                return Checkpoint(checkpoint_id, checked_scope, str(row["domain"]), int(row["sequence"]), str(row["digest"]), str(row["updated_at"]))

    update_checkpoint = save_checkpoint

    def record_migration_map(self, source_db: str, source_table: str, source_pk: str, source_hash: str, *, target_id: str = "", target_type: str = "", status: str = "mapped") -> str:
        now = _now(); source_db = str(source_db); source_table = str(source_table); source_pk = str(source_pk); source_hash = str(source_hash)
        map_id = stable_id("codegraph-map", source_db, source_table, source_pk, target_type)
        with open_database(self.db_path) as conn:
            with transaction(conn):
                row = conn.execute("SELECT source_hash,target_id,status FROM migration_map WHERE source_db=? AND source_table=? AND source_pk=? AND target_type=?", (source_db, source_table, source_pk, target_type)).fetchone()
                if row is not None:
                    if str(row[0]) != source_hash:
                        raise CodeGraphError("migration map source hash changed")
                    return map_id
                conn.execute("INSERT INTO migration_map(map_id,source_db,source_table,source_pk,source_hash,target_id,target_type,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (map_id, source_db, source_table, source_pk, source_hash, str(target_id), str(target_type), str(status), now, now))
        return map_id

    put_migration_map = record_migration_map

    def record_unknown(self, source_ref: str, code: str, detail: str = "", *, source_hash: str = "", status: str = "BLOCKED") -> str:
        now = _now(); ledger_id = stable_id("unknown", source_ref, code, detail)
        with open_database(self.db_path) as conn:
            with transaction(conn):
                conn.execute("INSERT INTO unknown_ledger(ledger_id,source_ref,code,detail,status,source_hash,created_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(source_ref,code,detail) DO NOTHING", (ledger_id, str(source_ref), str(code), str(detail), str(status), str(source_hash), now))
        return ledger_id

    record_unknown_ledger = record_unknown

    def list_unknown(self) -> tuple[UnknownLedgerEntry, ...]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM unknown_ledger ORDER BY source_ref,code,detail").fetchall()
        return tuple(UnknownLedgerEntry(str(row["ledger_id"]), str(row["source_ref"]), str(row["code"]), str(row["detail"]), str(row["status"]), str(row["source_hash"]), str(row["created_at"])) for row in rows)

    list_unknown_ledger = list_unknown

    def list_migration_map(self) -> tuple[dict[str, Any], ...]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM migration_map ORDER BY source_db,source_table,source_pk,target_type").fetchall()
        return tuple(dict(row) for row in rows)

    def counts(self, *, scope: CodeGraphScope | Mapping[str, Any] | None = None) -> dict[str, int]:
        checked_scope = self._scope(scope)
        scope_id = self._scope_id(checked_scope)
        with self.connection() as conn:
            counts = {
                "scopes": int(conn.execute("SELECT COUNT(*) FROM graph_scopes WHERE scope_id=?", (scope_id,)).fetchone()[0]),
                "source_files": int(conn.execute("SELECT COUNT(*) FROM source_files WHERE scope_id=?", (scope_id,)).fetchone()[0]),
                "active_source_files": int(conn.execute("SELECT COUNT(*) FROM source_files WHERE scope_id=? AND active=1", (scope_id,)).fetchone()[0]),
                "revisions": int(conn.execute("SELECT COUNT(*) FROM revisions WHERE scope_id=?", (scope_id,)).fetchone()[0]),
                "symbols": int(conn.execute("SELECT COUNT(*) FROM symbols WHERE scope_id=? AND active=1", (scope_id,)).fetchone()[0]),
                "edges": int(conn.execute("SELECT COUNT(*) FROM edges WHERE scope_id=? AND active=1", (scope_id,)).fetchone()[0]),
                "affected_queries": int(conn.execute("SELECT COUNT(*) FROM affected_queries WHERE scope_id=?", (scope_id,)).fetchone()[0]),
                "checkpoints": int(conn.execute("SELECT COUNT(*) FROM checkpoints WHERE scope_id=?", (scope_id,)).fetchone()[0]),
                "outbox": int(conn.execute("SELECT COUNT(*) FROM outbox WHERE scope_id=?", (scope_id,)).fetchone()[0]),
                "outbox_pending": int(conn.execute("SELECT COUNT(*) FROM outbox WHERE scope_id=? AND status='pending'", (scope_id,)).fetchone()[0]),
                "outbox_failed": int(conn.execute("SELECT COUNT(*) FROM outbox WHERE scope_id=? AND status='failed'", (scope_id,)).fetchone()[0]),
                "migration_map": int(conn.execute("SELECT COUNT(*) FROM migration_map").fetchone()[0]),
                "unknown_ledger": int(conn.execute("SELECT COUNT(*) FROM unknown_ledger").fetchone()[0]),
                "unknown_blocked": int(conn.execute("SELECT COUNT(*) FROM unknown_ledger WHERE status='BLOCKED'").fetchone()[0]),
                "tombstones": int(conn.execute("SELECT COUNT(*) FROM source_tombstones WHERE scope_id=?", (scope_id,)).fetchone()[0]),
            }
        return counts

    table_counts = counts

    def orphan_count(self, *, scope: CodeGraphScope | Mapping[str, Any] | None = None) -> int:
        checked_scope = self._scope(scope); scope_id = self._scope_id(checked_scope)
        with self.connection() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM edges e LEFT JOIN revisions r ON r.revision_id=e.revision_id AND r.scope_id=e.scope_id LEFT JOIN symbols s1 ON s1.symbol_id=e.from_id AND s1.scope_id=e.scope_id AND s1.active=1 LEFT JOIN symbols s2 ON s2.symbol_id=e.to_id AND s2.scope_id=e.scope_id AND s2.active=1 WHERE e.scope_id=? AND e.active=1 AND (r.revision_id IS NULL OR s1.symbol_id IS NULL OR s2.symbol_id IS NULL)", (scope_id,)).fetchone()[0])

    def integrity_check(self) -> list[str]:
        with self.connection() as conn:
            return [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]

    def foreign_key_check(self) -> list[tuple[Any, ...]]:
        with self.connection() as conn:
            return [tuple(row) for row in conn.execute("PRAGMA foreign_key_check").fetchall()]

    def status(self, *, scope: CodeGraphScope | Mapping[str, Any] | None = None) -> dict[str, Any]:
        return {"db_path": str(self.db_path), "schema_marker": CODEGRAPH_SCHEMA_MARKER, "counts": self.counts(scope=scope), "integrity": self.integrity_check(), "foreign_keys": self.foreign_key_check(), "orphan": self.orphan_count(scope=scope)}


CodeGraphStorage = CodeGraphStore
PersistentCodeGraph = CodeGraphStore
SCHEMA_VERSION = CODEGRAPH_SCHEMA_VERSION
SCHEMA_MARKER = CODEGRAPH_SCHEMA_MARKER


__all__ = [
    "CODEGRAPH_AUX_SCHEMA",
    "CodeGraphStorage",
    "CodeGraphStore",
    "PersistentCodeGraph",
    "SCHEMA_MARKER",
    "SCHEMA_VERSION",
    "normalize_relative_path",
]
