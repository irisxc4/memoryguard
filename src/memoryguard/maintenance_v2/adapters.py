"""Read-only SQLite adapters for Reference Audit.

No V2 Store is imported or initialized here.  The adapter opens an existing
database using SQLite's ``mode=ro`` URI and exposes bounded, keyset-paginated
rows plus immutable schema/integrity snapshots.
"""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import nullcontext
import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Mapping

from .registry import DomainSpec, TableSpec
from .storage_report import StorageReportError, _SQLiteIdentityLease, _path_identity, _sidecar_identities


class ReadOnlyAdapterError(RuntimeError):
    pass


class CursorError(ReadOnlyAdapterError):
    pass


def _reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        st = path.lstat()
        # FILE_ATTRIBUTE_REPARSE_POINT (Windows), harmless on POSIX.
        return bool(getattr(st, "st_file_attributes", 0) & 0x400)
    except (OSError, ValueError):
        return False


def assert_lexical_safe(path: str | Path, workspace: str | Path | None = None) -> Path:
    """Reject symlink/junction/reparse components before any ``resolve`` call."""

    candidate = Path(path).expanduser()
    if workspace is not None:
        root = Path(workspace).expanduser()
        # lexical containment: no resolve() is used for the security decision.
        try:
            candidate_abs = Path(os.path.abspath(os.fspath(candidate)))
            root_abs = Path(os.path.abspath(os.fspath(root)))
            candidate_abs.relative_to(root_abs)
        except ValueError as exc:
            raise ReadOnlyAdapterError("database path escapes workspace lexically") from exc
    current = Path(os.path.abspath(os.fspath(candidate)))
    parts = list(current.parents)[::-1] + [current]
    for item in parts:
        if _reparse(item):
            raise ReadOnlyAdapterError(f"symlink/reparse path is not authoritative: {item}")
    return current


def _safe_ident(value: str) -> str:
    if not isinstance(value, str) or not value or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for ch in value):
        raise ReadOnlyAdapterError(f"unsafe SQL identifier: {value!r}")
    return value


def _shadow_table(name: str, fts_bases: set[str] | None = None) -> bool:
    if name.startswith("sqlite_"):
        return True
    if not fts_bases:
        return False
    suffixes = ("_content", "_data", "_docsize", "_idx", "_config", "_segments", "_segdir", "_stat")
    return any(name == f"{base}{suffix}" for base in fts_bases for suffix in suffixes)


@dataclass(frozen=True, slots=True)
class SchemaSnapshot:
    domain: str
    path: str
    marker: str
    version: int
    user_version: int
    tables: Mapping[str, tuple[str, ...]]
    fingerprint: str
    integrity_check: str
    foreign_key_errors: tuple[tuple[Any, ...], ...]

    @property
    def ok(self) -> bool:
        return self.integrity_check == "ok" and not self.foreign_key_errors


@dataclass(frozen=True, slots=True)
class Page:
    rows: tuple[Mapping[str, Any], ...]
    next_cursor: str | None
    done: bool
    fingerprint: str

    @property
    def items(self) -> tuple[Mapping[str, Any], ...]:
        return self.rows


