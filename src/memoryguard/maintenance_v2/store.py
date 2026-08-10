"""Transactional Phase 7 maintenance ledger.

This module owns only the control plane for GC/compaction.  It performs audit,
marking, reporting and lease/CAS bookkeeping; it deliberately never deletes a
content row, executes ``VACUUM``, or replaces a database file.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Mapping
from uuid import uuid4

from ..cutover_v2.state import RuntimeSnapshot
from ..storage.database import connect_database
from ..storage.layout import LayoutError, WorkspaceV2Layout
from ..storage.transaction import transaction
from ..system.manifest import ManifestError, ManifestManager, ManifestState
from .models import (
    CandidateState,
    EpochState,
    MaintenanceAuthorizationError,
    MaintenanceCandidate,
    MaintenanceConflictError,
    MaintenanceContext,
    MaintenanceEpoch,
    MaintenanceError,
    MaintenanceJob,
    MaintenanceJobState,
    MaintenanceLease,
    MaintenanceLeaseError,
    MaintenanceOperation,
    MaintenanceReport,
    MaintenanceSchemaError,
    MaintenanceScope,
    SCHEMA_MARKER,
    SCHEMA_VERSION,
    stable_digest,
    stable_id,
)


DB_NAME = "maintenance.db"
SCHEMA_DOMAIN = "maintenance"

_REQUIRED_SCHEMA_COLUMNS: dict[str, frozenset[str]] = {
    "schema_meta": frozenset({"domain", "version", "marker", "updated_at"}),
    "jobs": frozenset({"job_id", "request_key", "operation", "state", "scope_json", "context_digest", "expected_generation", "dry_run", "idempotency_digest", "error_code", "created_at", "updated_at"}),
    "epochs": frozenset({"epoch_id", "job_id", "epoch_number", "state", "reference_digest", "complete", "created_at"}),
    "candidates": frozenset({"candidate_id", "epoch_id", "blob_id", "reference_digest", "hold_digest", "state", "created_at", "updated_at"}),
    "receipts": frozenset({"receipt_id", "request_key", "operation", "request_digest", "job_id", "result_digest", "status", "created_at"}),
    "ledger": frozenset({"ledger_id", "event_type", "job_id", "epoch_id", "candidate_id", "detail_digest", "created_at"}),
    "leases": frozenset({"lease_id", "scope_digest", "owner_id", "expires_at", "acquired_at", "released_at", "active"}),
    "reports": frozenset({"report_id", "job_id", "status", "counts_json", "safety_json", "report_digest", "created_at"}),
}
_REQUIRED_SCHEMA_TABLES = frozenset(_REQUIRED_SCHEMA_COLUMNS)
_SCHEMA_DDL = (
    "CREATE TABLE IF NOT EXISTS schema_meta (domain TEXT PRIMARY KEY, version INTEGER NOT NULL CHECK(version >= 1), marker TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS jobs (job_id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE, operation TEXT NOT NULL CHECK(operation IN ('audit','report','sweep','compact')), state TEXT NOT NULL CHECK(state IN ('PLANNED','AUDITING','READY','ACTIVE','SUCCEEDED','FAILED','CANCELLED')), scope_json TEXT NOT NULL, context_digest TEXT NOT NULL, expected_generation INTEGER CHECK(expected_generation IS NULL OR expected_generation >= 0), dry_run INTEGER NOT NULL CHECK(dry_run IN (0,1)), idempotency_digest TEXT NOT NULL, error_code TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS epochs (epoch_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, epoch_number INTEGER NOT NULL CHECK(epoch_number >= 1), state TEXT NOT NULL CHECK(state IN ('OPEN','COMPLETE','FAILED')), reference_digest TEXT NOT NULL DEFAULT '', complete INTEGER NOT NULL CHECK(complete IN (0,1)), created_at TEXT NOT NULL, UNIQUE(job_id, epoch_number), FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE)",
    "CREATE TABLE IF NOT EXISTS candidates (candidate_id TEXT PRIMARY KEY, epoch_id TEXT NOT NULL, blob_id TEXT NOT NULL, reference_digest TEXT NOT NULL, hold_digest TEXT NOT NULL DEFAULT '', state TEXT NOT NULL CHECK(state IN ('MARKED','CONFIRMED','DELETING','BLOCKED','SWEPT')), created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(epoch_id, blob_id), FOREIGN KEY(epoch_id) REFERENCES epochs(epoch_id) ON DELETE CASCADE)",
    "CREATE TABLE IF NOT EXISTS receipts (receipt_id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE, operation TEXT NOT NULL, request_digest TEXT NOT NULL, job_id TEXT NOT NULL, result_digest TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE)",
    "CREATE TABLE IF NOT EXISTS ledger (ledger_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, job_id TEXT NOT NULL, epoch_id TEXT, candidate_id TEXT, detail_digest TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE, FOREIGN KEY(epoch_id) REFERENCES epochs(epoch_id) ON DELETE SET NULL, FOREIGN KEY(candidate_id) REFERENCES candidates(candidate_id) ON DELETE SET NULL)",
    "CREATE TABLE IF NOT EXISTS leases (lease_id TEXT PRIMARY KEY, scope_digest TEXT NOT NULL, owner_id TEXT NOT NULL, expires_at TEXT NOT NULL, acquired_at TEXT NOT NULL, released_at TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL CHECK(active IN (0,1)), UNIQUE(scope_digest, owner_id, acquired_at))",
    "CREATE TABLE IF NOT EXISTS reports (report_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, status TEXT NOT NULL, counts_json TEXT NOT NULL, safety_json TEXT NOT NULL, report_digest TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(job_id, report_digest), FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE)",
    "CREATE INDEX IF NOT EXISTS idx_maintenance_leases_active ON leases(scope_digest, active, expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_maintenance_candidates_epoch ON candidates(epoch_id, state)",
)


def _normalized_schema_sql(value: str) -> str:
    return " ".join(value.replace(" IF NOT EXISTS", "").split())


_EXPECTED_SCHEMA_SQL = {
    statement.split(" ", 6)[5 if statement.startswith("CREATE TABLE") else 5]: _normalized_schema_sql(statement)
    for statement in _SCHEMA_DDL
}


class MaintenanceStore:
    """A fail-closed, immutable-ledger oriented SQLite maintenance store."""

    SCHEMA_VERSION = SCHEMA_VERSION
    SCHEMA_MARKER = SCHEMA_MARKER
    DB_NAME = DB_NAME

    def __init__(
        self,
        workspace_or_layout: str | Path | WorkspaceV2Layout,
        *,
        source_workspace: str | Path | None = None,
        path: str | Path | None = None,
        readonly: bool = False,
        read_only: bool | None = None,
    ) -> None:
        if read_only is not None:
            if type(read_only) is not bool:
                raise ValueError("read_only must be bool")
            readonly = read_only
        if type(readonly) is not bool:
            raise ValueError("readonly must be bool")
        if isinstance(workspace_or_layout, WorkspaceV2Layout):
            if source_workspace is None:
                raise LayoutError(
                    "source_workspace is required when a WorkspaceV2Layout is supplied"
                )
            raw_workspace = Path(source_workspace)
        else:
            if source_workspace is not None:
                raise LayoutError(
                    "source_workspace is only valid with a WorkspaceV2Layout"
                )
            raw_workspace = Path(workspace_or_layout)
        raw_workspace = Path(raw_workspace).expanduser()
        # Check the directory entry before any existence/resolve operation so
        # a dangling symlink cannot masquerade as a missing workspace.
        try:
            if raw_workspace.is_symlink() or (raw_workspace.exists() and WorkspaceV2Layout._is_reparse_or_symlink(raw_workspace)):
                raise LayoutError("maintenance workspace cannot be a symlink or reparse point")
        except OSError as exc:
            raise LayoutError("cannot inspect maintenance workspace") from exc
        if isinstance(workspace_or_layout, WorkspaceV2Layout):
            checked_layout = WorkspaceV2Layout(raw_workspace)
            if checked_layout.workspace != workspace_or_layout.workspace:
                raise LayoutError("source_workspace does not match the supplied layout")
            self.layout = workspace_or_layout
        else:
            self.layout = WorkspaceV2Layout(raw_workspace)
        expected = self.layout.system / DB_NAME
        self.db_path = expected
        self.path = expected
        if path is not None:
            candidate = Path(path).expanduser()
            if os.path.normcase(os.path.abspath(os.fspath(candidate))) != os.path.normcase(os.path.abspath(os.fspath(expected))):
                raise LayoutError(f"maintenance path must be exactly {expected}")
        self.readonly = readonly
        self.workspace = self.layout.workspace
        if self.readonly:
            # Missing read-only storage is an ordinary absence, not a reason
            # to create ``.memoryguard`` or to report an unsafe parent.
            if not self.db_path.is_file():
                raise FileNotFoundError(self.db_path)
            self._assert_path(allow_missing=False)
            self._check_schema(readonly=True)
        else:
            self._prepare_write()
            self._preflight_existing()
            self._init_schema()

    @property
    def schema_version(self) -> int:
        return self.SCHEMA_VERSION

    @property
    def schema_marker(self) -> str:
        return self.SCHEMA_MARKER

    @property
    def read_only(self) -> bool:
        return self.readonly

    # ------------------------------------------------------------------ paths
    def _assert_path(self, *, allow_missing: bool = True) -> None:
        try:
            self.layout.assert_contained(self.db_path)
            self.layout._assert_safe_component(self.layout.root, allow_missing=allow_missing)
            self.layout._assert_safe_component(self.layout.system, allow_missing=allow_missing)
            self.layout._assert_safe_component(self.db_path, allow_missing=allow_missing)
        except (LayoutError, OSError) as exc:
            raise LayoutError(f"unsafe maintenance path: {self.db_path}") from exc

    def _prepare_write(self) -> None:
        # Do not call connect_database before these checks: its parent mkdir is
        # intentionally generic and must not be allowed to follow a reparse.
        self.layout._assert_safe_component(self.layout.root, allow_missing=True)
        self.layout.root.mkdir(parents=True, exist_ok=True)
        self.layout._assert_safe_component(self.layout.root, allow_missing=False)
        self.layout._assert_safe_component(self.layout.system, allow_missing=True)
        self.layout.system.mkdir(parents=True, exist_ok=True)
        self.layout._assert_safe_component(self.layout.system, allow_missing=False)
        self._assert_path(allow_missing=True)

    def _preflight_existing(self) -> None:
        if not self.db_path.exists():
            return
        self._assert_path(allow_missing=False)
        try:
            with self._connection(readonly=True) as conn:
                self._check_schema_connection(conn, allow_fresh=True)
        except MaintenanceSchemaError:
            raise
        except (sqlite3.Error, OSError) as exc:
            raise MaintenanceSchemaError("cannot inspect maintenance database") from exc

    # --------------------------------------------------------------- schema/db
    @contextmanager
    def _connection(self, *, readonly: bool | None = None) -> Iterator[sqlite3.Connection]:
        ro = self.readonly if readonly is None else readonly
        if type(ro) is not bool:
            raise ValueError("readonly must be bool")
        self._assert_path(allow_missing=not ro)
        try:
            conn = connect_database(self.db_path, readonly=ro)
        except (sqlite3.Error, OSError) as exc:
            raise MaintenanceSchemaError("cannot open maintenance database") from exc
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _tables(conn: sqlite3.Connection) -> set[str]:
        return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    @classmethod
    def _check_schema_connection(cls, conn: sqlite3.Connection, *, allow_fresh: bool = False) -> bool:
        try:
            tables = cls._tables(conn)
        except sqlite3.Error as exc:
            raise MaintenanceSchemaError("cannot inspect maintenance schema") from exc
        if "schema_meta" not in tables:
            try:
                user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            except (sqlite3.Error, TypeError, ValueError, IndexError) as exc:
                raise MaintenanceSchemaError("malformed maintenance user_version") from exc
            if tables or user_version:
                raise MaintenanceSchemaError("maintenance schema metadata is missing")
            if not allow_fresh:
                raise MaintenanceSchemaError("fresh maintenance database is not allowed in read-only mode")
            return True
        try:
            rows = conn.execute("SELECT domain, version, marker FROM schema_meta").fetchall()
        except sqlite3.Error as exc:
            raise MaintenanceSchemaError("cannot read maintenance schema metadata") from exc
        if len(rows) != 1:
            raise MaintenanceSchemaError("maintenance schema metadata must contain one row")
        try:
            domain, version, marker = str(rows[0][0]), int(rows[0][1]), str(rows[0][2])
        except (TypeError, ValueError, IndexError) as exc:
            raise MaintenanceSchemaError("malformed maintenance schema metadata") from exc
        if domain != SCHEMA_DOMAIN:
            raise MaintenanceSchemaError(f"maintenance schema domain mismatch: {domain!r}")
        if marker != SCHEMA_MARKER:
            raise MaintenanceSchemaError(f"maintenance schema marker mismatch: {marker!r}")
        if version != SCHEMA_VERSION:
            direction = "future" if version > SCHEMA_VERSION else "unsupported"
            raise MaintenanceSchemaError(f"{direction} maintenance schema version: {version}")
        try:
            user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        except (sqlite3.Error, TypeError, ValueError, IndexError) as exc:
            raise MaintenanceSchemaError("malformed maintenance user_version") from exc
        if user_version != SCHEMA_VERSION:
            direction = "future" if user_version > SCHEMA_VERSION else "unsupported"
            raise MaintenanceSchemaError(f"{direction} maintenance user_version: {user_version}")
        unknown_tables = tables - _REQUIRED_SCHEMA_TABLES
        missing_tables = _REQUIRED_SCHEMA_TABLES - tables
        if missing_tables:
            raise MaintenanceSchemaError(f"maintenance schema missing table(s): {', '.join(sorted(missing_tables))}")
        if unknown_tables:
            raise MaintenanceSchemaError(f"maintenance schema has unknown table(s): {', '.join(sorted(unknown_tables))}")
        for table, expected_columns in _REQUIRED_SCHEMA_COLUMNS.items():
            try:
                actual_columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            except sqlite3.Error as exc:
                raise MaintenanceSchemaError(f"cannot inspect maintenance table: {table}") from exc
            if actual_columns != expected_columns:
                missing = expected_columns - actual_columns
                unknown = actual_columns - expected_columns
                details = []
                if missing:
                    details.append(f"missing={','.join(sorted(missing))}")
                if unknown:
                    details.append(f"unknown={','.join(sorted(unknown))}")
                raise MaintenanceSchemaError(f"maintenance schema columns mismatch for {table}: {';'.join(details)}")
        for name, expected_sql in _EXPECTED_SCHEMA_SQL.items():
            try:
                row = conn.execute("SELECT sql FROM sqlite_master WHERE name=? AND type IN ('table','index')", (name,)).fetchone()
            except sqlite3.Error as exc:
                raise MaintenanceSchemaError(f"cannot inspect maintenance schema object: {name}") from exc
            if row is None or not isinstance(row[0], str) or _normalized_schema_sql(str(row[0])) != expected_sql:
                raise MaintenanceSchemaError(f"maintenance schema definition mismatch: {name}")
        return False

    @classmethod
    def _create_schema(cls, conn: sqlite3.Connection) -> None:
        for statement in _SCHEMA_DDL:
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        now = _now()
        conn.execute("INSERT INTO schema_meta(domain, version, marker, updated_at) VALUES(?,?,?,?)", (SCHEMA_DOMAIN, SCHEMA_VERSION, SCHEMA_MARKER, now))

    def _init_schema(self) -> None:
        conn = connect_database(self.db_path, readonly=False)
        try:
            with transaction(conn):
                fresh = self._check_schema_connection(conn, allow_fresh=True)
                if fresh:
                    self._create_schema(conn)
        except sqlite3.Error as exc:
            raise MaintenanceSchemaError("cannot initialize maintenance schema") from exc
        finally:
            conn.close()

    def _check_schema(self, *, readonly: bool = False) -> None:
        with self._connection(readonly=readonly) as conn:
            self._check_schema_connection(conn, allow_fresh=False)

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        if self.readonly:
            raise PermissionError("maintenance store is read-only")
        self._check_schema(readonly=True)
        with self._connection(readonly=False) as conn:
            with transaction(conn):
                yield conn

    # -------------------------------------------------------------- validation
    def _context(self, context: MaintenanceContext | Mapping[str, Any] | None, *, required_lease: bool = False) -> MaintenanceContext:
        if context is None or not isinstance(context, MaintenanceContext):
            raise MaintenanceAuthorizationError("trusted MaintenanceContext is required")
        resolved = context
        if resolved.scope.workspace_id != str(self.workspace):
            raise MaintenanceAuthorizationError("maintenance scope workspace mismatch")
        if not resolved.trusted_context or not resolved.scope.trusted_context:
            raise MaintenanceAuthorizationError("untrusted maintenance context")
        if required_lease and not resolved.maintenance_lease_id:
            raise MaintenanceLeaseError("maintenance lease is required")
        return resolved

    @staticmethod
    def _stored_context(row: sqlite3.Row) -> dict[str, Any]:
        try:
            payload = json.loads(str(row["scope_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MaintenanceSchemaError("malformed stored maintenance context JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("scope"), dict) or not isinstance(payload.get("actor_id"), str):
            raise MaintenanceSchemaError("stored maintenance context JSON is incomplete")
        return payload

    def _authorize_job(self, row: sqlite3.Row, context: MaintenanceContext) -> None:
        """Require exact scope, actor, and immutable context digest ownership."""

        payload = self._stored_context(row)
        if payload.get("scope") != context.scope.to_dict() or payload.get("actor_id") != context.actor_id:
            raise MaintenanceAuthorizationError("maintenance job owner/scope mismatch")
        if str(row["context_digest"]) != context.digest:
            raise MaintenanceAuthorizationError("maintenance context digest mismatch")

    def _manifest(self) -> RuntimeSnapshot:
        try:
            record = ManifestManager(self.layout).current()
            return RuntimeSnapshot.from_value({"state": record.state.value, "generation": record.generation})
        except (ManifestError, OSError, ValueError) as exc:
            raise MaintenanceConflictError("manifest snapshot is unavailable") from exc

    def _require_active_pin(self, context: MaintenanceContext, *, lease_id: str | None = None, expected_generation: int | None = None) -> None:
        snapshot = self._manifest()
        if snapshot.state.value != ManifestState.V2_ACTIVE.value:
            raise MaintenanceConflictError("sweep/compact requires V2_ACTIVE manifest")
        expected = expected_generation if expected_generation is not None else context.expected_generation
        if type(expected) is not int or expected != snapshot.generation:
            raise MaintenanceConflictError("maintenance expected_generation conflict")
        if lease_id is not None and context.maintenance_lease_id != lease_id:
            raise MaintenanceLeaseError("context lease mismatch")
        self._verify_lease(context, context.maintenance_lease_id)

    def verify_active_lease(self, context: MaintenanceContext | Mapping[str, Any], *, expected_generation: int | None = None) -> MaintenanceLease:
        """Public fail-closed boundary used by maintenance executors."""

        ctx = self._context(context, required_lease=True)
        self._require_active_pin(ctx, lease_id=ctx.maintenance_lease_id, expected_generation=expected_generation)
        return self._verify_lease(ctx, ctx.maintenance_lease_id)

    # ------------------------------------------------------------------ jobs
    def create_job(
        self,
        context: MaintenanceContext | Mapping[str, Any],
        operation: MaintenanceOperation | str,
        request_key: str | None = None,
        *,
        idempotency_key: str | None = None,
        dry_run: bool = True,
        expected_generation: int | None = None,
    ) -> MaintenanceJob:
        ctx = self._context(context)
        if type(dry_run) is not bool:
            raise ValueError("dry_run must be bool")
        try:
            op = operation if isinstance(operation, MaintenanceOperation) else MaintenanceOperation(str(operation).casefold())
        except ValueError as exc:
            raise ValueError(f"unknown maintenance operation: {operation!r}") from exc
        if op in {MaintenanceOperation.SWEEP, MaintenanceOperation.COMPACT}:
            # BUILDING/READY are report/audit-only manifest states.  A dry-run
            # sweep is still a sweep request and must be scheduled only after
            # activation; callers can use the report operation for planning.
            snapshot = self._manifest()
            if snapshot.state.value != ManifestState.V2_ACTIVE.value:
                raise MaintenanceConflictError("sweep/compact jobs require V2_ACTIVE manifest")
        if request_key is None:
            request_key = idempotency_key
        elif idempotency_key is not None and request_key != idempotency_key:
            raise MaintenanceConflictError("request_key and idempotency_key disagree")
        request_key = _required_text(request_key, "request_key")
        if expected_generation is None:
            expected_generation = ctx.expected_generation
        if expected_generation is not None and (type(expected_generation) is not int or expected_generation < 0):
            raise ValueError("expected_generation must be non-negative int")
        request_digest = stable_digest({"request_key": request_key, "operation": op.value, "dry_run": dry_run, "expected_generation": expected_generation, "context_digest": ctx.digest})
        now = _now()
        with self._write() as conn:
            prior = conn.execute("SELECT * FROM jobs WHERE request_key=?", (request_key,)).fetchone()
            if prior is not None:
                if str(prior["idempotency_digest"]) != request_digest:
                    raise MaintenanceConflictError("idempotency key replay has different request data")
                return _job_from_row(prior)
            job_id = stable_id("maint-job", request_key, request_digest)
            # Keep the exact immutable actor/context tuple alongside the scope;
            # every later mutation must prove this owner binding.
            conn.execute("INSERT INTO jobs(job_id,request_key,operation,state,scope_json,context_digest,expected_generation,dry_run,idempotency_digest,error_code,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (job_id, request_key, op.value, MaintenanceJobState.PLANNED.value, _json({"scope": ctx.scope.to_dict(), "actor_id": ctx.actor_id, "maintenance_lease_id": ctx.maintenance_lease_id, "expected_generation": ctx.expected_generation, "trusted_context": ctx.trusted_context}), ctx.digest, expected_generation, int(dry_run), request_digest, "", now, now))
            conn.execute("INSERT INTO receipts(receipt_id,request_key,operation,request_digest,job_id,result_digest,status,created_at) VALUES(?,?,?,?,?,?,?,?)", (stable_id("maint-receipt", request_key), request_key, op.value, request_digest, job_id, stable_digest({"job_id": job_id}), "created", now))
            _ledger(conn, "JOB_CREATED", job_id, detail={"request_digest": request_digest})
            return _job_from_row(conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())

    def get_job(self, job_id: str) -> MaintenanceJob | None:
        job_id = _required_text(job_id, "job_id")
        with self._connection(readonly=True) as conn:
            self._check_schema_connection(conn)
            row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            return None if row is None else _job_from_row(row)

    def transition_job(
        self,
        job_id: str,
        target: MaintenanceJobState | str,
        context: MaintenanceContext | Mapping[str, Any],
        *,
        expected_generation: int | None = None,
        lease_id: str | None = None,
    ) -> MaintenanceJob:
        ctx = self._context(context)
        try:
            next_state = target if isinstance(target, MaintenanceJobState) else MaintenanceJobState(str(target).upper())
        except ValueError as exc:
            raise ValueError(f"unknown maintenance job state: {target!r}") from exc
        job_id = _required_text(job_id, "job_id")
        now = _now()
        allowed = {
            MaintenanceJobState.PLANNED: {MaintenanceJobState.AUDITING, MaintenanceJobState.READY, MaintenanceJobState.CANCELLED},
            MaintenanceJobState.AUDITING: {MaintenanceJobState.READY, MaintenanceJobState.FAILED},
            MaintenanceJobState.READY: {MaintenanceJobState.ACTIVE, MaintenanceJobState.SUCCEEDED, MaintenanceJobState.FAILED, MaintenanceJobState.CANCELLED},
            MaintenanceJobState.ACTIVE: {MaintenanceJobState.SUCCEEDED, MaintenanceJobState.FAILED},
            MaintenanceJobState.SUCCEEDED: set(), MaintenanceJobState.FAILED: set(), MaintenanceJobState.CANCELLED: set(),
        }
        with self._write() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            self._authorize_job(row, ctx)
            job = _job_from_row(row)
            if next_state is job.state:
                return job
            if next_state not in allowed[job.state]:
                raise MaintenanceConflictError(f"invalid maintenance state transition {job.state.value}->{next_state.value}")
            if next_state is MaintenanceJobState.ACTIVE:
                if job.operation not in {MaintenanceOperation.SWEEP, MaintenanceOperation.COMPACT}:
                    raise MaintenanceConflictError("only sweep/compact jobs may become ACTIVE")
                if job.dry_run:
                    raise MaintenanceConflictError("ACTIVE sweep/compact requires dry_run=False")
                self._require_active_pin(ctx, lease_id=lease_id, expected_generation=expected_generation if expected_generation is not None else job.expected_generation)
            updated = conn.execute("UPDATE jobs SET state=?,updated_at=? WHERE job_id=? AND state=?", (next_state.value, now, job_id, job.state.value))
            if updated.rowcount != 1:
                raise MaintenanceConflictError("maintenance job CAS conflict")
            _ledger(conn, "JOB_STATE", job_id, detail={"before": job.state.value, "after": next_state.value})
            return _job_from_row(conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())

    # --------------------------------------------------------------- epochs/candidates
    def begin_epoch(self, job_id: str, context: MaintenanceContext | Mapping[str, Any], *, epoch_number: int | None = None, reference_digest: str = "") -> MaintenanceEpoch:
        ctx = self._context(context)
        job_id = _required_text(job_id, "job_id")
        reference_digest = _text(reference_digest, "reference_digest")
        with self._write() as conn:
            job_row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if job_row is None:
                raise KeyError(job_id)
            self._authorize_job(job_row, ctx)
            job = _job_from_row(job_row)
            if job.state not in {MaintenanceJobState.AUDITING, MaintenanceJobState.READY}:
                raise MaintenanceConflictError("epoch requires an auditing/ready job")
            if epoch_number is None:
                row = conn.execute("SELECT COALESCE(MAX(epoch_number),0)+1 FROM epochs WHERE job_id=?", (job_id,)).fetchone()
                epoch_number = int(row[0])
            if type(epoch_number) is not int or epoch_number < 1:
                raise ValueError("epoch_number must be positive int")
            prior = conn.execute("SELECT * FROM epochs WHERE job_id=? AND epoch_number=?", (job_id, epoch_number)).fetchone()
            if prior is not None:
                current = _epoch_from_row(prior)
                if current.reference_digest != reference_digest:
                    raise MaintenanceConflictError("epoch replay has different reference digest")
                if current.state is not EpochState.OPEN:
                    return current
                return _epoch_from_row(prior)
            if epoch_number > 1:
                previous = conn.execute("SELECT state,complete FROM epochs WHERE job_id=? AND epoch_number=?", (job_id, epoch_number - 1)).fetchone()
                if previous is None or str(previous[0]) != EpochState.COMPLETE.value or int(previous[1]) != 1:
                    raise MaintenanceConflictError("previous reference audit epoch is not complete")
            now = _now()
            epoch_id = stable_id("maint-epoch", job_id, epoch_number)
            conn.execute("INSERT INTO epochs(epoch_id,job_id,epoch_number,state,reference_digest,complete,created_at) VALUES(?,?,?,?,?,?,?)", (epoch_id, job_id, epoch_number, EpochState.OPEN.value, reference_digest, 0, now))
            _ledger(conn, "EPOCH_OPEN", job_id, epoch_id=epoch_id, detail={"epoch_number": epoch_number})
            return _epoch_from_row(conn.execute("SELECT * FROM epochs WHERE epoch_id=?", (epoch_id,)).fetchone())

    def complete_epoch(self, epoch_id: str, context: MaintenanceContext | Mapping[str, Any], *, reference_digest: str | None = None, failed: bool = False) -> MaintenanceEpoch:
        ctx = self._context(context)
        epoch_id = _required_text(epoch_id, "epoch_id")
        if type(failed) is not bool:
            raise ValueError("failed must be bool")
        with self._write() as conn:
            row = conn.execute("SELECT * FROM epochs WHERE epoch_id=?", (epoch_id,)).fetchone()
            if row is None:
                raise KeyError(epoch_id)
            job_row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (str(row["job_id"]),)).fetchone()
            if job_row is None:
                raise MaintenanceSchemaError("epoch references missing maintenance job")
            self._authorize_job(job_row, ctx)
            current = _epoch_from_row(row)
            target = EpochState.FAILED if failed else EpochState.COMPLETE
            if current.state is target:
                return current
            if current.state is not EpochState.OPEN:
                raise MaintenanceConflictError("epoch is already terminal")
            digest = current.reference_digest if reference_digest is None else _text(reference_digest, "reference_digest")
            changed = conn.execute("UPDATE epochs SET state=?,complete=?,reference_digest=? WHERE epoch_id=? AND state=?", (target.value, int(not failed), digest, epoch_id, EpochState.OPEN.value))
            if changed.rowcount != 1:
                raise MaintenanceConflictError("epoch CAS conflict")
            _ledger(conn, "EPOCH_COMPLETE" if not failed else "EPOCH_FAILED", current.job_id, epoch_id=epoch_id, detail={"reference_digest": digest})
            return _epoch_from_row(conn.execute("SELECT * FROM epochs WHERE epoch_id=?", (epoch_id,)).fetchone())

    def get_epoch(self, epoch_id: str) -> MaintenanceEpoch | None:
        epoch_id = _required_text(epoch_id, "epoch_id")
        with self._connection(readonly=True) as conn:
            self._check_schema_connection(conn)
            row = conn.execute("SELECT * FROM epochs WHERE epoch_id=?", (epoch_id,)).fetchone()
            return None if row is None else _epoch_from_row(row)

    def mark_candidate(self, epoch_id: str, blob_id: str, context: MaintenanceContext | Mapping[str, Any], *, reference_digest: str, hold_digest: str = "", state: CandidateState | str = CandidateState.MARKED) -> MaintenanceCandidate:
        ctx = self._context(context)
        epoch_id, blob_id = _required_text(epoch_id, "epoch_id"), _required_text(blob_id, "blob_id")
        reference_digest, hold_digest = _required_text(reference_digest, "reference_digest"), _text(hold_digest, "hold_digest")
        candidate_state = state if isinstance(state, CandidateState) else CandidateState(str(state).upper())
        if candidate_state not in {CandidateState.MARKED, CandidateState.CONFIRMED, CandidateState.BLOCKED}:
            raise MaintenanceConflictError("candidate cannot be inserted as swept")
        with self._write() as conn:
            epoch = conn.execute("SELECT * FROM epochs WHERE epoch_id=?", (epoch_id,)).fetchone()
            if epoch is None:
                raise KeyError(epoch_id)
            job_row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (str(epoch["job_id"]),)).fetchone()
            if job_row is None:
                raise MaintenanceSchemaError("epoch references missing maintenance job")
            self._authorize_job(job_row, ctx)
            if str(epoch["state"]) != EpochState.OPEN.value or int(epoch["complete"]):
                raise MaintenanceConflictError("candidates may only be marked in an OPEN epoch")
            candidate_id = stable_id("maint-candidate", epoch_id, blob_id)
            prior = conn.execute("SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
            if prior is not None:
                current = _candidate_from_row(prior)
                if current.reference_digest != reference_digest or current.hold_digest != hold_digest:
                    raise MaintenanceConflictError("candidate replay has different reference/hold digest")
                return current
            now = _now()
            conn.execute("INSERT INTO candidates(candidate_id,epoch_id,blob_id,reference_digest,hold_digest,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (candidate_id, epoch_id, blob_id, reference_digest, hold_digest, candidate_state.value, now, now))
            _ledger(conn, "CANDIDATE_MARKED", str(epoch["job_id"]), epoch_id=epoch_id, candidate_id=candidate_id, detail={"reference_digest": reference_digest, "hold_digest": hold_digest})
            return _candidate_from_row(conn.execute("SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)).fetchone())

    def confirm_candidate(self, candidate_id: str, context: MaintenanceContext | Mapping[str, Any], *, reference_digest: str, hold_digest: str = "") -> MaintenanceCandidate:
        ctx = self._context(context)
        candidate_id = _required_text(candidate_id, "candidate_id")
        reference_digest, hold_digest = _required_text(reference_digest, "reference_digest"), _text(hold_digest, "hold_digest")
        with self._write() as conn:
            row = conn.execute("SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
            if row is None:
                raise KeyError(candidate_id)
            epoch = conn.execute("SELECT * FROM epochs WHERE epoch_id=?", (str(row["epoch_id"]),)).fetchone()
            if epoch is None:
                raise MaintenanceSchemaError("candidate references missing epoch")
            job_row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (str(epoch["job_id"]),)).fetchone()
            if job_row is None:
                raise MaintenanceSchemaError("epoch references missing maintenance job")
            self._authorize_job(job_row, ctx)
            if str(epoch["state"]) != EpochState.OPEN.value or int(epoch["complete"]):
                raise MaintenanceConflictError("candidates may only be confirmed in an OPEN epoch")
            current = _candidate_from_row(row)
            if current.state is CandidateState.CONFIRMED:
                if current.reference_digest == reference_digest and current.hold_digest == hold_digest:
                    return current
                raise MaintenanceConflictError("confirmed candidate digest conflict")
            if current.state is not CandidateState.MARKED:
                raise MaintenanceConflictError("only marked candidates can be confirmed")
            changed = conn.execute("UPDATE candidates SET state=?,reference_digest=?,hold_digest=?,updated_at=? WHERE candidate_id=? AND state=? AND reference_digest=?", (CandidateState.CONFIRMED.value, reference_digest, hold_digest, _now(), candidate_id, CandidateState.MARKED.value, current.reference_digest))
            if changed.rowcount != 1:
                raise MaintenanceConflictError("candidate CAS conflict")
            job_id = str(conn.execute("SELECT j.job_id FROM jobs j JOIN epochs e ON e.job_id=j.job_id JOIN candidates c ON c.epoch_id=e.epoch_id WHERE c.candidate_id=?", (candidate_id,)).fetchone()[0])
            _ledger(conn, "CANDIDATE_CONFIRMED", job_id, candidate_id=candidate_id, detail={"reference_digest": reference_digest, "hold_digest": hold_digest})
            return _candidate_from_row(conn.execute("SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)).fetchone())

    def mark_candidate_swept(
        self,
        candidate_id: str,
        context: MaintenanceContext | Mapping[str, Any],
        *,
        expected_generation: int | None = None,
        deletion_digest: str,
    ) -> MaintenanceCandidate:
        """Record a completed physical deletion after an executor's final CAS."""

        ctx = self._context(context, required_lease=True)
        candidate_id = _required_text(candidate_id, "candidate_id")
        deletion_digest = _required_text(deletion_digest, "deletion_digest")
        self._require_active_pin(ctx, lease_id=ctx.maintenance_lease_id, expected_generation=expected_generation)
        with self._write() as conn:
            row = conn.execute("SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
            if row is None:
                raise KeyError(candidate_id)
            epoch = conn.execute("SELECT * FROM epochs WHERE epoch_id=?", (str(row["epoch_id"]),)).fetchone()
            if epoch is None:
                raise MaintenanceSchemaError("candidate references missing epoch")
            job_row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (str(epoch["job_id"]),)).fetchone()
            if job_row is None:
                raise MaintenanceSchemaError("candidate references missing maintenance job")
            self._authorize_job(job_row, ctx)
            job = _job_from_row(job_row)
            current = _candidate_from_row(row)
            if job.operation is not MaintenanceOperation.SWEEP or job.state is not MaintenanceJobState.ACTIVE or job.dry_run:
                raise MaintenanceConflictError("candidate sweep requires an ACTIVE non-dry-run sweep job")
            if str(epoch["state"]) != EpochState.COMPLETE.value or not int(epoch["complete"]):
                raise MaintenanceConflictError("candidate sweep requires a complete reference epoch")
            if current.state is CandidateState.SWEPT:
                return current
            if current.state is not CandidateState.DELETING:
                raise MaintenanceConflictError("only deleting candidates can be swept")
            changed = conn.execute("UPDATE candidates SET state=?,updated_at=? WHERE candidate_id=? AND state=?", (CandidateState.SWEPT.value, _now(), candidate_id, CandidateState.DELETING.value))
            if changed.rowcount != 1:
                raise MaintenanceConflictError("candidate sweep CAS conflict")
            _ledger(conn, "CANDIDATE_SWEPT", job.job_id, epoch_id=str(epoch["epoch_id"]), candidate_id=candidate_id, detail={"deletion_digest": deletion_digest})
            return _candidate_from_row(conn.execute("SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)).fetchone())

    mark_swept = mark_candidate_swept

    def begin_candidate_sweep(
        self,
        candidate_id: str,
        context: MaintenanceContext | Mapping[str, Any],
        *,
        expected_generation: int | None = None,
        deletion_digest: str,
    ) -> MaintenanceCandidate:
        """Persist a deletion intent before touching the authoritative Blob row."""

        ctx = self._context(context, required_lease=True)
        candidate_id = _required_text(candidate_id, "candidate_id")
        deletion_digest = _required_text(deletion_digest, "deletion_digest")
        self._require_active_pin(ctx, lease_id=ctx.maintenance_lease_id, expected_generation=expected_generation)
        with self._write() as conn:
            row = conn.execute("SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
            if row is None:
                raise KeyError(candidate_id)
            epoch = conn.execute("SELECT * FROM epochs WHERE epoch_id=?", (str(row["epoch_id"]),)).fetchone()
            if epoch is None:
                raise MaintenanceSchemaError("candidate references missing epoch")
            job_row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (str(epoch["job_id"]),)).fetchone()
            if job_row is None:
                raise MaintenanceSchemaError("candidate references missing maintenance job")
            self._authorize_job(job_row, ctx)
            job = _job_from_row(job_row)
            current = _candidate_from_row(row)
            if job.operation is not MaintenanceOperation.SWEEP or job.state is not MaintenanceJobState.ACTIVE or job.dry_run:
                raise MaintenanceConflictError("candidate sweep requires an ACTIVE non-dry-run sweep job")
            if str(epoch["state"]) != EpochState.COMPLETE.value or not int(epoch["complete"]):
                raise MaintenanceConflictError("candidate sweep requires a complete reference epoch")
            if current.state is CandidateState.DELETING:
                return current
            if current.state is not CandidateState.CONFIRMED:
                raise MaintenanceConflictError("only confirmed candidates can begin deletion")
            changed = conn.execute(
                "UPDATE candidates SET state=?,updated_at=? WHERE candidate_id=? AND state=?",
                (CandidateState.DELETING.value, _now(), candidate_id, CandidateState.CONFIRMED.value),
            )
            if changed.rowcount != 1:
                raise MaintenanceConflictError("candidate deletion intent CAS conflict")
            _ledger(conn, "CANDIDATE_SWEEP_INTENT", job.job_id, epoch_id=str(epoch["epoch_id"]), candidate_id=candidate_id, detail={"deletion_digest": deletion_digest})
            return _candidate_from_row(conn.execute("SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)).fetchone())

    def restore_candidate_confirmed(
        self,
        candidate_id: str,
        context: MaintenanceContext | Mapping[str, Any],
        *,
        expected_generation: int | None = None,
        rollback_digest: str,
    ) -> MaintenanceCandidate:
        """Compensate an uncommitted content deletion without widening scope."""

        ctx = self._context(context, required_lease=True)
        candidate_id = _required_text(candidate_id, "candidate_id")
        rollback_digest = _required_text(rollback_digest, "rollback_digest")
        self._require_active_pin(ctx, lease_id=ctx.maintenance_lease_id, expected_generation=expected_generation)
        with self._write() as conn:
            row = conn.execute("SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
            if row is None:
                raise KeyError(candidate_id)
            epoch = conn.execute("SELECT * FROM epochs WHERE epoch_id=?", (str(row["epoch_id"]),)).fetchone()
            if epoch is None:
                raise MaintenanceSchemaError("candidate references missing epoch")
            job_row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (str(epoch["job_id"]),)).fetchone()
            if job_row is None:
                raise MaintenanceSchemaError("candidate references missing maintenance job")
            self._authorize_job(job_row, ctx)
            job = _job_from_row(job_row)
            current = _candidate_from_row(row)
            if job.operation is not MaintenanceOperation.SWEEP or job.state is not MaintenanceJobState.ACTIVE:
                raise MaintenanceConflictError("candidate compensation requires an ACTIVE sweep job")
            if current.state is CandidateState.CONFIRMED:
                return current
            if current.state is not CandidateState.DELETING:
                raise MaintenanceConflictError("only deleting candidates can be restored")
            changed = conn.execute(
                "UPDATE candidates SET state=?,updated_at=? WHERE candidate_id=? AND state=?",
                (CandidateState.CONFIRMED.value, _now(), candidate_id, CandidateState.DELETING.value),
            )
            if changed.rowcount != 1:
                raise MaintenanceConflictError("candidate compensation CAS conflict")
            _ledger(conn, "CANDIDATE_SWEEP_ROLLED_BACK", job.job_id, epoch_id=str(epoch["epoch_id"]), candidate_id=candidate_id, detail={"rollback_digest": rollback_digest})
            return _candidate_from_row(conn.execute("SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)).fetchone())

    def list_job_candidates(
        self,
        job_id: str,
        context: MaintenanceContext | Mapping[str, Any],
        *,
        epoch_number: int | None = None,
    ) -> tuple[MaintenanceCandidate, ...]:
        """Return only candidates owned by the caller's exact maintenance job."""

        ctx = self._context(context)
        job_id = _required_text(job_id, "job_id")
        if epoch_number is not None and (type(epoch_number) is not int or epoch_number < 1):
            raise ValueError("epoch_number must be an int >= 1")
        with self._connection(readonly=True) as conn:
            self._check_schema_connection(conn)
            job_row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if job_row is None:
                raise KeyError(job_id)
            self._authorize_job(job_row, ctx)
            sql = "SELECT c.* FROM candidates c JOIN epochs e ON e.epoch_id=c.epoch_id WHERE e.job_id=?"
            params: tuple[Any, ...] = (job_id,)
            if epoch_number is not None:
                sql += " AND e.epoch_number=?"
                params = (job_id, epoch_number)
            rows = conn.execute(sql + " ORDER BY c.candidate_id", params).fetchall()
            return tuple(_candidate_from_row(row) for row in rows)

    def get_candidate(self, candidate_id: str) -> MaintenanceCandidate | None:
        candidate_id = _required_text(candidate_id, "candidate_id")
        with self._connection(readonly=True) as conn:
            self._check_schema_connection(conn)
            row = conn.execute("SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
            return None if row is None else _candidate_from_row(row)

    def list_candidates(self, epoch_id: str, *, readonly: bool = True) -> tuple[MaintenanceCandidate, ...]:
        epoch_id = _required_text(epoch_id, "epoch_id")
        with self._connection(readonly=True) as conn:
            self._check_schema_connection(conn)
            rows = conn.execute("SELECT * FROM candidates WHERE epoch_id=? ORDER BY candidate_id", (epoch_id,)).fetchall()
            return tuple(_candidate_from_row(row) for row in rows)

    # ------------------------------------------------------------- lease/report
    def acquire_lease(self, context: MaintenanceContext | Mapping[str, Any], *, owner_id: str | None = None, ttl_seconds: int = 60, now: datetime | None = None) -> MaintenanceLease:
        ctx = self._context(context)
        owner = _required_text(owner_id or ctx.actor_id, "owner_id")
        if owner != ctx.actor_id:
            raise MaintenanceLeaseError("lease owner must equal context actor")
        if type(ttl_seconds) is not int or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive int")
        when = _utc(now)
        expires = when + timedelta(seconds=ttl_seconds)
        now_text, expires_text = when.isoformat(), expires.isoformat()
        with self._write() as conn:
            rows = conn.execute("SELECT * FROM leases WHERE scope_digest=? AND active=1 ORDER BY acquired_at", (ctx.scope.digest,)).fetchall()
            for row in rows:
                if _parse_time(str(row["expires_at"])) > when:
                    if str(row["owner_id"]) == owner:
                        return _lease_from_row(row)
                    raise MaintenanceLeaseError("maintenance scope already leased")
                conn.execute("UPDATE leases SET active=0,released_at=? WHERE lease_id=? AND active=1", (now_text, str(row["lease_id"])))
            lease_id = stable_id("maint-lease", ctx.scope.digest, owner, now_text, uuid4().hex)
            conn.execute("INSERT INTO leases(lease_id,scope_digest,owner_id,expires_at,acquired_at,released_at,active) VALUES(?,?,?,?,?,?,1)", (lease_id, ctx.scope.digest, owner, expires_text, now_text, ""))
            return _lease_from_row(conn.execute("SELECT * FROM leases WHERE lease_id=?", (lease_id,)).fetchone())

    def release_lease(self, context: MaintenanceContext | Mapping[str, Any], lease_id: str | None = None, *, now: datetime | None = None) -> MaintenanceLease:
        ctx = self._context(context, required_lease=True)
        lease_id = _required_text(lease_id or ctx.maintenance_lease_id, "lease_id")
        when = _utc(now)
        with self._write() as conn:
            row = conn.execute("SELECT * FROM leases WHERE lease_id=?", (lease_id,)).fetchone()
            if row is None:
                raise KeyError(lease_id)
            if str(row["owner_id"]) != ctx.actor_id or str(row["scope_digest"]) != ctx.scope.digest:
                raise MaintenanceLeaseError("lease owner or scope mismatch")
            if not int(row["active"]):
                return _lease_from_row(row)
            conn.execute("UPDATE leases SET active=0,released_at=? WHERE lease_id=? AND active=1 AND owner_id=?", (when.isoformat(), lease_id, ctx.actor_id))
            return _lease_from_row(conn.execute("SELECT * FROM leases WHERE lease_id=?", (lease_id,)).fetchone())

    def _verify_lease(self, context: MaintenanceContext, lease_id: str) -> MaintenanceLease:
        lease_id = _required_text(lease_id, "lease_id")
        when = _utc(None)
        with self._connection(readonly=True) as conn:
            row = conn.execute("SELECT * FROM leases WHERE lease_id=?", (lease_id,)).fetchone()
            if row is None:
                raise MaintenanceLeaseError("maintenance lease not found")
            lease = _lease_from_row(row)
            if not lease.active or lease.owner_id != context.actor_id or lease.scope_digest != context.scope.digest or _parse_time(lease.expires_at) <= when:
                raise MaintenanceLeaseError("maintenance lease is not active for this owner/scope")
            return lease

    def record_report(self, job_id: str, context: MaintenanceContext | Mapping[str, Any], *, status: str, counts: Mapping[str, int] | None = None, safety: Mapping[str, Any] | None = None) -> MaintenanceReport:
        ctx = self._context(context)
        job_id, status = _required_text(job_id, "job_id"), _required_text(status, "status")
        counts = {} if counts is None else dict(counts)
        safety = {} if safety is None else dict(safety)
        _validate_report_value(safety)
        if len(counts) > 512:
            raise ValueError("report counts have too many entries")
        for key, value in counts.items():
            normalized_key = str(key).strip().casefold().replace("-", "_")
            if normalized_key in _REPORT_DENY_KEYS or len(str(key)) > 4096:
                raise ValueError(f"report count field is forbidden or too large: {key!r}")
            if type(value) is not int or value < 0:
                raise ValueError(f"report count {key!r} must be non-negative int")
        report_digest = stable_digest({"job_id": job_id, "status": status, "counts": counts, "safety": safety})
        now = _now()
        with self._write() as conn:
            job_row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if job_row is None:
                raise KeyError(job_id)
            self._authorize_job(job_row, ctx)
            prior = conn.execute("SELECT * FROM reports WHERE job_id=? AND report_digest=?", (job_id, report_digest)).fetchone()
            if prior is not None:
                return _report_from_row(prior)
            report_id = stable_id("maint-report", job_id, report_digest)
            conn.execute("INSERT INTO reports(report_id,job_id,status,counts_json,safety_json,report_digest,created_at) VALUES(?,?,?,?,?,?,?)", (report_id, job_id, status, _json(counts), _json(safety), report_digest, now))
            _ledger(conn, "REPORT_WRITTEN", job_id, detail={"report_digest": report_digest})
            return _report_from_row(conn.execute("SELECT * FROM reports WHERE report_id=?", (report_id,)).fetchone())

    def get_report(self, report_id: str) -> MaintenanceReport | None:
        report_id = _required_text(report_id, "report_id")
        with self._connection(readonly=True) as conn:
            self._check_schema_connection(conn)
            row = conn.execute("SELECT * FROM reports WHERE report_id=?", (report_id,)).fetchone()
            return None if row is None else _report_from_row(row)

    def get_latest_job_report(
        self,
        job_id: str,
        context: MaintenanceContext | Mapping[str, Any],
        *,
        status: str | None = None,
    ) -> MaintenanceReport | None:
        """Read the newest report only after exact job ownership is proven."""

        ctx = self._context(context)
        job_id = _required_text(job_id, "job_id")
        if status is not None:
            status = _required_text(status, "status")
        with self._connection(readonly=True) as conn:
            self._check_schema_connection(conn)
            job_row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if job_row is None:
                raise KeyError(job_id)
            self._authorize_job(job_row, ctx)
            if status is None:
                row = conn.execute(
                    "SELECT * FROM reports WHERE job_id=? ORDER BY created_at DESC, report_id DESC LIMIT 1",
                    (job_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM reports WHERE job_id=? AND status=? ORDER BY created_at DESC, report_id DESC LIMIT 1",
                    (job_id, status),
                ).fetchone()
            return None if row is None else _report_from_row(row)

    def report(self, job_id: str, context: MaintenanceContext | Mapping[str, Any], **kwargs: Any) -> MaintenanceReport:
        return self.record_report(job_id, context, **kwargs)

    # Narrow aliases keep the public contract readable for the later GC and
    # compaction executors without introducing a generic ORM layer.
    submit_job = create_job
    transition = transition_job
    begin_reference_audit = begin_epoch
    open_epoch = begin_epoch
    mark = mark_candidate
    confirm = confirm_candidate
    create_lease = acquire_lease
    release = release_lease
    storage_report = record_report

    def integrity(self) -> dict[str, Any]:
        with self._connection(readonly=True) as conn:
            self._check_schema_connection(conn)
            result = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
            fk = tuple(conn.execute("PRAGMA foreign_key_check").fetchall())
            return {"ok": result == "ok" and not fk, "integrity_check": result, "foreign_key_errors": len(fk)}


# ---------------------------------------------------------------- utilities
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if not isinstance(value, datetime):
        raise ValueError("now must be datetime")
    return value.astimezone(timezone.utc)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _text(value: Any, field_name: str) -> str:
    if value is None or not isinstance(value, str) or "\x00" in value:
        raise ValueError(f"{field_name} must be a string")
    return value.strip()


def _json(value: Any) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("maintenance JSON must be finite and JSON-compatible") from exc
    if len(encoded.encode("utf-8")) > 64 * 1024:
        raise ValueError("maintenance JSON is too large")
    return encoded


_REPORT_DENY_KEYS = frozenset({
    "body", "content", "text", "raw", "payload", "blob", "document", "transcript",
    "secret", "secrets", "token", "tokens", "password", "credential", "credentials",
    "api_key", "apikey", "private_key", "command", "code", "control", "control_payload",
    "authority", "admin", "acl", "scope", "authorization", "owner", "actor", "agent",
    "project", "share_group", "capability",
})


def _validate_report_value(value: Any, *, depth: int = 0, nodes: list[int] | None = None) -> None:
    """Bound report metadata and reject body/secret/control fields."""

    if nodes is None:
        nodes = [0]
    if depth > 8:
        raise ValueError("report safety metadata is too deeply nested")
    nodes[0] += 1
    if nodes[0] > 512:
        raise ValueError("report safety metadata has too many nodes")
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().casefold().replace("-", "_")
            if normalized in _REPORT_DENY_KEYS:
                raise ValueError(f"report safety field is forbidden: {key}")
            if len(str(key)) > 4096:
                raise ValueError("report safety key is too large")
            _validate_report_value(child, depth=depth + 1, nodes=nodes)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _validate_report_value(child, depth=depth + 1, nodes=nodes)
    elif isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError("report safety metadata cannot contain binary values")
    elif isinstance(value, str) and len(value) > 4096:
        raise ValueError("report safety string is too large")
    elif not isinstance(value, (str, int, float, bool)) and value is not None:
        try:
            json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("report safety metadata must be JSON-compatible") from exc


def _ledger(conn: sqlite3.Connection, event_type: str, job_id: str, *, epoch_id: str = "", candidate_id: str = "", detail: Mapping[str, Any] | None = None) -> None:
    conn.execute("INSERT INTO ledger(ledger_id,event_type,job_id,epoch_id,candidate_id,detail_digest,created_at) VALUES(?,?,?,?,?,?,?)", (stable_id("maint-ledger", event_type, job_id, epoch_id, candidate_id, _now(), uuid4().hex), event_type, job_id, epoch_id or None, candidate_id or None, stable_digest(detail or {}), _now()))


def _job_from_row(row: sqlite3.Row) -> MaintenanceJob:
    try:
        payload = json.loads(str(row["scope_json"]))
        if not isinstance(payload, dict):
            raise ValueError("stored job context must be an object")
        return MaintenanceJob(job_id=str(row["job_id"]), request_key=str(row["request_key"]), operation=str(row["operation"]), state=str(row["state"]), dry_run=bool(int(row["dry_run"])), expected_generation=None if row["expected_generation"] is None else int(row["expected_generation"]), context_digest=str(row["context_digest"]), created_at=str(row["created_at"]), updated_at=str(row["updated_at"]), error_code=str(row["error_code"]))
    except (TypeError, ValueError, json.JSONDecodeError, KeyError) as exc:
        raise MaintenanceSchemaError("malformed stored maintenance job") from exc


def _epoch_from_row(row: sqlite3.Row) -> MaintenanceEpoch:
    try:
        return MaintenanceEpoch(epoch_id=str(row["epoch_id"]), job_id=str(row["job_id"]), epoch_number=int(row["epoch_number"]), state=str(row["state"]), reference_digest=str(row["reference_digest"]), complete=bool(int(row["complete"])), created_at=str(row["created_at"]))
    except (TypeError, ValueError, KeyError) as exc:
        raise MaintenanceSchemaError("malformed stored maintenance epoch") from exc


def _candidate_from_row(row: sqlite3.Row) -> MaintenanceCandidate:
    try:
        return MaintenanceCandidate(candidate_id=str(row["candidate_id"]), epoch_id=str(row["epoch_id"]), blob_id=str(row["blob_id"]), reference_digest=str(row["reference_digest"]), hold_digest=str(row["hold_digest"]), state=str(row["state"]), created_at=str(row["created_at"]), updated_at=str(row["updated_at"]))
    except (TypeError, ValueError, KeyError) as exc:
        raise MaintenanceSchemaError("malformed stored maintenance candidate") from exc


def _lease_from_row(row: sqlite3.Row) -> MaintenanceLease:
    try:
        return MaintenanceLease(lease_id=str(row["lease_id"]), owner_id=str(row["owner_id"]), scope_digest=str(row["scope_digest"]), expires_at=str(row["expires_at"]), acquired_at=str(row["acquired_at"]), released_at=str(row["released_at"]), active=bool(int(row["active"])))
    except (TypeError, ValueError, KeyError) as exc:
        raise MaintenanceSchemaError("malformed stored maintenance lease") from exc


def _report_from_row(row: sqlite3.Row) -> MaintenanceReport:
    try:
        counts = json.loads(str(row["counts_json"]))
        safety = json.loads(str(row["safety_json"]))
        if not isinstance(counts, dict) or not isinstance(safety, dict):
            raise ValueError("stored report JSON must be objects")
        return MaintenanceReport(report_id=str(row["report_id"]), job_id=str(row["job_id"]), status=str(row["status"]), counts=counts, safety=safety, digest=str(row["report_digest"]), created_at=str(row["created_at"]))
    except (TypeError, ValueError, json.JSONDecodeError, KeyError) as exc:
        raise MaintenanceSchemaError("malformed stored maintenance report JSON") from exc


# Compatibility aliases used by callers that call this a ledger or registry.
MaintenanceLedger = MaintenanceStore
