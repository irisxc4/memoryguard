from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest

from memoryguard.maintenance_v2 import (
    MaintenanceAuthorizationError,
    MaintenanceConflictError,
    MaintenanceContext,
    MaintenanceLeaseError,
    MaintenanceOperation,
    MaintenanceSchemaError,
    MaintenanceScope,
    MaintenanceStore,
)
from memoryguard.storage.layout import LayoutError, WorkspaceV2Layout
from memoryguard.system.manifest import ManifestManager, ManifestState


def _context(root: Path, *, actor: str = "tester") -> MaintenanceContext:
    return MaintenanceContext(
        MaintenanceScope(str(root.resolve()), share_group_id="shared", trusted_context=True),
        actor_id=actor,
        trusted_context=True,
    )


def test_fixed_path_and_readonly_no_create(tmp_path: Path):
    path = tmp_path / ".memoryguard" / "system" / "maintenance.db"
    with pytest.raises(FileNotFoundError):
        MaintenanceStore(tmp_path, readonly=True)
    assert not path.exists()
    store = MaintenanceStore(tmp_path)
    assert store.path == path
    with pytest.raises(Exception):
        MaintenanceStore(tmp_path, path=tmp_path / "outside.db")


def test_layout_requires_original_workspace_and_rejects_resolved_symlink(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "workspace-link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable")

    layout = WorkspaceV2Layout(link)
    with pytest.raises(LayoutError, match="source_workspace"):
        MaintenanceStore(layout)
    with pytest.raises(LayoutError, match="symlink|reparse"):
        MaintenanceStore(layout, source_workspace=link)

    safe_layout = WorkspaceV2Layout(real)
    store = MaintenanceStore(safe_layout, source_workspace=real)
    assert store.db_path.is_file()


def test_trusted_context_and_unknown_acl_are_fail_closed(tmp_path: Path):
    with pytest.raises(ValueError):
        MaintenanceScope(str(tmp_path), trusted_context=True, share_group_id="__UNKNOWN__")
    store = MaintenanceStore(tmp_path)
    with pytest.raises(MaintenanceAuthorizationError):
        store.create_job(object(), "audit", "r1")
    with pytest.raises(MaintenanceAuthorizationError):
        store.create_job({"workspace_id": str(tmp_path), "trusted_context": True}, "audit", "r2")
    with pytest.raises(ValueError):
        MaintenanceScope.from_value({"workspace": str(tmp_path), "wat": 1})


def test_job_idempotency_and_cas(tmp_path: Path):
    store = MaintenanceStore(tmp_path)
    context = _context(tmp_path)
    first = store.create_job(context, "audit", "same")
    assert store.create_job(context, "audit", "same").job_id == first.job_id
    with pytest.raises(MaintenanceConflictError):
        store.create_job(context, "report", "same")
    with pytest.raises(MaintenanceConflictError):
        store.create_job(context, "sweep", "blocked-before-active")
    auditing = store.transition_job(first.job_id, "AUDITING", context)
    assert auditing.state.value == "AUDITING"
    with pytest.raises(MaintenanceConflictError):
        store.transition_job(first.job_id, "ACTIVE", context)


def test_two_epoch_and_fk_integrity(tmp_path: Path):
    store = MaintenanceStore(tmp_path)
    context = _context(tmp_path)
    job = store.create_job(context, MaintenanceOperation.AUDIT, "epoch")
    store.transition_job(job.job_id, "AUDITING", context)
    first = store.begin_epoch(job.job_id, context)
    store.complete_epoch(first.epoch_id, context, reference_digest="refs-1")
    second = store.begin_epoch(job.job_id, context)
    assert second.epoch_number == 2
    candidate = store.mark_candidate(second.epoch_id, "blob-1", context, reference_digest="refs-2")
    assert candidate.blob_id == "blob-1"
    assert store.integrity()["ok"]


def test_lease_owner_and_expiry(tmp_path: Path):
    store = MaintenanceStore(tmp_path)
    context = _context(tmp_path)
    now = datetime.now(timezone.utc)
    lease = store.acquire_lease(context, ttl_seconds=10, now=now)
    assert lease.active
    with pytest.raises(MaintenanceLeaseError):
        store.acquire_lease(_context(tmp_path, actor="other"), ttl_seconds=10, now=now)
    expired = store.acquire_lease(context, ttl_seconds=10, now=now + timedelta(seconds=20))
    assert expired.lease_id != lease.lease_id


def test_future_schema_fails_closed(tmp_path: Path):
    store = MaintenanceStore(tmp_path)
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE schema_meta SET version=99, marker='future-maintenance'")
        conn.execute("PRAGMA user_version=99")
    with pytest.raises(MaintenanceSchemaError):
        MaintenanceStore(tmp_path, readonly=True)


