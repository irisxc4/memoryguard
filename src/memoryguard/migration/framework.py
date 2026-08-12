"""Safety-first V1 -> V2 migration framework (Phase 1).

This module deliberately does *not* migrate business rows.  It provides the
read-only inventory, durable checkpoint and state-machine primitives needed by
later phases.  In particular, it never constructs a legacy Store: several of
the legacy constructors initialise a missing database as a side effect.

The implementation is intentionally standard-library-only.  ``storage`` and
``system`` are optional dependencies while the parallel V2 work lands; small
protocols and the fallback journal below keep this layer usable in isolation.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import stat
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Protocol, Sequence


V2_DOMAINS: tuple[str, ...] = (
    "runtime",
    "memory",
    "rules",
    "evidence",
    "content",
    "knowledge",
    "codegraph",
    "assets",
    "projection",
    "system",
)

V2_DOMAIN_DB_NAMES: Mapping[str, tuple[str, ...]] = {
    "runtime": ("runtime.db",),
    "memory": ("memory.db",),
    "rules": ("rules.db",),
    "evidence": ("evidence.db",),
    "content": ("content.db",),
    "knowledge": ("knowledge.db",),
    "codegraph": ("codegraph.db",),
    "assets": ("assets.db",),
    "projection": ("scenario.db", "profile.db"),
    "system": ("manifest.db",),
}


class MigrationError(RuntimeError):
    """Base class for migration failures."""


class MigrationReadError(MigrationError):
    """A V1 source could not be read safely (fail-closed)."""


class PathContainmentError(MigrationError, ValueError):
    """A configured or discovered path escapes an allowed root."""


class MigrationState(str, Enum):
    """The only states that can be persisted by the Phase 1 coordinator."""

    V1_ACTIVE = "V1_ACTIVE"
    V2_BUILDING = "V2_BUILDING"
    V2_READY = "V2_READY"
    V2_ACTIVE = "V2_ACTIVE"


class MigrationPhase(str, Enum):
    """Compatibility alias used by callers that call the state a phase."""

    V1_ACTIVE = MigrationState.V1_ACTIVE.value
    V2_BUILDING = MigrationState.V2_BUILDING.value
    V2_READY = MigrationState.V2_READY.value
    V2_ACTIVE = MigrationState.V2_ACTIVE.value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def _stable_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _resolve_no_follow(path: str | Path) -> Path:
    """Resolve a path for containment checks without creating anything."""

    # ``Path.resolve(strict=False)`` resolves existing symlink components, but
    # does not touch the filesystem for a missing path.
    return Path(path).expanduser().resolve(strict=False)


def _contained(path: str | Path, roots: Sequence[Path]) -> Path:
    candidate = _resolve_no_follow(path)
    for root in roots:
        try:
            candidate.relative_to(root)
            return candidate
        except ValueError:
            continue
    roots_text = ", ".join(str(root) for root in roots)
    raise PathContainmentError(f"path escapes allowed roots: {candidate} not under {roots_text}")


def _file_scope_summary(path: Path) -> dict[str, Any]:
    """Return ACL-ish metadata without exposing file contents."""

    info = path.stat()
    mode = stat.S_IMODE(info.st_mode)
    summary: dict[str, Any] = {
        "mode": oct(mode),
        "read_only": not bool(mode & stat.S_IWUSR),
        "owner_uid": getattr(info, "st_uid", None),
        "owner_gid": getattr(info, "st_gid", None),
    }
    return summary


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _sqlite_uri(path: Path, *, immutable: bool = False) -> str:
    # ``as_uri`` produces a correctly escaped URI on Windows and POSIX.  The
    # mode=ro query is the important invariant: sqlite must never create V1.
    # Immutable mode is the stronger read-only transport used by dry-run
    # inventories/validators: SQLite does not attach or update WAL/SHM files.
    query = "mode=ro&immutable=1" if immutable else "mode=ro"
    return f"{path.as_uri()}?{query}"


def _nonempty_wal(path: Path) -> bool:
    """Return whether immutable mode would ignore committed WAL frames."""

    wal = path.with_name(path.name + "-wal")
    try:
        return wal.is_file() and wal.stat().st_size > 0
    except OSError:
        # Treat an unreadable WAL as unsafe for immutable mode.  Callers report
        # a stable read error instead of risking a stale snapshot.
        return True


def _table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [str(row[0]) for row in rows]


def _schema_marker(conn: sqlite3.Connection) -> str:
    """Read a marker without assuming one particular V1 schema."""

    user_version = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
    markers: list[str] = []
    tables = _table_names(conn)
    marker_tables = {
        "schema_meta",
        "schema_metadata",
        "metadata",
        "meta",
        "_schema",
        "schema_version",
    }
    for table in tables:
        if table.lower() not in marker_tables:
            continue
        try:
            columns = [str(row[1]) for row in conn.execute(f"PRAGMA table_info({_quote_identifier(table)})")]
            if not columns:
                continue
            preferred = next(
                (column for column in columns if column.lower() in {"schema_version", "version", "marker", "value"}),
                columns[0],
            )
            value = conn.execute(
                f"SELECT {_quote_identifier(preferred)} FROM {_quote_identifier(table)} LIMIT 1"
            ).fetchone()
            if value is not None and value[0] is not None:
                markers.append(f"{table}:{value[0]}")
        except sqlite3.Error:
            # The integrity/error path records the actual query error.  A
            # marker probe must not hide it or turn a damaged DB into success.
            continue
    if markers:
        return ";".join(markers)
    return f"sqlite:user_version={user_version}" if user_version else ""


def _sqlite_scope_summary(conn: sqlite3.Connection, tables: Sequence[str]) -> dict[str, Any]:
    scope_names = {
        "agent_instance_id",
        "agent_id",
        "share_group_id",
        "project_ref",
        "workspace_id",
        "scope",
        "access_scope",
        "policy_class",
    }
    columns: list[str] = []
    values: list[str] = []
    for table in tables:
        try:
            table_columns = [str(row[1]) for row in conn.execute(f"PRAGMA table_info({_quote_identifier(table)})")]
        except sqlite3.Error:
            continue
        for column in table_columns:
            if column.lower() not in scope_names:
                continue
            columns.append(f"{table}.{column}")
            try:
                rows = conn.execute(
                    f"SELECT {_quote_identifier(column)} FROM {_quote_identifier(table)} "
                    "WHERE " + _quote_identifier(column) + " IS NOT NULL "
                    "GROUP BY " + _quote_identifier(column) + " ORDER BY " + _quote_identifier(column) + " LIMIT 128"
                ).fetchall()
                values.extend(f"{table}.{column}={row[0]!r}" for row in rows)
            except sqlite3.Error:
                continue
    return {
        "scope_columns": sorted(set(columns)),
        "scope_values_digest": _stable_digest(sorted(values)) if values else "",
    }


def _sqlite_content_digest(
    conn: sqlite3.Connection,
    tables: Sequence[str],
) -> tuple[str, int, dict[str, int]]:
    """Hash logical SQLite rows in a stable, streaming order.

    ``rowid`` is not available for ``WITHOUT ROWID`` tables and an unordered
    table scan is allowed to change after a vacuum or a different insertion
    order.  Prefer the declared primary-key columns; when a table has no
    primary key, order by every declared column.  Duplicate rows then produce
    identical encodings, so their physical order is immaterial.  Rows are
    encoded with type tags so ``NULL``, text and BLOB values cannot collide.
    """

    def normalise(value: Any) -> list[Any]:
        if value is None:
            return ["null", None]
        if isinstance(value, (bytes, bytearray, memoryview)):
            return ["blob", bytes(value).hex()]
        if isinstance(value, bool):
            return ["integer", int(value)]
        if isinstance(value, int):
            return ["integer", value]
        if isinstance(value, float):
            if math.isnan(value):
                return ["real", "nan"]
            if math.isinf(value):
                return ["real", "-inf" if value < 0 else "inf"]
            # repr() is deterministic and preserves enough precision for a
            # SQLite REAL round-trip.
            return ["real", repr(value)]
        if isinstance(value, str):
            return ["text", value]
        return [type(value).__name__, str(value)]

    def order_columns(table: str) -> list[str]:
        quoted = _quote_identifier(table)
        info = conn.execute(f"PRAGMA table_info({quoted})").fetchall()
        columns = [(str(row[1]), int(row[5] or 0)) for row in info]
        primary_key = [name for name, position in sorted(columns, key=lambda item: item[1]) if position > 0]
        if primary_key:
            return primary_key
        return [name for name, _ in columns]

    digest = hashlib.sha256()
    total = 0
    table_rows: dict[str, int] = {}
    for table in tables:
        quoted = _quote_identifier(table)
        count = int(conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
        table_rows[table] = count
        total += count
        digest.update(table.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(count).encode("ascii"))
        digest.update(b"\0")
        columns = order_columns(table)
        if columns:
            order_by = ", ".join(_quote_identifier(column) for column in columns)
            cursor = conn.execute(f"SELECT * FROM {quoted} ORDER BY {order_by}")
        else:
            # SQLite permits a zero-column table.  Every row has the same
            # logical encoding; rowid only controls iteration and cannot alter
            # the resulting digest because every encoded row is identical.
            cursor = conn.execute(f"SELECT * FROM {quoted} ORDER BY rowid")
        for row in cursor:
            encoded = json.dumps(
                [normalise(value) for value in tuple(row)],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            digest.update(encoded)
            digest.update(b"\n")
    return digest.hexdigest(), total, table_rows


def _sqlite_inventory(
    path: Path,
    *,
    immutable: bool = False,
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """Collect metrics from a SQLite file using a read-only URI."""

    errors: list[tuple[str, str]] = []
    try:
        sha256, size = _sha256_file(path)
    except OSError as exc:
        return {
            "exists": True,
            "file_size": None,
            "sha256": "",
            "content_digest": "",
            "schema_marker": "",
            "row_count": None,
            "table_rows": {},
            "scope_summary": {},
        }, [("file_read_error", str(exc))]

    item: dict[str, Any] = {
        "exists": True,
        "file_size": size,
        "sha256": sha256,
        "content_digest": "",
        "schema_marker": "",
        "row_count": None,
        "table_rows": {},
        "scope_summary": {},
    }
    conn: sqlite3.Connection | None = None
    try:
        if immutable and _nonempty_wal(path):
            raise sqlite3.OperationalError("immutable read blocked by non-empty WAL")
        conn = sqlite3.connect(_sqlite_uri(path, immutable=immutable), uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        # Both checks are read-only and make corruption/foreign-key damage
        # explicit instead of allowing a partial inventory to look complete.
        integrity = conn.execute("PRAGMA integrity_check").fetchall()
        bad_integrity = [str(row[0]) for row in integrity if str(row[0]).lower() != "ok"]
        if bad_integrity:
            errors.append(("integrity_check_failed", "; ".join(bad_integrity[:8])))
        foreign = conn.execute("PRAGMA foreign_key_check").fetchall()
        if foreign:
            errors.append(("foreign_key_check_failed", f"{len(foreign)} violation(s)"))
        tables = _table_names(conn)
        item["schema_marker"] = _schema_marker(conn)
        if not item["schema_marker"]:
            errors.append(("schema_marker_unknown", "SQLite database has no schema_meta marker or user_version"))
        content_digest, row_count, table_rows = _sqlite_content_digest(conn, tables)
        item["content_digest"] = content_digest
        item["row_count"] = row_count
        item["table_rows"] = table_rows
        item["scope_summary"] = _sqlite_scope_summary(conn, tables)
    except (sqlite3.Error, OSError, ValueError) as exc:
        # Keep the already-computed SHA/size in the ledger.  A damaged DB is
        # still evidence that must be preserved, not an item to silently skip.
        errors.append(("sqlite_read_error", f"{type(exc).__name__}: {exc}"))
    finally:
        if conn is not None:
            conn.close()
    return item, errors


def _json_inventory(path: Path) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    errors: list[tuple[str, str]] = []
    try:
        sha256, size = _sha256_file(path)
    except OSError as exc:
        return {
            "exists": True,
            "file_size": None,
            "sha256": "",
            "content_digest": "",
            "schema_marker": "",
            "row_count": None,
            "table_rows": {},
            "scope_summary": {},
        }, [("file_read_error", str(exc))]
    item: dict[str, Any] = {
        "exists": True,
        "file_size": size,
        "sha256": sha256,
        "content_digest": "",
        "schema_marker": "",
        "row_count": None,
        "table_rows": {},
        "scope_summary": {},
    }
    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
        item["content_digest"] = _stable_digest(value)
        if isinstance(value, Mapping):
            marker = value.get("schema_marker", value.get("schema_version", value.get("version", "")))
            item["schema_marker"] = str(marker or "")
            item["row_count"] = len(value)
            scope_keys = sorted(
                str(key)
                for key in value
                if str(key).lower() in {
                    "agent_id", "agent_instance_id", "share_group_id", "project_ref", "workspace_id", "scope", "acl",
                }
            )
            item["scope_summary"] = {"scope_keys": scope_keys}
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            item["row_count"] = len(value)
            item["scope_summary"] = {"scope_keys": []}
        else:
            item["row_count"] = 1
            item["scope_summary"] = {"scope_keys": []}
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        errors.append(("manifest_parse_error", f"{type(exc).__name__}: {exc}"))
    return item, errors


@dataclass(frozen=True)
class ErrorLedgerEntry:
    domain: str
    path: str
    code: str
    message: str
    observed_at: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "path": self.path,
            "code": self.code,
            "message": self.message,
            "observed_at": self.observed_at,
        }

    as_dict = to_dict


@dataclass(frozen=True)
class InventoryItem:
    """One source artifact and its immutable-at-observation metrics."""

    domain: str
    path: str
    exists: bool
    schema_marker: str = ""
    row_count: int | None = None
    file_size: int | None = None
    sha256: str = ""
    content_digest: str = ""
    scope_summary: Mapping[str, Any] = field(default_factory=dict)
    table_rows: Mapping[str, int] = field(default_factory=dict)
    errors: tuple[str, ...] = ()

    @property
    def absolute_path(self) -> str:
        return self.path

    @property
    def hash(self) -> str:
        return self.sha256

    @property
    def acl_summary(self) -> Mapping[str, Any]:
        return self.scope_summary

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "path": self.path,
            "exists": self.exists,
            "schema_marker": self.schema_marker,
            "row_count": self.row_count,
            "file_size": self.file_size,
            "sha256": self.sha256,
            "content_digest": self.content_digest,
            "scope_summary": dict(self.scope_summary),
            "table_rows": dict(self.table_rows),
            "errors": list(self.errors),
        }

    as_dict = to_dict

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


@dataclass(frozen=True)
class InventorySnapshot:
    items: tuple[InventoryItem, ...]
    errors: tuple[ErrorLedgerEntry, ...] = ()
    created_at: str = field(default_factory=_utc_now)
    source_root: str = ""
    workspace_source_pointer: str = ""
    global_source_pointer: str = ""
    data_home_root: str = ""

    @property
    def inventory_digest(self) -> str:
        # SQLite file bytes contain page/free-list details that vary with
        # insertion order.  The logical content digest is the migration
        # identity for databases; retain sha256 in the inventory evidence but
        # do not let it make an equivalent database look different.
        canonical_items: list[dict[str, Any]] = []
        for item in self.items:
            payload = item.to_dict()
            if Path(item.path).suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
                payload["sha256"] = ""
            canonical_items.append(payload)
        return _stable_digest(
            {
                "workspace_source_pointer": self.workspace_source_pointer,
                "global_source_pointer": self.global_source_pointer,
                "data_home_root": self.data_home_root,
                "items": canonical_items,
            }
        )

    @property
    def digest(self) -> str:
        return self.inventory_digest

    @property
    def content_digest(self) -> str:
        return _stable_digest(
            [
                {
                    "domain": item.domain,
                    "path": item.path,
                    "content_digest": item.content_digest,
                    "sha256": "" if Path(item.path).suffix.lower() in {".db", ".sqlite", ".sqlite3"} else item.sha256,
                }
                for item in self.items
            ]
        )

    @property
    def error_ledger(self) -> tuple[dict[str, Any], ...]:
        return tuple(entry.to_dict() for entry in self.errors)

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)

    @property
    def ok(self) -> bool:
        return not self.errors

    def for_domain(self, domain: str) -> tuple[InventoryItem, ...]:
        return tuple(item for item in self.items if item.domain == domain)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_root": self.source_root,
            "workspace_source_pointer": self.workspace_source_pointer,
            "global_source_pointer": self.global_source_pointer,
            "data_home_root": self.data_home_root,
            "created_at": self.created_at,
            "inventory_digest": self.inventory_digest,
            "content_digest": self.content_digest,
            "items": [item.to_dict() for item in self.items],
            "errors": [entry.to_dict() for entry in self.errors],
        }

    as_dict = to_dict

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


class V1Reader:
    """Read-only V1 inventory reader.

    ``workspace`` and ``data_home`` are separate roots because V1 knowledge
    may already be outside a project.  A missing path is represented in the
    inventory and error ledger; ``read(strict=True)`` raises before returning,
    which gives callers an explicit fail-closed entry point.
    """

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        workspace: str | Path | None = None,
        workspace_source_pointer: str | Path | None = None,
        data_home: str | Path | None = None,
        global_source_pointer: str | Path | None = None,
        manifest: Mapping[str, Any] | None = None,
        v1_root: str | Path | None = None,
        v2_root: str | Path | None = None,
        legacy_paths: Mapping[str, Iterable[str | Path]] | None = None,
        required_domains: Iterable[str] | None = None,
    ) -> None:
        manifest_values = dict(manifest or {})
        if workspace_source_pointer is None:
            workspace_source_pointer = manifest_values.get("workspace_source_pointer") or None
        if global_source_pointer is None:
            global_source_pointer = manifest_values.get("global_source_pointer") or None
        if data_home is None:
            data_home = manifest_values.get("data_home_root") or None

        selected = workspace if workspace is not None else root if root is not None else v1_root
        if selected is None and workspace_source_pointer is not None:
            selected = workspace_source_pointer
        # Keep a non-creating fallback for backwards-compatible object
        # construction, but remember that the source pointer was not explicit
        # so scan() can report a fail-closed configuration error.
        self.workspace_configured = selected is not None
        selected = selected or Path.cwd()
        self.workspace = _resolve_no_follow(selected)
        self.workspace_source_pointer = (
            str(_resolve_no_follow(workspace_source_pointer) if workspace_source_pointer is not None else self.workspace)
            if self.workspace_configured or workspace_source_pointer is not None
            else "NOT_CONFIGURED"
        )

        if data_home is not None and global_source_pointer is not None:
            data_root = _resolve_no_follow(data_home)
            global_pointer = _resolve_no_follow(global_source_pointer)
            try:
                global_pointer.relative_to(data_root)
            except ValueError as exc:
                raise PathContainmentError("global_source_pointer must be contained by data_home") from exc
        selected_global = global_source_pointer if global_source_pointer is not None else data_home
        self.global_source_configured = selected_global is not None
        self.data_home = _resolve_no_follow(data_home) if data_home is not None else (
            _resolve_no_follow(selected_global) if selected_global is not None else None
        )
        if data_home is not None:
            data_home_root = self.data_home
            self.global_source_pointer = str(
                _resolve_no_follow(global_source_pointer)
                if global_source_pointer is not None
                else data_home_root / "knowledge" / "knowledge.db"
            )
        elif global_source_pointer is not None:
            self.global_source_pointer = str(_resolve_no_follow(global_source_pointer))
        else:
            self.global_source_pointer = "NOT_CONFIGURED"
        self.data_home_root = str(self.data_home) if self.data_home is not None else "NOT_CONFIGURED"
        roots = [self.workspace]
        if self.data_home is not None:
            roots.append(self.data_home)
        self._allowed_roots = tuple(dict.fromkeys(roots))
        # Preparation stores immutable source snapshots under this control
        # subtree.  It is never a V1 input, even when a previous batch left a
        # nested manifest or source file there.  Keep the check relative to
        # the configured reader root: an explicitly selected source snapshot
        # remains readable, while its own nested migration-backups subtree is
        # still excluded.
        if self.workspace_source_pointer != "NOT_CONFIGURED":
            _contained(self.workspace_source_pointer, (self.workspace,))
        if self.global_source_pointer != "NOT_CONFIGURED":
            _contained(self.global_source_pointer, self._allowed_roots)
        self.v2_root = _resolve_no_follow(v2_root) if v2_root is not None else None
        self.required_domains = frozenset(required_domains or {"shared_memory", "rule_intelligence", "conversation_history", "knowledge"})
        self.legacy_paths = {
            key: tuple(_contained(path, self._allowed_roots) for path in paths)
            for key, paths in (legacy_paths or {}).items()
        }

    def _is_migration_backup(self, path: str | Path) -> bool:
        """Return whether *path* is inside a ``.memoryguard/migration-backups`` subtree.

        Check both the lexical path and the resolved path.  The lexical check
        prevents a symlinked entry from becoming an input before containment
        validation; the resolved check covers a path reached through a safe
        directory link.  The relation is relative to this reader's configured
        roots so a caller can intentionally read a frozen source snapshot.
        """

        candidates = (Path(path).expanduser().absolute(), _resolve_no_follow(path))
        for candidate in candidates:
            for root in self._allowed_roots:
                try:
                    relative = candidate.relative_to(root)
                except ValueError:
                    continue
                parts = tuple(part.casefold() for part in relative.parts)
                if any(
                    parts[index] == ".memoryguard"
                    and index + 1 < len(parts)
                    and parts[index + 1] == "migration-backups"
                    for index in range(len(parts))
                ):
                    return True
        return False

    def _candidate(self, domain: str) -> list[Path]:
        if domain in self.legacy_paths:
            return [path for path in self.legacy_paths[domain] if not self._is_migration_backup(path)]
        memory_root = self.workspace / ".memoryguard"
        if domain == "shared_memory":
            bases = [memory_root / "shared-memory", memory_root / "shared_memory", self.workspace / "shared-memory"]
            paths: list[Path] = []
            for base in bases:
                base = _contained(base, self._allowed_roots)
                if base.is_dir():
                    try:
                        children = sorted(base.iterdir(), key=lambda item: item.name)
                    except OSError:
                        children = []
                    paths.extend(child / "memory.db" for child in children if child.is_dir())
            # Keep a canonical missing marker when there is no group.  This is
            # an error in strict mode, never an invitation to create a group.
            candidates = paths or [_contained(memory_root / "shared-memory" / "memory.db", self._allowed_roots)]
            return [path for path in candidates if not self._is_migration_backup(path)]
        if domain == "rule_intelligence":
            candidate = _contained(memory_root / "rule-intelligence" / "memory.db", self._allowed_roots)
            return [] if self._is_migration_backup(candidate) else [candidate]
        if domain == "conversation_history":
            candidate = _contained(memory_root / "history" / "history.sqlite", self._allowed_roots)
            return [] if self._is_migration_backup(candidate) else [candidate]
        if domain == "knowledge":
            candidates = [memory_root / "knowledge" / "knowledge.db", self.workspace / "knowledge" / "knowledge.db"]
            if self.global_source_pointer != "NOT_CONFIGURED":
                global_path = _resolve_no_follow(self.global_source_pointer)
                # An explicitly supplied pointer may identify the database
                # itself.  Otherwise only the documented knowledge.db child
                # is considered; no directory-name guessing is performed.
                if global_path.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
                    global_path = self.data_home / "knowledge" / "knowledge.db"  # type: ignore[operator]
                candidates.insert(0, global_path)
            return [
                path
                for path in (_contained(path, self._allowed_roots) for path in candidates)
                if not self._is_migration_backup(path)
            ]
        return []

    def _manifest_paths(self) -> list[Path]:
        paths: set[Path] = set()
        # Manifests are intentionally restricted to likely control/data roots;
        # walking an arbitrary source tree can read user content and is not a
        # safe migration inventory operation.
        roots = [self.workspace / ".memoryguard", self.workspace]
        if self.data_home is not None and self.data_home != self.workspace:
            roots.append(self.data_home)
        for root in roots:
            root = _contained(root, self._allowed_roots)
            if not root.exists() or not root.is_dir():
                continue
            try:
                walker = os.walk(root, followlinks=False)
                for current, dirs, files in walker:
                    current_path = Path(current)
                    if self._is_migration_backup(current_path):
                        dirs[:] = []
                        continue
                    # V2 artifacts are not V1 sources; this matters when both
                    # generations share a project ``.memoryguard`` root.
                    if self._is_v2_artifact(current_path):
                        dirs[:] = []
                        continue
                    dirs[:] = [
                        name
                        for name in dirs
                        if name not in {"__pycache__", ".git", "graphify-out"}
                        and not self._is_migration_backup(current_path / name)
                    ]
                    for name in files:
                        lowered = name.lower()
                        if lowered == "manifest.json" or lowered.endswith(".manifest.json") or (lowered.startswith("manifest") and lowered.endswith(".json")):
                            try:
                                candidate = _contained(current_path / name, self._allowed_roots)
                            except PathContainmentError:
                                # Preserve the entry for the explicit
                                # containment ledger; ``_inventory_path``
                                # records the failure without reading it.
                                candidate = Path(current_path / name).absolute()
                            if not self._is_v2_artifact(candidate) and not self._is_migration_backup(candidate):
                                paths.add(candidate)
            except OSError as exc:
                # The root itself is represented as an explicit ledger entry by
                # ``scan``; no hidden skip is allowed.
                paths.add(root / "manifest.json")
        return sorted(paths)

    def _is_v2_artifact(self, path: Path) -> bool:
        """Exclude only V2 domain directories, not legacy ``.memoryguard``.

        V1 and V2 intentionally share the project control root.  Excluding
        the whole ``.memoryguard`` directory would silently omit legacy
        manifests and violate inventory completeness.
        """

        if self.v2_root is None:
            return False
        try:
            relative = path.resolve(strict=False).relative_to(self.v2_root)
        except ValueError:
            return False
        return bool(relative.parts and relative.parts[0] in V2_DOMAINS)

    def _inventory_path(
        self,
        domain: str,
        path: Path,
        *,
        immutable: bool = False,
    ) -> tuple[InventoryItem, list[ErrorLedgerEntry]]:
        errors: list[ErrorLedgerEntry] = []
        try:
            path = _contained(path, self._allowed_roots)
        except PathContainmentError as exc:
            # Do not allow one unsafe symlink to abort the entire inventory;
            # it becomes an explicit error-ledger item instead.
            raw_path = str(Path(path).expanduser().absolute())
            entry = ErrorLedgerEntry(domain, raw_path, "path_escape", str(exc))
            return InventoryItem(domain, raw_path, True, errors=("path_escape",)), [entry]
        # A symlink target outside the allowed roots is a containment error even
        # if the directory entry itself is under the workspace.
        if path.is_symlink():
            try:
                _contained(path.resolve(), self._allowed_roots)
            except PathContainmentError as exc:
                errors.append(ErrorLedgerEntry(domain, str(path), "path_escape", str(exc)))
                return InventoryItem(domain, str(path), True, errors=("path_escape",)), errors
        if not path.exists():
            code = "missing_required_source" if domain != "manifests" else "manifest_not_found"
            entry = ErrorLedgerEntry(domain, str(path), code, "V1 source path does not exist; no database was created")
            return InventoryItem(domain, str(path), False, errors=(code,)), [entry]
        try:
            scope = _file_scope_summary(path)
        except OSError as exc:
            entry = ErrorLedgerEntry(domain, str(path), "stat_error", str(exc))
            return InventoryItem(domain, str(path), True, errors=("stat_error",)), [entry]
        if path.suffix.lower() in {".db", ".sqlite", ".sqlite3"} or domain in {"shared_memory", "rule_intelligence", "conversation_history", "knowledge"}:
            metrics, raw_errors = _sqlite_inventory(path, immutable=immutable)
        else:
            metrics, raw_errors = _json_inventory(path)
        scope = {**scope, **dict(metrics.get("scope_summary") or {})}
        entries = [ErrorLedgerEntry(domain, str(path), code, message) for code, message in raw_errors]
        return InventoryItem(
            domain=domain,
            path=str(path),
            exists=True,
            schema_marker=str(metrics.get("schema_marker") or ""),
            row_count=metrics.get("row_count"),
            file_size=metrics.get("file_size"),
            sha256=str(metrics.get("sha256") or ""),
            content_digest=str(metrics.get("content_digest") or ""),
            scope_summary=scope,
            table_rows=metrics.get("table_rows") or {},
            errors=tuple(entry.code for entry in entries),
        ), entries

    def scan(self, *, strict: bool = False, immutable: bool = False) -> InventorySnapshot:
        items: list[InventoryItem] = []
        errors: list[ErrorLedgerEntry] = []
        seen: set[tuple[str, str]] = set()
        for domain in ("shared_memory", "rule_intelligence", "conversation_history", "knowledge"):
            for path in self._candidate(domain):
                if self._is_migration_backup(path):
                    continue
                key = (domain, str(path))
                if key in seen:
                    continue
                seen.add(key)
                # Knowledge may have multiple legacy candidates.  Missing
                # alternatives are only ledger errors when no candidate exists.
                item, item_errors = self._inventory_path(domain, path, immutable=immutable)
                items.append(item)
                if domain in self.required_domains or item.exists:
                    errors.extend(item_errors)
        knowledge_items = [item for item in items if item.domain == "knowledge" and item.exists]
        if knowledge_items:
            errors = [entry for entry in errors if not (entry.domain == "knowledge" and entry.code == "missing_required_source")]
        for path in self._manifest_paths():
            key = ("manifests", str(path))
            if key in seen:
                continue
            seen.add(key)
            item, item_errors = self._inventory_path("manifests", path, immutable=immutable)
            # No manifest is a valid empty legacy surface; malformed or
            # unreadable manifests remain explicit errors.
            if item.exists:
                items.append(item)
                errors.extend(item_errors)
        # A source pointer is configuration evidence, not an inferred path.
        # If no workspace source was supplied, fail closed without silently
        # treating the process CWD as the migration source.  A missing global
        # pointer is reported when no workspace knowledge source exists, which
        # is the only case where a caller would otherwise be tempted to guess
        # the legacy global knowledge location.
        if self.workspace_source_pointer == "NOT_CONFIGURED":
            errors.append(
                ErrorLedgerEntry(
                    "workspace",
                    "<workspace_source_pointer:NOT_CONFIGURED>",
                    "NOT_CONFIGURED",
                    "workspace source pointer is required; no path was inferred",
                )
            )
        if "knowledge" in self.required_domains and not self.global_source_configured:
            marker = "<global_source_pointer:NOT_CONFIGURED>"
            items.append(InventoryItem("knowledge", marker, False, errors=("NOT_CONFIGURED",)))
            errors.append(
                ErrorLedgerEntry(
                    "knowledge",
                    marker,
                    "NOT_CONFIGURED",
                    "global knowledge source pointer is required; data_home was not supplied",
                )
            )
        snapshot = InventorySnapshot(
            tuple(items),
            tuple(errors),
            source_root=str(self.workspace),
            workspace_source_pointer=self.workspace_source_pointer,
            global_source_pointer=self.global_source_pointer,
            data_home_root=self.data_home_root,
        )
        if strict and snapshot.errors:
            detail = "; ".join(f"{entry.domain}:{entry.code}:{entry.path}" for entry in snapshot.errors[:8])
            raise MigrationReadError(f"V1 inventory failed closed ({len(snapshot.errors)} error(s)): {detail}")
        return snapshot

    inventory = scan
    snapshot = scan

    def read(self, *, strict: bool = True) -> InventorySnapshot:
        return self.scan(strict=strict)

    def read_only(self, *, strict: bool = True) -> InventorySnapshot:
        return self.scan(strict=strict, immutable=True)


class ManifestStore(Protocol):
    """Minimal system-manifest API consumed by the coordinator."""

    def load(self) -> Mapping[str, Any] | None: ...

    def save(self, payload: Mapping[str, Any]) -> None: ...


class JsonManifestStore:
    """Fallback journal used only when the V2 system API is unavailable."""

    def __init__(self, path: str | Path) -> None:
        self.path = _resolve_no_follow(path)

    def load(self) -> Mapping[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise MigrationError(f"migration journal unreadable: {self.path}: {exc}") from exc
        if not isinstance(value, Mapping):
            raise MigrationError(f"migration journal must be an object: {self.path}")
        return value

    def save(self, payload: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(dict(payload), handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _latest_checkpoint(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Select the most recent immutable checkpoint from a checkpoint map."""

    if not isinstance(value, Mapping) or not value:
        return {}
    if "step" in value:
        return dict(value)
    candidates = [item for item in value.values() if isinstance(item, Mapping)]
    if not candidates:
        return {}
    return dict(max(candidates, key=lambda item: str(item.get("updated_at") or item.get("rolled_back_at") or "")))


