"""Explicit, fail-closed SQLite maintenance operations for Phase 7.

Only SQLite housekeeping is implemented here.  No operation deletes business
rows.  Compaction is intentionally a full replacement transaction with a
same-directory temporary file, so an interrupted run leaves the original file
usable.  Every mutating operation requires a trusted maintenance context,
the exact active manifest generation, an unexpired scope lease, writer
quiescence, and drained outboxes.
"""

from __future__ import annotations

from dataclasses import dataclass
import gc
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
from typing import Any, Mapping
from uuid import uuid4

from ..cutover_v2.state import CutoverState, RuntimeSnapshot, snapshot_from_port
from ..storage.layout import LayoutError, WorkspaceV2Layout
from ..system.manifest import ManifestManager
from .models import MaintenanceAuthorizationError, MaintenanceConflictError, MaintenanceContext, MaintenanceJobState, MaintenanceLeaseError, MaintenanceOperation
from .store import MaintenanceStore
from .storage_report import (
    StorageReport,
    StorageReporter,
    StorageReportError,
    _SQLiteIdentityLease,
    _path_identity,
    _sidecar_identities,
)


class SQLiteMaintenanceError(RuntimeError):
    """Base error raised before or during a guarded SQLite operation."""


class MaintenancePreconditionError(SQLiteMaintenanceError):
    """A state, CAS, lease, quiescence, or outbox gate is not satisfied."""


class MaintenanceFault(SQLiteMaintenanceError):
    """A deliberately injected or unexpected compaction fault."""


@dataclass(frozen=True, slots=True)
class MaintenanceActionResult:
    operation: str
    path: str
    applied: bool
    changed: bool
    before: StorageReport
    after: StorageReport
    status: str = "ok"
    reason: str = ""
    temp_path: str = ""

    @property
    def dry_run(self) -> bool:
        return not self.applied

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "path": self.path,
            "applied": self.applied,
            "dry_run": self.dry_run,
            "changed": self.changed,
            "status": self.status,
            "reason": self.reason,
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
            "temp_path": self.temp_path,
        }

    as_dict = to_dict


@dataclass(frozen=True, slots=True)
class _ArtifactFingerprint:
    """Identity/content receipt for a replacement or recovery artifact."""

    identity: tuple[int, int, int, int]
    digest: str
    sidecars: tuple[tuple[str, tuple[int, int, int, int] | None, str | None], ...]


def _artifact_fingerprint(path: Path) -> _ArtifactFingerprint:
    """Capture an artifact without following a swapped link or reparse point."""

    if path.is_symlink() or WorkspaceV2Layout._is_reparse_or_symlink(path) or not path.is_file():
        raise StorageReportError("artifact is missing or not a regular file")
    identity = _path_identity(path)
    digest = _sha256(path)
    if _path_identity(path) != identity:
        raise StorageReportError("artifact changed while hashing")
    captured: list[tuple[str, tuple[int, int, int, int] | None, str | None]] = []
    for suffix, sidecar_identity in _sidecar_identities(path).items():
        sidecar = path.with_name(path.name + suffix)
        sidecar_digest: str | None = None
        if sidecar_identity is not None:
            sidecar_digest = _sha256(sidecar)
            if _path_identity(sidecar) != sidecar_identity:
                raise StorageReportError("artifact sidecar changed while hashing")
        captured.append((suffix, sidecar_identity, sidecar_digest))
    return _ArtifactFingerprint(identity, digest, tuple(captured))


def _optional_artifact_fingerprint(path: Path) -> _ArtifactFingerprint | None:
    """Capture an optional backup artifact, rejecting links/broken paths."""

    if path.is_symlink() or WorkspaceV2Layout._is_reparse_or_symlink(path):
        raise StorageReportError("artifact is a symlink/reparse point")
    if not path.exists():
        return None
    return _artifact_fingerprint(path)


def _assert_artifact_fingerprint(path: Path, expected: _ArtifactFingerprint, label: str) -> None:
    """Fail closed when an artifact changes after it was captured."""

    try:
        current = _artifact_fingerprint(path)
    except (OSError, ValueError, StorageReportError) as exc:
        raise MaintenancePreconditionError(f"{label} artifact identity is unavailable") from exc
    if current != expected:
        raise MaintenancePreconditionError(f"{label} artifact identity changed")


def _assert_main_artifact_fingerprint(path: Path, expected: _ArtifactFingerprint, label: str) -> None:
    """Check the replacement's main inode/bytes without pinning sidecars.

    SQLite may lazily create or remove WAL/SHM files while a report is being
    collected.  The main database, however, must still be the exact inode and
    digest that was verified before the atomic replacement.
    """

    try:
        current = _artifact_fingerprint(path)
    except (OSError, ValueError, StorageReportError) as exc:
        raise MaintenancePreconditionError(f"{label} artifact identity is unavailable") from exc
    if current.identity != expected.identity or current.digest != expected.digest:
        raise MaintenancePreconditionError(f"{label} artifact identity changed")


