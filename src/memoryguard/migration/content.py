"""Read-only V1 history/knowledge migration into the V2 Content Plane.

The migrator is intentionally a reader, not an adapter around either V1
store.  It opens the two explicitly selected SQLite files with ``mode=ro``,
preflights both before creating a V2 target, then performs one transactional,
idempotent import.  A missing data home is reported as ``NOT_CONFIGURED``;
an absent configured file is ``NO_SOURCE``; a malformed source raises
``ContentMigrationError`` and leaves the target transaction untouched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Iterator, Mapping

from ..content.store import (
    ContentError,
    ContentStore,
    _assert_workspace_no_reparse,
    acl_digest,
    canonicalize_text,
    stable_id,
)
from ..storage.database import open_database
from ..storage.layout import WorkspaceV2Layout
from ..storage.transaction import transaction


class ContentMigrationError(RuntimeError):
    """A configured V1 source is unreadable or cannot be migrated safely."""


MigrationError = ContentMigrationError


_KNOWN_KNOWLEDGE_TABLES = frozenset(
    {
        "books",
        "documents",
        "chunks",
        "entities",
        "relations",
        "chunk_entities",
        "embeddings",
        "memory_candidates",
        "candidates",
        "deleted_books",
    }
)

_HISTORY_RECEIPT_COLUMNS: tuple[str, ...] = (
    "idempotency_key",
    "operation",
    "payload_digest",
    "result_json",
    "created_at",
)
@dataclass
class MigrationReport:
    status: str = "OK"
    history_status: str = "NO_SOURCE"
    knowledge_status: str = "NOT_CONFIGURED"
    source_counts: dict[str, int] = field(default_factory=dict)
    target_counts: dict[str, int] = field(default_factory=dict)
    acl_digests: dict[str, str] = field(default_factory=dict)
    migration_map_count: int = 0
    source_complete: dict[str, bool] = field(default_factory=dict)
    errors: list[dict[str, str]] = field(default_factory=list)
    lossless: bool = False

    @property
    def ok(self) -> bool:
        return self.status in {"OK", "PARTIAL"}

    @property
    def partial(self) -> bool:
        return self.status == "PARTIAL"

    @property
    def source_count(self) -> dict[str, int]:
        return self.source_counts

    @property
    def target_count(self) -> dict[str, int]:
        return self.target_counts

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "history_status": self.history_status,
            "knowledge_status": self.knowledge_status,
            "history": {"status": self.history_status, "complete": self.source_complete.get("history", False)},
            "knowledge": {"status": self.knowledge_status, "complete": self.source_complete.get("knowledge", False)},
            "source_counts": dict(self.source_counts),
            "target_counts": dict(self.target_counts),
            "acl_digests": dict(self.acl_digests),
            "migration_map_count": self.migration_map_count,
            "source_complete": dict(self.source_complete),
            "errors": list(self.errors),
            "lossless": False,
        }


@dataclass(frozen=True)
class _Source:
    kind: str
    path: Path | None
    status: str
    tables: tuple[str, ...] = ()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    def encode(item: Any) -> Any:
        if isinstance(item, bytes):
            return {"__bytes__": base64.b64encode(item).decode("ascii")}
        return item
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=encode)


_METADATA_MAX_DEPTH = 8
_METADATA_MAX_BYTES = 64 * 1024
_FORBIDDEN_METADATA_KEYS = frozenset(
    {
        "body",
        "raw",
        "raw_content",
        "content",
        "text",
        "document",
        "document_body",
        "conversation",
        "conversation_body",
        "full_transcript",
        "transcript",
        "raw_text",
        "source_text",
        "payload",
        "full",
        "full_content",
        "content_body",
    }
)


def _metadata_key(value: Any) -> str:
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def _sanitize_metadata(
    value: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Keep bounded structured metadata, dropping body-shaped fields.

    Returned issue paths are persisted in the source-sync anomaly ledger by
    the caller.  Body bytes therefore never reach metadata JSON, while the
    migration remains auditable instead of silently claiming full fidelity.
    """

    if value is None:
        return {}, ()
    if not isinstance(value, Mapping):
        raise ContentMigrationError("metadata must be a JSON object")
    issues: list[str] = []

    def walk(item: Any, *, depth: int, path: str) -> Any:
        if depth > _METADATA_MAX_DEPTH:
            issues.append(f"{path}:depth")
            return None
        if isinstance(item, Mapping):
            output: dict[str, Any] = {}
            for raw_key, raw_value in item.items():
                key = str(raw_key)
                key_path = f"{path}.{key}" if path else key
                if _metadata_key(key) in _FORBIDDEN_METADATA_KEYS:
                    issues.append(f"{key_path}:forbidden")
                    continue
                output[key] = walk(raw_value, depth=depth + 1, path=key_path)
            return output
        if isinstance(item, (list, tuple)):
            return [walk(raw, depth=depth + 1, path=f"{path}[{index}]") for index, raw in enumerate(item)]
        if item is None or isinstance(item, (str, int, float, bool, bytes)):
            return item
        issues.append(f"{path}:unsupported_type")
        return None

    result = walk(value, depth=0, path="")
    assert isinstance(result, dict)
    if len(_json(result).encode("utf-8")) > _METADATA_MAX_BYTES:
        raise ContentMigrationError("metadata exceeds 64 KiB")
    return result, tuple(issues)


def _hash_row(row: Mapping[str, Any]) -> str:
    return hashlib.sha256(_json({str(k): row[k] for k in sorted(row) if not str(k).startswith("__")}).encode("utf-8")).hexdigest()


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="strict")
    return str(value)


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {str(key): row[key] for key in row.keys()}


def _first(row: Mapping[str, Any], *names: str, default: Any = "") -> Any:
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return default


def _pk(table: str, row: Mapping[str, Any]) -> str:
    if table == "embeddings":
        chunk = _as_text(row.get("chunk_id"))
        space = _as_text(row.get("embedding_space_id", row.get("embedding_model", "")))
        if chunk or space:
            return f"{chunk}:{space}"
    choices = {
        "conversation_sessions": ("session_id", "id"),
        "conversation_turns": ("turn_id", "id"),
        "session_summaries": ("session_id", "summary_id", "id"),
        "observations": ("observation_id", "id"),
        "evidence_links": ("link_id", "id"),
        "books": ("book_id", "id"),
        "documents": ("document_id", "id"),
        "chunks": ("chunk_id", "id"),
        "entities": ("entity_id", "id"),
        "relations": ("relation_id", "id"),
        "chunk_entities": ("chunk_id", "entity_id", "id"),
        "embeddings": ("chunk_id", "embedding_space_id", "embedding_model", "id"),
        "memory_candidates": ("candidate_id", "id"),
        "deleted_books": ("deletion_id", "book_id", "id"),
        "index_jobs": ("job_id", "id"),
    }.get(table, ("id", "pk", "key"))
    for name in choices:
        if name in row and row[name] not in (None, ""):
            if table == "chunk_entities" and name == "chunk_id" and row.get("entity_id") is not None:
                return f"{row[name]}:{row['entity_id']}"
            return _as_text(row[name])
    if "__rowid__" in row:
        return _as_text(row["__rowid__"])
    for name, value in row.items():
        if not str(name).startswith("__"):
            return _as_text(value)
    return ""