class SystemManifestStore:
    """Adapter for the V2 ``system.manifest.ManifestManager`` API.

    The system service owns schema creation, transition validation and its
    migration ledger.  This adapter only translates the coordinator's
    checkpoint shape; it does not maintain a second manifest database.
    """

    def __init__(self, workspace_or_layout: str | Path | Any) -> None:
        from ..system.manifest import ManifestManager

        self.manager = ManifestManager(workspace_or_layout)

    def load(self) -> Mapping[str, Any] | None:
        # Loading is a read-only manifest operation.  Immutable mode prevents
        # SQLite from touching an existing WAL/SHM sidecar while preserving the
        # adapter's no-creation contract; save() remains the write path.
        if _nonempty_wal(self.manager.db_path):
            raise MigrationReadError("immutable manifest read blocked by non-empty WAL")
        record = self.manager.current(immutable=True)
        checkpoints = dict(getattr(record, "checkpoints", {}) or record.digests.get("checkpoints") or {})
        inventory = record.digests.get("inventory") or record.digests.get("inventory_snapshot")
        return {
            "state": record.state.value,
            "migration_id": record.migration_id,
            "failure_reason": record.last_error,
            "checkpoint": _latest_checkpoint(checkpoints) or dict(record.digests.get("checkpoint") or {}),
            "checkpoints": checkpoints,
            "inventory": inventory,
            "manifest_digests": dict(record.digests),
            "source_digest": record.source_digest,
            "target_digest": record.target_digest,
            "manifest_digest": record.manifest_digest,
            "workspace_source_pointer": getattr(record, "workspace_source_pointer", ""),
            "global_source_pointer": getattr(record, "global_source_pointer", ""),
            "data_home_root": getattr(record, "data_home_root", ""),
        }

    def save(self, payload: Mapping[str, Any]) -> None:
        from ..system.manifest import ManifestState

        target = ManifestState(str(payload.get("state") or ManifestState.V1_ACTIVE.value))
        current = self.manager.current()
        migration_id = str(payload.get("migration_id") or "")
        pointer_kwargs = {
            "workspace_source_pointer": str(payload.get("workspace_source_pointer") or "") or None,
            "global_source_pointer": str(payload.get("global_source_pointer") or "") or None,
            "data_home_root": str(payload.get("data_home_root") or "") or None,
        }
        if target is current.state and target is not ManifestState.V1_ACTIVE:
            # Checkpoints are the one legal same-state write.  Use an
            # immutable, step-namespaced key; repeating ``step``/``updated_at``
            # would attempt to overwrite an earlier checkpoint.
            if target in {ManifestState.V2_BUILDING, ManifestState.V2_READY}:
                checkpoint = dict(payload.get("checkpoint") or {})
                step = str(checkpoint.get("step") or "")
                if step:
                    entry = dict(checkpoint)
                    self.manager.record_checkpoint(
                        {step: entry},
                        migration_id=migration_id,
                    )
            return
        checkpoint = dict(payload.get("checkpoint") or {})
        checkpoints = dict(payload.get("checkpoints") or {})
        if not checkpoints and isinstance(payload.get("manifest_digests"), Mapping):
            checkpoints = dict(payload["manifest_digests"].get("checkpoints") or {})
        if checkpoint.get("step") and checkpoint.get("step") not in checkpoints:
            checkpoints[str(checkpoint["step"])] = checkpoint
        inventory = payload.get("inventory")
        digests = {
            **dict(payload.get("manifest_digests") or {}),
            "checkpoint": checkpoint,
            "checkpoints": checkpoints,
            "inventory": inventory,
            "inventory_digest": checkpoint.get("inventory_digest", ""),
            "migration_id": migration_id,
        }
        source_digest = str(checkpoint.get("inventory_digest") or payload.get("source_digest") or "")
        target_digest = str(payload.get("target_digest") or "")
        manifest_digest = str(payload.get("manifest_digest") or _stable_digest({"state": target.value, "source_digest": source_digest, "target_digest": target_digest, "digests": digests}))
        if target is ManifestState.V1_ACTIVE:
            reason = str(payload.get("failure_reason") or checkpoint.get("failure_reason") or "migration failed")
            error_map = {
                "checkpoint": checkpoint,
                "checkpoints": checkpoints,
                "inventory": inventory,
                "inventory_digest": checkpoint.get("inventory_digest", ""),
            }
            if current.state is ManifestState.V1_ACTIVE:
                # ManifestManager deliberately rejects a same-state replay
                # with changed failure evidence.  Open a fresh BUILDING edge
                # solely to make the failure durable, then close it in the
                # same batch; no JSON side journal is involved.
                migration_id = migration_id or uuid.uuid4().hex
                self.manager.transition(
                    ManifestState.V2_BUILDING,
                    migration_id=migration_id,
                    digests=digests,
                    **pointer_kwargs,
                )
            self.manager.transition(
                ManifestState.V1_ACTIVE,
                migration_id=migration_id,
                source_digest=source_digest,
                target_digest=target_digest,
                manifest_digest=manifest_digest,
                digests=digests,
                error=reason,
                errors=error_map,
                **pointer_kwargs,
            )
            return
        if target is ManifestState.V2_ACTIVE:
            # Activation inherits the immutable READY evidence.  Passing new
            # digests here would be rejected by the system manifest API.
            self.manager.activate_v2(migration_id=migration_id)
            return
        self.manager.transition(
            target,
            migration_id=migration_id,
            source_digest=source_digest,
            target_digest=target_digest,
            manifest_digest=manifest_digest,
            digests=digests,
            **pointer_kwargs,
        )