class SQLiteReadOnlyAdapter:
    """A bounded read-only view over one authoritative V2 database."""

    def __init__(self, path: str | Path, spec: DomainSpec | None = None, *, domain: str = "", immutable: bool = False) -> None:
        if type(immutable) is not bool:
            raise TypeError("immutable must be bool")
        self.path = assert_lexical_safe(path)
        self.spec = spec
        self.domain = domain or (spec.name if spec else self.path.stem)
        self.immutable = immutable
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        try:
            self._identity = _path_identity(self.path)
            self._sidecars = _sidecar_identities(self.path)
        except StorageReportError as exc:
            raise ReadOnlyAdapterError("database identity preflight failed") from exc
        self._lease: _SQLiteIdentityLease | None = None

    def connect(self) -> sqlite3.Connection:
        try:
            lease = _SQLiteIdentityLease.open(self.path, readonly=True, expected_identity=self._identity, expected_sidecars=self._sidecars, immutable=self.immutable)
            conn = lease.connection
            self._lease = lease
            lease.assert_current()
            conn.execute("PRAGMA query_only=ON")
            lease.assert_current()
            return conn
        except (sqlite3.Error, OSError, ValueError, TypeError, ReadOnlyAdapterError) as exc:
            raise ReadOnlyAdapterError("cannot open read-only database safely") from exc

    def _assert_lease(self, connection: sqlite3.Connection) -> None:
        lease = self._lease
        if lease is None or lease.connection is not connection:
            return
        try:
            lease.assert_current()
        except StorageReportError as exc:
            raise ReadOnlyAdapterError("database identity changed during audit") from exc

    def __enter__(self) -> "SQLiteReadOnlyAdapter":
        self._conn = self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        conn = getattr(self, "_conn", None)
        if conn is not None:
            conn.close()
        lease = getattr(self, "_lease", None)
        if lease is not None:
            self._lease = None

    @staticmethod
    def _table_names(conn: sqlite3.Connection) -> tuple[str, ...]:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        return tuple(str(row[0]) for row in rows)

    @staticmethod
    def _columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
        _safe_ident(table)
        return tuple(str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall())

    def schema(self, connection: sqlite3.Connection | None = None) -> SchemaSnapshot:
        manager = nullcontext(connection) if connection is not None else self.connect()
        with manager as conn:
            assert conn is not None
            self._assert_lease(conn)
            tables: dict[str, tuple[str, ...]] = {}
            fts_bases = {
                str(row[0]) for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND upper(COALESCE(sql,'')) LIKE '%USING FTS%'"
                ).fetchall()
            }
            for name in self._table_names(conn):
                if not _shadow_table(name, fts_bases):
                    tables[name] = self._columns(conn, name)
            marker, version = self._marker(conn)
            user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            payload = [(name, list(columns)) for name, columns in sorted(tables.items())]
            encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
            fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
            fk = tuple(tuple(row) for row in conn.execute("PRAGMA foreign_key_check").fetchall())
            self._assert_lease(conn)
            return SchemaSnapshot(self.domain, str(self.path), marker, version, user_version, tables, fingerprint, integrity, fk)

    def _marker(self, conn: sqlite3.Connection) -> tuple[str, int]:
        if self.spec is None:
            return "", int(conn.execute("PRAGMA user_version").fetchone()[0])
        # V2 domains use schema_meta; specialised stores may use key/value
        # metadata.  Missing metadata is surfaced to the audit as BLOCKED.
        names = set(self._table_names(conn))
        marker = ""
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if "schema_meta" in names:
            cols = set(self._columns(conn, "schema_meta"))
            if {"marker", "version"} <= cols:
                row = conn.execute("SELECT marker,version FROM schema_meta ORDER BY rowid LIMIT 1").fetchone()
                if row:
                    marker, version = str(row[0]), int(row[1])
        elif "*_schema_meta" in names:  # defensive; never true, retained for clarity
            pass
        for metadata_name in (f"{self.domain}_schema_meta", "asset_schema_meta", "codegraph_schema_meta", "runtime_v2_schema_meta", "content_schema_meta", "projection_schema_meta"):
            if metadata_name not in names:
                continue
            cols = set(self._columns(conn, metadata_name))
            if {"domain", "marker", "version"} <= cols:
                row = conn.execute(f'SELECT marker,version FROM "{metadata_name}" ORDER BY rowid LIMIT 1').fetchone()
                if row:
                    marker, version = str(row[0]), int(row[1])
            elif {"schema_id", "marker", "version"} <= cols:
                row = conn.execute(f'SELECT marker,version FROM "{metadata_name}" ORDER BY rowid LIMIT 1').fetchone()
                if row:
                    marker, version = str(row[0]), int(row[1])
            elif {"key", "value"} <= cols:
                rows = conn.execute(f'SELECT key,value FROM "{metadata_name}" WHERE key IN (\'marker\',\'version\') ORDER BY key').fetchall()
                values = {str(row[0]): str(row[1]) for row in rows}
                if "marker" in values:
                    marker = values["marker"]
                elif "version" in values:
                    marker = values["version"]
                if "version" in values:
                    try:
                        version = int(values["version"])
                    except ValueError:
                        pass
        # Phase-2/3/5 auxiliary schemas use a key/value version marker rather
        # than a textual marker.  ``1``/``2`` is the explicit marker strategy
        # recorded by the authoritative registry.
        if not marker and self.domain in {"content", "codegraph", "assets", "scenario", "profile"}:
            marker = str(version)
        return marker, version

    def page(self, table: str, *, limit: int = 256, cursor: str | None = None, key_columns: tuple[str, ...] | None = None, columns: tuple[str, ...] | None = None, connection: sqlite3.Connection | None = None, snapshot: SchemaSnapshot | None = None) -> Page:
        table = _safe_ident(table)
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        snap = snapshot or self.schema(connection)
        if table not in snap.tables:
            raise ReadOnlyAdapterError(f"table is not authoritative or does not exist: {table}")
        known = tuple(columns or snap.tables[table])
        for column in known:
            _safe_ident(column)
        if not known:
            raise ReadOnlyAdapterError("table has no columns")
        keys = tuple(key_columns or (self.spec.tables[table].key_columns if self.spec and table in self.spec.tables else ("rowid",)))
        for key in keys:
            _safe_ident(key)
        where = ""
        params: list[Any] = []
        if cursor:
            try:
                raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
                payload = json.loads(raw)
                if payload.get("table") != table or payload.get("keys") != list(keys) or payload.get("fingerprint") != snap.fingerprint:
                    raise CursorError("cursor schema/table binding mismatch")
                values = payload.get("values")
                if not isinstance(values, list) or len(values) != len(keys):
                    raise CursorError("cursor values are malformed")
            except (ValueError, TypeError, json.JSONDecodeError, UnicodeError, CursorError) as exc:
                if isinstance(exc, CursorError):
                    raise
                raise CursorError("cursor is malformed") from exc
            # Lexicographic keyset tuple comparison; SQLite NULL ordering is
            # avoided by requiring stable non-null keys in the registry.
            if len(keys) == 1:
                where = f' WHERE "{keys[0]}" > ?'
                params.append(values[0])
            else:
                terms = []
                for i, key in enumerate(keys):
                    prefix = " AND ".join(f'"{keys[j]}" = ?' for j in range(i))
                    terms.append(f'({prefix + " AND " if prefix else ""}"{key}" > ?)')
                    params.extend(values[:i] + [values[i]])
                where = " WHERE " + " OR ".join(terms)
        order = ", ".join(f'"{key}" ASC' for key in keys)
        select_parts = [f'"{column}"' for column in known]
        if "rowid" in keys:
            select_parts.insert(0, 'rowid AS "__audit_rowid"')
        select = ", ".join(select_parts)
        try:
            manager = nullcontext(connection) if connection is not None else self.connect()
            with manager as conn:
                assert conn is not None
                self._assert_lease(conn)
                rows = conn.execute(f'SELECT {select} FROM "{table}"{where} ORDER BY {order} LIMIT ?', (*params, limit + 1)).fetchall()
                self._assert_lease(conn)
        except sqlite3.Error as exc:
            raise ReadOnlyAdapterError(f"cannot page table {table}") from exc
        more = len(rows) > limit
        rows = rows[:limit]
        mapped = tuple({column: row[column] for column in known} for row in rows)
        next_cursor = None
        if more and rows:
            values = [rows[-1][key] if key != "rowid" else rows[-1]["__audit_rowid"] for key in keys]
            encoded = json.dumps({"table": table, "keys": list(keys), "values": values, "fingerprint": snap.fingerprint}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            next_cursor = base64.urlsafe_b64encode(encoded).decode("ascii")
        return Page(mapped, next_cursor, not more, snap.fingerprint)

    # Compatibility names used by audit callers/tests.
    read_page = page
    paginate = page


__all__ = ["ReadOnlyAdapterError", "CursorError", "SchemaSnapshot", "Page", "SQLiteReadOnlyAdapter", "assert_lexical_safe"]