def test_report_rejects_body_and_roundtrips(tmp_path: Path):
    store = MaintenanceStore(tmp_path)
    context = _context(tmp_path)
    job = store.create_job(context, "report", "report-1")
    with pytest.raises(ValueError):
        store.record_report(job.job_id, context, status="ok", counts={"body": "secret"})
    report = store.record_report(job.job_id, context, status="ok", counts={"candidates": 1}, safety={"integrity": "ok"})
    assert report.stable_digest


def test_dangling_workspace_symlink_is_rejected(tmp_path: Path):
    link = tmp_path / "dangling-workspace"
    try:
        link.symlink_to(tmp_path / "missing-target", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(LayoutError):
        MaintenanceStore(link)


def test_mutations_require_exact_job_owner_context(tmp_path: Path):
    store = MaintenanceStore(tmp_path)
    owner = _context(tmp_path, actor="owner")
    other = _context(tmp_path, actor="other")
    job = store.create_job(owner, "audit", "owner-bound")
    with pytest.raises(MaintenanceAuthorizationError):
        store.transition_job(job.job_id, "AUDITING", other)
    store.transition_job(job.job_id, "AUDITING", owner)
    with pytest.raises(MaintenanceAuthorizationError):
        store.begin_epoch(job.job_id, other)
    with pytest.raises(MaintenanceAuthorizationError):
        store.record_report(job.job_id, other, status="blocked")


def test_epoch_replay_digest_terminal_and_failed_job_are_immutable(tmp_path: Path):
    store = MaintenanceStore(tmp_path)
    context = _context(tmp_path)
    job = store.create_job(context, "audit", "immutable")
    store.transition_job(job.job_id, "AUDITING", context)
    epoch = store.begin_epoch(job.job_id, context, epoch_number=1, reference_digest="ref-1")
    with pytest.raises(MaintenanceConflictError):
        store.begin_epoch(job.job_id, context, epoch_number=1, reference_digest="ref-2")
    store.complete_epoch(epoch.epoch_id, context, reference_digest="ref-1")
    with pytest.raises(MaintenanceConflictError):
        store.mark_candidate(epoch.epoch_id, "blob", context, reference_digest="ref-1")
    failed_job = store.create_job(context, "audit", "failed-terminal")
    store.transition_job(failed_job.job_id, "AUDITING", context)
    store.transition_job(failed_job.job_id, "FAILED", context)
    with pytest.raises(MaintenanceConflictError):
        store.transition_job(failed_job.job_id, "AUDITING", context)
    with pytest.raises(MaintenanceAuthorizationError):
        store.transition_job(failed_job.job_id, "FAILED", _context(tmp_path, actor="intruder"))
    assert store.transition_job(failed_job.job_id, "FAILED", context).state.value == "FAILED"


def test_lease_owner_cannot_be_overridden_and_metadata_is_bounded(tmp_path: Path):
    store = MaintenanceStore(tmp_path)
    context = _context(tmp_path)
    with pytest.raises(MaintenanceLeaseError):
        store.acquire_lease(context, owner_id="other")
    job = store.create_job(context, "report", "bounded")
    with pytest.raises(ValueError):
        store.record_report(job.job_id, context, status="bad", safety={"control_payload": {}})
    with pytest.raises(ValueError):
        store.record_report(job.job_id, context, status="deep", safety={"x": {"x": {"x": {"x": {"x": {"x": {"x": {"x": {"x": 1}}}}}}}}})


def test_schema_unknown_and_malformed_json_fail_closed(tmp_path: Path):
    store = MaintenanceStore(tmp_path)
    context = _context(tmp_path)
    job = store.create_job(context, "report", "malformed-json")
    report = store.record_report(job.job_id, context, status="ok", counts={"items": 1})
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE reports SET safety_json=? WHERE report_id=?", ("{broken", report.report_id))
    with pytest.raises(MaintenanceSchemaError):
        store.get_report(report.report_id)
    with sqlite3.connect(store.path) as conn:
        conn.execute("CREATE TABLE unexpected (value TEXT)")
    with pytest.raises(MaintenanceSchemaError):
        MaintenanceStore(tmp_path, readonly=True)


def test_writable_store_reopen_is_idempotent(tmp_path: Path):
    first = MaintenanceStore(tmp_path)
    before = first.db_path.read_bytes()
    second = MaintenanceStore(tmp_path)
    assert second.integrity()["ok"]
    assert second.db_path.read_bytes() == before


def test_schema_constraint_tamper_is_rejected(tmp_path: Path):
    store = MaintenanceStore(tmp_path)
    with sqlite3.connect(store.db_path) as conn:
        version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute(
            "UPDATE sqlite_master SET sql=replace(sql, ?, ?) WHERE name='candidates'",
            ("'CONFIRMED','DELETING','BLOCKED'", "'CONFIRMED','BLOCKED'"),
        )
        conn.execute(f"PRAGMA schema_version={version + 1}")
        conn.execute("PRAGMA writable_schema=OFF")
        conn.commit()
    with pytest.raises(MaintenanceSchemaError, match="definition mismatch"):
        MaintenanceStore(tmp_path, readonly=True)