def _default_v2_root(workspace: Path) -> Path:
    # Import lazily so importing migration remains cheap, but never fall back
    # to a parallel journal when the canonical storage package is unavailable.
    from ..storage.layout import WorkspaceV2Layout  # type: ignore

    return Path(WorkspaceV2Layout(workspace).root)


class MigrationCoordinator:
    """Prepare/validate/activate coordinator with compensating rollback."""

    ALLOWED_TRANSITIONS: Mapping[MigrationState, frozenset[MigrationState]] = {
        MigrationState.V1_ACTIVE: frozenset({MigrationState.V2_BUILDING}),
        MigrationState.V2_BUILDING: frozenset({MigrationState.V2_READY, MigrationState.V1_ACTIVE}),
        MigrationState.V2_READY: frozenset({MigrationState.V2_ACTIVE, MigrationState.V1_ACTIVE}),
        MigrationState.V2_ACTIVE: frozenset({MigrationState.V1_ACTIVE}),
    }

    def __init__(
        self,
        workspace: str | Path | None = None,
        *,
        v1_reader: V1Reader | None = None,
        reader: V1Reader | None = None,
        workspace_source_pointer: str | Path | None = None,
        data_home: str | Path | None = None,
        global_source_pointer: str | Path | None = None,
        source_manifest: Mapping[str, Any] | None = None,
        v2_root: str | Path | None = None,
        manifest_store: ManifestStore | None = None,
        checkpoint_store: ManifestStore | None = None,
        fault_hook: Callable[[str], None] | None = None,
        fail_at: str | None = None,
    ) -> None:
        self.reader = v1_reader or reader or V1Reader(
            workspace,
            workspace_source_pointer=workspace_source_pointer,
            data_home=data_home,
            global_source_pointer=global_source_pointer,
            manifest=source_manifest,
        )
        self.workspace = self.reader.workspace
        supplied_manifest_store = manifest_store or checkpoint_store
        canonical_root = _default_v2_root(self.workspace)
        self.v2_root = _resolve_no_follow(v2_root) if v2_root is not None else canonical_root
        try:
            _contained(self.v2_root, self.reader._allowed_roots)
        except PathContainmentError as exc:
            raise PathContainmentError(f"V2 root escapes workspace/data-home roots: {self.v2_root}") from exc
        if self.reader.v2_root is None:
            self.reader.v2_root = self.v2_root
        self._allowed_v2_root = self.v2_root
        if supplied_manifest_store is not None:
            # Explicit stores are the only supported journal injection point;
            # this is useful for isolated tests and fault-injection harnesses.
            self.manifest_store = supplied_manifest_store
        else:
            if self.v2_root != canonical_root:
                raise MigrationError(
                    "v2_root cannot be mapped to canonical WorkspaceV2Layout; "
                    "provide an explicit manifest_store for test injection"
                )
            from ..storage.layout import WorkspaceV2Layout

            self.manifest_store = SystemManifestStore(WorkspaceV2Layout(self.workspace))
        self.fault_hook = fault_hook
        self.fail_at = fail_at
        loaded = self.manifest_store.load()
        if loaded:
            raw_state = str(loaded.get("state") or MigrationState.V1_ACTIVE.value)
            try:
                self._state = MigrationState(raw_state)
            except ValueError as exc:
                raise MigrationError(f"unknown migration state: {raw_state!r}") from exc
            self.checkpoint: dict[str, Any] = dict(loaded.get("checkpoint") or {})
            self._checkpoints: dict[str, Any] = dict(loaded.get("checkpoints") or {})
            self.failure_reason = str(loaded.get("failure_reason") or "")
            self.migration_id = str(loaded.get("migration_id") or "")
            self._source_digest = str(loaded.get("source_digest") or "")
            self._target_digest = str(loaded.get("target_digest") or "")
            self._manifest_digest = str(loaded.get("manifest_digest") or "")
            self._manifest_digests: dict[str, Any] = dict(loaded.get("manifest_digests") or {})
            self._inventory: InventorySnapshot | None = None
            if isinstance(loaded.get("inventory"), Mapping):
                self._inventory = _snapshot_from_dict(loaded["inventory"])
            elif isinstance(loaded.get("inventory_snapshot"), Mapping):
                self._inventory = _snapshot_from_dict(loaded["inventory_snapshot"])
        else:
            self._state = MigrationState.V1_ACTIVE
            self.checkpoint = {}
            self._checkpoints = {}
            self.failure_reason = ""
            self.migration_id = ""
            self._source_digest = ""
            self._target_digest = ""
            self._manifest_digest = ""
            self._manifest_digests = {}
            self._inventory = None

    @property
    def state(self) -> MigrationState:
        return self._state

    @property
    def status(self) -> MigrationState:
        return self._state

    @property
    def inventory_snapshot(self) -> InventorySnapshot | None:
        return self._inventory

    @property
    def checkpoints(self) -> dict[str, Any]:
        """All immutable checkpoints observed for the current batch."""

        return {key: dict(value) if isinstance(value, Mapping) else value for key, value in self._checkpoints.items()}

    @property
    def runtime_read_path_switched(self) -> bool:
        # Phase 1 never toggles an existing runtime read path.
        return False

    @property
    def v2_paths(self) -> dict[str, tuple[str, ...]]:
        return {
            domain: tuple(str(self.v2_root / domain / name) for name in names)
            for domain, names in V2_DOMAIN_DB_NAMES.items()
        }

    def _checkpoint(self, step: str, **extra: Any) -> None:
        self.checkpoint = {
            "step": step,
            "state": self._state.value,
            "migration_id": self.migration_id,
            "updated_at": _utc_now(),
            **extra,
        }
        self._checkpoints[step] = dict(self.checkpoint)
        self._persist()
        self._fault(step)

    def _fault(self, step: str) -> None:
        if self.fail_at and self.fail_at == step:
            raise RuntimeError(f"injected migration failure at {step}")
        if self.fault_hook is not None:
            self.fault_hook(step)

    def _persist(self) -> None:
        self.manifest_store.save(
            {
                "schema_version": 1,
                "state": self._state.value,
                "migration_id": self.migration_id,
                "failure_reason": self.failure_reason,
                "source_digest": self._source_digest,
                "target_digest": self._target_digest,
                "manifest_digest": self._manifest_digest,
                "checkpoint": dict(self.checkpoint),
                "checkpoints": dict(self._checkpoints),
                "inventory": self._inventory.to_dict() if self._inventory else None,
                "manifest_digests": dict(self._manifest_digests),
                "v2_paths": self.v2_paths,
                "workspace_source_pointer": self.reader.workspace_source_pointer,
                "global_source_pointer": self.reader.global_source_pointer,
                "data_home_root": self.reader.data_home_root,
            }
        )

    def _transition(self, target: MigrationState) -> None:
        if target not in self.ALLOWED_TRANSITIONS[self._state]:
            raise MigrationError(f"invalid migration transition {self._state.value} -> {target.value}")
        self._state = target
        self._persist()

    def _rollback(self, reason: BaseException | str, *, step: str) -> None:
        self.failure_reason = str(reason)
        self.checkpoint = {
            **self.checkpoint,
            "step": step,
            "state_before_rollback": self._state.value,
            "state": MigrationState.V1_ACTIVE.value,
            "migration_id": self.migration_id,
            "failure_reason": self.failure_reason,
            "updated_at": _utc_now(),
            "rolled_back_at": _utc_now(),
        }
        self._checkpoints[step] = dict(self.checkpoint)
        self._state = MigrationState.V1_ACTIVE
        self._persist()

    def prepare(self, *, strict: bool = True) -> InventorySnapshot:
        """Inventory V1 and enter ``V2_BUILDING``; no business rows are copied."""

        if self._state in {MigrationState.V2_BUILDING, MigrationState.V2_READY, MigrationState.V2_ACTIVE}:
            if self._inventory is not None:
                # Idempotent re-entry: no new dirs, rows or checkpoint expansion.
                return self._inventory
            if self._state is MigrationState.V2_BUILDING:
                # A process may have exited after the system manifest recorded
                # V2_BUILDING but before the inventory checkpoint.  Re-read
                # V1 (still read-only) instead of starting a second build.
                self._inventory = self.reader.scan(strict=strict)
                return self._inventory
            raise MigrationError(f"inventory unavailable in persisted state {self._state.value}")
        if self._state != MigrationState.V1_ACTIVE:
            raise MigrationError(f"cannot prepare from {self._state.value}")
        try:
            # Every new attempt receives a fresh batch identity.  A resumed
            # V2_BUILDING coordinator takes the early idempotent branch above
            # and therefore keeps its original migration_id.
            self.migration_id = uuid.uuid4().hex
            self._checkpoints = {}
            self.checkpoint = {}
            self._inventory = None
            self._manifest_digests = {
                "phase": "phase1",
                "validator_passed": False,
                "migration_id": self.migration_id,
            }
            self._fault("before_prepare")
            self.checkpoint = {
                "step": "state_entered_building",
                "state": MigrationState.V2_BUILDING.value,
                "migration_id": self.migration_id,
                "updated_at": _utc_now(),
            }
            self._checkpoints["state_entered_building"] = dict(self.checkpoint)
            self._manifest_digests["checkpoints"] = dict(self._checkpoints)
            self._transition(MigrationState.V2_BUILDING)
            self._fault("state_entered_building")
            inventory = self.reader.scan(strict=strict)
            self._inventory = inventory
            self._checkpoint("inventory_complete", inventory_digest=inventory.inventory_digest, error_count=len(inventory.errors))
            return inventory
        except Exception as exc:
            self._rollback(exc, step="prepare_failed")
            raise

    build = prepare

    def mark_ready(self, validation: Any | None = None, *, allow_not_evaluated: bool = False) -> Any:
        """Promote a validated build.  Phase 1's default validator blocks this."""

        if self._state == MigrationState.V2_READY:
            return validation
        if self._state != MigrationState.V2_BUILDING:
            raise MigrationError(f"cannot mark ready from {self._state.value}")
        try:
            self._fault("before_ready")
            if validation is None:
                raise MigrationError("validation_required_before_v2_ready")
            status = str(getattr(validation, "status", ""))
            can_promote = bool(getattr(validation, "can_promote", getattr(validation, "ok", False)))
            if status == "NOT_EVALUATED" and not allow_not_evaluated:
                raise MigrationError("validation_not_evaluated")
            if not can_promote and not allow_not_evaluated:
                raise MigrationError("validation_failed")
            if self._inventory is None:
                raise MigrationError("inventory_required_before_v2_ready")
            validation_payload = getattr(validation, "to_dict", lambda: validation)()
            self._source_digest = self._inventory.inventory_digest
            self._target_digest = str(
                getattr(validation, "target_digest", "")
                or _stable_digest({"phase": "phase1", "v2_paths": self.v2_paths})
            )
            checkpoints = {
                "inventory": dict(self.checkpoint),
                "validator": validation_payload,
            }
            self._checkpoints["validator"] = {
                "step": "validator",
                "state": MigrationState.V2_BUILDING.value,
                "migration_id": self.migration_id,
                "updated_at": _utc_now(),
                "validation": validation_payload,
            }
            self._manifest_digests = {
                "phase": "phase1",
                "validator_passed": bool(can_promote),
                "migration_id": self.migration_id,
                "checkpoints": checkpoints,
                "inventory_digest": self._source_digest,
            }
            self._manifest_digest = _stable_digest({
                "source_digest": self._source_digest,
                "target_digest": self._target_digest,
                "digests": self._manifest_digests,
            })
            self.checkpoint = {
                **self.checkpoint,
                "step": "v2_ready",
                "migration_id": self.migration_id,
                "inventory_digest": self._source_digest,
                "validation_digest": _stable_digest(validation_payload),
            }
            self._checkpoints["v2_ready"] = dict(self.checkpoint)
            self._manifest_digests["checkpoints"] = dict(self._checkpoints)
            self._transition(MigrationState.V2_READY)
            self._fault("v2_ready")
            return validation
        except Exception as exc:
            self._rollback(exc, step="ready_failed")
            raise

    ready = mark_ready

    def activate(self) -> MigrationState:
        """Record V2 active metadata without switching an existing runtime."""

        if self._state == MigrationState.V2_ACTIVE:
            return self._state
        if self._state != MigrationState.V2_READY:
            raise MigrationError(f"cannot activate from {self._state.value}")
        try:
            self._fault("before_activate")
            # Activation evidence is declared while READY.  The subsequent
            # ACTIVE transition can therefore inherit it atomically; a
            # same-state post-ACTIVE checkpoint write is never required.
            self._checkpoint(
                "v2_active_recorded",
                target_state=MigrationState.V2_ACTIVE.value,
                runtime_read_path_switched=False,
            )
            self._transition(MigrationState.V2_ACTIVE)
            return self._state
        except Exception as exc:
            self._rollback(exc, step="activate_failed")
            raise

    def rollback(self, reason: str = "explicit rollback") -> MigrationState:
        if self._state == MigrationState.V1_ACTIVE:
            self.failure_reason = reason
            self._persist()
            return self._state
        self._rollback(reason, step="explicit_rollback")
        return self._state

    def run(self, *, strict: bool = True, validator: Any | None = None) -> InventorySnapshot:
        """Prepare only; Phase 1 deliberately stops before V2_READY."""

        snapshot = self.prepare(strict=strict)
        if validator is not None:
            result = validator.validate(snapshot)
            if bool(getattr(result, "can_promote", False)):
                self.mark_ready(result)
        return snapshot