def _assert_optional_artifact_fingerprint(path: Path, expected: _ArtifactFingerprint | None, label: str) -> None:
    """Recheck optional artifacts, including unexpected creation/removal."""

    try:
        current = _optional_artifact_fingerprint(path)
    except (OSError, ValueError, StorageReportError) as exc:
        raise MaintenancePreconditionError(f"{label} artifact identity is unavailable") from exc
    if current != expected:
        raise MaintenancePreconditionError(f"{label} artifact identity changed")


def _strict_generation(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise MaintenancePreconditionError("expected_generation must be a non-negative int")
    return value


def _strict_evidence(value: Any, name: str) -> None:
    if type(value) is not bool or value is not True:
        raise MaintenancePreconditionError(f"{name} evidence is required")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _same_schema_and_rows(before: StorageReport, after: StorageReport) -> bool:
    return before.schema_fingerprint == after.schema_fingerprint and dict(before.row_counts) == dict(after.row_counts) and dict(before.table_digests) == dict(after.table_digests)


class SQLiteMaintenanceExecutor:
    """Run one explicit SQLite maintenance action against an exact V2 path."""

    def __init__(
        self,
        workspace_or_layout: str | Path | WorkspaceV2Layout,
        *,
        source_workspace: str | Path | None = None,
        maintenance_store: MaintenanceStore | None = None,
        manifest: Any | None = None,
        quiescence_verifier: Any | None = None,
        outbox_verifier: Any | None = None,
        writer_quiesce_verifier: Any | None = None,
        outbox_drain_verifier: Any | None = None,
    ) -> None:
        if isinstance(workspace_or_layout, WorkspaceV2Layout):
            if source_workspace is None:
                raise LayoutError("source_workspace is required with a WorkspaceV2Layout")
            raw = Path(source_workspace).expanduser()
            if raw.is_symlink() or WorkspaceV2Layout._is_reparse_or_symlink(raw):
                raise LayoutError("source workspace cannot be a symlink or reparse point")
            if WorkspaceV2Layout(raw).workspace != workspace_or_layout.workspace:
                raise LayoutError("source_workspace does not match supplied layout")
            self.layout = workspace_or_layout
        else:
            if source_workspace is not None:
                raise LayoutError("source_workspace requires a WorkspaceV2Layout")
            self.layout = WorkspaceV2Layout(Path(workspace_or_layout))
        self.maintenance_store = maintenance_store
        self.manifest = manifest
        self.quiescence_verifier = quiescence_verifier if quiescence_verifier is not None else writer_quiesce_verifier
        self.outbox_verifier = outbox_verifier if outbox_verifier is not None else outbox_drain_verifier
        self.reporter = StorageReporter(self.layout, source_workspace=self.layout.workspace)

    # ---------------------------------------------------------------- paths
    def _target(self, target: str | Path, domain: str | None = None) -> tuple[Path, str]:
        raw = Path(target).expanduser()
        if domain is None:
            matches: list[tuple[Path, str]] = []
            for name, paths in self.layout.databases.items():
                for path in paths:
                    if os.path.normcase(os.path.abspath(os.fspath(path))) == os.path.normcase(os.path.abspath(os.fspath(raw))):
                        matches.append((path, name))
            if not matches:
                # system/maintenance.db is control-plane storage and not a
                # WorkspaceV2Layout target; accept it only to reject cleanly.
                raise SQLiteMaintenanceError("target is not an exact V2 database path")
            path, name = matches[0]
            cursor = path
            while True:
                if cursor.is_symlink() or WorkspaceV2Layout._is_reparse_or_symlink(cursor):
                    raise SQLiteMaintenanceError("target path is a symlink/reparse point")
                if cursor == self.layout.workspace or cursor.parent == cursor:
                    break
                cursor = cursor.parent
            try:
                self.layout.assert_database_path(path, name)
            except (LayoutError, OSError) as exc:
                raise SQLiteMaintenanceError("unsafe target path") from exc
            if not path.is_file():
                raise SQLiteMaintenanceError("target database is missing")
            for suffix in ("-wal", "-shm", "-journal"):
                sidecar = path.with_name(path.name + suffix)
                if sidecar.is_symlink() or WorkspaceV2Layout._is_reparse_or_symlink(sidecar):
                    raise SQLiteMaintenanceError("target database sidecar is a symlink/reparse point")
            return path, name
        try:
            expected = tuple(self.layout.db_paths(domain))
        except (LayoutError, TypeError) as exc:
            raise SQLiteMaintenanceError("unknown V2 storage domain") from exc
        key = os.path.normcase(os.path.abspath(os.fspath(raw)))
        for path in expected:
            if os.path.normcase(os.path.abspath(os.fspath(path))) == key:
                # Check lexical symlink/reparse components before any resolve.
                cursor = path
                while True:
                    if cursor.is_symlink() or WorkspaceV2Layout._is_reparse_or_symlink(cursor):
                        raise SQLiteMaintenanceError("target path is a symlink/reparse point")
                    if cursor == self.layout.workspace or cursor.parent == cursor:
                        break
                    cursor = cursor.parent
                try:
                    self.layout.assert_database_path(path, domain)
                except (LayoutError, OSError) as exc:
                    raise SQLiteMaintenanceError("unsafe target path") from exc
                if not path.is_file():
                    raise SQLiteMaintenanceError("target database is missing")
                for suffix in ("-wal", "-shm", "-journal"):
                    sidecar = path.with_name(path.name + suffix)
                    if sidecar.is_symlink() or WorkspaceV2Layout._is_reparse_or_symlink(sidecar):
                        raise SQLiteMaintenanceError("target database sidecar is a symlink/reparse point")
                return path, domain
        raise SQLiteMaintenanceError("target is not an exact V2 database path")

    def _snapshot(self) -> RuntimeSnapshot:
        if self.manifest is None:
            try:
                record = ManifestManager(self.layout).current()
                return RuntimeSnapshot.from_value({"state": record.state.value, "generation": record.generation})
            except Exception as exc:
                raise MaintenancePreconditionError("manifest snapshot is unavailable") from exc
        snap = snapshot_from_port(self.manifest)
        if not snap.available or not snap.trusted:
            raise MaintenancePreconditionError("manifest snapshot is unavailable or untrusted")
        return snap

    def _context(self, context: MaintenanceContext | Mapping[str, Any] | None, *, expected_generation: int | None, lease_required: bool = True) -> MaintenanceContext:
        if context is None or not isinstance(context, MaintenanceContext):
            raise MaintenanceAuthorizationError("trusted MaintenanceContext is required")
        ctx = context
        if not ctx.trusted_context or not ctx.scope.trusted_context:
            raise MaintenanceAuthorizationError("untrusted MaintenanceContext")
        if ctx.workspace_id != str(self.layout.workspace):
            raise MaintenanceAuthorizationError("maintenance scope workspace mismatch")
        if lease_required and not ctx.maintenance_lease_id:
            raise MaintenanceLeaseError("maintenance lease is required")
        expected = expected_generation if expected_generation is not None else ctx.expected_generation
        _strict_generation(expected)
        snap = self._snapshot()
        if snap.state is not CutoverState.V2_ACTIVE:
            raise MaintenancePreconditionError("maintenance mutation requires V2_ACTIVE manifest")
        if snap.generation != expected:
            raise MaintenancePreconditionError("maintenance expected_generation CAS conflict")
        if self.maintenance_store is None:
            raise MaintenanceLeaseError("MaintenanceStore is required to verify the lease")
        try:
            self.maintenance_store._verify_lease(ctx, ctx.maintenance_lease_id)  # narrow trusted boundary
        except Exception as exc:
            if isinstance(exc, (MaintenanceLeaseError, MaintenancePreconditionError)):
                raise
            raise MaintenanceLeaseError("maintenance lease is not active") from exc
        return ctx

    @staticmethod
    def _verify_evidence(verifier: Any, name: str, *, context: MaintenanceContext, path: Path, domain: str, generation: int) -> None:
        if verifier is None:
            raise MaintenancePreconditionError(f"trusted {name} verifier is required")
        fn = getattr(verifier, "verify", verifier)
        if not callable(fn):
            raise MaintenancePreconditionError(f"trusted {name} verifier is not callable")
        try:
            result = fn(context=context, path=str(path), domain=domain, expected_generation=generation, lease_id=context.maintenance_lease_id, workspace_id=context.workspace_id)
        except TypeError:
            result = fn(context, str(path), domain, generation, context.maintenance_lease_id)
        if type(result) is not bool or result is not True:
            raise MaintenancePreconditionError(f"{name} verifier did not prove the required evidence")

    def _quiesced(self, *, context: MaintenanceContext, path: Path, domain: str, generation: int, writer_quiesced: Any, outbox_drained: Any, quiesced: Any | None = None, outbox: Any | None = None) -> None:
        # Naked booleans are intentionally rejected: evidence must come from
        # an injected trusted verifier bound to this exact operation.
        if quiesced is not None or outbox is not None or writer_quiesced is not False or outbox_drained is not False:
            raise MaintenancePreconditionError("writer/outbox evidence must be supplied by trusted verifiers")
        self._verify_evidence(self.quiescence_verifier, "writer_quiesced", context=context, path=path, domain=domain, generation=generation)
        self._verify_evidence(self.outbox_verifier, "outbox_drained", context=context, path=path, domain=domain, generation=generation)

    def _require_job(self, *, job_id: str | None, context: MaintenanceContext, operation: MaintenanceOperation, generation: int) -> None:
        if not isinstance(job_id, str) or not job_id.strip():
            raise MaintenancePreconditionError("job_id is required for physical maintenance")
        if self.maintenance_store is None:
            raise MaintenancePreconditionError("MaintenanceStore is required to verify maintenance job")
        try:
            job = self.maintenance_store.get_job(job_id)
        except Exception as exc:
            raise MaintenancePreconditionError("maintenance job cannot be read") from exc
        if job is None:
            raise MaintenancePreconditionError("maintenance job not found")
        if job.operation is not operation:
            raise MaintenancePreconditionError("maintenance job operation mismatch")
        if job.state is not MaintenanceJobState.ACTIVE or job.dry_run:
            raise MaintenancePreconditionError("maintenance job must be ACTIVE with dry_run=false")
        if job.expected_generation != generation:
            raise MaintenancePreconditionError("maintenance job generation mismatch")
        if job.context_digest != context.digest:
            raise MaintenanceAuthorizationError("maintenance job context mismatch")

    def _result(self, operation: str, path: Path, before: StorageReport, *, applied: bool, reason: str = "", status: str = "ok", temp_path: str = "") -> MaintenanceActionResult:
        after = self.reporter.report(path, domain=before.domain)
        return MaintenanceActionResult(operation, str(path), applied, not _same_schema_and_rows(before, after) or after.allocated_bytes != before.allocated_bytes or after.wal_bytes != before.wal_bytes, before, after, status=status, reason=reason, temp_path=temp_path)

    # ------------------------------------------------------------- safe auto
    def auto_maintenance(self, target: str | Path, *, domain: str | None = None, apply: bool = False, fault_point: str = "") -> MaintenanceActionResult:
        """Run only PASSIVE checkpoint + optimize; never rewrites rows."""

        if type(apply) is not bool:
            raise ValueError("apply must be bool")
        path, resolved_domain = self._target(target, domain)
        baseline_identity = _path_identity(path)
        baseline_sidecars = _sidecar_identities(path)
        before = self.reporter.report(path, domain=resolved_domain)
        if _path_identity(path)[:2] != baseline_identity[:2]:
            raise MaintenancePreconditionError("source database identity changed during report")
        if not apply:
            return self._result("auto", path, before, applied=False, reason="dry_run")
        if fault_point == "before":
            raise MaintenanceFault("fault before auto maintenance")
        try:
            expected_identity = baseline_identity
            expected_sidecars = baseline_sidecars
            lease = _SQLiteIdentityLease.open(path, readonly=False, expected_identity=expected_identity, expected_sidecars=expected_sidecars)
            with lease as conn:
                lease.assert_current()
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                lease.refresh_sidecars()
                lease.identity = _path_identity(path)
                row = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                if row is None:
                    raise SQLiteMaintenanceError("PASSIVE checkpoint returned no result")
                conn.execute("PRAGMA optimize")
                conn.commit()
                lease.refresh_sidecars()
                lease.assert_current()
        except (sqlite3.Error, OSError) as exc:
            raise SQLiteMaintenanceError("safe SQLite maintenance failed") from exc
        # Do not raise an uncertain post-commit fault: the passive checkpoint
        # and optimize have already been applied and the result below is the
        # durable receipt for this direct executor call.
        return self._result("auto", path, before, applied=True)

    # ---------------------------------------------------------- incremental
    def incremental_vacuum(
        self,
        target: str | Path,
        *,
        domain: str | None = None,
        context: MaintenanceContext | Mapping[str, Any] | None = None,
        job_id: str | None = None,
        expected_generation: int | None = None,
        apply: bool = False,
        pages: int | None = None,
        writer_quiesced: bool = False,
        outbox_drained: bool = False,
        quiesced: bool | None = None,
        outbox: bool | None = None,
        fault_point: str = "",
    ) -> MaintenanceActionResult:
        path, resolved_domain = self._target(target, domain)
        baseline_identity = _path_identity(path)
        baseline_sidecars = _sidecar_identities(path)
        before = self.reporter.report(path, domain=resolved_domain)
        if _path_identity(path)[:2] != baseline_identity[:2]:
            raise MaintenancePreconditionError("source database identity changed during report")
        if not apply:
            return self._result("incremental_vacuum", path, before, applied=False, reason="dry_run")
        ctx = self._context(context, expected_generation=expected_generation)
        generation = _strict_generation(expected_generation if expected_generation is not None else ctx.expected_generation)
        self._require_job(job_id=job_id, context=ctx, operation=MaintenanceOperation.COMPACT, generation=generation)
        self._quiesced(context=ctx, path=path, domain=resolved_domain, generation=generation, writer_quiesced=writer_quiesced, outbox_drained=outbox_drained, quiesced=quiesced, outbox=outbox)
        if before.auto_vacuum != "INCREMENTAL":
            raise MaintenancePreconditionError("incremental_vacuum requires auto_vacuum=INCREMENTAL")
        if pages is not None and (type(pages) is not int or pages < 0):
            raise ValueError("pages must be a non-negative int")
        if fault_point == "before":
            raise MaintenanceFault("fault before incremental vacuum")
        try:
            expected_identity = baseline_identity
            expected_sidecars = baseline_sidecars
            lease = _SQLiteIdentityLease.open(path, readonly=False, expected_identity=expected_identity, expected_sidecars=expected_sidecars)
            with lease as conn:
                lease.assert_current()
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                lease.refresh_sidecars()
                lease.identity = _path_identity(path)
                conn.execute("PRAGMA incremental_vacuum" if pages is None else f"PRAGMA incremental_vacuum({int(pages)})")
                conn.commit()
                lease.refresh_sidecars()
                lease.assert_current()
        except (sqlite3.Error, OSError) as exc:
            raise SQLiteMaintenanceError("incremental_vacuum failed") from exc
        # There is deliberately no post-apply fault injection: after SQLite
        # commits, reporting an exception would make callers retry an already
        # applied operation without a durable receipt.
        return self._result("incremental_vacuum", path, before, applied=True)

    # --------------------------------------------------------------- compact
    def deep_compact(
        self,
        target: str | Path,
        *,
        domain: str | None = None,
        context: MaintenanceContext | Mapping[str, Any] | None = None,
        job_id: str | None = None,
        expected_generation: int | None = None,
        apply: bool = False,
        writer_quiesced: bool = False,
        outbox_drained: bool = False,
        quiesced: bool | None = None,
        outbox: bool | None = None,
        fault_point: str = "",
    ) -> MaintenanceActionResult:
        path, resolved_domain = self._target(target, domain)
        if resolved_domain == "system" or path.name == "manifest.db":
            raise MaintenancePreconditionError("system manifest cannot be compacted")
        baseline_identity = _path_identity(path)
        baseline_sidecars = _sidecar_identities(path)
        before = self.reporter.report(path, domain=resolved_domain)
        if _path_identity(path)[:2] != baseline_identity[:2]:
            raise MaintenancePreconditionError("source database identity changed during report")
        if not apply:
            return self._result("compact", path, before, applied=False, reason="dry_run")
        ctx = self._context(context, expected_generation=expected_generation)
        generation = _strict_generation(expected_generation if expected_generation is not None else ctx.expected_generation)
        self._require_job(job_id=job_id, context=ctx, operation=MaintenanceOperation.COMPACT, generation=generation)
        self._quiesced(context=ctx, path=path, domain=resolved_domain, generation=generation, writer_quiesced=writer_quiesced, outbox_drained=outbox_drained, quiesced=quiesced, outbox=outbox)
        # Store calls are connection-scoped, but CPython may retain an
        # unreachable sqlite wrapper until cyclic GC.  Release only such dead
        # handles after the trusted writer barrier; live handles still make
        # the Windows atomic replace fail closed.
        gc.collect()
        if not before.integrity_ok:
            raise MaintenancePreconditionError("integrity/FK check must pass before compaction")
        try:
            source_identity = baseline_identity
            source_sidecars = baseline_sidecars
        except (OSError, ValueError, StorageReportError) as exc:
            raise MaintenancePreconditionError("source database identity unavailable") from exc
        original_mode = stat.S_IMODE(path.stat().st_mode)
        temp = path.with_name(f".{path.name}.phase7-{uuid4().hex}.tmp")
        backup = path.with_name(f".{path.name}.phase7-{uuid4().hex}.bak")
        wal_path = path.with_name(path.name + "-wal")
        shm_path = path.with_name(path.name + "-shm")
        wal_backup = path.with_name(f".{path.name}.phase7-{uuid4().hex}.wal.bak")
        shm_backup = path.with_name(f".{path.name}.phase7-{uuid4().hex}.shm.bak")
        recovery = path.with_name(f".{path.name}.phase7-{uuid4().hex}.recovery")
        # Keep a second, independent recovery copy.  A hostile or flaky
        # filesystem can swap ``recovery`` in the check→replace window; the
        # shadow preserves the only known-good bytes for a verified retry.
        recovery_shadow = path.with_name(f".{path.name}.phase7-{uuid4().hex}.recovery-shadow")
        replaced = False
        replacement_identity: tuple[int, int, int, int] | None = None
        temp_fingerprint: _ArtifactFingerprint | None = None
        backup_fingerprint: _ArtifactFingerprint | None = None
        wal_backup_fingerprint: _ArtifactFingerprint | None = None
        shm_backup_fingerprint: _ArtifactFingerprint | None = None
        recovery_fingerprint: _ArtifactFingerprint | None = None
        recovery_shadow_fingerprint: _ArtifactFingerprint | None = None
        for candidate in (temp, backup, wal_backup, shm_backup, recovery, recovery_shadow):
            if candidate.exists() or candidate.is_symlink():
                raise SQLiteMaintenanceError("compaction temporary path already exists")
        try:
            # Preserve the pre-checkpoint bytes.  TRUNCATE legitimately changes
            # the main file, but an injected fault must still restore exactly
            # what the caller handed us (including any sidecars).
            # Keep an open read lease across the backup copy.  A concurrent
            # rename cannot redirect this copy to an outside inode without
            # tripping the identity check before any writable operation.
            source_lease = _SQLiteIdentityLease.open(path, readonly=True, expected_identity=source_identity, expected_sidecars=_sidecar_identities(path))
            with source_lease:
                source_lease.assert_current()
                shutil.copy2(path, backup)
                if wal_path.is_file():
                    shutil.copy2(wal_path, wal_backup)
                if shm_path.is_file():
                    shutil.copy2(shm_path, shm_backup)
                source_lease.assert_current()
            backup_fingerprint = _artifact_fingerprint(backup)
            wal_backup_fingerprint = _optional_artifact_fingerprint(wal_backup)
            shm_backup_fingerprint = _optional_artifact_fingerprint(shm_backup)
            # A TRUNCATE checkpoint is mandatory before swapping the main file;
            # otherwise an old WAL could be replayed onto the compacted file.
            checkpoint_lease = _SQLiteIdentityLease.open(path, readonly=False, expected_identity=source_identity, expected_sidecars=source_sidecars)
            with checkpoint_lease as conn:
                checkpoint_lease.assert_current()
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                checkpoint_lease.refresh_sidecars()
                checkpoint_lease.identity = _path_identity(path)
                row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if row is None or any(int(value) != 0 for value in row):
                    raise MaintenancePreconditionError("WAL checkpoint could not drain active writers")
                conn.commit()
                checkpoint_lease.refresh_sidecars()
                checkpoint_lease.assert_current()
            if wal_path.exists() and wal_path.stat().st_size:
                raise MaintenancePreconditionError("WAL remains non-empty after TRUNCATE checkpoint")
            source_lease = _SQLiteIdentityLease.open(path, readonly=True, expected_identity=source_identity, expected_sidecars=_sidecar_identities(path))
            with source_lease:
                checkpoint_sha = _sha256(path)
                source_lease.assert_current()
            if fault_point == "after_checkpoint":
                raise MaintenanceFault("fault after checkpoint")
            vacuum_lease = _SQLiteIdentityLease.open(path, readonly=False, expected_identity=source_identity, expected_sidecars=_sidecar_identities(path))
            with vacuum_lease as conn:
                vacuum_lease.assert_current()
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                vacuum_lease.refresh_sidecars()
                vacuum_lease.identity = _path_identity(path)
                conn.execute("VACUUM INTO ?", (str(temp),))
                conn.commit()
                vacuum_lease.refresh_sidecars()
                vacuum_lease.assert_current()
            if fault_point == "after_vacuum":
                raise MaintenanceFault("fault after VACUUM INTO")
            # The temporary is deliberately outside the fixed V2 name set;
            # inspect it with a path-only read-only reporter, then discard it.
            compacted = StorageReporter().report(temp)
            if not compacted.integrity_ok or not _same_schema_and_rows(before, compacted):
                raise SQLiteMaintenanceError("compacted database failed schema/row/digest validation")
            if stat.S_IMODE(temp.stat().st_mode) != original_mode:
                try:
                    os.chmod(temp, original_mode)
                except OSError:
                    raise SQLiteMaintenanceError("compacted database ACL sample differs")
            if stat.S_IMODE(temp.stat().st_mode) != original_mode:
                raise SQLiteMaintenanceError("compacted database ACL sample differs")
            temp_fingerprint = _artifact_fingerprint(temp)
            source_lease = _SQLiteIdentityLease.open(path, readonly=True, expected_identity=source_identity, expected_sidecars=_sidecar_identities(path))
            with source_lease:
                source_lease.assert_current()
                source_unchanged = _sha256(path) == checkpoint_sha
                source_lease.assert_current()
            if not source_unchanged:
                raise MaintenancePreconditionError("source database changed during compaction")
            if fault_point == "before_replace":
                raise MaintenanceFault("fault before replace")
            # Every SQLite handle has been closed before this operation.  This
            # is required on Windows where an open handle prevents rename.
            replace_guard = _SQLiteIdentityLease.open(path, readonly=True, expected_identity=source_identity, expected_sidecars=_sidecar_identities(path))
            with replace_guard:
                replace_guard.assert_current()
                if temp_fingerprint is None:
                    raise SQLiteMaintenanceError("compacted database identity receipt is unavailable")
                _assert_artifact_fingerprint(temp, temp_fingerprint, "temporary")
            os.replace(temp, path)
            replaced = True
            replacement_identity = _path_identity(path)
            # A swap can occur in the tiny interval between the preflight and
            # os.replace call.  Verify the target received the exact captured
            # temp inode and bytes before proceeding to post-replace checks.
            if temp_fingerprint is None or replacement_identity != temp_fingerprint.identity or _sha256(path) != temp_fingerprint.digest:
                raise MaintenancePreconditionError("replacement target does not match compacted artifact")
            if fault_point == "after_replace":
                raise MaintenanceFault("fault after replace")
            after = self.reporter.report(path, domain=resolved_domain)
            if not after.integrity_ok or not _same_schema_and_rows(before, after):
                raise SQLiteMaintenanceError("post-replace compacted database validation failed")
            # Recheck immediately before removing the only recovery copy.  A
            # report can succeed while another actor swaps the pathname; in
            # that case raise and leave all artifacts for reconciliation.
            if temp_fingerprint is None:
                raise SQLiteMaintenanceError("compacted database identity receipt is unavailable")
            _assert_main_artifact_fingerprint(path, temp_fingerprint, "replacement target")
            cleanup_guard = path.with_name(f".{path.name}.phase7-{uuid4().hex}.cleanup.bak")
            cleanup_guard_fingerprint: _ArtifactFingerprint | None = None
            shutil.copy2(backup, cleanup_guard)
            cleanup_guard_fingerprint = _artifact_fingerprint(cleanup_guard)
            try:
                # Keep the main backup until the sidecar cleanup has passed a
                # second target check.  If either check fails, the guard (and
                # any not-yet-removed artifacts) remains available.
                _assert_main_artifact_fingerprint(path, temp_fingerprint, "replacement target")
                wal_backup.unlink(missing_ok=True)
                _assert_main_artifact_fingerprint(path, temp_fingerprint, "replacement target")
                shm_backup.unlink(missing_ok=True)
                _assert_main_artifact_fingerprint(path, temp_fingerprint, "replacement target")
                backup.unlink(missing_ok=True)
                _assert_main_artifact_fingerprint(path, temp_fingerprint, "replacement target")
                _assert_artifact_fingerprint(cleanup_guard, cleanup_guard_fingerprint, "cleanup guard")
                for sidecar in (path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
                    if sidecar.exists() and sidecar.stat().st_size == 0:
                        sidecar.unlink(missing_ok=True)
                _assert_main_artifact_fingerprint(path, temp_fingerprint, "replacement target")
                cleanup_guard.unlink(missing_ok=True)
            except Exception:
                # The guard is deliberately not removed on an uncertain
                # cleanup.  It is an independent recovery artifact even if a
                # concurrently swapped pathname consumed ``backup``.
                raise
            return MaintenanceActionResult("compact", str(path), True, not _same_schema_and_rows(before, after) or after.allocated_bytes != before.allocated_bytes, before, after)
        except Exception:
            # Restore only after all handles are closed.  If replacement never
            # happened, the original remains in place and backup is removed.
            restored = False
            try:
                if backup_fingerprint is not None:
                    _assert_artifact_fingerprint(backup, backup_fingerprint, "backup")
                    _assert_optional_artifact_fingerprint(wal_backup, wal_backup_fingerprint, "WAL backup")
                    _assert_optional_artifact_fingerprint(shm_backup, shm_backup_fingerprint, "SHM backup")
                    expected_restore = replacement_identity if replaced else source_identity
                    try:
                        restore_guard = _SQLiteIdentityLease.open(
                            path, readonly=True, expected_identity=expected_restore, expected_sidecars=_sidecar_identities(path),
                        )
                        restore_guard.close()
                    except Exception as identity_exc:
                        raise SQLiteMaintenanceError("source database identity changed during recovery") from identity_exc
                    # Never unlink the current target before an atomic backup
                    # replacement: if the second rename fails, the recovery
                    # path remains available instead of losing both copies.
                    moved_to_recovery = False
                    if path.exists():
                        # Copy before the move so a failed recovery rename
                        # still has an untouched source.  The shadow is not
                        # used on the happy path and is removed only after
                        # the target has been verified.
                        shutil.copy2(path, recovery_shadow)
                        recovery_shadow_fingerprint = _artifact_fingerprint(recovery_shadow)
                        os.replace(path, recovery)
                        moved_to_recovery = True
                        recovery_fingerprint = _artifact_fingerprint(recovery)
                    try:
                        # Recheck immediately before replacing the target; a
                        # backup swap must fail closed without consuming it.
                        _assert_artifact_fingerprint(backup, backup_fingerprint, "backup")
                        _assert_optional_artifact_fingerprint(wal_backup, wal_backup_fingerprint, "WAL backup")
                        _assert_optional_artifact_fingerprint(shm_backup, shm_backup_fingerprint, "SHM backup")
                        os.replace(backup, path)
                        if _path_identity(path) != backup_fingerprint.identity or _sha256(path) != backup_fingerprint.digest:
                            raise MaintenancePreconditionError("recovery target does not match backup artifact")
                    except Exception:
                        # If the backup changed after the target was moved,
                        # put the current target back rather than leaving the
                        # workspace path absent.  Keep recovery artifacts if
                        # that rollback itself cannot be proven.
                        if moved_to_recovery and recovery_fingerprint is not None:
                            try:
                                _assert_artifact_fingerprint(recovery, recovery_fingerprint, "recovery")
                                os.replace(recovery, path)
                                _assert_main_artifact_fingerprint(path, recovery_fingerprint, "recovery target")
                                moved_to_recovery = False
                            except Exception:
                                # The recovery pathname itself may have been
                                # swapped after its fingerprint check.  Retry
                                # from the independent shadow, but only after
                                # verifying both source and destination.
                                if recovery_shadow_fingerprint is None:
                                    raise
                                _assert_artifact_fingerprint(recovery_shadow, recovery_shadow_fingerprint, "recovery shadow")
                                os.replace(recovery_shadow, path)
                                _assert_main_artifact_fingerprint(path, recovery_shadow_fingerprint, "recovery target")
                                moved_to_recovery = False
                        raise
                    for sidecar, sidecar_backup in ((wal_path, wal_backup), (shm_path, shm_backup)):
                        expected_sidecar = wal_backup_fingerprint if sidecar_backup == wal_backup else shm_backup_fingerprint
                        _assert_optional_artifact_fingerprint(sidecar_backup, expected_sidecar, "sidecar backup")
                        sidecar.unlink(missing_ok=True)
                        if expected_sidecar is not None:
                            _assert_artifact_fingerprint(sidecar_backup, expected_sidecar, "sidecar backup")
                            os.replace(sidecar_backup, sidecar)
                            if _path_identity(sidecar) != expected_sidecar.identity or _sha256(sidecar) != expected_sidecar.digest:
                                raise MaintenancePreconditionError("recovery sidecar does not match backup artifact")
                    if recovery_fingerprint is not None:
                        _assert_artifact_fingerprint(recovery, recovery_fingerprint, "recovery")
                    recovery.unlink(missing_ok=True)
                    if recovery_shadow_fingerprint is not None:
                        _assert_artifact_fingerprint(recovery_shadow, recovery_shadow_fingerprint, "recovery shadow")
                        recovery_shadow.unlink(missing_ok=True)
                    restored = True
            finally:
                temp.unlink(missing_ok=True)
                # Keep every artifact when restoration failed; they are the
                # only recoverable bytes and must not be hidden.
                if restored:
                    backup.unlink(missing_ok=True)
                    wal_backup.unlink(missing_ok=True)
                    shm_backup.unlink(missing_ok=True)
                    recovery_shadow.unlink(missing_ok=True)
            raise


# Friendly aliases for callers and older design notes.
SQLiteMaintenance = SQLiteMaintenanceExecutor
MaintenanceExecutor = SQLiteMaintenanceExecutor
compact_sqlite = SQLiteMaintenanceExecutor.deep_compact
incremental_vacuum = SQLiteMaintenanceExecutor.incremental_vacuum


__all__ = [
    "SQLiteMaintenanceError", "MaintenancePreconditionError", "MaintenanceFault",
    "MaintenanceActionResult", "SQLiteMaintenanceExecutor", "SQLiteMaintenance",
    "MaintenanceExecutor", "compact_sqlite", "incremental_vacuum",
]
