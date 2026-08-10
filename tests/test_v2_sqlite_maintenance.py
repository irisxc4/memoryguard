from __future__ import annotations

import os
import importlib
from pathlib import Path
import shutil
import sqlite3

import pytest

from memoryguard.maintenance_v2 import sqlite_maintenance as sqlite_maintenance_module
from memoryguard.maintenance_v2.models import MaintenanceContext, MaintenanceScope
from memoryguard.maintenance_v2.sqlite_maintenance import (
    MaintenanceFault,
    MaintenancePreconditionError,
    SQLiteMaintenanceError,
    SQLiteMaintenanceExecutor,
)
from memoryguard.maintenance_v2.storage_report import StorageReportError, StorageReporter
from memoryguard.maintenance_v2.storage_report import _SQLiteIdentityLease

storage_report_module = importlib.import_module("memoryguard.maintenance_v2.storage_report")
from memoryguard.maintenance_v2.store import MaintenanceStore
from memoryguard.storage.layout import WorkspaceV2Layout
from memoryguard.system.manifest import ManifestManager, ManifestState


def _active_workspace(tmp_path: Path, *, incremental: bool = False) -> tuple[WorkspaceV2Layout, Path, MaintenanceStore, MaintenanceContext, str]:
    layout = WorkspaceV2Layout(tmp_path)
    layout.ensure_dirs()
    path = layout.runtime_db
    conn = sqlite3.connect(path)
    try:
        if incremental:
            conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        conn.execute("CREATE TABLE facts(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        conn.executemany("INSERT INTO facts(value) VALUES (?)", [(f"v{i}",) for i in range(20)])
        conn.commit()
    finally:
        conn.close()
    manager = ManifestManager(layout)
    manager.transition(ManifestState.V2_BUILDING, migration_id="p7-tests")
    manager.transition(ManifestState.V2_READY, source_digest="s", target_digest="t", manifest_digest="m", digests={"validator_passed": True, "checkpoints": {"fixture": True}})
    record = manager.transition(ManifestState.V2_ACTIVE)
    scope = MaintenanceScope(workspace_id=str(layout.workspace), runtime_role="maintenance", trusted_context=True)
    base = MaintenanceContext.trusted(scope, actor_id="test-actor", expected_generation=record.generation)
    store = MaintenanceStore(layout.workspace)
    lease = store.acquire_lease(base, ttl_seconds=300)
    context = MaintenanceContext.trusted(scope, actor_id="test-actor", maintenance_lease_id=lease.lease_id, expected_generation=record.generation)
    job = store.create_job(context, "compact", request_key="p7-test-job", dry_run=False, expected_generation=record.generation)
    store.transition_job(job.job_id, "READY", context)
    store.transition_job(job.job_id, "ACTIVE", context, expected_generation=record.generation, lease_id=lease.lease_id)
    return layout, path, store, context, job.job_id


def test_storage_report_is_read_only_and_complete(tmp_path: Path):
    layout, path, _store, _ctx, _job_id = _active_workspace(tmp_path)
    before = path.read_bytes()
    report = StorageReporter(layout, source_workspace=tmp_path).report(path, domain="runtime")
    assert report.readable and report.integrity_ok
    assert report.logical_pages + report.free_pages == report.derived_pages
    assert report.allocated_bytes == report.page_size * report.derived_pages
    assert report.row_counts["facts"] == 20
    assert report.schema_fingerprint and report.table_digests["facts"]
    assert path.read_bytes() == before


def test_report_rejects_missing_and_symlink(tmp_path: Path):
    layout = WorkspaceV2Layout(tmp_path)
    layout.ensure_dirs()
    with pytest.raises(StorageReportError):
        StorageReporter(layout, source_workspace=tmp_path).report(layout.runtime_db, domain="runtime")
    outside = tmp_path / "outside.db"
    sqlite3.connect(outside).close()
    try:
        layout.runtime_db.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(StorageReportError):
        StorageReporter(layout, source_workspace=tmp_path).report(layout.runtime_db, domain="runtime")


def test_auto_maintenance_defaults_to_zero_write(tmp_path: Path):
    layout, path, _store, _ctx, _job_id = _active_workspace(tmp_path)
    before = path.read_bytes()
    result = SQLiteMaintenanceExecutor(layout, source_workspace=tmp_path).auto_maintenance(path, domain="runtime")
    assert result.dry_run and result.status == "ok"
    assert path.read_bytes() == before


def test_auto_apply_is_only_passive_checkpoint_and_optimize(tmp_path: Path):
    layout, path, _store, _ctx, _job_id = _active_workspace(tmp_path)
    result = SQLiteMaintenanceExecutor(layout, source_workspace=tmp_path).auto_maintenance(path, domain="runtime", apply=True)
    assert result.applied and result.after.integrity_ok


def test_incremental_requires_active_cas_lease_and_evidence(tmp_path: Path):
    layout, path, store, ctx, job_id = _active_workspace(tmp_path, incremental=True)
    executor = SQLiteMaintenanceExecutor(layout, source_workspace=tmp_path, maintenance_store=store, quiescence_verifier=lambda **_: True, outbox_verifier=lambda **_: True)
    with pytest.raises((MaintenancePreconditionError, SQLiteMaintenanceError)):
        executor.incremental_vacuum(path, domain="runtime", context=ctx, apply=True)
    with pytest.raises(MaintenancePreconditionError):
        SQLiteMaintenanceExecutor(layout, source_workspace=tmp_path, maintenance_store=store).incremental_vacuum(path, domain="runtime", context=ctx, job_id=job_id, apply=True)
    with pytest.raises(MaintenancePreconditionError):
        executor.incremental_vacuum(path, domain="runtime", context=ctx, job_id=job_id, expected_generation=ctx.expected_generation + 1, apply=True)
    result = executor.incremental_vacuum(path, domain="runtime", context=ctx, job_id=job_id, apply=True)
    assert result.applied and result.after.integrity_ok


def test_incremental_rejects_non_incremental_database(tmp_path: Path):
    layout, path, store, ctx, job_id = _active_workspace(tmp_path, incremental=False)
    with pytest.raises(MaintenancePreconditionError):
        SQLiteMaintenanceExecutor(layout, source_workspace=tmp_path, maintenance_store=store, quiescence_verifier=lambda **_: True, outbox_verifier=lambda **_: True).incremental_vacuum(path, domain="runtime", context=ctx, job_id=job_id, apply=True)


def test_deep_compact_dry_run_and_atomic_fault_cleanup(tmp_path: Path):
    layout, path, store, ctx, job_id = _active_workspace(tmp_path)
    executor = SQLiteMaintenanceExecutor(layout, source_workspace=tmp_path, maintenance_store=store, quiescence_verifier=lambda **_: True, outbox_verifier=lambda **_: True)
    before = path.read_bytes()
    dry = executor.deep_compact(path, domain="runtime")
    assert dry.dry_run and path.read_bytes() == before
    with pytest.raises(MaintenanceFault):
        executor.deep_compact(path, domain="runtime", context=ctx, job_id=job_id, apply=True, fault_point="before_replace")
    assert path.read_bytes() == before
    assert not list(path.parent.glob(f".{path.name}.phase7-*.tmp"))
    assert not list(path.parent.glob(f".{path.name}.phase7-*.bak"))


def test_deep_compact_preserves_schema_rows_and_is_idempotent(tmp_path: Path):
    layout, path, store, ctx, job_id = _active_workspace(tmp_path)
    executor = SQLiteMaintenanceExecutor(layout, source_workspace=tmp_path, maintenance_store=store, quiescence_verifier=lambda **_: True, outbox_verifier=lambda **_: True)
    result = executor.deep_compact(path, domain="runtime", context=ctx, job_id=job_id, apply=True)
    assert result.applied and result.after.integrity_ok
    again = executor.deep_compact(path, domain="runtime", context=ctx, job_id=job_id, apply=True)
    assert again.after.row_counts == result.after.row_counts
    assert again.after.table_digests == result.after.table_digests


def test_deep_compact_restores_after_replace_fault(tmp_path: Path):
    layout, path, store, ctx, job_id = _active_workspace(tmp_path)
    executor = SQLiteMaintenanceExecutor(layout, source_workspace=tmp_path, maintenance_store=store, quiescence_verifier=lambda **_: True, outbox_verifier=lambda **_: True)
    before = path.read_bytes()
    with pytest.raises(MaintenanceFault):
        executor.deep_compact(path, domain="runtime", context=ctx, job_id=job_id, apply=True, fault_point="after_replace")
    assert path.read_bytes() == before
    assert not list(path.parent.glob(f".{path.name}.phase7-*.tmp"))
    assert not list(path.parent.glob(f".{path.name}.phase7-*.bak"))


def test_deep_compact_rejects_temp_swap_with_same_rows_and_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    layout, path, store, ctx, job_id = _active_workspace(tmp_path)
    executor = SQLiteMaintenanceExecutor(layout, source_workspace=tmp_path, maintenance_store=store, quiescence_verifier=lambda **_: True, outbox_verifier=lambda **_: True)
    before = path.read_bytes()
    real_replace = sqlite_maintenance_module.os.replace
    displaced = tmp_path / "displaced-compacted-temp.db"
    injected = False
    backup_failed = False
    attacker_report = None
    original_identity = None
    attacker_identity = None

    def swap_temp(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        nonlocal attacker_identity, attacker_report, injected, original_identity
        source = Path(src)
        if not injected and Path(dst) == path and source.name.endswith(".tmp") and ".phase7-" in source.name:
            injected = True
            real_replace(source, displaced)
            original_identity = displaced.stat()
            shutil.copy2(displaced, source)
            stat = source.stat()
            os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
            attacker_identity = source.stat()
            attacker_report = StorageReporter().report(source)
        real_replace(src, dst)

    monkeypatch.setattr(sqlite_maintenance_module.os, "replace", swap_temp)
    with pytest.raises(MaintenancePreconditionError, match="replacement target|temporary artifact"):
        executor.deep_compact(path, domain="runtime", context=ctx, job_id=job_id, apply=True)
    assert injected
    assert attacker_report is not None and attacker_report.integrity_ok
    assert attacker_report.row_counts["facts"] == 20 and attacker_report.table_digests["facts"]
    assert original_identity is not None and attacker_identity is not None
    assert original_identity.st_ino != attacker_identity.st_ino
    assert original_identity.st_mtime_ns != attacker_identity.st_mtime_ns
    assert path.read_bytes() == before
    assert not list(path.parent.glob(f".{path.name}.phase7-*.tmp"))
    assert not list(path.parent.glob(f".{path.name}.phase7-*.bak"))


def test_deep_compact_rejects_backup_swap_without_losing_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    layout, path, store, ctx, job_id = _active_workspace(tmp_path)
    executor = SQLiteMaintenanceExecutor(layout, source_workspace=tmp_path, maintenance_store=store, quiescence_verifier=lambda **_: True, outbox_verifier=lambda **_: True)
    real_replace = sqlite_maintenance_module.os.replace
    displaced = tmp_path / "displaced-original-backup.db"
    injected = False
    attacker_report = None
    original_identity = None
    attacker_identity = None

    def swap_backup(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        nonlocal attacker_identity, attacker_report, injected, original_identity
        source = Path(src)
        if not injected and Path(dst) == path and source.name.endswith(".bak") and not source.name.endswith((".wal.bak", ".shm.bak")):
            injected = True
            real_replace(source, displaced)
            original_identity = displaced.stat()
            shutil.copy2(displaced, source)
            stat = source.stat()
            os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
            attacker_identity = source.stat()
            attacker_report = StorageReporter().report(source)
        real_replace(src, dst)

    monkeypatch.setattr(sqlite_maintenance_module.os, "replace", swap_backup)
    with pytest.raises(MaintenancePreconditionError, match="recovery target|backup artifact"):
        executor.deep_compact(path, domain="runtime", context=ctx, job_id=job_id, apply=True, fault_point="after_replace")
    assert injected
    assert attacker_report is not None and attacker_report.integrity_ok
    assert attacker_report.row_counts["facts"] == 20 and attacker_report.table_digests["facts"]
    assert original_identity is not None and attacker_identity is not None
    assert original_identity.st_ino != attacker_identity.st_ino
    assert original_identity.st_mtime_ns != attacker_identity.st_mtime_ns
    # The compacted target remains present and logically intact while the
    # displaced original backup remains available for manual recovery.
    report = StorageReporter(layout, source_workspace=tmp_path).report(path, domain="runtime")
    assert report.integrity_ok and report.row_counts["facts"] == 20
    assert displaced.is_file()
    assert not list(path.parent.glob(f".{path.name}.phase7-*.tmp"))


def test_manifest_database_is_never_compacted(tmp_path: Path):
    layout, _path, store, ctx, job_id = _active_workspace(tmp_path)
    with pytest.raises(MaintenancePreconditionError):
        SQLiteMaintenanceExecutor(layout, source_workspace=tmp_path, maintenance_store=store, quiescence_verifier=lambda **_: True, outbox_verifier=lambda **_: True).deep_compact(layout.manifest_db, domain="system", context=ctx, job_id=job_id, apply=True)


def test_writable_lease_rejects_sidecar_create_delete_and_swap(tmp_path: Path):
    layout, path, _store, _ctx, _job_id = _active_workspace(tmp_path)
    sidecar = path.with_name(path.name + "-wal")

    lease = _SQLiteIdentityLease.open(path, readonly=False)
    try:
        sidecar.write_bytes(b"attacker")
        with pytest.raises(StorageReportError, match="sidecar identity"):
            lease.assert_current()
    finally:
        lease.close()
        sidecar.unlink(missing_ok=True)

    lease = _SQLiteIdentityLease.open(path, readonly=False)
    try:
        sidecar.write_bytes(b"attacker")
        lease.refresh_sidecars()
        sidecar.unlink()
        with pytest.raises(StorageReportError, match="sidecar identity"):
            lease.assert_current()
    finally:
        lease.close()

    sidecar.write_bytes(b"original")
    lease = _SQLiteIdentityLease.open(path, readonly=False)
    try:
        replacement = sidecar.with_name(sidecar.name + ".replacement")
        replacement.write_bytes(b"replacement")
        os.replace(replacement, sidecar)
        with pytest.raises(StorageReportError, match="sidecar identity"):
            lease.assert_current()
    finally:
        lease.close()
        sidecar.unlink(missing_ok=True)


def test_inode_zero_fails_closed_instead_of_accepting_same_volume_swap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    layout, path, _store, _ctx, _job_id = _active_workspace(tmp_path)
    real_identity = storage_report_module._path_identity

    def weak_identity(candidate: Path):
        identity = real_identity(candidate)
        if Path(candidate) == path:
            return (identity[0], 0, identity[2], identity[3])
        return identity

    monkeypatch.setattr(storage_report_module, "_path_identity", weak_identity)
    with pytest.raises(StorageReportError, match="cannot read SQLite storage report safely|identity is unavailable"):
        StorageReporter().report(path)


def test_recovery_swap_after_fingerprint_check_restores_known_good_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    layout, path, store, ctx, job_id = _active_workspace(tmp_path)
    executor = SQLiteMaintenanceExecutor(layout, source_workspace=tmp_path, maintenance_store=store, quiescence_verifier=lambda **_: True, outbox_verifier=lambda **_: True)
    before = path.read_bytes()
    real_replace = sqlite_maintenance_module.os.replace
    displaced = tmp_path / "displaced-recovery.db"
    injected = False
    backup_failed = False

    def swap_recovery(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        nonlocal injected, backup_failed
        source = Path(src)
        if not backup_failed and Path(dst) == path and source.name.endswith(".bak") and not source.name.endswith((".wal.bak", ".shm.bak")):
            backup_failed = True
            raise OSError("injected backup replace failure")
        if not injected and Path(dst) == path and source.name.endswith(".recovery"):
            injected = True
            real_replace(source, displaced)
            shutil.copy2(displaced, source)
            source.write_bytes(b"ATTACK")
        real_replace(src, dst)

    monkeypatch.setattr(sqlite_maintenance_module.os, "replace", swap_recovery)
    with pytest.raises(Exception):
        executor.deep_compact(path, domain="runtime", context=ctx, job_id=job_id, apply=True, fault_point="after_replace")
    assert injected
    assert backup_failed
    assert path.read_bytes() != b"ATTACK"


def test_cleanup_window_swap_fails_closed_and_keeps_recovery_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    layout, path, store, ctx, job_id = _active_workspace(tmp_path)
    executor = SQLiteMaintenanceExecutor(layout, source_workspace=tmp_path, maintenance_store=store, quiescence_verifier=lambda **_: True, outbox_verifier=lambda **_: True)
    real_report = executor.reporter.report
    calls = 0

    def swap_after_report(target: str | Path, *, domain: str | None = None):
        nonlocal calls
        result = real_report(target, domain=domain)
        calls += 1
        if Path(target) == path and calls == 2:
            attacker = tmp_path / "attacker-cleanup.db"
            sqlite3.connect(attacker).close()
            os.replace(attacker, path)
        return result

    monkeypatch.setattr(executor.reporter, "report", swap_after_report)
    with pytest.raises((MaintenancePreconditionError, SQLiteMaintenanceError)):
        executor.deep_compact(path, domain="runtime", context=ctx, job_id=job_id, apply=True)
    assert list(path.parent.glob(f".{path.name}.phase7-*.bak"))