@dataclass(frozen=True)
class ValidationResult:
    """Structured validator output with explicit non-evaluation semantics."""

    status: str
    ok: bool
    can_promote: bool
    integrity: Mapping[str, Any] = field(default_factory=dict)
    foreign_keys: Mapping[str, Any] = field(default_factory=dict)
    schema_markers: Mapping[str, Any] = field(default_factory=dict)
    inventory_digest: Mapping[str, Any] = field(default_factory=dict)
    loss_metrics: Mapping[str, Any] = field(default_factory=lambda: {"status": "NOT_EVALUATED", "value": None})
    orphan_metrics: Mapping[str, Any] = field(default_factory=lambda: {"status": "NOT_EVALUATED", "value": None})
    errors: tuple[str, ...] = ()
    source_digest: str = ""

    @property
    def ready(self) -> bool:
        return self.can_promote

    @property
    def loss(self) -> str:
        return str(self.loss_metrics.get("status", "NOT_EVALUATED"))

    @property
    def orphan(self) -> str:
        return str(self.orphan_metrics.get("status", "NOT_EVALUATED"))

    @property
    def migration_loss(self) -> Any:
        return self.loss_metrics.get("value")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "can_promote": self.can_promote,
            "integrity": dict(self.integrity),
            "foreign_keys": dict(self.foreign_keys),
            "schema_markers": dict(self.schema_markers),
            "inventory_digest": dict(self.inventory_digest),
            "loss_metrics": dict(self.loss_metrics),
            "orphan_metrics": dict(self.orphan_metrics),
            "errors": list(self.errors),
            "source_digest": self.source_digest,
        }

    as_dict = to_dict

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


