"""Phase 1 migration safety contract tests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from memoryguard.migration import (
    JsonManifestStore,
    MigrationCoordinator,
    MigrationError,
    MigrationReadError,
    MigrationState,
    MigrationValidator,
    PathContainmentError,
    V1Reader,
)
from memoryguard.migration.framework import _sqlite_inventory


def _make_db(path: Path, *, user_version: int = 1, rows: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version={int(user_version)}")
    conn.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, scope TEXT, body TEXT)")
    for index in range(rows):
        conn.execute("INSERT INTO records(scope, body) VALUES (?, ?)", ("agent-a", f"body-{index}"))
    conn.commit()
    conn.close()


def _make_v1(root: Path) -> list[Path]:
    paths = [
        root / ".memoryguard" / "shared-memory" / "group-a" / "memory.db",
        root / ".memoryguard" / "rule-intelligence" / "memory.db",
        root / ".memoryguard" / "history" / "history.sqlite",
        root / "knowledge" / "knowledge.db",
        root / "global" / "knowledge" / "knowledge.db",
    ]
    for path in paths:
        _make_db(path)
    manifest = root / ".memoryguard" / "native_releases" / "release-1" / "manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"schema_version": 1, "scope": "agent-a"}), encoding="utf-8")
    return paths + [manifest]


def _reader(root: Path) -> V1Reader:
    return V1Reader(root, data_home=root / "global")


def _json_store(root: Path) -> JsonManifestStore:
    return JsonManifestStore(root / "v2" / "migration.json")


def _hashes(paths: list[Path]) -> dict[Path, str]:
    return {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def test_v1_inventory_is_read_only_and_hashes_are_unchanged(tmp_path: Path) -> None:
    paths = _make_v1(tmp_path)
    before = _hashes(paths)

    snapshot = _reader(tmp_path).read(strict=True)

    assert snapshot.ok
    assert {item.domain for item in snapshot.items} >= {
        "shared_memory",
        "rule_intelligence",
        "conversation_history",
        "knowledge",
        "manifests",
    }
    assert _hashes(paths) == before
    assert not any(path.name in {"memory.db", "history.sqlite"} for path in (tmp_path / ".memoryguard").glob("*.db"))


def test_missing_read_only_paths_fail_closed_without_creating_a_database(tmp_path: Path) -> None:
    reader = _reader(tmp_path)
    assert list(tmp_path.rglob("*.db")) == []
    snapshot = reader.scan(strict=False)
    assert any(error.code == "missing_required_source" for error in snapshot.errors)
    with pytest.raises(MigrationReadError, match="failed closed"):
        reader.read()
    assert list(tmp_path.rglob("*.db")) == []


def test_corrupt_sqlite_is_explained_and_not_skipped(tmp_path: Path) -> None:
    paths = _make_v1(tmp_path)
    corrupt = paths[2]
    corrupt.write_bytes(b"not a sqlite database")

    snapshot = _reader(tmp_path).scan(strict=False)

    errors = [error for error in snapshot.errors if error.path == str(corrupt.resolve())]
    assert errors
    assert any(error.code in {"sqlite_read_error", "integrity_check_failed"} for error in errors)
    item = next(item for item in snapshot.items if item.path == str(corrupt.resolve()))
    assert item.sha256 == hashlib.sha256(corrupt.read_bytes()).hexdigest()
    with pytest.raises(MigrationReadError, match="conversation_history"):
        _reader(tmp_path).read()


def test_coordinator_failure_rolls_back_and_preserves_checkpoint(tmp_path: Path) -> None:
    paths = _make_v1(tmp_path)
    before = _hashes(paths)
    coordinator = MigrationCoordinator(
        tmp_path,
        v2_root=tmp_path / "v2",
        manifest_store=_json_store(tmp_path),
        data_home=tmp_path / "global",
        fail_at="inventory_complete",
    )

    with pytest.raises(RuntimeError, match="injected migration failure"):
        coordinator.prepare()

    assert coordinator.state is MigrationState.V1_ACTIVE
    assert coordinator.failure_reason
    assert coordinator.checkpoint["step"] == "prepare_failed"
    assert _hashes(paths) == before


def test_repeated_prepare_is_idempotent(tmp_path: Path) -> None:
    _make_v1(tmp_path)
    coordinator = MigrationCoordinator(
        tmp_path,
        v2_root=tmp_path / "v2",
        manifest_store=_json_store(tmp_path),
        data_home=tmp_path / "global",
    )
    first = coordinator.prepare()
    checkpoint = dict(coordinator.checkpoint)

    second = coordinator.prepare()

    assert first.inventory_digest == second.inventory_digest
    assert coordinator.state is MigrationState.V2_BUILDING
    assert coordinator.checkpoint == checkpoint


def test_validator_does_not_report_unmigrated_data_as_zero_loss(tmp_path: Path) -> None:
    _make_v1(tmp_path)
    source = _reader(tmp_path).read(strict=True)
    result = MigrationValidator(tmp_path / "v2").validate(source)

    assert result.status == "NOT_EVALUATED"
    assert not result.ok
    assert result.loss == "NOT_EVALUATED"
    assert result.orphan == "NOT_EVALUATED"
    assert result.migration_loss is None
    assert result.loss_metrics["reason"] == "phase1_conversion_not_implemented"


def test_state_machine_requires_ready_and_allows_explicit_active_rollback(tmp_path: Path) -> None:
    _make_v1(tmp_path)
    coordinator = MigrationCoordinator(
        tmp_path,
        v2_root=tmp_path / "v2",
        manifest_store=_json_store(tmp_path),
        data_home=tmp_path / "global",
    )
    coordinator.prepare()
    with pytest.raises(Exception):
        coordinator.activate()

    validation = SimpleNamespace(
        status="PASS",
        can_promote=True,
        to_dict=lambda: {"status": "PASS", "loss_metrics": {"status": "PASS"}},
    )
    coordinator.mark_ready(validation)
    assert coordinator.state is MigrationState.V2_READY
    coordinator.activate()
    assert coordinator.state is MigrationState.V2_ACTIVE
    coordinator.rollback("operator requested rollback")
    assert coordinator.state is MigrationState.V1_ACTIVE


def test_custom_path_outside_allowed_root_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"outside-{tmp_path.name}"
    with pytest.raises(PathContainmentError):
        V1Reader(tmp_path, legacy_paths={"conversation_history": [outside / "history.sqlite"]})


def test_default_system_manifest_receives_ready_evidence_and_active_inherits_it(tmp_path: Path) -> None:
    _make_v1(tmp_path)
    coordinator = MigrationCoordinator(tmp_path, data_home=tmp_path / "global")
    coordinator.prepare()
    validation = SimpleNamespace(
        status="PASS",
        can_promote=True,
        to_dict=lambda: {"status": "PASS", "loss_metrics": {"status": "PASS"}},
    )
    coordinator.mark_ready(validation)

    from memoryguard.system.manifest import ManifestManager, ManifestState

    manager = ManifestManager(tmp_path)
    ready = manager.current()
    assert ready.state is ManifestState.V2_READY
    assert ready.source_digest
    assert ready.target_digest
    assert ready.manifest_digest
    assert ready.digests.get("validator_passed") is True
    assert ready.digests.get("checkpoints")
    migration_id = ready.migration_id
    digest_triplet = (ready.source_digest, ready.target_digest, ready.manifest_digest)

    coordinator.activate()
    active = manager.current()
    assert active.state is ManifestState.V2_ACTIVE
    assert active.migration_id == migration_id
    assert (active.source_digest, active.target_digest, active.manifest_digest) == digest_triplet


def test_system_checkpoint_and_inventory_survive_coordinator_restart(tmp_path: Path) -> None:
    _make_v1(tmp_path)
    first = MigrationCoordinator(tmp_path, data_home=tmp_path / "global")
    snapshot = first.prepare()

    from memoryguard.system.manifest import ManifestManager, ManifestState

    record = ManifestManager(tmp_path).current()
    assert record.state is ManifestState.V2_BUILDING
    assert "state_entered_building" in record.checkpoints
    assert "inventory_complete" in record.checkpoints
    assert record.checkpoints["inventory_complete"]["inventory_digest"] == snapshot.inventory_digest

    restarted = MigrationCoordinator(tmp_path, data_home=tmp_path / "global")
    assert restarted.state is MigrationState.V2_BUILDING
    resumed = restarted.prepare()
    assert resumed.inventory_digest == snapshot.inventory_digest


def test_failed_checkpoint_reason_and_inventory_survive_new_coordinator(tmp_path: Path) -> None:
    _make_v1(tmp_path)
    first = MigrationCoordinator(
        tmp_path,
        data_home=tmp_path / "global",
        fail_at="inventory_complete",
    )
    with pytest.raises(RuntimeError, match="injected migration failure"):
        first.prepare()

    from memoryguard.system.manifest import ManifestManager, ManifestState

    failed = ManifestManager(tmp_path).current()
    assert failed.state is ManifestState.V1_ACTIVE
    assert failed.last_error
    assert failed.checkpoints["prepare_failed"]["step"] == "prepare_failed"
    inventory_digest = failed.checkpoints["prepare_failed"]["inventory_digest"]
    assert inventory_digest

    restarted = MigrationCoordinator(tmp_path, data_home=tmp_path / "global")
    assert restarted.state is MigrationState.V1_ACTIVE
    assert restarted.failure_reason == failed.last_error
    assert restarted.checkpoint["step"] == "prepare_failed"
    assert restarted.checkpoint["inventory_digest"] == inventory_digest
    assert restarted.inventory_snapshot is not None
    assert restarted.inventory_snapshot.inventory_digest == inventory_digest

    restarted.prepare()
    assert restarted.migration_id != failed.migration_id


def test_datahome_pointer_is_explicit_and_missing_global_is_not_configured(tmp_path: Path) -> None:
    _make_v1(tmp_path)
    configured = _reader(tmp_path)
    assert configured.workspace_source_pointer == str(tmp_path.resolve())
    assert configured.global_source_pointer == str((tmp_path / "global" / "knowledge" / "knowledge.db").resolve())
    assert configured.data_home_root == str((tmp_path / "global").resolve())
    snapshot = configured.read(strict=True)
    assert snapshot.ok
    assert snapshot.global_source_pointer == configured.global_source_pointer

    missing = V1Reader(tmp_path)
    missing_snapshot = missing.scan(strict=False)
    assert missing.global_source_pointer == "NOT_CONFIGURED"
    assert missing.data_home_root == "NOT_CONFIGURED"
    assert any(error.code == "NOT_CONFIGURED" for error in missing_snapshot.errors)
    assert any(item.path == "<global_source_pointer:NOT_CONFIGURED>" for item in missing_snapshot.items)
    assert not any(str(tmp_path / "global") in item.path for item in missing_snapshot.items)


def test_custom_v2_root_cannot_select_json_manifest_without_explicit_store(tmp_path: Path) -> None:
    _make_v1(tmp_path)
    with pytest.raises(MigrationError, match="canonical WorkspaceV2Layout"):
        MigrationCoordinator(tmp_path, v2_root=tmp_path / "custom-v2", data_home=tmp_path / "global")

    injected = MigrationCoordinator(
        tmp_path,
        v2_root=tmp_path / "custom-v2",
        data_home=tmp_path / "global",
        manifest_store=JsonManifestStore(tmp_path / "custom-v2" / "test.json"),
    )
    injected.prepare()
    assert (tmp_path / "custom-v2" / "test.json").is_file()


def test_activation_checkpoint_is_persisted_before_active_and_survives_restart(tmp_path: Path) -> None:
    _make_v1(tmp_path)
    coordinator = MigrationCoordinator(tmp_path, data_home=tmp_path / "global")
    coordinator.prepare()
    validation = SimpleNamespace(
        status="PASS",
        can_promote=True,
        to_dict=lambda: {"status": "PASS", "loss_metrics": {"status": "PASS"}},
    )
    coordinator.mark_ready(validation)
    migration_id = coordinator.migration_id
    coordinator.activate()

    restarted = MigrationCoordinator(tmp_path, data_home=tmp_path / "global")
    assert restarted.state is MigrationState.V2_ACTIVE
    assert restarted.migration_id == migration_id
    assert restarted.checkpoint["step"] == "v2_active_recorded"
    assert restarted.checkpoint["target_state"] == MigrationState.V2_ACTIVE.value


def test_without_rowid_digest_is_stable_across_insertion_order(tmp_path: Path) -> None:
    def make(path: Path, rows: list[tuple[int, str]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA user_version=1")
        conn.execute("CREATE TABLE records (id INTEGER, body TEXT, PRIMARY KEY(id, body)) WITHOUT ROWID")
        conn.executemany("INSERT INTO records(id, body) VALUES (?, ?)", rows)
        conn.commit()
        conn.close()

    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    make(first, [(2, "b"), (1, "a"), (3, "c")])
    make(second, [(3, "c"), (1, "a"), (2, "b")])
    first_metrics, first_errors = _sqlite_inventory(first)
    second_metrics, second_errors = _sqlite_inventory(second)
    assert not first_errors
    assert not second_errors
    assert first_metrics["content_digest"] == second_metrics["content_digest"]
