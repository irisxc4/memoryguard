from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from memoryguard.migration.workspace_prepare import (
    WorkspaceCASConflict,
    WorkspacePrepareError,
    prepare_v2_workspace,
    verify_v2_source_snapshot,
)
from memoryguard.storage.layout import WorkspaceV2Layout
from memoryguard.system.manifest import ManifestError, ManifestManager, ManifestState


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "prepare_v2_workspace.py"


def _phase2_fixture(root: Path) -> None:
    """Reuse the established Phase-2 source fixture without creating V2 DBs."""

    sys.path.insert(0, str(Path(__file__).parent))
    from test_v2_phase2_integration import _fixture

    _fixture(root)


def test_default_dry_run_is_zero_write(tmp_path: Path) -> None:
    workspace = tmp_path / "new-workspace"
    result = prepare_v2_workspace(workspace)
    assert result["status"] == "DRY_RUN"
    assert result["ok"] is True
    assert result["readiness_eligible"] is False
    assert result["plan"]["writes_performed"] is False
    assert not workspace.exists()

    cli = subprocess.run(
        [sys.executable, str(SCRIPT), "--workspace", str(workspace)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert cli.returncode == 0
    assert json.loads(cli.stdout)["status"] == "DRY_RUN"
    assert not workspace.exists()


def test_apply_builds_all_targets_and_stays_building(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    result = prepare_v2_workspace(workspace, apply=True)
    layout = WorkspaceV2Layout(workspace)
    assert result["status"] == "V2_BUILDING"
    assert result["ok"] is True
    assert result["readiness_eligible"] is False
    assert ManifestManager(layout).current().state is ManifestState.V2_BUILDING
    assert all(path.is_file() for path in layout.all_db_paths)
    assert (layout.root / "skills" / "skills.db").is_file()
    assert (layout.system / "maintenance.db").is_file()
    auxiliary = ManifestManager(layout).current().checkpoints["v2_auxiliary_initialized"]
    assert auxiliary["status"] == "READY"
    assert auxiliary["skills"]["schema_version"] >= 1
    assert auxiliary["maintenance"]["schema_version"] >= 1
    assert list((layout.root / "migration-backups").rglob("prepare-plan.json"))
    assert result["plan"]["manifest_state"] != "V2_ACTIVE"


def test_apply_resume_is_idempotent_and_reuses_backup(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    first = prepare_v2_workspace(workspace, apply=True)
    second = prepare_v2_workspace(workspace, apply=True)
    assert first["plan"]["migration_id"] == second["plan"]["migration_id"]
    assert second["status"] == "V2_BUILDING"
    assert second["ok"] is True
    first_keys = {item["key"] for item in first["backups"]}
    assert all(item["action"] == "reused" for item in second["backups"] if item["key"] in first_keys)
    assert ManifestManager(workspace).current().state is ManifestState.V2_BUILDING


def test_v1_active_historical_batch_id_is_never_reused_for_new_apply(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    manager = ManifestManager(workspace)
    building = manager.begin(migration_id="historical-failed-batch")
    rolled_back = manager.fail(
        error="fixture failure",
        migration_id=building.migration_id,
        expected_generation=building.generation,
    )
    assert rolled_back.state is ManifestState.V1_ACTIVE
    assert rolled_back.migration_id == "historical-failed-batch"

    result = prepare_v2_workspace(workspace, apply=True)
    assert result["ok"] is True, result
    new_id = result["plan"]["migration_id"]
    assert new_id.startswith("prepare-")
    assert new_id != "historical-failed-batch"
    current = ManifestManager(workspace).current()
    assert current.state is ManifestState.V2_BUILDING
    assert current.migration_id == new_id


def test_explicit_historical_batch_id_is_rejected_from_v1_active(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    manager = ManifestManager(workspace)
    building = manager.begin(migration_id="historical-failed-batch")
    manager.fail(
        error="fixture failure",
        migration_id=building.migration_id,
        expected_generation=building.generation,
    )

    with pytest.raises(WorkspacePrepareError, match="historical migration_id"):
        prepare_v2_workspace(
            workspace,
            apply=True,
            migration_id="historical-failed-batch",
        )
    current = ManifestManager(workspace).current()
    assert current.state is ManifestState.V1_ACTIVE
    assert current.migration_id == "historical-failed-batch"


def test_generation_cas_conflict_fails_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    prepare_v2_workspace(workspace, apply=True)
    before = {path: path.read_bytes() for path in WorkspaceV2Layout(workspace).all_db_paths}
    with pytest.raises(WorkspaceCASConflict):
        prepare_v2_workspace(workspace, apply=True, expected_generation=0)
    after = {path: path.read_bytes() for path in WorkspaceV2Layout(workspace).all_db_paths}
    assert before == after
    assert ManifestManager(workspace).current().state is ManifestState.V2_BUILDING


def test_migration_failure_keeps_building_and_leaves_artifacts(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    result = prepare_v2_workspace(workspace, apply=True, fail_at="rules_migrated")
    record = ManifestManager(workspace).current()
    assert result["status"] == "FAILED"
    assert result["ok"] is False
    assert record.state is ManifestState.V2_BUILDING
    assert "phase2_failed" in record.checkpoints
    assert all(path.is_file() for path in WorkspaceV2Layout(workspace).all_db_paths)
    assert result["readiness_eligible"] is False


def test_failed_batch_resumes_same_id_after_fault_is_removed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    failed = prepare_v2_workspace(workspace, apply=True, fail_at="rules_migrated")
    resumed = prepare_v2_workspace(workspace, apply=True)
    assert failed["plan"]["migration_id"] == resumed["plan"]["migration_id"]
    assert resumed["status"] == "V2_BUILDING"
    assert resumed["ok"] is True
    assert ManifestManager(workspace).current().state is ManifestState.V2_BUILDING


def test_resume_checkpoint_attempts_preserve_failure_and_promote_latest_success(tmp_path: Path) -> None:
    manager = ManifestManager(tmp_path)
    manager.begin(migration_id="resume-batch")
    failed = manager.record_checkpoint_attempt(
        {"memory_migrated": {"status": "failed", "error": "old bytes"}},
        migration_id="resume-batch", expected_generation=1,
    )
    old_failure = failed.checkpoints["memory_migrated"]
    corrected = manager.record_checkpoint_attempt(
        {"memory_migrated": {"status": "ok", "counts": {"atoms": 3}}},
        migration_id="resume-batch", expected_generation=1,
    )
    assert corrected.state is ManifestState.V2_BUILDING
    assert corrected.generation == 1
    assert corrected.checkpoints["memory_migrated"]["status"] == "ok"
    assert any(
        item["checkpoint"] == "memory_migrated" and item["result"] == old_failure
        for item in corrected.checkpoints["_history"]
    )
    attempts = [item for item in corrected.checkpoints["_attempts"] if item["checkpoint"] == "memory_migrated"]
    assert [item["sequence"] for item in attempts] == [1, 2]
    assert corrected.checkpoints["_authoritative"]["memory_migrated"]["attempt_id"] == attempts[-1]["attempt_id"]

    replay = manager.record_checkpoint_attempt(
        {"memory_migrated": {"status": "ok", "counts": {"atoms": 3}}},
        migration_id="resume-batch", expected_generation=1,
    )
    assert replay.checkpoints == corrected.checkpoints
    with pytest.raises(ManifestError, match="generation conflict"):
        manager.record_checkpoint_attempt(
            {"rules_migrated": {"status": "ok"}},
            migration_id="resume-batch", expected_generation=0,
        )


def test_resume_attempt_sequence_is_append_only_across_manager_restart(tmp_path: Path) -> None:
    first = ManifestManager(tmp_path)
    first.begin(migration_id="concurrent-batch")
    first.record_checkpoint_attempt(
        {"phase2_failed": {"status": "failed", "error": "transient"}},
        migration_id="concurrent-batch", expected_generation=1,
    )
    second = ManifestManager(tmp_path)
    second.record_checkpoint_attempt(
        {"phase2_failed": {"status": "ok"}},
        migration_id="concurrent-batch", expected_generation=1,
    )
    record = ManifestManager(tmp_path).current()
    attempts = record.checkpoints["_attempts"]
    assert [item["sequence"] for item in attempts] == [1, 2]
    assert record.checkpoints["phase2_failed"]["status"] == "ok"
    assert record.state is ManifestState.V2_BUILDING and record.generation == 1


def test_source_hash_drift_blocks_resume_and_legacy_bytes_remain(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _phase2_fixture(workspace)
    source = workspace / ".memoryguard" / "shared-memory" / "g1" / "memory.db"
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    first = prepare_v2_workspace(workspace, apply=True, data_home=workspace / "data")
    assert first["status"] == "V2_BUILDING"
    with sqlite3.connect(source) as conn:
        conn.execute("UPDATE records SET body=body || ' drift', updated_at='drifted' WHERE memory_id=(SELECT memory_id FROM records ORDER BY memory_id LIMIT 1)")
        conn.commit()
    drifted = hashlib.sha256(source.read_bytes()).hexdigest()
    second = prepare_v2_workspace(workspace, apply=True, data_home=workspace / "data")
    assert second["status"] == "FAILED"
    assert ManifestManager(workspace).current().state is ManifestState.V2_BUILDING
    assert any(item["kind"] == "source_drift" for item in second["failures"])
    assert second["live_source_verification"]["status"] == "DRIFT"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == drifted
    assert before != drifted


def test_nonempty_wal_is_frozen_by_online_backup_and_migrated(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _phase2_fixture(workspace)
    source = workspace / ".memoryguard" / "shared-memory" / "g1" / "memory.db"
    conn = sqlite3.connect(source)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        memory_id = str(conn.execute("SELECT memory_id FROM records ORDER BY memory_id LIMIT 1").fetchone()[0])
        conn.execute("UPDATE records SET body='wal committed body', updated_at='wal-commit' WHERE memory_id=?", (memory_id,))
        conn.commit()
        wal = Path(str(source) + "-wal")
        assert wal.is_file() and wal.stat().st_size > 0

        result = prepare_v2_workspace(workspace, apply=True, data_home=workspace / "data")
        assert result["status"] == "V2_BUILDING", result
        assert result["ok"] is True
        assert result["live_source_verification"]["status"] == "PASS"
        snapshot = Path(result["source_snapshot"]["workspace"]) / ".memoryguard" / "shared-memory" / "g1" / "memory.db"
        assert snapshot.is_file()
        assert not Path(str(snapshot) + "-wal").exists()
        with sqlite3.connect(snapshot) as frozen:
            assert frozen.execute("SELECT body FROM records WHERE memory_id=?", (memory_id,)).fetchone()[0] == "wal committed body"
        layout = WorkspaceV2Layout(workspace)
        with sqlite3.connect(layout.memory_db) as v2:
            assert v2.execute("SELECT body FROM atoms WHERE memory_id=?", (memory_id,)).fetchone()[0] == "wal committed body"
        verified = verify_v2_source_snapshot(workspace, data_home=workspace / "data")
        assert verified["status"] == "PASS"
        assert verified["activation_safe"] is True
    finally:
        conn.close()


def test_preexisting_dirty_shadow_is_archived_before_fresh_build(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _phase2_fixture(workspace)
    layout = WorkspaceV2Layout(workspace)
    layout.memory.mkdir(parents=True, exist_ok=True)
    layout.memory_db.write_bytes(b"dirty-old-shadow")
    stale_hash = hashlib.sha256(layout.memory_db.read_bytes()).hexdigest()

    result = prepare_v2_workspace(workspace, apply=True, data_home=workspace / "data")
    assert result["status"] == "V2_BUILDING", result
    assert result["ok"] is True
    archived = [item for item in result["archived_shadow"] if item["domain"] == "memory"]
    assert len(archived) == 1
    archived_db = Path(archived[0]["archive"]) / "memory.db"
    assert hashlib.sha256(archived_db.read_bytes()).hexdigest() == stale_hash
    assert layout.memory_db.is_file()
    assert layout.memory_db.read_bytes() != b"dirty-old-shadow"
    with sqlite3.connect(layout.memory_db) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_activation_snapshot_verifier_detects_logical_live_drift(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _phase2_fixture(workspace)
    source = workspace / ".memoryguard" / "shared-memory" / "g1" / "memory.db"
    built = prepare_v2_workspace(workspace, apply=True, data_home=workspace / "data")
    assert built["ok"] is True
    assert verify_v2_source_snapshot(workspace, data_home=workspace / "data")["activation_safe"] is True
    with sqlite3.connect(source) as conn:
        conn.execute("UPDATE records SET body=body || ' later-change', updated_at='later' WHERE memory_id=(SELECT memory_id FROM records ORDER BY memory_id LIMIT 1)")
        conn.commit()
    verification = verify_v2_source_snapshot(workspace, data_home=workspace / "data")
    assert verification["status"] == "DRIFT"
    assert verification["activation_safe"] is False
    assert verification["changed"]


def _mark_prepared_workspace_ready(workspace: Path):
    manager = ManifestManager(workspace)
    building = manager.current()
    return manager.mark_v2_ready(
        migration_id=building.migration_id,
        source_digest="source-ready",
        target_digest="target-ready",
        manifest_digest="manifest-ready",
        digests={"validator_passed": True, "checkpoints": dict(building.checkpoints)},
        expected_generation=building.generation,
    )


def test_frozen_build_cannot_bypass_fresh_ready_verification(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _phase2_fixture(workspace)
    built = prepare_v2_workspace(workspace, apply=True, data_home=workspace / "data")
    assert built["ok"] is True
    manager = ManifestManager(workspace)
    building = manager.current()
    with pytest.raises(ManifestError, match="fresh process-issued live source verification"):
        manager.transition(
            ManifestState.V2_READY,
            migration_id=building.migration_id,
            source_digest="source-ready",
            target_digest="target-ready",
            manifest_digest="manifest-ready",
            digests={"validator_passed": True, "checkpoints": dict(building.checkpoints)},
            expected_generation=building.generation,
        )
    assert manager.current().state is ManifestState.V2_BUILDING


def test_frozen_ready_cannot_bypass_fresh_activation_verification(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _phase2_fixture(workspace)
    built = prepare_v2_workspace(workspace, apply=True, data_home=workspace / "data")
    assert built["ok"] is True
    ready = _mark_prepared_workspace_ready(workspace)
    assert ready.state is ManifestState.V2_READY
    manager = ManifestManager(workspace)
    with pytest.raises(ManifestError, match="fresh process-issued live source verification"):
        manager.transition(ManifestState.V2_ACTIVE, expected_generation=ready.generation)
    active = manager.activate_v2(expected_generation=ready.generation)
    assert active.state is ManifestState.V2_ACTIVE
    assert active.errors["activation_source_verification"]["status"] == "PASS"


def test_activation_rechecks_live_v1_after_ready_and_blocks_drift(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _phase2_fixture(workspace)
    source = workspace / ".memoryguard" / "shared-memory" / "g1" / "memory.db"
    built = prepare_v2_workspace(workspace, apply=True, data_home=workspace / "data")
    assert built["ok"] is True
    ready = _mark_prepared_workspace_ready(workspace)
    with sqlite3.connect(source) as conn:
        conn.execute("UPDATE records SET body=body || ' out-of-band', updated_at='after-ready' WHERE memory_id=(SELECT memory_id FROM records ORDER BY memory_id LIMIT 1)")
        conn.commit()
    manager = ManifestManager(workspace)
    with pytest.raises(ManifestError, match="live V1 source drift"):
        manager.activate_v2(expected_generation=ready.generation)
    assert manager.current().state is ManifestState.V2_READY


def test_symlink_workspace_rejected_before_any_write(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    with pytest.raises(WorkspacePrepareError, match="symlink|reparse"):
        prepare_v2_workspace(link, apply=True)
    assert not (real / ".memoryguard").exists()