class MigrationValidator:
    """Validate structural V2 databases without claiming data conversion."""

    def __init__(self, v2_root: str | Path | None = None, *, target_root: str | Path | None = None) -> None:
        self.v2_root = _resolve_no_follow(v2_root or target_root or Path.cwd() / ".memoryguard")

    @property
    def v2_paths(self) -> dict[str, tuple[Path, ...]]:
        return {
            domain: tuple(self.v2_root / domain / name for name in names)
            for domain, names in V2_DOMAIN_DB_NAMES.items()
        }

    def _validate_db(self, path: Path) -> tuple[str, str, str, list[str]]:
        if not path.exists():
            return "NOT_EVALUATED", "NOT_EVALUATED", "NOT_EVALUATED", [f"missing_v2_database:{path}"]
        errors: list[str] = []
        if _nonempty_wal(path):
            return "FAIL", "FAIL", "FAIL", [f"immutable_read_blocked_nonempty_wal:{path}"]
        try:
            conn = sqlite3.connect(_sqlite_uri(path, immutable=True), uri=True, timeout=2.0)
            try:
                integrity_rows = conn.execute("PRAGMA integrity_check").fetchall()
                integrity = "PASS" if all(str(row[0]).lower() == "ok" for row in integrity_rows) else "FAIL"
                if integrity == "FAIL":
                    errors.append(f"integrity_check_failed:{path}")
                foreign_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
                foreign = "PASS" if not foreign_rows else "FAIL"
                if foreign == "FAIL":
                    errors.append(f"foreign_key_check_failed:{path}:{len(foreign_rows)}")
                marker = _schema_marker(conn)
                marker_status = "PASS" if marker else "FAIL"
                if marker_status == "FAIL":
                    errors.append(f"schema_marker_missing:{path}")
                return integrity, foreign, marker_status, errors
            finally:
                conn.close()
        except (sqlite3.Error, OSError, ValueError) as exc:
            errors.append(f"v2_database_unreadable:{path}:{type(exc).__name__}:{exc}")
            return "FAIL", "FAIL", "FAIL", errors

    def validate(
        self,
        source: InventorySnapshot | Mapping[str, Any] | None = None,
        *,
        target: InventorySnapshot | Mapping[str, Any] | None = None,
        expected_inventory_digest: str | None = None,
    ) -> ValidationResult:
        source_snapshot = _coerce_snapshot(source)
        expected = expected_inventory_digest or (source_snapshot.inventory_digest if source_snapshot else "")
        integrity: dict[str, str] = {}
        foreign: dict[str, str] = {}
        markers: dict[str, str] = {}
        errors: list[str] = []
        present = False
        for domain, paths in self.v2_paths.items():
            for path in paths:
                if path.exists():
                    present = True
                i_status, f_status, m_status, db_errors = self._validate_db(path)
                key = f"{domain}/{path.name}"
                integrity[key] = i_status
                foreign[key] = f_status
                markers[key] = m_status
                errors.extend(db_errors)
        # A target snapshot/digest can be supplied by a future conversion
        # phase.  Phase 1 has no conversion rows, so absence is explicit.
        actual_digest = ""
        if target is not None:
            target_snapshot = _coerce_snapshot(target)
            actual_digest = target_snapshot.inventory_digest if target_snapshot else ""
        digest_status = "NOT_EVALUATED"
        if expected and actual_digest:
            digest_status = "PASS" if expected == actual_digest else "FAIL"
            if digest_status == "FAIL":
                errors.append("inventory_digest_mismatch")
        elif expected and present:
            errors.append("inventory_digest_not_recorded")

        structural_fail = any(value == "FAIL" for value in (*integrity.values(), *foreign.values(), *markers.values()))
        status = "FAIL" if structural_fail or errors and any(not item.startswith("missing_v2_database:") for item in errors) else "NOT_EVALUATED"
        if present and not structural_fail and not errors and digest_status == "PASS":
            # This branch is intentionally reachable only when a later phase
            # supplies a target digest; Phase 1 itself never manufactures one.
            status = "PASS"
        loss = {"status": "NOT_EVALUATED", "value": None, "reason": "phase1_conversion_not_implemented"}
        orphan = {"status": "NOT_EVALUATED", "value": None, "reason": "phase1_conversion_not_implemented"}
        ok = status == "PASS" and loss["status"] == "PASS" and orphan["status"] == "PASS"
        return ValidationResult(
            status=status,
            ok=ok,
            can_promote=ok,
            integrity=integrity,
            foreign_keys=foreign,
            schema_markers=markers,
            inventory_digest={"status": digest_status, "expected": expected, "actual": actual_digest},
            loss_metrics=loss,
            orphan_metrics=orphan,
            errors=tuple(errors),
            source_digest=expected,
        )

    check = validate


