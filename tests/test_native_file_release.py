from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.native_file_release import SafeNativeFilePublisher, sha256_file


def test_safe_native_file_release_applies_and_rolls_back_real_file(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    target_dir = tmp_path / "agent-memory"
    workspace.mkdir()
    target_dir.mkdir()
    target = target_dir / "memory.md"
    original = "# Memory\n\n旧记忆\n"
    updated = "# Memory\n\n重构后的记忆\n"
    target.write_text(original, encoding="utf-8")

    publisher = SafeNativeFilePublisher(workspace)
    applied = publisher.apply({target: updated.encode("utf-8")}, label="test-native")

    assert applied.ok is True
    assert target.read_text(encoding="utf-8") == updated
    manifest = json.loads(Path(applied.manifest_path).read_text(encoding="utf-8"))
    assert manifest["status"] == "applied_verified"
    assert Path(manifest["files"][0]["backup_path"]).read_text(encoding="utf-8") == original
    assert sha256_file(target) == manifest["files"][0]["after_hash"]

    rolled_back = publisher.rollback(applied.release_id)

    assert rolled_back.ok is True
    assert target.read_text(encoding="utf-8") == original
    rolled_manifest = json.loads(Path(rolled_back.manifest_path).read_text(encoding="utf-8"))
    assert rolled_manifest["status"] == "rolled_back"


def test_safe_native_file_release_rolls_back_new_file_by_deleting_it(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    target_dir = tmp_path / "agent-memory"
    workspace.mkdir()
    target_dir.mkdir()
    target = target_dir / "new-memory.md"

    publisher = SafeNativeFilePublisher(workspace)
    applied = publisher.apply({target: b"new memory"}, label="test-new-file")

    assert applied.ok is True
    assert target.exists()

    rolled_back = publisher.rollback(applied.release_id)

    assert rolled_back.ok is True
    assert not target.exists()


def test_safe_native_file_release_refuses_rollback_if_target_was_modified_after_release(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    target_dir = tmp_path / "agent-memory"
    workspace.mkdir()
    target_dir.mkdir()
    target = target_dir / "memory.md"
    target.write_text("old", encoding="utf-8")

    publisher = SafeNativeFilePublisher(workspace)
    applied = publisher.apply({target: b"new"}, label="test-conflict")
    target.write_text("changed by another process", encoding="utf-8")

    rolled_back = publisher.rollback(applied.release_id)

    assert rolled_back.ok is False
    assert "target changed after release" in rolled_back.errors[0]
    assert target.read_text(encoding="utf-8") == "changed by another process"


def test_safe_native_file_release_reports_unwritable_target_before_apply(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    target_dir = tmp_path / "agent-memory"
    workspace.mkdir()
    target_dir.mkdir()
    target = target_dir / "memory.md"
    publisher = SafeNativeFilePublisher(workspace)
    monkeypatch.setattr(publisher, "_ensure_parent_writable", lambda target: (_ for _ in ()).throw(PermissionError("目标文件夹不可写：x")))

    result = publisher.apply({target: b"new memory"}, label="unwritable")

    assert result.ok is False
    assert result.status == "failed_before_apply"
    assert "目标文件夹不可写" in result.errors[0]
    assert not target.exists()
    release = next(item for item in publisher.list_releases() if item["release_id"] == result.release_id)
    assert release["can_rollback"] is False


def test_safe_native_file_release_creates_new_target_as_restorable_version(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    target_dir = tmp_path / "agent-memory"
    workspace.mkdir()
    target_dir.mkdir()
    target = target_dir / "memory.md"
    publisher = SafeNativeFilePublisher(workspace)

    applied = publisher.apply({target: b"new memory"}, label="new-target")

    assert applied.ok is True
    assert target.read_bytes() == b"new memory"
    release = next(item for item in publisher.list_releases() if item["release_id"] == applied.release_id)
    assert release["status"] == "applied_verified"
    assert release["can_rollback"] is True

    rolled = publisher.rollback(applied.release_id)

    assert rolled.ok is True
    assert not target.exists()


def test_safe_native_file_release_uses_real_target_hash_even_when_status_is_wrong(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    target_dir = tmp_path / "agent-memory"
    workspace.mkdir()
    target_dir.mkdir()
    target = target_dir / "memory.md"
    target.write_text("old", encoding="utf-8")
    publisher = SafeNativeFilePublisher(workspace)
    applied = publisher.apply({target: b"new"}, label="status-drift")
    manifest_path = workspace / ".memoryguard" / "native_releases" / applied.release_id / "manifest.json"
    import json
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "failed_rolled_back"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    release = next(item for item in publisher.list_releases() if item["release_id"] == applied.release_id)

    assert release["can_rollback"] is True
    assert release["rollback_reason"] == "可恢复"

    rolled = publisher.rollback(applied.release_id)

    assert rolled.ok is True
    assert target.read_text(encoding="utf-8") == "old"


def test_safe_native_file_release_lists_only_really_restorable_versions(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    target_dir = tmp_path / "agent-memory"
    workspace.mkdir()
    target_dir.mkdir()
    target = target_dir / "memory.md"
    target.write_text("old", encoding="utf-8")
    publisher = SafeNativeFilePublisher(workspace)
    applied = publisher.apply({target: b"new"}, label="test-list")

    releases = publisher.list_releases()
    current = next(item for item in releases if item["release_id"] == applied.release_id)
    assert current["can_rollback"] is True
    assert current["rollback_reason"] == "可恢复"
    assert "manifest_path" not in current

    target.write_text("changed after release", encoding="utf-8")
    changed = next(item for item in publisher.list_releases() if item["release_id"] == applied.release_id)
    assert changed["can_rollback"] is False
    assert changed["rollback_reason"] == "目标已被后续修改"

    publisher.rollback(applied.release_id, force=True)
    rolled = next(item for item in publisher.list_releases() if item["release_id"] == applied.release_id)
    assert rolled["can_rollback"] is False
    assert rolled["rollback_reason"] == "已经恢复过"


def test_safe_native_file_release_force_rollback_restores_even_after_target_change(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    target_dir = tmp_path / "agent-memory"
    workspace.mkdir()
    target_dir.mkdir()
    target = target_dir / "memory.md"
    target.write_text("old", encoding="utf-8")

    publisher = SafeNativeFilePublisher(workspace)
    applied = publisher.apply({target: b"new"}, label="test-force")
    target.write_text("changed by another process", encoding="utf-8")

    rolled_back = publisher.rollback(applied.release_id, force=True)

    assert rolled_back.ok is True
    assert target.read_text(encoding="utf-8") == "old"
