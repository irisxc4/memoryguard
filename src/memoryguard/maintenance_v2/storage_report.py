"""Read-only SQLite storage reports used by the Phase 7 maintenance plane.

The reporter deliberately opens databases through SQLite's ``mode=ro`` URI.
It never creates a missing database, runs a write PRAGMA, checkpoints a WAL, or
touches a sidecar.  Reports are therefore safe to collect while a workspace is
still in ``V2_BUILDING``/``V2_READY``.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from ..storage.layout import LayoutError, WorkspaceV2Layout


class StorageReportError(RuntimeError):
    """The requested database cannot be inspected safely."""


def _json_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"__bytes__": hashlib.sha256(value).hexdigest(), "size": len(value)}
    if isinstance(value, (bytearray, memoryview)):
        return _json_value(bytes(value))
    if isinstance(value, Mapping):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(v) for v in value]
    return value


def _digest_rows(rows: Iterable[sqlite3.Row]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        payload = json.dumps(_json_value(dict(row)), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        digest.update(payload.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _schema_fingerprint(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()
    digest = hashlib.sha256()
    for row in rows:
        digest.update(json.dumps(tuple(row), ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _safe_database_path(target: str | Path, *, layout: WorkspaceV2Layout | None = None, domain: str | None = None) -> tuple[Path, str]:
    """Resolve one exact layout database without following a reparse point."""

    if isinstance(target, WorkspaceV2Layout):
        raise StorageReportError("a concrete database path is required")
    raw = Path(target).expanduser()
    if layout is not None:
        if domain is None:
            raise StorageReportError("domain is required with a WorkspaceV2Layout")
        try:
            expected = tuple(layout.db_paths(domain))
        except (LayoutError, TypeError) as exc:
            raise StorageReportError("unknown V2 storage domain") from exc
        key = os.path.normcase(os.path.abspath(os.fspath(raw)))
        matches = [p for p in expected if os.path.normcase(os.path.abspath(os.fspath(p))) == key]
        if not matches:
            raise StorageReportError("database path is not the exact V2 layout path")
        raw = matches[0]
    # Check lexical components before resolve(); otherwise a symlink can be
    # made to point at a regular database outside the workspace.
    try:
        cursor = raw
        while True:
            if cursor.is_symlink() or WorkspaceV2Layout._is_reparse_or_symlink(cursor):
                raise StorageReportError("database path or parent is a symlink/reparse point")
            if cursor.parent == cursor:
                break
            if layout is not None and cursor == layout.workspace:
                break
            cursor = cursor.parent
        resolved = raw.resolve(strict=False)
        if layout is not None:
            resolved.relative_to(layout.root.resolve(strict=False))
        if not raw.is_file():
            raise StorageReportError("database file is missing or not regular")
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = raw.with_name(raw.name + suffix)
            if sidecar.is_symlink() or WorkspaceV2Layout._is_reparse_or_symlink(sidecar):
                raise StorageReportError("database sidecar is a symlink/reparse point")
    except (OSError, ValueError, LayoutError) as exc:
        if isinstance(exc, StorageReportError):
            raise
        raise StorageReportError("cannot inspect database path") from exc
    return raw, (domain or "")


def _path_identity(path: Path) -> tuple[int, int, int, int]:
    """Return identity tuple without following a symlink/reparse path."""

    info = path.stat()
    return (
        int(getattr(info, "st_dev", 0)),
        int(getattr(info, "st_ino", 0)),
        int(info.st_size),
        int(info.st_mtime_ns),
    )


def _identity_is_reliable(identity: tuple[int, int, int, int]) -> bool:
    """Return whether the platform supplied a usable file identity.

    Some filesystems report ``st_ino == 0`` (and a few report ``st_dev == 0``)
    for every path.  Treating that pair as an identity makes a same-volume
    rename indistinguishable from the original database.  Maintenance and
    reporting therefore fail closed instead of falling back to a weak path
    comparison.  The size/mtime fields remain part of the receipt for the
    filesystems that do provide stable inode identities.
    """

    return int(identity[0]) > 0 and int(identity[1]) > 0


def _require_reliable_identity(identity: tuple[int, int, int, int], label: str = "database") -> None:
    if not _identity_is_reliable(identity):
        raise StorageReportError(f"{label} identity is unavailable")


def _assert_lexical_artifacts(path: Path) -> None:
    """Reject path/sidecars that become links between preflight and use."""

    cursor = path
    while True:
        if cursor.is_symlink() or WorkspaceV2Layout._is_reparse_or_symlink(cursor):
            raise StorageReportError("database path or parent is a symlink/reparse point")
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.is_symlink() or WorkspaceV2Layout._is_reparse_or_symlink(sidecar):
            raise StorageReportError("database sidecar is a symlink/reparse point")


def _sidecar_identities(path: Path) -> dict[str, tuple[int, int, int, int] | None]:
    """Capture sidecar inode identities before opening SQLite."""

    result: dict[str, tuple[int, int, int, int] | None] = {}
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.is_symlink() or WorkspaceV2Layout._is_reparse_or_symlink(sidecar):
            raise StorageReportError("database sidecar is a symlink/reparse point")
        if sidecar.exists():
            identity = _path_identity(sidecar)
            _require_reliable_identity(identity, "database sidecar")
            result[suffix] = identity
        else:
            result[suffix] = None
    return result


class _SQLiteIdentityLease:
    """Connection plus lexical/file identity lease.

    SQLite opens by pathname, so checking a path once is insufficient: a
    concurrent rename can redirect the connection to an outside inode.  The
    lease validates the exact inode immediately after opening and again around
    every caller operation.  Keeping the connection open also prevents a
    Windows rename/delete from silently swapping the live handle.
    """

    def __init__(self, path: Path, conn: sqlite3.Connection, identity: tuple[int, int, int, int], *, readonly: bool, sidecars: Mapping[str, tuple[int, int, int, int] | None] | None = None) -> None:
        self.path = path
        self.connection = conn
        self.identity = identity
        self.readonly = readonly
        self._sidecars = dict(sidecars or {
            suffix: self._optional_identity(path.with_name(path.name + suffix))
            for suffix in ("-wal", "-shm", "-journal")
        })

    @staticmethod
    def _optional_identity(path: Path) -> tuple[int, int, int, int] | None:
        try:
            if not path.exists():
                return None
            identity = _path_identity(path)
            _require_reliable_identity(identity, "database sidecar")
            return identity
        except OSError as exc:
            raise StorageReportError("database sidecar is unavailable") from exc

    @classmethod
    def open(cls, path: Path, *, readonly: bool, expected_identity: tuple[int, int, int, int] | None = None, expected_sidecars: Mapping[str, tuple[int, int, int, int] | None] | None = None, immutable: bool = False) -> "_SQLiteIdentityLease":
        _assert_lexical_artifacts(path)
        current_sidecars = _sidecar_identities(path)
        if expected_sidecars is not None:
            for suffix, expected in expected_sidecars.items():
                current = current_sidecars.get(suffix)
                if expected is not None and (current is None or current[:2] != expected[:2]):
                    raise StorageReportError("database sidecar identity changed before open")
        if not path.is_file():
            raise StorageReportError("database file is missing or not regular")
        identity = _path_identity(path)
        _require_reliable_identity(identity)
        if expected_identity is not None:
            _require_reliable_identity(expected_identity)
        if expected_identity is not None and identity[:2] != expected_identity[:2]:
            raise StorageReportError("database identity changed before open")
        if readonly:
            from urllib.parse import quote
            query = "mode=ro&immutable=1" if immutable else "mode=ro"
            uri = "file:" + quote(str(path), safe="/:\\") + "?" + query
            conn = sqlite3.connect(uri, uri=True, timeout=5.0, factory=_LeasedConnection)
        else:
            conn = sqlite3.connect(str(path), timeout=5.0, factory=_LeasedConnection)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            lease = cls(path, conn, identity, readonly=readonly, sidecars=current_sidecars)
            conn._identity_lease = lease  # type: ignore[attr-defined]
            lease.assert_current()
            return lease
        except Exception:
            conn.close()
            raise

    def assert_current(self) -> None:
        _assert_lexical_artifacts(self.path)
        try:
            current_identity = _path_identity(self.path)
            _require_reliable_identity(current_identity)
            _require_reliable_identity(self.identity)
            # The main inode is the compare-and-swap identity for both modes.
            # Read-only reports additionally require byte metadata to remain
            # stable; a writable maintenance statement may legitimately change
            # size/mtime and refreshes that receipt after the statement.
            if current_identity[:2] != self.identity[:2] or (self.readonly and current_identity != self.identity):
                raise StorageReportError("database identity changed during operation")
            rows = self.connection.execute("PRAGMA database_list").fetchall()
            main = next((str(row[2]) for row in rows if str(row[1]) == "main"), "")
            expected = os.path.normcase(os.path.abspath(os.fspath(self.path)))
            actual = os.path.normcase(os.path.abspath(os.fspath(main))) if main else ""
            if actual != expected:
                raise StorageReportError("database connection identity mismatch")
            for suffix, prior in self._sidecars.items():
                current = self._optional_identity(self.path.with_name(self.path.name + suffix))
                if not self.readonly:
                    # Writable leases are used as a short CAS window.  A
                    # sidecar may be created, deleted, or rotated by SQLite,
                    # but only an explicit refresh immediately after the
                    # operation may accept that lifecycle change.  An
                    # unannounced external change is never silently accepted.
                    if current != prior:
                        raise StorageReportError("database sidecar identity changed during operation")
                else:
                    # A read-only WAL connection may lazily create ``-shm``;
                    # preserve the historical tolerance for that one-way
                    # creation while rejecting replacement of an existing
                    # sidecar inode.
                    if prior is not None and current is not None and current[:2] != prior[:2]:
                        raise StorageReportError("database sidecar identity changed during operation")
                if current is not None and self._has_link(self.path.with_name(self.path.name + suffix)):
                    raise StorageReportError("database sidecar is a symlink/reparse point")
        except StorageReportError:
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            raise StorageReportError("database identity lease check failed") from exc

    @staticmethod
    def _has_link(path: Path) -> bool:
        return path.is_symlink() or WorkspaceV2Layout._is_reparse_or_symlink(path)

    def refresh_sidecars(self) -> None:
        """Record sidecars changed by the SQLite statement just completed.

        This is intentionally explicit: callers must place it directly after
        a trusted checkpoint/VACUUM statement.  An attacker-created sidecar
        appearing between two assertions is otherwise rejected by
        :meth:`assert_current`.
        """

        _assert_lexical_artifacts(self.path)
        self._sidecars = _sidecar_identities(self.path)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> sqlite3.Connection:
        return self.connection

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        self.close()
        return False


class _LeasedConnection(sqlite3.Connection):
    """Close SQLite handle when callers use ``with adapter.connect()``."""

    _identity_lease: _SQLiteIdentityLease | None = None

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        result = super().__exit__(exc_type, exc_value, traceback)
        try:
            self.close()
        except sqlite3.Error:
            pass
        return result


@dataclass(frozen=True, slots=True)
class StorageReport:
    """Immutable logical/physical view of one SQLite file."""

    path: str
    domain: str = ""
    logical_pages: int = 0
    derived_pages: int = 0
    free_pages: int = 0
    allocated_bytes: int = 0
    page_size: int = 0
    wal_bytes: int = 0
    shm_bytes: int = 0
    journal_mode: str = ""
    auto_vacuum: str = "NONE"
    integrity_check: str = ""
    foreign_key_errors: int = 0
    integrity_ok: bool = False
    schema_fingerprint: str = ""
    row_counts: Mapping[str, int] = MappingProxyType({})
    table_digests: Mapping[str, str] = MappingProxyType({})
    acl_mode: int = 0
    readable: bool = False

    @property
    def free_bytes(self) -> int:
        return self.free_pages * self.page_size

    # Compatibility names used by report consumers and CLI serializers.
    @property
    def page_count(self) -> int:
        return self.derived_pages

    @property
    def freelist_count(self) -> int:
        return self.free_pages

    @property
    def physical_bytes(self) -> int:
        return self.allocated_bytes

    @property
    def wal_size_bytes(self) -> int:
        return self.wal_bytes

    @property
    def integrity(self) -> str:
        return self.integrity_check

    @property
    def table_row_counts(self) -> Mapping[str, int]:
        return self.row_counts

    @property
    def fingerprint(self) -> str:
        return self.schema_fingerprint

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "domain": self.domain,
            "logical_pages": self.logical_pages,
            "derived_pages": self.derived_pages,
            "free_pages": self.free_pages,
            "free_bytes": self.free_bytes,
            "allocated_bytes": self.allocated_bytes,
            "page_size": self.page_size,
            "wal_bytes": self.wal_bytes,
            "shm_bytes": self.shm_bytes,
            "journal_mode": self.journal_mode,
            "auto_vacuum": self.auto_vacuum,
            "integrity_check": self.integrity_check,
            "foreign_key_errors": self.foreign_key_errors,
            "integrity_ok": self.integrity_ok,
            "schema_fingerprint": self.schema_fingerprint,
            "row_counts": dict(self.row_counts),
            "table_digests": dict(self.table_digests),
            "acl_mode": self.acl_mode,
            "readable": self.readable,
        }

    as_dict = to_dict


class StorageReporter:
    """Collect :class:`StorageReport` values without mutating SQLite state."""

    def __init__(self, workspace_or_layout: str | Path | WorkspaceV2Layout | None = None, *, source_workspace: str | Path | None = None, immutable: bool = False) -> None:
        if type(immutable) is not bool:
            raise TypeError("immutable must be bool")
        self.immutable = immutable
        if isinstance(workspace_or_layout, WorkspaceV2Layout):
            if source_workspace is None:
                raise LayoutError("source_workspace is required with a WorkspaceV2Layout")
            raw = Path(source_workspace).expanduser()
            if raw.is_symlink() or WorkspaceV2Layout._is_reparse_or_symlink(raw):
                raise LayoutError("source workspace cannot be a symlink or reparse point")
            if WorkspaceV2Layout(raw).workspace != workspace_or_layout.workspace:
                raise LayoutError("source_workspace does not match supplied layout")
            self.layout = workspace_or_layout
        elif workspace_or_layout is None:
            if source_workspace is not None:
                raise LayoutError("source_workspace requires a WorkspaceV2Layout")
            self.layout = None
        else:
            if source_workspace is not None:
                raise LayoutError("source_workspace requires a WorkspaceV2Layout")
            self.layout = WorkspaceV2Layout(Path(workspace_or_layout))

    def report(self, target: str | Path, *, domain: str | None = None) -> StorageReport:
        path, resolved_domain = _safe_database_path(target, layout=self.layout, domain=domain)
        try:
            expected_identity = _path_identity(path)
            expected_sidecars = _sidecar_identities(path)
            wal = path.with_name(path.name + "-wal")
            shm = path.with_name(path.name + "-shm")
            lease_obj = _SQLiteIdentityLease.open(path, readonly=True, expected_identity=expected_identity, expected_sidecars=expected_sidecars, immutable=self.immutable)
            with lease_obj as conn:
                st = path.stat()
                lease_obj.assert_current()
                page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
                page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
                free_pages = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
                auto_vacuum_value = int(conn.execute("PRAGMA auto_vacuum").fetchone()[0])
                auto_vacuum = {0: "NONE", 1: "FULL", 2: "INCREMENTAL"}.get(auto_vacuum_value, f"UNKNOWN:{auto_vacuum_value}")
                journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).upper()
                integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
                foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
                schema_fp = _schema_fingerprint(conn)
                table_names = [str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
                counts: dict[str, int] = {}
                digests: dict[str, str] = {}
                for table in table_names:
                    quoted = '"' + table.replace('"', '""') + '"'
                    cursor = conn.execute(f"SELECT * FROM {quoted}")
                    count = 0
                    digest = hashlib.sha256()
                    while rows := cursor.fetchmany(512):
                        count += len(rows)
                        for row in rows:
                            payload = json.dumps(_json_value(dict(row)), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
                            digest.update(payload.encode("utf-8"))
                            digest.update(b"\n")
                    counts[table] = count
                    digests[table] = digest.hexdigest()
                lease_obj.assert_current()
                wal_bytes = wal.stat().st_size if wal.is_file() else 0
                shm_bytes = shm.stat().st_size if shm.is_file() else 0
            return StorageReport(
                path=str(path), domain=resolved_domain,
                logical_pages=max(page_count - free_pages, 0), derived_pages=page_count,
                free_pages=free_pages, allocated_bytes=page_count * page_size,
                page_size=page_size, wal_bytes=wal_bytes,
                shm_bytes=shm_bytes,
                journal_mode=journal_mode, auto_vacuum=auto_vacuum,
                integrity_check=integrity, foreign_key_errors=len(foreign_keys),
                integrity_ok=integrity == "ok" and not foreign_keys,
                schema_fingerprint=schema_fp, row_counts=MappingProxyType(counts),
                table_digests=MappingProxyType(digests), acl_mode=stat.S_IMODE(st.st_mode), readable=True,
            )
        except (sqlite3.Error, OSError, ValueError, TypeError, StorageReportError) as exc:
            raise StorageReportError("cannot read SQLite storage report safely") from exc


def storage_report(target: str | Path, *, domain: str | None = None, workspace: str | Path | WorkspaceV2Layout | None = None, source_workspace: str | Path | None = None) -> StorageReport:
    """Functional convenience wrapper."""

    return StorageReporter(workspace, source_workspace=source_workspace).report(target, domain=domain)


__all__ = ["StorageReport", "StorageReporter", "StorageReportError", "storage_report"]
