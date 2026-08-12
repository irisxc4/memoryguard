from __future__ import annotations

from pathlib import Path

from memoryguard.migration.backup_cleanup import cleanup_migration_backups
from memoryguard.migration.upgrade import run_upgrade
from memoryguard.migration.workspace_prepare import prepare_v2_workspace
from memoryguard.system.manifest import ManifestManager, ManifestState


def test_cleanup_rejects_path_escape_and_leaves_outside_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    backup_root = workspace / ".memoryguard" / "migration-backups"
    backup_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_text("keep", encoding="utf-8")

    result = cleanup_migration_backups(workspace, "../outside")

    assert result["ok"] is False
    assert result["cleanup_warning"] is True
    assert result["remaining"] == ["../outside"]
    assert outside.read_text(encoding="utf-8") == "keep"


def test_failed_prepare_retains_recovery_evidence(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"

    result = prepare_v2_workspace(workspace, apply=True, fail_at="rules_migrated")

    assert result["status"] == "FAILED"
    assert ManifestManager(workspace).current().state is ManifestState.V2_BUILDING
    migration_id = result["plan"]["migration_id"]
    backup_dir = workspace / ".memoryguard" / "migration-backups" / migration_id
    assert backup_dir.is_dir()
    assert (backup_dir / "prepare-plan.json").is_file()


def test_active_upgrade_cleans_only_its_successful_batch_and_reports_noop_replay(
    tmp_path: Path,
) -> None:
    # Reuse the established minimal V1 fixture and public upgrade path.
    from test_v2_public_upgrade import _legacy_0_6_2_fixture

    workspace = tmp_path / "workspace"
    _legacy_0_6_2_fixture(workspace)

    ready = run_upgrade(workspace, data_home=workspace, apply=True)
    assert ready["status"] == "V2_READY"
    migration_id = ready["migration_id"]
    backup_dir = workspace / ".memoryguard" / "migration-backups" / migration_id
    assert backup_dir.is_dir()

    active = run_upgrade(workspace, data_home=workspace, apply=True, confirm="V2_ACTIVE")
    assert active["status"] == "V2_ACTIVE"
    assert active["ok"] is True
    assert active["cleanup"]["status"] == "CLEANED"
    assert active["cleanup"]["remaining"] == []
    assert not backup_dir.exists()

    replay = run_upgrade(workspace, data_home=workspace, apply=True)
    assert replay["status"] == "V2_ACTIVE"
    assert replay["code"] == "already_active"
    assert replay["ok"] is True
    assert replay["cleanup"]["status"] == "NOOP"
    assert replay["cleanup"]["remaining"] == []


def test_cleanup_warning_reports_remaining_without_changing_active_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from memoryguard.migration import backup_cleanup

    workspace = tmp_path / "workspace"
    target = workspace / ".memoryguard" / "migration-backups" / "batch-1"
    target.mkdir(parents=True)
    (target / "phase6-recovery").write_text("evidence", encoding="utf-8")
    monkeypatch.setattr(backup_cleanup, "_remove_entry", lambda *_args: None)

    result = cleanup_migration_backups(workspace, "batch-1")

    assert result["ok"] is False
    assert result["cleanup_warning"] is True
    assert result["remaining"] == ["batch-1/phase6-recovery"]