def _table_names(conn: sqlite3.Connection) -> tuple[str, ...]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name").fetchall()
    names: list[str] = []
    for row in rows:
        name = str(row[0])
        if name.startswith("sqlite_") or name.endswith(("_data", "_idx", "_content", "_docsize", "_config")):
            continue
        names.append(name)
    return tuple(names)


def _iter_rows(conn: sqlite3.Connection, table: str, *, batch_size: int) -> Iterator[dict[str, Any]]:
    quoted = '"' + table.replace('"', '""') + '"'
    try:
        cursor = conn.execute(f"SELECT rowid AS __rowid__, * FROM {quoted} ORDER BY rowid")
    except sqlite3.Error:
        cursor = conn.execute(f"SELECT * FROM {quoted}")
    while True:
        rows = cursor.fetchmany(max(1, int(batch_size)))
        if not rows:
            return
        for row in rows:
            yield _row_dict(row)


class V1ContentMigrator:
    """Migrate explicit V1 history and Knowledge DBs into one Content DB."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        data_home: str | Path | None = None,
        history_path: str | Path | None = None,
        knowledge_path: str | Path | None = None,
        layout: WorkspaceV2Layout | None = None,
        batch_size: int = 1_000,
        page_size: int | None = None,
        namespace_prefix: str = "",
        immutable_sources: bool = False,
    ) -> None:
        _assert_workspace_no_reparse(workspace)
        self.workspace = Path(workspace).expanduser().resolve()
        self.layout = layout or WorkspaceV2Layout(self.workspace)
        self.data_home = Path(data_home).expanduser().resolve() if data_home is not None else None
        self.history_path = Path(history_path).expanduser().resolve() if history_path is not None else self.workspace / ".memoryguard" / "history" / "history.sqlite"
        self.knowledge_path = Path(knowledge_path).expanduser().resolve() if knowledge_path is not None else (self.data_home / "knowledge" / "knowledge.db" if self.data_home is not None else None)
        self.batch_size = max(1, int(page_size or batch_size))
        self.namespace_prefix = str(namespace_prefix)
        self.immutable_sources = bool(immutable_sources)
        self.last_report: MigrationReport | None = None

    def _preflight(self, kind: str, path: Path | None) -> _Source:
        if path is None:
            return _Source(kind, None, "NOT_CONFIGURED")
        if not path.is_file():
            return _Source(kind, path, "NO_SOURCE")
        try:
            with open_database(path, readonly=True, immutable=self.immutable_sources) as conn:
                check = str(conn.execute("PRAGMA integrity_check").fetchone()[0]).lower()
                if check != "ok":
                    raise ContentMigrationError(f"{kind} integrity_check failed: {check}")
                tables = _table_names(conn)
                if not tables:
                    raise ContentMigrationError(f"{kind} source has no tables: {path}")
                if kind == "history" and "history_mutation_receipts" in tables:
                    receipt_columns = {
                        str(info[1])
                        for info in conn.execute("PRAGMA table_info(history_mutation_receipts)").fetchall()
                    }
                    if receipt_columns != set(_HISTORY_RECEIPT_COLUMNS):
                        raise ContentMigrationError(
                            "history_mutation_receipts schema is not the 0.6.2 authority contract"
                        )
                # Trigger a read of schema metadata and one row from each
                # table.  This catches malformed pages before target writes.
                for table in tables:
                    conn.execute(f'SELECT 1 FROM "{table.replace(chr(34), chr(34) * 2)}" LIMIT 1').fetchone()
                return _Source(kind, path, "READY", tables)
        except ContentMigrationError:
            raise
        except (sqlite3.Error, OSError, ValueError) as exc:
            raise ContentMigrationError(f"{kind} source unreadable: {path}") from exc

    def scan_sources(self) -> dict[str, str]:
        """Return statuses without creating V2 files or touching V1 bytes."""
        return {"history": self._preflight("history", self.history_path).status, "knowledge": self._preflight("knowledge", self.knowledge_path).status}

    scan = scan_sources

    def migrate(
        self,
        *,
        mode: str = "ro",
        history_complete: bool = True,
        knowledge_complete: bool = True,
        complete: bool | None = None,
        partial: bool = False,
        fail_after: int | None = None,
        include_history: bool = True,
        include_knowledge: bool = True,
    ) -> MigrationReport:
        if mode not in {"ro", "read_only", "readonly"}:
            raise ValueError("V1 sources must be opened in read-only mode")
        if complete is not None:
            history_complete = knowledge_complete = bool(complete)
        if partial:
            history_complete = knowledge_complete = False
        sources: dict[str, _Source] = {}
        if include_history:
            sources["history"] = self._preflight("history", self.history_path)
        else:
            sources["history"] = _Source("history", self.history_path, "NOT_CONFIGURED")
        if include_knowledge:
            sources["knowledge"] = self._preflight("knowledge", self.knowledge_path)
        else:
            sources["knowledge"] = _Source("knowledge", self.knowledge_path, "NOT_CONFIGURED")

        report = MigrationReport(
            status="OK",
            history_status=sources["history"].status,
            knowledge_status=sources["knowledge"].status,
            source_complete={"history": sources["history"].status == "READY" and history_complete, "knowledge": sources["knowledge"].status == "READY" and knowledge_complete},
        )
        ready = [source for source in sources.values() if source.status == "READY"]
        if not ready:
            report.status = "PARTIAL" if any(source.status == "NO_SOURCE" for source in sources.values()) else "NOT_CONFIGURED"
            self.last_report = report
            return report

        # Both source files were preflighted before this constructor can create
        # a target.  A target transaction now covers all source rows.
        store = ContentStore(self.layout)
        now = _now()
        try:
            with open_database(store.db_path) as target:
                with transaction(target):
                    if fail_after is not None:
                        fail_after = int(fail_after)
                    counter = [0]
                    if sources["history"].status == "READY":
                        self._import_history(target, store, sources["history"], complete=history_complete, report=report, counter=counter, fail_after=fail_after)
                    if sources["knowledge"].status == "READY":
                        self._import_knowledge(target, store, sources["knowledge"], complete=knowledge_complete, report=report, counter=counter, fail_after=fail_after)
        except Exception:
            # The storage transaction rolls back all rows.  Do not convert a
            # source/schema/collision error into a misleading partial result.
            raise
        report.target_counts = store.counts()
        with open_database(store.db_path, readonly=True) as conn:
            report.migration_map_count = int(conn.execute("SELECT COUNT(*) FROM migration_map").fetchone()[0])
            if (
                sources["history"].status == "READY"
                and "history_mutation_receipts" in sources["history"].tables
            ):
                report.target_counts["history_mutation_receipts"] = int(
                    conn.execute("SELECT COUNT(*) FROM history_mutation_receipts").fetchone()[0]
                )
            for kind in ("history", "knowledge"):
                row = conn.execute("SELECT source_id,coverage_digest FROM source_sync_state WHERE source_id LIKE ? ORDER BY source_id LIMIT 1", (f"{kind}-%",)).fetchone()
                if row is not None:
                    report.acl_digests[kind] = str(row[1] or "")
        report.status = "PARTIAL" if any(status in {"NO_SOURCE", "NOT_CONFIGURED"} for status in (report.history_status, report.knowledge_status)) or not all(report.source_complete.values()) else "OK"
        self.last_report = report
        return report

    run = migrate
    execute = migrate
    import_all = migrate
    migrate_content = migrate

    def _check_fail(self, counter: list[int], fail_after: int | None) -> None:
        counter[0] += 1
        if fail_after is not None and counter[0] >= fail_after:
            raise RuntimeError("injected content migration failure")

    def _record_anomaly(
        self,
        conn: sqlite3.Connection,
        *,
        source_id: str,
        error_code: str,
        detail: str,
    ) -> None:
        now = _now()
        fingerprint = stable_id("anomaly", error_code, detail)
        conn.execute(
            "INSERT INTO source_sync_anomalies(source_id,error_fingerprint,error_code,detail,first_seen_at,last_seen_at,occurrence_count,resolved_at) "
            "VALUES(?,?,?,?,?,?,1,'') ON CONFLICT(source_id,error_fingerprint) DO UPDATE SET last_seen_at=excluded.last_seen_at,occurrence_count=source_sync_anomalies.occurrence_count+1,resolved_at=''",
            (source_id, fingerprint, error_code, detail, now, now),
        )

    def _safe_metadata(
        self,
        conn: sqlite3.Connection,
        *,
        source_id: str,
        value: Mapping[str, Any] | None,
        label: str,
    ) -> dict[str, Any]:
        result, issues = _sanitize_metadata(value)
        for issue in issues:
            self._record_anomaly(
                conn,
                source_id=source_id,
                error_code="metadata_filtered",
                detail=f"{label}:{issue}",
            )
        return result

    def _upsert_map(self, conn: sqlite3.Connection, *, source: _Source, table: str, pk: str, target_type: str, target_id: str, source_hash: str, target_hash: str = "", acl: Mapping[str, Any] | None = None, status: str = "mapped", metadata: Mapping[str, Any] | None = None) -> None:
        now = _now()
        source_db = str(source.path or f"<source:{source.kind}>")
        map_id = stable_id("map", source_db, table, pk, target_type)
        safe_metadata, metadata_issues = _sanitize_metadata(metadata)
        digest = acl_digest(acl or {})
        metadata_json = _json(safe_metadata)
        values = (target_id, source_hash, target_hash, digest, status, metadata_json)
        existing = conn.execute(
            "SELECT target_id,source_hash,target_hash,acl_digest,status,metadata_json FROM migration_map "
            "WHERE source_db=? AND source_table=? AND source_pk=? AND target_type=?",
            (source_db, table, pk, target_type),
        ).fetchone()
        if existing is not None and tuple(str(item) for item in existing) != tuple(str(item) for item in values):
            raise ContentMigrationError(
                f"migration_map identity changed for {source_db}:{table}:{pk}:{target_type}"
            )
        for issue in metadata_issues:
            self._record_anomaly(
                conn,
                source_id=stable_id(source.kind, source_db),
                error_code="metadata_filtered",
                detail=f"migration_map:{table}:{pk}:{issue}",
            )
        if existing is not None:
            return
        conn.execute(
            "INSERT INTO migration_map(map_id,source_db,source_table,source_pk,target_type,target_id,source_hash,target_hash,acl_digest,status,metadata_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (map_id, source_db, table, pk, target_type, target_id, source_hash, target_hash, digest, status, metadata_json, now, now),
        )

    def _source_connector(self, target: sqlite3.Connection, store: ContentStore, source: _Source, *, provider: str, source_type: str) -> str:
        source_id = stable_id(source.kind, str(source.path or ""))
        store.upsert_source_connector(source_id=source_id, provider=provider, source_type=source_type, external_root_key=str(source.path or ""), conn=target)
        return source_id

    def _namespace(self, store: ContentStore, target: sqlite3.Connection, *, trust: str, sensitivity: str | None = None, retention: str = "migration") -> str:
        # Missing legacy visibility is explicit unknown, never silently
        # rewritten as the ordinary/private defaults.
        return store.ensure_namespace(workspace_id=str(self.workspace), trust_domain=f"{self.namespace_prefix}{trust}", sensitivity=sensitivity or "__UNKNOWN__", retention_authority=retention, conn=target).namespace_id

    def _source_object(self, target: sqlite3.Connection, *, source_id: str, source_kind: str, external_key: str, object_type: str, title: str = "", metadata: Mapping[str, Any] | None = None) -> str:
        object_id = stable_id("obj", source_id, external_key)
        now = _now()
        safe_metadata = self._safe_metadata(
            target,
            source_id=source_id,
            value=metadata,
            label=f"source_object:{external_key}",
        )
        target.execute(
            "INSERT INTO source_objects(source_object_id,source_kind,external_object_key,title,metadata_json,active,first_seen_at,last_seen_at,source_id,object_type,parent_object_id,deleted_scan_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(source_object_id) DO UPDATE SET title=excluded.title,metadata_json=excluded.metadata_json,active=1,last_seen_at=excluded.last_seen_at,deleted_scan_id=''",
            (object_id, source_kind, external_key, title, _json(safe_metadata), 1, now, now, source_id, object_type, "", ""),
        )
        return object_id

    def _mark_sync(self, target: sqlite3.Connection, *, source_id: str, state: str, complete: bool, run_id: str, manifest: Iterable[str], coverage: Iterable[str]) -> None:
        now = _now()
        manifest = tuple(manifest)
        manifest_digest = hashlib.sha256("\n".join(sorted(manifest)).encode("utf-8")).hexdigest()
        coverage_digest = hashlib.sha256("\n".join(sorted(coverage)).encode("utf-8")).hexdigest()
        for item in manifest:
            external, _, occurrence = item.partition("|")
            if not occurrence:
                occurrence = external
            target.execute(
                "INSERT INTO source_manifest_items(source_id,external_object_key,occurrence_key,source_revision,content_hash,active,last_complete_scan_id) VALUES(?,?,?,?,?,?,?) ON CONFLICT(source_id,external_object_key,occurrence_key) DO UPDATE SET active=1,last_complete_scan_id=CASE WHEN excluded.last_complete_scan_id<>'' THEN excluded.last_complete_scan_id ELSE source_manifest_items.last_complete_scan_id END",
                (source_id, external, occurrence, "", "", 1, run_id if complete else ""),
            )
        target.execute(
            "INSERT INTO source_sync_state(source_id,active_run_id,state,cursor,last_complete_scan_id,manifest_digest,coverage_digest,last_started_at,last_finished_at,last_error_code,revision) VALUES(?,?,?,?,?,?,?,?,?,?,1) "
            "ON CONFLICT(source_id) DO UPDATE SET active_run_id=excluded.active_run_id,state=excluded.state,last_complete_scan_id=CASE WHEN excluded.last_complete_scan_id<>'' THEN excluded.last_complete_scan_id ELSE source_sync_state.last_complete_scan_id END,manifest_digest=excluded.manifest_digest,coverage_digest=excluded.coverage_digest,last_started_at=excluded.last_started_at,last_finished_at=excluded.last_finished_at,revision=source_sync_state.revision+1",
            (source_id, run_id, state, "", run_id if complete else "", manifest_digest, coverage_digest, now, now, "",),
        )

    def _import_history_mutation_receipts(
        self,
        target: sqlite3.Connection,
        source: _Source,
        source_id: str,
        conn: sqlite3.Connection,
        *,
        counter: list[int],
        fail_after: int | None,
        report: MigrationReport,
        seen: set[str],
    ) -> None:
        """Copy the V1 idempotency authority without rewriting its payload."""

        if "history_mutation_receipts" not in source.tables:
            return
        columns = ", ".join(_HISTORY_RECEIPT_COLUMNS)
        for raw in conn.execute(
            f"SELECT {columns} FROM history_mutation_receipts ORDER BY idempotency_key"
        ).fetchall():
            self._check_fail(counter, fail_after)
            values = tuple(raw[column] for column in _HISTORY_RECEIPT_COLUMNS)
            key = _as_text(values[0])
            existing = target.execute(
                f"SELECT {columns} FROM history_mutation_receipts WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            if existing is not None and tuple(existing) != values:
                raise ContentMigrationError(
                    f"history_mutation_receipt identity changed: {key}"
                )
            if existing is None:
                target.execute(
                    f"INSERT INTO history_mutation_receipts({columns}) VALUES (?,?,?,?,?)",
                    values,
                )
            self._upsert_map(
                target,
                source=source,
                table="history_mutation_receipts",
                pk=key,
                target_type="history_mutation_receipt",
                target_id=stable_id("history-receipt", source_id, key),
                source_hash=_hash_row(_row_dict(raw)),
            )
            seen.add(f"receipt:{key}|receipt")
            report.source_counts["history.history_mutation_receipts"] = report.source_counts.get(
                "history.history_mutation_receipts", 0
            ) + 1

    def _import_history(self, target: sqlite3.Connection, store: ContentStore, source: _Source, *, complete: bool, report: MigrationReport, counter: list[int], fail_after: int | None) -> None:
        assert source.path is not None
        source_id = self._source_connector(target, store, source, provider="history", source_type="conversation_history")
        seen: set[str] = set()
        acl_rows: list[str] = []
        run_id = stable_id("scan", source_id)
        with open_database(source.path, readonly=True, immutable=self.immutable_sources) as conn:
            tables = set(source.tables)
            sessions: dict[str, str] = {}
            session_acls: dict[str, dict[str, Any]] = {}
            self._import_history_mutation_receipts(
                target,
                source,
                source_id,
                conn,
                counter=counter,
                fail_after=fail_after,
                report=report,
                seen=seen,
            )
            if "conversation_sessions" in tables:
                for row in _iter_rows(conn, "conversation_sessions", batch_size=self.batch_size):
                    self._check_fail(counter, fail_after)
                    pk = _pk("conversation_sessions", row); source_hash = _hash_row(row)
                    external = f"session:{pk}"
                    obj = self._source_object(target, source_id=source_id, source_kind="history", external_key=external, object_type="session", title=_as_text(_first(row, "title")), metadata={"external_id": _as_text(_first(row, "external_id"))})
                    agent = _as_text(_first(row, "agent_instance_id", "agent_id")); project = _as_text(_first(row, "project_ref")); group = _as_text(_first(row, "share_group_id")); provider_raw = _first(row, "provider", default=None); provider = _as_text(provider_raw) or None
                    policy_raw = _first(row, "policy_class", "visibility", default=None); policy = _as_text(policy_raw) or None
                    sensitivity_raw = _first(row, "sensitivity", default=None); row_sensitivity = _as_text(sensitivity_raw) or None
                    acl = {"workspace_id": str(self.workspace), "agent_instance_id": agent, "project_ref": project, "share_group_id": group, "provider": provider or "__UNKNOWN__", "policy_class": policy or "__UNKNOWN__", "sensitivity": row_sensitivity or "__UNKNOWN__"}; acl_rows.append(_json(acl))
                    session_acls[pk] = {"agent_instance_id": agent, "project_ref": project, "share_group_id": group, "provider": provider, "policy_class": policy, "sensitivity": row_sensitivity}
                    session_provider = provider or "__UNKNOWN__"; session_policy = policy or "__UNKNOWN__"
                    target.execute(
                        "INSERT INTO conversation_sessions(session_id,source_object_id,external_id,title,provider,workspace_id,agent_instance_id,project_ref,share_group_id,policy_class,created_at,imported_at,active) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1) ON CONFLICT(source_object_id) DO UPDATE SET title=excluded.title,provider=excluded.provider,agent_instance_id=excluded.agent_instance_id,project_ref=excluded.project_ref,share_group_id=excluded.share_group_id,active=1",
                        (stable_id("session", source_id, pk), obj, _as_text(_first(row, "external_id", default=pk)), _as_text(_first(row, "title")), session_provider, str(self.workspace), agent, project, group, session_policy, _as_text(_first(row, "created_at")), _as_text(_first(row, "imported_at", "created_at", default=_now()))),
                    )
                    sessions[pk] = stable_id("session", source_id, pk); seen.add(f"{external}|session")
                    self._upsert_map(target, source=source, table="conversation_sessions", pk=pk, target_type="conversation_session", target_id=sessions[pk], source_hash=source_hash, acl=acl)
                    report.source_counts["history.conversation_sessions"] = report.source_counts.get("history.conversation_sessions", 0) + 1
            if "conversation_turns" in tables:
                for row in _iter_rows(conn, "conversation_turns", batch_size=self.batch_size):
                    self._check_fail(counter, fail_after)
                    pk = _pk("conversation_turns", row); source_hash = _hash_row(row); session_pk = _as_text(_first(row, "session_id", "session_ref")); session_id = sessions.get(session_pk, stable_id("session", source_id, session_pk));
                    if not target.execute("SELECT 1 FROM conversation_sessions WHERE session_id=?", (session_id,)).fetchone():
                        obj_session = self._source_object(target, source_id=source_id, source_kind="history", external_key=f"session:{session_pk}", object_type="session")
                        target.execute("INSERT OR IGNORE INTO conversation_sessions(session_id,source_object_id,external_id,workspace_id,imported_at) VALUES(?,?,?,?,?)", (session_id, obj_session, session_pk, str(self.workspace), _now()))
                    event = _as_text(_first(row, "event_key", "event_id", "turn_id", default=pk)) or f"capture:{pk}"
                    external = f"session:{session_pk}"; obj = stable_id("obj", source_id, external); occ_key = f"turn:{event}"
                    content = _as_text(_first(row, "content", "text", "body")); parent_acl = session_acls.get(session_pk, {}); agent = _as_text(_first(row, "agent_instance_id")) or parent_acl.get("agent_instance_id", ""); project = _as_text(_first(row, "project_ref")) or parent_acl.get("project_ref", ""); group = _as_text(_first(row, "share_group_id")) or parent_acl.get("share_group_id", ""); provider = _as_text(_first(row, "provider")) or parent_acl.get("provider"); policy = _as_text(_first(row, "policy_class", "visibility", default=None)) or parent_acl.get("policy_class"); row_sensitivity = _as_text(_first(row, "sensitivity", default=None)) or parent_acl.get("sensitivity");
                    ns = self._namespace(store, target, trust=f"history:{group or agent or 'private'}", sensitivity=row_sensitivity or "__UNKNOWN__")
                    blob = store.put_blob(content, namespace_id=ns, conn=target) if content else None
                    if blob:
                        occ = store.upsert_occurrence(source_object_id=obj, occurrence_key=occ_key, blob_id=blob, source_revision=_as_text(_first(row, "content_hash", "source_revision")), ordinal=int(_first(row, "ordinal", default=0) or 0), locator={"legacy_pk": pk}, content_role="conversation", sensitivity=row_sensitivity, workspace_id=str(self.workspace), agent_instance_id=agent, project_ref=project, share_group_id=group, policy_class=policy, provider=provider, conn=target)
                        target.execute("INSERT INTO conversation_turns(turn_id,occurrence_id,session_ref,role,ordinal,metadata_json,created_at,session_id,event_key,content_type,source_revision) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(turn_id) DO UPDATE SET occurrence_id=excluded.occurrence_id,role=excluded.role,ordinal=excluded.ordinal,metadata_json=excluded.metadata_json,session_id=excluded.session_id,event_key=excluded.event_key,source_revision=excluded.source_revision", (stable_id("turn", source_id, session_pk, event), occ, session_pk, _as_text(_first(row, "role", default="unknown")), int(_first(row, "ordinal", default=0) or 0), _json({"legacy_pk": pk}), _as_text(_first(row, "created_at", default=_now())), session_id, event, _as_text(_first(row, "content_type", default="text")), _as_text(_first(row, "content_hash", "source_revision"))))
                    else:
                        occ = stable_id("occ", obj, occ_key)
                    seen.add(f"{external}|{occ_key}")
                    self._upsert_map(target, source=source, table="conversation_turns", pk=pk, target_type="conversation_turn", target_id=occ, source_hash=source_hash, target_hash=hashlib.sha256(content.encode("utf-8")).hexdigest() if content else "", acl={"workspace_id": str(self.workspace), "agent_instance_id": agent, "project_ref": project, "provider": provider or "__UNKNOWN__", "share_group_id": group, "policy_class": policy or "__UNKNOWN__", "sensitivity": row_sensitivity or "__UNKNOWN__"})
                    report.source_counts["history.conversation_turns"] = report.source_counts.get("history.conversation_turns", 0) + 1
            # Summary/observation/evidence rows are imported even when their
            # text is empty; content rows remain only in Content Plane.
            if "session_summaries" in tables:
                for row in _iter_rows(conn, "session_summaries", batch_size=self.batch_size):
                    self._import_history_summary(target, store, source, source_id, row, sessions, counter, fail_after, report)
            if "observations" in tables:
                for row in _iter_rows(conn, "observations", batch_size=self.batch_size):
                    self._import_history_observation(target, store, source, source_id, row, sessions, counter, fail_after, report)
            if "evidence_links" in tables:
                for row in _iter_rows(conn, "evidence_links", batch_size=self.batch_size):
                    self._check_fail(counter, fail_after); pk = _pk("evidence_links", row); source_hash = _hash_row(row); link_id = stable_id("evidence", source_id, pk); target.execute("INSERT INTO content_evidence_links(link_id,memory_id,session_id,turn_id,occurrence_id,status,created_at,invalidated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(link_id) DO UPDATE SET status=excluded.status,invalidated_at=excluded.invalidated_at", (link_id, _as_text(_first(row, "memory_id")), _as_text(_first(row, "session_id")), _as_text(_first(row, "turn_id")), "", _as_text(_first(row, "status", default="valid")), _as_text(_first(row, "created_at", default=_now())), _as_text(_first(row, "invalidated_at")))); self._upsert_map(target, source=source, table="evidence_links", pk=pk, target_type="content_evidence_link", target_id=link_id, source_hash=source_hash); report.source_counts["history.evidence_links"] = report.source_counts.get("history.evidence_links", 0) + 1
            self._map_unhandled_rows(target, source, tables, known={"conversation_sessions", "conversation_turns", "session_summaries", "observations", "evidence_links", "history_mutation_receipts", "history_fts"}, report=report, counter=counter, fail_after=fail_after)
        self._tombstone_missing(target, source_id=source_id, seen=seen, complete=complete, run_id=run_id, reason="source_deleted")
        self._mark_sync(target, source_id=source_id, state="complete" if complete else "partial", complete=complete, run_id=run_id, manifest=seen, coverage=acl_rows)
        report.acl_digests["history"] = hashlib.sha256("\n".join(sorted(acl_rows)).encode("utf-8")).hexdigest()

    def _import_history_summary(self, target: sqlite3.Connection, store: ContentStore, source: _Source, source_id: str, row: Mapping[str, Any], sessions: Mapping[str, str], counter: list[int], fail_after: int | None, report: MigrationReport) -> None:
        self._check_fail(counter, fail_after); pk = _pk("session_summaries", row); session_pk = _as_text(_first(row, "session_id")); sid = sessions.get(session_pk, stable_id("session", source_id, session_pk)); obj = stable_id("obj", source_id, f"session:{session_pk}");
        if not target.execute("SELECT 1 FROM conversation_sessions WHERE session_id=?", (sid,)).fetchone():
            self._source_object(target, source_id=source_id, source_kind="history", external_key=f"session:{session_pk}", object_type="session")
            target.execute("INSERT OR IGNORE INTO conversation_sessions(session_id,source_object_id,external_id,workspace_id,imported_at) VALUES(?,?,?,?,?)", (sid, obj, session_pk, str(self.workspace), _now()))
        text = _as_text(_first(row, "summary", "content", "text")); ns = self._namespace(store, target, trust=f"history:{session_pk}", sensitivity="__UNKNOWN__"); blob = store.put_blob(text, namespace_id=ns, conn=target) if text else None; occ = ""; key = f"summary:{_as_text(_first(row, 'summary_kind', default='import'))}";
        if blob:
            occ = store.upsert_occurrence(source_object_id=obj, occurrence_key=key, blob_id=blob, content_role="summary", workspace_id=str(self.workspace), conn=target)
        target.execute("INSERT INTO conversation_summaries(summary_id,session_id,occurrence_id,summary_kind,summary_hash,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(session_id,summary_kind) DO UPDATE SET occurrence_id=excluded.occurrence_id,summary_hash=excluded.summary_hash,updated_at=excluded.updated_at", (stable_id("summary", source_id, pk), sid, occ or None, _as_text(_first(row, "summary_kind", default="import")), hashlib.sha256(text.encode("utf-8")).hexdigest() if text else "", _as_text(_first(row, "updated_at", default=_now())))); self._upsert_map(target, source=source, table="session_summaries", pk=pk, target_type="conversation_summary", target_id=stable_id("summary", source_id, pk), source_hash=_hash_row(row)); report.source_counts["history.session_summaries"] = report.source_counts.get("history.session_summaries", 0) + 1

    def _import_history_observation(self, target: sqlite3.Connection, store: ContentStore, source: _Source, source_id: str, row: Mapping[str, Any], sessions: Mapping[str, str], counter: list[int], fail_after: int | None, report: MigrationReport) -> None:
        self._check_fail(counter, fail_after); pk = _pk("observations", row); session_pk = _as_text(_first(row, "session_id")); sid = sessions.get(session_pk, stable_id("session", source_id, session_pk)); obj = stable_id("obj", source_id, f"session:{session_pk}");
        if not target.execute("SELECT 1 FROM conversation_sessions WHERE session_id=?", (sid,)).fetchone():
            self._source_object(target, source_id=source_id, source_kind="history", external_key=f"session:{session_pk}", object_type="session")
            target.execute("INSERT OR IGNORE INTO conversation_sessions(session_id,source_object_id,external_id,workspace_id,imported_at) VALUES(?,?,?,?,?)", (sid, obj, session_pk, str(self.workspace), _now()))
        text = _as_text(_first(row, "summary", "content", "text")); ns = self._namespace(store, target, trust=f"history:{session_pk}", sensitivity="__UNKNOWN__"); blob = store.put_blob(text, namespace_id=ns, conn=target) if text else None; occ = ""; key = f"observation:{pk}";
        if blob:
            occ = store.upsert_occurrence(source_object_id=obj, occurrence_key=key, blob_id=blob, content_role="observation", workspace_id=str(self.workspace), conn=target)
        oid = stable_id("observation", source_id, pk); target.execute("INSERT INTO conversation_observations(observation_id,session_id,turn_id,occurrence_id,observation_type,summary_hash,created_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(observation_id) DO UPDATE SET occurrence_id=excluded.occurrence_id,summary_hash=excluded.summary_hash,observation_type=excluded.observation_type", (oid, sid, _as_text(_first(row, "turn_id")), occ or None, _as_text(_first(row, "observation_type", default="import")), hashlib.sha256(text.encode("utf-8")).hexdigest() if text else "", _as_text(_first(row, "created_at", default=_now())))); self._upsert_map(target, source=source, table="observations", pk=pk, target_type="conversation_observation", target_id=oid, source_hash=_hash_row(row)); report.source_counts["history.observations"] = report.source_counts.get("history.observations", 0) + 1

    def _import_knowledge(self, target: sqlite3.Connection, store: ContentStore, source: _Source, *, complete: bool, report: MigrationReport, counter: list[int], fail_after: int | None) -> None:
        assert source.path is not None
        source_id = self._source_connector(target, store, source, provider="knowledge", source_type="knowledge_db")
        run_id = stable_id("scan", source_id); seen: set[str] = set(); active_books: set[str] = set(); acl_rows: list[str] = []
        with open_database(source.path, readonly=True, immutable=self.immutable_sources) as conn:
            tables = set(source.tables)
            known_tables = set(_KNOWN_KNOWLEDGE_TABLES) | {
                table for table in tables if table.endswith("_fts")
            }
            for table in sorted(tables):
                if table not in known_tables:
                    continue
                for row in _iter_rows(conn, table, batch_size=self.batch_size):
                    self._check_fail(counter, fail_after); pk = _pk(table, row); source_hash = _hash_row(row); acl_sensitivity = _as_text(_first(row, "sensitivity", default=None)) or None; acl_policy = _as_text(_first(row, "policy_class", "visibility", default=None)) or None; acl_provider = _as_text(_first(row, "provider", default=None)) or None; acl = {"workspace_id": str(self.workspace), "agent_instance_id": "", "project_ref": _as_text(_first(row, "root_path", "relative_path")), "provider": acl_provider or "__UNKNOWN__", "share_group_id": "", "policy_class": acl_policy or "__UNKNOWN__", "sensitivity": acl_sensitivity or "__UNKNOWN__"}; acl_rows.append(_json(acl));
                    if table == "deleted_books":
                        book_id = _as_text(_first(row, "book_id", default=pk)); obj = self._source_object(target, source_id=source_id, source_kind="knowledge", external_key=f"book:{book_id}", object_type="book", title=_as_text(_first(row, "title")), metadata={"root_path": _as_text(_first(row, "root_path")), "snapshot_hash": hashlib.sha256(_as_text(_first(row, "snapshot_json")).encode("utf-8")).hexdigest()}); tomb = stable_id("tomb", obj, "book_deleted"); target.execute("INSERT INTO content_tombstones(tombstone_id,source_object_id,occurrence_id,blob_id,reason,scan_id,metadata_json,created_at,restored_at,active) VALUES(?,?,?,?,?,?,?,?,?,1) ON CONFLICT(source_object_id,occurrence_id,reason) DO UPDATE SET scan_id=excluded.scan_id,metadata_json=excluded.metadata_json,active=1,restored_at=''", (tomb, obj, "", "", "book_deleted", run_id, _json({"title": _as_text(_first(row, "title")), "root_path": _as_text(_first(row, "root_path")), "snapshot_hash": hashlib.sha256(_as_text(_first(row, "snapshot_json")).encode("utf-8")).hexdigest()}), _now(), "")); seen.add(f"book:{book_id}|book"); report.source_counts[f"knowledge.{table}"] = report.source_counts.get(f"knowledge.{table}", 0) + 1; continue
                        # A deleted book never copies its recovery snapshot.
                        # Existing canonical chunk/candidate blobs are held by
                        # reference so a later restore can reuse them.
                        candidates = target.execute("SELECT content_blob_id,metadata_json FROM knowledge_records WHERE content_blob_id<>''").fetchall()
                        for candidate in candidates:
                            try:
                                metadata = json.loads(str(candidate[1] or "{}"))
                            except (TypeError, ValueError):
                                metadata = {}
                            if _as_text(metadata.get("book_id")) == book_id:
                                target.execute("INSERT INTO content_holds(hold_id,blob_id,reason,source_ref,active,created_at,released_at) VALUES(?,?,?,?,1,?,'') ON CONFLICT(blob_id,reason,source_ref) DO UPDATE SET active=1,released_at=''", (stable_id("hold", candidate[0], "book_deleted", book_id), candidate[0], "book_deleted", book_id, _now()))
                        self._upsert_map(target, source=source, table=table, pk=pk, target_type="knowledge_tombstone", target_id=tomb, source_hash=source_hash, acl=acl, metadata={"no_body_copied": True}); seen.add(f"book:{book_id}|book"); report.source_counts[f"knowledge.{table}"] = report.source_counts.get(f"knowledge.{table}", 0) + 1; continue
                    if table == "books":
                        active_books.add(_as_text(_first(row, "book_id", default=pk)))
                    if table in {"chunks", "memory_candidates"}:
                        content = _as_text(_first(row, "text", "content"));
                    else:
                        content = ""
                    blob = None; target_id = stable_id("record", source_id, table, pk)
                    if content:
                        ns = self._namespace(store, target, trust="knowledge", sensitivity=acl_sensitivity or "__UNKNOWN__", retention="knowledge"); blob = store.put_blob(content, namespace_id=ns, conn=target)
                        book_id = _as_text(_first(row, "book_id", default="")); doc_id = _as_text(_first(row, "document_id", default="")); ext = f"chunk:{pk}" if table == "chunks" else f"candidate:{pk}"; obj = self._source_object(target, source_id=source_id, source_kind="knowledge", external_key=ext, object_type=table[:-1] if table.endswith("s") else table, metadata={"book_id": book_id, "document_id": doc_id}); occ = store.upsert_occurrence(source_object_id=obj, occurrence_key=ext, blob_id=blob, source_revision=_as_text(_first(row, "text_hash", "source_text_hash")), ordinal=int(_first(row, "ordinal", default=0) or 0), locator={"legacy_pk": pk, "book_id": book_id, "document_id": doc_id}, content_role="knowledge" if table == "chunks" else "candidate", sensitivity=acl_sensitivity, workspace_id=str(self.workspace), project_ref=_as_text(_first(row, "relative_path", "root_path")), policy_class=acl_policy, provider=acl_provider, conn=target); target_id = occ; seen.add(f"{ext}|{ext}")
                    if table in {"embeddings", "chunks_fts", "history_fts"}:
                        derived = "DERIVED_REBUILD"; metadata = self._safe_metadata(target, source_id=source_id, value={key: value for key, value in row.items() if key not in {"vector", "embedding", "content", "text"}}, label=f"derived:{table}:{pk}"); target.execute("INSERT INTO knowledge_records(record_id,source_table,source_pk,record_type,content_blob_id,status,derived_status,metadata_json) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source_table,source_pk,record_type) DO UPDATE SET metadata_json=excluded.metadata_json,derived_status=excluded.derived_status", (target_id, table, pk, "derived", "", "active", derived, _json(metadata))); self._upsert_map(target, source=source, table=table, pk=pk, target_type="derived_rebuild", target_id=target_id, source_hash=source_hash, target_hash="DERIVED_REBUILD", acl=acl); report.source_counts[f"knowledge.{table}"] = report.source_counts.get(f"knowledge.{table}", 0) + 1; continue
                    if table in {"relations", "chunk_entities"}:
                        relation_type = "relation" if table == "relations" else "chunk_entity"
                        relation_id = stable_id("relation", source_id, table, pk)
                        relation_payload = self._safe_metadata(
                            target,
                            source_id=source_id,
                            value={key: value for key, value in row.items() if key not in {"vector", "embedding", "text", "content"}},
                            label=f"relation:{table}:{pk}",
                        )
                        target.execute("INSERT INTO knowledge_relations(relation_id,source_table,source_pk,relation_type,payload_json) VALUES(?,?,?,?,?) ON CONFLICT(source_table,source_pk,relation_type) DO UPDATE SET payload_json=excluded.payload_json", (relation_id, table, pk, relation_type, _json(relation_payload)))
                    metadata = self._safe_metadata(
                        target,
                        source_id=source_id,
                        value={key: value for key, value in row.items() if key not in {"text", "content", "snapshot_json", "vector", "embedding"}},
                        label=f"knowledge:{table}:{pk}",
                    ); target.execute("INSERT INTO knowledge_records(record_id,source_table,source_pk,record_type,content_blob_id,status,derived_status,metadata_json) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source_table,source_pk,record_type) DO UPDATE SET content_blob_id=excluded.content_blob_id,metadata_json=excluded.metadata_json,status=excluded.status", (target_id, table, pk, table, blob or "", "active", "CANONICAL", _json(metadata))); self._upsert_map(target, source=source, table=table, pk=pk, target_type="knowledge_record", target_id=target_id, source_hash=source_hash, target_hash=hashlib.sha256(content.encode("utf-8")).hexdigest() if content else "", acl=acl, metadata={"content_blob_id": blob or ""}); report.source_counts[f"knowledge.{table}"] = report.source_counts.get(f"knowledge.{table}", 0) + 1
            self._map_unhandled_rows(target, source, tables, known=known_tables, report=report, counter=counter, fail_after=fail_after)
        # Deleted-book rows may sort before chunks/candidates.  Resolve holds
        # after all canonical records have been imported so every existing
        # book blob is retained without copying a recovery snapshot.
        deleted_objects = target.execute("SELECT source_object_id,external_object_key FROM source_objects WHERE source_id=? AND object_type='book' AND external_object_key LIKE 'book:%'", (source_id,)).fetchall()
        for object_row in deleted_objects:
            book_id = _as_text(object_row[1])[5:]
            tombstone = target.execute("SELECT 1 FROM content_tombstones WHERE source_object_id=? AND reason='book_deleted' AND active=1", (object_row[0],)).fetchone()
            if tombstone is None:
                continue
            candidates = target.execute("SELECT content_blob_id,metadata_json FROM knowledge_records WHERE content_blob_id<>''").fetchall()
            for candidate in candidates:
                try:
                    metadata = json.loads(str(candidate[1] or "{}"))
                except (TypeError, ValueError):
                    metadata = {}
                if _as_text(metadata.get("book_id")) == book_id:
                    target.execute("INSERT INTO content_holds(hold_id,blob_id,reason,source_ref,active,created_at,released_at) VALUES(?,?,?,?,1,?,'') ON CONFLICT(blob_id,reason,source_ref) DO UPDATE SET active=1,released_at=''", (stable_id("hold", candidate[0], "book_deleted", book_id), candidate[0], "book_deleted", book_id, _now()))
        if complete and active_books:
            for book_id in sorted(active_books):
                rows = target.execute("SELECT source_object_id FROM source_objects WHERE source_id=? AND external_object_key=?", (source_id, f"book:{book_id}")).fetchall()
                for object_row in rows:
                    target.execute("UPDATE content_tombstones SET active=0,restored_at=? WHERE source_object_id=? AND reason='book_deleted' AND active=1", (_now(), object_row[0]))
                    target.execute("UPDATE content_holds SET active=0,released_at=? WHERE reason='book_deleted' AND source_ref=? AND active=1", (_now(), book_id))
        self._mark_sync(target, source_id=source_id, state="complete" if complete else "partial", complete=complete, run_id=run_id, manifest=seen, coverage=acl_rows); report.acl_digests["knowledge"] = hashlib.sha256("\n".join(sorted(acl_rows)).encode("utf-8")).hexdigest()

    def _map_unhandled_rows(self, target: sqlite3.Connection, source: _Source, tables: Iterable[str], *, known: set[str], report: MigrationReport, counter: list[int], fail_after: int | None) -> None:
        assert source.path is not None
        source_id = stable_id(source.kind, str(source.path))
        with open_database(source.path, readonly=True, immutable=self.immutable_sources) as conn:
            for table in sorted(set(tables) - known):
                self._record_anomaly(
                    target,
                    source_id=source_id,
                    error_code="unknown_authoritative_content",
                    detail=f"{table}:unknown_table",
                )
                for row in _iter_rows(conn, table, batch_size=self.batch_size):
                    self._check_fail(counter, fail_after); pk = _pk(table, row); digest = _hash_row(row)
                    metadata, issues = _sanitize_metadata(
                        {"legacy_table": table, "legacy_row": row},
                    )
                    self._record_anomaly(
                        target,
                        source_id=source_id,
                        error_code="unknown_authoritative_content",
                        detail=f"{table}:{pk}:unknown_table",
                    )
                    for issue in issues:
                        self._record_anomaly(
                            target,
                            source_id=source_id,
                            error_code="unknown_authoritative_content",
                            detail=f"{table}:{pk}:{issue}",
                        )
                    blocked = True
                    metadata["derived_status"] = "BLOCKED" if blocked else "metadata_only"
                    self._upsert_map(
                        target,
                        source=source,
                        table=table,
                        pk=pk,
                        target_type="legacy_row",
                        target_id=stable_id("legacy", source.kind, table, pk),
                        source_hash=digest,
                        target_hash="",
                        status="blocked" if blocked else "mapped",
                        metadata=metadata,
                    )
                    report.source_counts[f"{source.kind}.{table}"] = report.source_counts.get(f"{source.kind}.{table}", 0) + 1

    def _tombstone_missing(self, target: sqlite3.Connection, *, source_id: str, seen: set[str], complete: bool, run_id: str, reason: str) -> None:
        if not complete:
            return
        rows = target.execute("SELECT o.occurrence_id,o.source_object_id,o.occurrence_key,o.blob_id,so.external_object_key FROM content_occurrences o JOIN source_objects so ON so.source_object_id=o.source_object_id WHERE so.source_id=? AND o.active=1", (source_id,)).fetchall()
        for row in rows:
            key = f"{row[4]}|{row[2]}"
            if key in seen:
                continue
            now = _now(); target.execute("UPDATE content_occurrences SET active=0,deleted_scan_id=?,last_seen_at=? WHERE occurrence_id=?", (run_id, now, row[0])); tomb = stable_id("tomb", row[0], reason); target.execute("INSERT INTO content_tombstones(tombstone_id,source_object_id,occurrence_id,blob_id,reason,scan_id,metadata_json,created_at,restored_at,active) VALUES(?,?,?,?,?,?,?,?,?,1) ON CONFLICT(source_object_id,occurrence_id,reason) DO UPDATE SET scan_id=excluded.scan_id,active=1,restored_at=''", (tomb, row[1], row[0], row[3], reason, run_id, "{}", now, "")); target.execute("INSERT INTO content_holds(hold_id,blob_id,reason,source_ref,active,created_at,released_at) VALUES(?,?,?,?,1,?,'') ON CONFLICT(blob_id,reason,source_ref) DO UPDATE SET active=1,released_at=''", (stable_id("hold", row[3], reason, row[0]), row[3], reason, row[0], now))


__all__ = ["ContentMigrationError", "MigrationError", "MigrationReport", "V1ContentMigrator"]