def _snapshot_from_dict(value: Mapping[str, Any]) -> InventorySnapshot:
    items: list[InventoryItem] = []
    for raw in value.get("items", ()):
        if not isinstance(raw, Mapping):
            continue
        items.append(
            InventoryItem(
                domain=str(raw.get("domain") or ""),
                path=str(raw.get("path") or ""),
                exists=bool(raw.get("exists")),
                schema_marker=str(raw.get("schema_marker") or ""),
                row_count=raw.get("row_count"),
                file_size=raw.get("file_size"),
                sha256=str(raw.get("sha256") or ""),
                content_digest=str(raw.get("content_digest") or ""),
                scope_summary=dict(raw.get("scope_summary") or {}),
                table_rows=dict(raw.get("table_rows") or {}),
                errors=tuple(str(item) for item in raw.get("errors", ())),
            )
        )
    errors: list[ErrorLedgerEntry] = []
    for raw in value.get("errors", ()):
        if isinstance(raw, Mapping):
            errors.append(
                ErrorLedgerEntry(
                    domain=str(raw.get("domain") or ""),
                    path=str(raw.get("path") or ""),
                    code=str(raw.get("code") or ""),
                    message=str(raw.get("message") or ""),
                    observed_at=str(raw.get("observed_at") or _utc_now()),
                )
            )
    return InventorySnapshot(
        tuple(items),
        tuple(errors),
        str(value.get("created_at") or _utc_now()),
        str(value.get("source_root") or ""),
        str(value.get("workspace_source_pointer") or ""),
        str(value.get("global_source_pointer") or ""),
        str(value.get("data_home_root") or ""),
    )


def _coerce_snapshot(value: InventorySnapshot | Mapping[str, Any] | None) -> InventorySnapshot | None:
    if value is None:
        return None
    if isinstance(value, InventorySnapshot):
        return value
    if isinstance(value, Mapping):
        return _snapshot_from_dict(value)
    raise TypeError("expected InventorySnapshot or mapping")


__all__ = [
    "ErrorLedgerEntry",
    "InventoryItem",
    "InventorySnapshot",
    "JsonManifestStore",
    "ManifestStore",
    "MigrationCoordinator",
    "MigrationError",
    "MigrationPhase",
    "MigrationReadError",
    "MigrationState",
    "MigrationValidator",
    "PathContainmentError",
    "SystemManifestStore",
    "V1Reader",
    "V2_DOMAIN_DB_NAMES",
    "V2_DOMAINS",
    "ValidationResult",
]
