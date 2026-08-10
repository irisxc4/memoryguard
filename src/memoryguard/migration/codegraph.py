"""Read-only V1 CodeGraph/Knowledge relation migration into CodeGraph V2.

The legacy database is opened through SQLite ``mode=ro`` and only bounded
metadata columns are selected.  Rows whose authority or ownership is absent
or unknown are recorded in the V2 unknown ledger as ``BLOCKED``; they are never
silently promoted into a trusted graph.  Migration maps pin the source row
hash, so a changed source fails closed rather than rewriting an immutable
mapping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
from typing import Any, Iterable, Iterator, Mapping, Sequence

from ..codegraph_v2 import CodeGraphProjector, CodeGraphScope, CodeGraphStore, Edge, CodeGraphError, stable_digest, stable_id
from ..codegraph_v2.store import _assert_no_reparse, normalize_relative_path
from ..storage.layout import WorkspaceV2Layout
from ..storage.database import connect_database


class CodeGraphMigrationError(RuntimeError):
    """A configured legacy source cannot be migrated safely."""


MigrationError = CodeGraphMigrationError

_BODY_KEYS = frozenset({
    "body", "text", "raw", "content", "source_text", "document", "document_body", "payload", "full_transcript", "transcript",
})
_AUTHORITY_VALUES = frozenset({"trusted", "observed", "public", "system", "source", "v1", "workspace"})
_OWNERSHIP_VALUES = frozenset({"owned", "workspace", "project", "agent", "agent_managed", "external_read_only", "public", "source"})
_PATH_KEYS = ("path", "relative_path", "file_path", "source_path", "filename")
_HASH_KEYS = ("content_hash", "source_hash", "file_hash", "hash", "digest")
_PK_KEYS = ("id", "key", "pk", "symbol_id", "edge_id", "relation_id", "node_id")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_json(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, bytes):
        return {"bytes_hash": hashlib.sha256(value).hexdigest(), "byte_count": len(value)}
    return str(value)


def _row_hash(row: Mapping[str, Any]) -> str:
    safe = {
        str(key): _safe_json(value)
        for key, value in sorted(row.items())
        if str(key).lower() not in _BODY_KEYS and not str(key).startswith("__")
    }
    return stable_digest(safe)


def _table_names(conn: sqlite3.Connection) -> tuple[str, ...]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    return tuple(str(row[0]) for row in rows if not str(row[0]).startswith("sqlite_"))


def _safe_columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    body_tokens = {"body", "text", "raw", "content", "payload", "document", "transcript", "vector", "embedding"}
    columns: list[str] = []
    for row in conn.execute(f'PRAGMA table_info("{table.replace(chr(34), chr(34) * 2)}")').fetchall():
        name = str(row[1])
        lowered = name.lower()
        if lowered in body_tokens or any(token in lowered for token in ("_body", "_text", "_content", "_payload", "transcript")):
            continue
        columns.append(name)
    return tuple(columns)


def _iter_rows(conn: sqlite3.Connection, table: str, *, batch_size: int = 1000) -> Iterator[dict[str, Any]]:
    columns = _safe_columns(conn, table)
    if not columns:
        yield {"__rowid__": ""}
        return
    quoted = ",".join('"' + col.replace('"', '""') + '"' for col in columns)
    try:
        cursor = conn.execute(f'SELECT rowid AS "__rowid__",{quoted} FROM "{table.replace(chr(34), chr(34) * 2)}" ORDER BY rowid')
    except sqlite3.Error:
        cursor = conn.execute(f'SELECT {quoted} FROM "{table.replace(chr(34), chr(34) * 2)}"')
    while True:
        rows = cursor.fetchmany(max(1, int(batch_size)))
        if not rows:
            return
        for row in rows:
            yield {str(key): row[key] for key in row.keys()}


def _value(row: Mapping[str, Any], *keys: str, default: Any = "") -> Any:
    lowered = {str(key).lower(): key for key in row}
    for key in keys:
        actual = lowered.get(key.lower())
        if actual is not None and row.get(actual) not in (None, ""):
            return row.get(actual)
    return default


def _pk(row: Mapping[str, Any]) -> str:
    for key in _PK_KEYS:
        value = _value(row, key)
        if value not in (None, ""):
            return str(value)
    value = row.get("__rowid__")
    return str(value or stable_digest(row))


@dataclass
class CodeGraphMigrationReport:
    status: str = "NOT_CONFIGURED"
    source_status: str = "NOT_CONFIGURED"
    source_path: str = ""
    source_hash: str = ""
    source_tables: tuple[str, ...] = ()
    source_rows: int = 0
    projected_files: int = 0
    projected_symbols: int = 0
    projected_edges: int = 0
    blocked_rows: int = 0
    unknown_authority: int = 0
    unknown_ownership: int = 0
    orphan: int = 0
    loss: int = 0
    outbox_pending: int = 0
    errors: list[str] = field(default_factory=list)
    source_complete: bool = False

    @property
    def ok(self) -> bool:
        return self.status in {"OK", "PARTIAL", "BLOCKED", "NO_SOURCE", "NOT_CONFIGURED"}

    @property
    def ready(self) -> bool:
        return False

    @property
    def can_promote(self) -> bool:
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "source_status": self.source_status,
            "source_path": self.source_path,
            "source_hash": self.source_hash,
            "source_tables": list(self.source_tables),
            "source_rows": self.source_rows,
            "counts": {
                "source_rows": self.source_rows,
                "projected_files": self.projected_files,
                "projected_symbols": self.projected_symbols,
                "projected_edges": self.projected_edges,
                "blocked_rows": self.blocked_rows,
            },
            "unknown": {
                "authority": self.unknown_authority,
                "ownership": self.unknown_ownership,
                "blocked": self.blocked_rows,
            },
            "loss": self.loss,
            "orphan": self.orphan,
            "outbox_pending": self.outbox_pending,
            "source_complete": self.source_complete,
            "ready": False,
            "can_promote": False,
            "errors": list(self.errors),
        }

    as_dict = to_dict


@dataclass(frozen=True)
class _Source:
    path: Path | None
    status: str
    tables: tuple[str, ...] = ()
    digest: str = ""


class V1CodeGraphMigrator:
    """Read legacy LightGraph/Knowledge relation metadata and project safely."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        source_path: str | Path | None = None,
        codegraph_path: str | Path | None = None,
        knowledge_path: str | Path | None = None,
        scope: CodeGraphScope | Mapping[str, Any] | None = None,
        authority: str | None = None,
        ownership: str | None = None,
        batch_size: int = 1000,
    ) -> None:
        _assert_no_reparse(workspace)
        self.workspace = Path(workspace).expanduser().resolve()
        raw_source = source_path or codegraph_path or knowledge_path
        if raw_source is not None:
            _assert_no_reparse(raw_source)
        self.source_path = Path(raw_source).expanduser().resolve() if raw_source else None
        if scope is None:
            scope = CodeGraphScope(str(self.workspace), "migration", "legacy", "migration", "legacy", "migration", True)
        self.scope = CodeGraphScope.from_value(scope)
        self.default_authority = str(authority or "")
        self.default_ownership = str(ownership or "")
        self.batch_size = max(1, int(batch_size))
        self.last_report: CodeGraphMigrationReport | None = None

    def _preflight(self) -> _Source:
        if self.source_path is None:
            return _Source(None, "NOT_CONFIGURED")
        if not self.source_path.is_file():
            return _Source(self.source_path, "NO_SOURCE")
        conn: sqlite3.Connection | None = None
        try:
            conn = connect_database(self.source_path, readonly=True)
            check = str(conn.execute("PRAGMA integrity_check").fetchone()[0]).lower()
            if check != "ok":
                raise CodeGraphMigrationError(f"source integrity_check failed: {check}")
            tables = _table_names(conn)
            if not tables:
                raise CodeGraphMigrationError("source has no tables")
            for table in tables:
                conn.execute(f'SELECT 1 FROM "{table.replace(chr(34), chr(34) * 2)}" LIMIT 1').fetchone()
            return _Source(self.source_path, "READY", tables, _hash_file(self.source_path))
        except CodeGraphMigrationError:
            raise
        except (sqlite3.Error, OSError, ValueError) as exc:
            raise CodeGraphMigrationError(f"source unreadable: {self.source_path}") from exc
        finally:
            if conn is not None:
                conn.close()

    def scan_sources(self) -> dict[str, Any]:
        source = self._preflight()
        return {"status": source.status, "path": str(source.path or ""), "tables": list(source.tables), "source_hash": source.digest}

    scan = scan_sources

    def _authority_ownership(self, row: Mapping[str, Any]) -> tuple[bool, str, str]:
        authority = str(_value(row, "authority", "trust", "source_authority", default=self.default_authority) or "").strip().lower()
        ownership = str(_value(row, "ownership", "owner_type", "source_ownership", default=self.default_ownership) or "").strip().lower()
        return authority in _AUTHORITY_VALUES, authority, ownership

    def _row_kind(self, table: str, row: Mapping[str, Any]) -> str:
        lowered = table.lower()
        if any(token in lowered for token in ("edge", "relation", "link")):
            return "edge"
        if any(token in lowered for token in ("symbol", "node", "entity", "claim")):
            return "symbol"
        if any(key in {str(k).lower() for k in row} for key in _PATH_KEYS):
            return "file"
        return "unknown"

    def _file_path(self, row: Mapping[str, Any]) -> str:
        return str(_value(row, *_PATH_KEYS, default="") or "").strip()

    def _symbol_mapping(self, row: Mapping[str, Any], *, table: str, pk: str) -> dict[str, Any]:
        name = str(_value(row, "name", "label", "symbol", "display_label", default=pk) or pk)
        kind = str(_value(row, "kind", "symbol_kind", "node_kind", "entity_type", default="symbol") or "symbol")
        signature = str(_value(row, "signature", "signature_hash", "declaration", default="") or "")
        symbol_hash = str(_value(row, "symbol_hash", "hash", "digest", default="") or "")
        line_start = int(_value(row, "line_start", "start_line", default=0) or 0)
        line_end = int(_value(row, "line_end", "end_line", default=0) or 0)
        return {"symbol_id": stable_id("legacy-symbol", table, pk), "name": name, "kind": kind, "signature": signature, "symbol_hash": symbol_hash, "line_start": max(0, line_start), "line_end": max(0, line_end)}

    def _edge_mapping(self, row: Mapping[str, Any], *, table: str, pk: str) -> dict[str, Any] | None:
        from_id = str(_value(row, "from_id", "from", "source_id", "subject_id", "subject", "source", default="") or "")
        to_id = str(_value(row, "to_id", "to", "target_id", "object_id", "object", "target", default="") or "")
        if not from_id or not to_id:
            return None
        relation = str(_value(row, "relation", "edge_kind", "predicate", "type", default="related") or "related")
        return {"edge_id": stable_id("legacy-edge", table, pk), "from_id": from_id, "to_id": to_id, "relation": relation}

    @staticmethod
    def _backup_target(path: Path) -> tuple[Path | None, bool]:
        """Create a SQLite-consistent compensation snapshot before writes."""

        existed = path.is_file()
        if not existed:
            return None, False
        backup = path.with_name(path.name + ".migration-backup")
        source: sqlite3.Connection | None = None
        target: sqlite3.Connection | None = None
        completed = False
        try:
            if backup.exists():
                backup.unlink()
            source = sqlite3.connect(str(path))
            target = sqlite3.connect(str(backup))
            source.backup(target)
            completed = True
            return backup, True
        finally:
            if target is not None:
                target.close()
            if source is not None:
                source.close()
            if not completed:
                try:
                    backup.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _restore_target(path: Path, backup: Path | None, existed: bool) -> None:
        """Restore the pre-migration DB or remove a newly-created target."""

        for sidecar in (Path(str(path) + "-wal"), Path(str(path) + "-shm")):
            try:
                sidecar.unlink(missing_ok=True)
            except OSError:
                pass
        if existed and backup is not None:
            os.replace(str(backup), str(path))
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def migrate(
        self,
        *,
        mode: str = "ro",
        dry_run: bool = False,
        write_shadow: bool = True,
        fail_after: int | None = None,
    ) -> CodeGraphMigrationReport:
        if mode not in {"ro", "read_only", "readonly"}:
            raise ValueError("legacy CodeGraph source must be opened read-only")
        source = self._preflight()
        report = CodeGraphMigrationReport(source_status=source.status, source_path=str(source.path or ""), source_hash=source.digest, source_tables=source.tables, source_complete=source.status == "READY")
        if source.status != "READY":
            report.status = source.status
            self.last_report = report
            return report
        rows: list[tuple[str, str, dict[str, Any], str, str]] = []
        try:
            assert source.path is not None
            conn = connect_database(source.path, readonly=True)
            try:
                for table in source.tables:
                    for row in _iter_rows(conn, table, batch_size=self.batch_size):
                        pk = _pk(row)
                        row_hash = _row_hash(row)
                        kind = self._row_kind(table, row)
                        authority_ok, authority, ownership = self._authority_ownership(row)
                        ownership_ok = ownership in _OWNERSHIP_VALUES
                        rows.append((table, pk, row, kind, row_hash))
                        report.source_rows += 1
                        if not authority_ok:
                            report.unknown_authority += 1
                        if not ownership_ok:
                            report.unknown_ownership += 1
                        if not authority_ok or not ownership_ok:
                            report.blocked_rows += 1
            finally:
                conn.close()
            # Establish file/symbol identities before traversing relation
            # rows.  SQLite table order is not a dependency order (and the
            # legacy ``edges`` table commonly sorts before ``files``).
            rows.sort(key=lambda item: {"file": 0, "symbol": 1, "edge": 2}.get(item[3], 3))
            # Validate every source path before creating/opening the target.
            # A malformed path is a batch failure, never a partially projected
            # migration.  Body-bearing columns were excluded by _safe_columns;
            # the source bytes remain read-only and are never copied.
            for table, pk, row, kind, _row_hash_value in rows:
                if kind in {"file", "symbol"}:
                    path = self._file_path(row)
                    if path:
                        normalize_relative_path(path)
                    elif kind == "file":
                        raise CodeGraphMigrationError(f"missing source path: {table}:{pk}")
            if dry_run or not write_shadow:
                report.status = "BLOCKED" if report.blocked_rows else "OK"
                report.loss = report.blocked_rows
                self.last_report = report
                return report
            if fail_after is not None:
                # The migration API exposes fault injection for acceptance
                # tests.  Fail before opening/creating the target so a
                # simulated cross-domain fault cannot leave half a graph.
                raise RuntimeError("injected codegraph migration failure")
            target_path = WorkspaceV2Layout(self.workspace).codegraph_db
            backup_path, target_existed = self._backup_target(target_path)
            store = CodeGraphStore(self.workspace)
            # Check all immutable maps before any target write.  A changed
            # source must fail before a second, partial batch is projected.
            target = connect_database(store.db_path, readonly=True)
            try:
                for table, pk, _row, _kind, row_hash in rows:
                    existing = target.execute("SELECT source_hash FROM migration_map WHERE source_db=? AND source_table=? AND source_pk=? AND target_type=?", (str(source.path), table, pk, "codegraph")).fetchone()
                    if existing is not None and str(existing[0]) != row_hash:
                        raise CodeGraphMigrationError(f"migration map source hash changed: {table}:{pk}")
            finally:
                target.close()
            projected_symbols_by_source: dict[str, str] = {}
            processed = 0
            for table, pk, row, kind, row_hash in rows:
                source_ref = f"{table}:{pk}"
                authority_ok, authority, ownership = self._authority_ownership(row)
                ownership_ok = ownership in _OWNERSHIP_VALUES
                if not authority_ok or not ownership_ok:
                    detail = f"{source_ref}:authority={authority or '<missing>'}:ownership={ownership or '<missing>'}"
                    if not authority_ok:
                        store.record_unknown(source_ref, "unknown_authority", detail, source_hash=row_hash)
                    if not ownership_ok:
                        store.record_unknown(source_ref, "unknown_ownership", detail, source_hash=row_hash)
                    store.record_migration_map(str(source.path), table, pk, row_hash, target_type="codegraph", status="blocked")
                    continue
                if kind == "file":
                    path = self._file_path(row)
                    if not path:
                        store.record_unknown(source_ref, "missing_source_path", "file row has no relative path", source_hash=row_hash)
                        store.record_migration_map(str(source.path), table, pk, row_hash, target_type="codegraph", status="blocked")
                        report.blocked_rows += 1
                        continue
                    content_hash = str(_value(row, *_HASH_KEYS, default="") or row_hash)
                    symbol = self._symbol_mapping(row, table=table, pk=pk)
                    projected = store.upsert_source_file(path, content_hash, scope=self.scope, source_id=source_ref, source_revision=str(_value(row, "source_revision", "revision", "version", default="") or ""), language=str(_value(row, "language", "language_id", default="") or ""), symbols=(symbol,))
                    projected_symbols_by_source[pk] = str(symbol["symbol_id"])
                    report.projected_files += 1
                    report.projected_symbols += 1
                    store.record_migration_map(str(source.path), table, pk, row_hash, target_id=projected.file_id, target_type="codegraph")
                elif kind == "symbol":
                    # A relation/node source can be projected as a metadata
                    # symbol only when a safe relative path is supplied.
                    path = self._file_path(row)
                    if not path:
                        store.record_unknown(source_ref, "missing_source_path", "symbol row has no relative path", source_hash=row_hash)
                        store.record_migration_map(str(source.path), table, pk, row_hash, target_type="codegraph", status="blocked")
                        report.blocked_rows += 1
                        continue
                    content_hash = str(_value(row, *_HASH_KEYS, default="") or row_hash)
                    symbol = self._symbol_mapping(row, table=table, pk=pk)
                    projected = store.upsert_source_file(path, content_hash, scope=self.scope, source_id=source_ref, symbols=(symbol,))
                    report.projected_files += 1; report.projected_symbols += 1
                    projected_symbols_by_source[pk] = str(symbol["symbol_id"])
                    store.record_migration_map(str(source.path), table, pk, row_hash, target_id=projected.file_id, target_type="codegraph")
                elif kind == "edge":
                    edge = self._edge_mapping(row, table=table, pk=pk)
                    if edge is None:
                        store.record_unknown(source_ref, "missing_edge_endpoint", "edge row has no stable endpoints", source_hash=row_hash)
                        store.record_migration_map(str(source.path), table, pk, row_hash, target_type="codegraph", status="blocked")
                        report.blocked_rows += 1
                        continue
                    # Legacy endpoints are only usable when they were mapped
                    # to stable V2 symbols in this batch.  Unknown ownership
                    # is therefore BLOCKED instead of an unscoped edge.
                    from_key = projected_symbols_by_source.get(str(edge["from_id"]), str(edge["from_id"]))
                    to_key = projected_symbols_by_source.get(str(edge["to_id"]), str(edge["to_id"]))
                    edge["from_id"], edge["to_id"] = from_key, to_key
                    try:
                        store.put_edges((edge,), scope=self.scope)
                    except Exception as exc:
                        store.record_unknown(source_ref, "edge_endpoint_unmapped", str(exc), source_hash=row_hash)
                        store.record_migration_map(str(source.path), table, pk, row_hash, target_type="codegraph", status="blocked")
                        report.blocked_rows += 1
                        continue
                    report.projected_edges += 1
                    store.record_migration_map(str(source.path), table, pk, row_hash, target_id=str(edge["edge_id"]), target_type="codegraph")
                else:
                    store.record_unknown(source_ref, "unknown_codegraph_row", "source row is not a file, symbol or edge", source_hash=row_hash)
                    store.record_migration_map(str(source.path), table, pk, row_hash, target_type="codegraph", status="blocked")
                    report.blocked_rows += 1
                processed += 1
            if _hash_file(source.path) != source.digest:
                raise CodeGraphMigrationError("legacy source hash changed during migration")
            counts = store.counts(scope=self.scope)
            report.orphan = store.orphan_count(scope=self.scope)
            report.outbox_pending = counts.get("outbox_pending", 0)
            report.loss = report.blocked_rows
            report.status = "BLOCKED" if report.blocked_rows else "OK"
            if backup_path is not None:
                backup_path.unlink(missing_ok=True)
        except Exception as exc:
            # Every projection API commits independently.  Compensate with a
            # SQLite backup snapshot so an unexpected row/path/source-hash
            # failure leaves the target byte-for-byte at its prior state (or
            # absent when this batch created it).
            try:
                if "target_path" in locals():
                    self._restore_target(target_path, backup_path, target_existed)
            except Exception as restore_exc:
                report.errors.append(f"compensation:{type(restore_exc).__name__}: {restore_exc}")
            report.status = "FAILED"
            report.errors.append(f"{type(exc).__name__}: {exc}")
            self.last_report = report
            raise
        self.last_report = report
        return report

    run = migrate
    execute = migrate


CodeGraphMigrator = V1CodeGraphMigrator
V1LightGraphMigrator = V1CodeGraphMigrator


__all__ = [
    "CodeGraphMigrationError",
    "CodeGraphMigrationReport",
    "CodeGraphMigrator",
    "MigrationError",
    "V1CodeGraphMigrator",
    "V1LightGraphMigrator",
]
