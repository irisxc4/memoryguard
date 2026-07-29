from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.gui import GovernanceApi
from memoryguard.memory_ir import MemoryIR, MemoryNormalizer
from memoryguard.schema_v3 import MemoryKind, MemoryRecord, SourceRootType
from memoryguard.security import MUTATION_API_METHODS, READONLY_API_METHODS
from memoryguard.source_registry import SourceRegistry
from memoryguard.governance_scope import grant_root_to_agent
from _publish_helpers import prepare_publish_target, publish


def test_publish_reconstructed_memory_writes_real_file_and_rollback_restores(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    target_dir = tmp_path / "native"
    workspace.mkdir()
    target_dir.mkdir()
    target = target_dir / "memory.md"
    target.write_text("# Memory\n\n旧原生记忆\n", encoding="utf-8")
    ir = MemoryIR(records=[MemoryRecord(
        memory_id="m1",
        kind=MemoryKind.FACT,
        title="重构事实",
        body="这是自动治理后的记忆",
    )], snapshot_id="snap")
    MemoryNormalizer(workspace).save(ir)

    api, root_id, scope = prepare_publish_target(workspace, target, ir)
    published = publish(api, scope=scope, target_root_id=root_id)

    assert published["ok"] is True
    release_list = api.list_native_memory_releases(scope=scope, agent_instance_id=scope["agent_instance_id"])
    assert any(item["release_id"] == published["release_id"] for item in release_list["releases"])
    assert "重构事实" in target.read_text(encoding="utf-8")
    assert "这是自动治理后的记忆" in target.read_text(encoding="utf-8")

    rolled_back = api.rollback_native_memory_release(
        published["release_id"], confirmed=True,
        scope=scope, agent_instance_id=scope["agent_instance_id"], target_root_id=root_id,
    )

    assert rolled_back["ok"] is True
    assert target.read_text(encoding="utf-8") == "# Memory\n\n旧原生记忆\n"


def test_publish_redacts_secrets_in_body(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    target_dir = tmp_path / "native"
    workspace.mkdir()
    target_dir.mkdir()
    target = target_dir / "memory.md"
    target.write_text("# Memory\n\n旧原生记忆\n", encoding="utf-8")
    secret_body = "Store credentials safely: api_key=super-secret-value and sk-abcdefghijklmnopqrstuvwxyz123456"
    ir = MemoryIR(records=[MemoryRecord(
        memory_id="m-secret",
        kind=MemoryKind.FACT,
        title="Deployment notes",
        body=secret_body,
    )], snapshot_id="snap")
    MemoryNormalizer(workspace).save(ir)

    api, root_id, scope = prepare_publish_target(workspace, target, ir)
    published = publish(api, scope=scope, target_root_id=root_id)
    written = target.read_text(encoding="utf-8")

    assert published["ok"] is True
    assert "super-secret-value" not in written
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in written
    assert "[REDACTED:" in written
    assert published.get("redactions")
    assert published["redactions"][0]["memory_id"] == "m-secret"
    assert "body" in published["redactions"][0]["fields"]


def test_publish_redacts_pem_private_key_block(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    target_dir = tmp_path / "native"
    workspace.mkdir()
    target_dir.mkdir()
    target = target_dir / "memory.md"
    target.write_text("# Memory\n\n旧原生记忆\n", encoding="utf-8")
    pem_body = (
        "Deploy key:\n"
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA7fakebase64line1\n"
        "MIIEpAIBAAKCAQEA7fakebase64line2\n"
        "-----END RSA PRIVATE KEY-----\n"
        "End of notes."
    )
    ir = MemoryIR(records=[MemoryRecord(
        memory_id="m-pem",
        kind=MemoryKind.FACT,
        title="SSH deploy key",
        body=pem_body,
    )], snapshot_id="snap")
    MemoryNormalizer(workspace).save(ir)

    api, root_id, scope = prepare_publish_target(workspace, target, ir)
    published = publish(api, scope=scope, target_root_id=root_id)
    written = target.read_text(encoding="utf-8")

    assert published["ok"] is True
    assert "MIIEpAIBAAKCAQEA7fakebase64line1" not in written
    assert "MIIEpAIBAAKCAQEA7fakebase64line2" not in written
    assert "[REDACTED:private_key]" in written
    assert "private_key" in published["redactions"][0]["patterns"]


def test_list_publish_targets_returns_enabled_native_sources(tmp_path) -> None:
    file_target = tmp_path / "native.md"
    folder_target = tmp_path / "native-folder"
    file_target.write_text("old", encoding="utf-8")
    folder_target.mkdir()
    docs_target = tmp_path / "docs"
    docs_target.mkdir()
    reg = SourceRegistry(tmp_path)
    file_root = reg.add(str(file_target), SourceRootType.SELECTED_FILE, "Native File", scope="user")
    file_root.source_category = "native_memory"
    file_root.enabled = True
    grant_root_to_agent(file_root, "agent-1")
    folder_root = reg.add(str(folder_target), SourceRootType.SELECTED_DIRECTORY, "Native Folder", scope="project")
    folder_root.source_category = "project_memory"
    folder_root.enabled = True
    grant_root_to_agent(folder_root, "agent-1")
    ignored = reg.add(str(docs_target), SourceRootType.SELECTED_DIRECTORY, "Docs", scope="project")
    ignored.source_category = "knowledge_source"
    ignored.enabled = True
    grant_root_to_agent(ignored, "agent-1")
    reg._save()

    targets = GovernanceApi(str(tmp_path)).list_publish_targets(agent_instance_id="agent-1")["targets"]
    by_name = {target["display_name"]: target for target in targets}

    assert by_name["Native File"]["target_file"] == str(file_target.resolve())
    assert by_name["Native File"]["is_agent_native_memory"] is False
    assert by_name["Native Folder"]["target_file"] == str((folder_target / "memory.md").resolve())
    assert by_name["Native Folder"]["is_agent_native_memory"] is False
    assert "Docs" not in by_name


def test_list_publish_targets_marks_agent_native_memory_entry(tmp_path) -> None:
    native_file = tmp_path / "user_profile.md"
    native_file.write_text("old", encoding="utf-8")
    reg = SourceRegistry(tmp_path)
    root = reg.add(str(native_file), SourceRootType.SELECTED_FILE, "TRAE Profile", scope="user")
    root.source_category = "native_memory"
    root.ownership = "agent_managed"
    root.target_role = "takeover_input"
    grant_root_to_agent(root, "agent-1")
    root.surface_id = "trae_user_profile"
    root.enabled = True
    reg._save()

    targets = GovernanceApi(str(tmp_path)).list_publish_targets(agent_instance_id="agent-1")["targets"]
    target = next(item for item in targets if item["root_id"] == root.root_id)

    assert target["is_agent_native_memory"] is True
    assert target["ownership"] == "agent_managed"
    assert target["target_role"] == "takeover_input"


def test_choose_publish_target_path_uses_windows_dialog_without_tkinter(tmp_path, monkeypatch) -> None:
    selected_folder = tmp_path / "selected"
    selected_folder.mkdir()

    class Completed:
        returncode = 0
        stdout = str(selected_folder) + "\n"
        stderr = ""

    import platform
    import subprocess
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: Completed())

    result = GovernanceApi(str(tmp_path)).choose_publish_target_path("folder")

    assert result["ok"] is True
    assert result["target_file"] == str(selected_folder / "memory.md")


def test_publish_and_rollback_native_memory_apis_are_registered_as_mutations() -> None:
    assert "publish_reconstructed_memory" not in MUTATION_API_METHODS
    assert "rollback_native_memory_release" not in MUTATION_API_METHODS
    assert "list_native_memory_releases" in READONLY_API_METHODS
    assert "list_publish_targets" in READONLY_API_METHODS
    assert "choose_publish_target_path" in READONLY_API_METHODS


def test_publish_writes_release_under_releases_dir(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    target_dir = tmp_path / "native"
    workspace.mkdir()
    target_dir.mkdir()
    target = target_dir / "memory.md"
    target.write_text("# Memory\n\n旧原生记忆\n", encoding="utf-8")
    ir = MemoryIR(records=[MemoryRecord(
        memory_id="m1",
        kind=MemoryKind.FACT,
        title="重构事实",
        body="这是自动治理后的记忆",
    )], snapshot_id="snap")
    MemoryNormalizer(workspace).save(ir)

    api, root_id, scope = prepare_publish_target(workspace, target, ir)
    published = publish(api, scope=scope, target_root_id=root_id)

    assert published["ok"] is True
    assert published["release_id"].startswith("rel-")
    release_path = workspace / ".memoryguard" / "releases" / f"{published['release_id']}.json"
    assert release_path.exists()
    release_data = json.loads(release_path.read_text(encoding="utf-8"))
    assert release_data["record_type"] == "memory_release"
    assert release_data["schema_version"] == "3.1"


def test_publish_build_manifest_has_record_mappings(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    target_dir = tmp_path / "native"
    workspace.mkdir()
    target_dir.mkdir()
    target = target_dir / "memory.md"
    target.write_text("# Memory\n\n旧原生记忆\n", encoding="utf-8")
    ir = MemoryIR(records=[MemoryRecord(
        memory_id="m1",
        kind=MemoryKind.FACT,
        title="重构事实",
        body="这是自动治理后的记忆",
    )], snapshot_id="snap")
    MemoryNormalizer(workspace).save(ir)

    api, root_id, scope = prepare_publish_target(workspace, target, ir)
    published = publish(api, scope=scope, target_root_id=root_id)

    assert published["ok"] is True
    assert published.get("published_record_count", 0) > 0
    assert published.get("record_mapping_count", 0) > 0


def test_publish_writes_requested_non_memory_md_file(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    target_dir = tmp_path / "agent-home"
    workspace.mkdir()
    target_dir.mkdir()
    user_profile = target_dir / "user_profile.md"
    user_profile.write_text("# Profile\n\n旧 profile\n", encoding="utf-8")
    ir = MemoryIR(records=[MemoryRecord(
        memory_id="m1",
        kind=MemoryKind.FACT,
        title="用户偏好",
        body="喜欢深色主题界面",
    )], snapshot_id="snap")
    MemoryNormalizer(workspace).save(ir)

    api, root_id, scope = prepare_publish_target(workspace, user_profile, ir)
    published = publish(api, scope=scope, target_root_id=root_id)

    assert published["ok"] is True
    assert published.get("published_target_file") == str(user_profile.resolve())
    profile_text = user_profile.read_text(encoding="utf-8")
    memory_md = target_dir / "memory.md"
    assert memory_md.exists()
    assert "用户偏好" in profile_text
    assert "喜欢深色主题界面" in profile_text
    assert profile_text == memory_md.read_text(encoding="utf-8")

    release_path = workspace / ".memoryguard" / "releases" / f"{published['release_id']}.json"
    release_data = json.loads(release_path.read_text(encoding="utf-8"))
    assert str(user_profile.resolve()) in release_data["changed_paths"]
    assert release_data.get("published_target_file") == str(user_profile.resolve())
    assert release_data.get("exact_file_existed_before") is True


def test_exact_file_sync_rewrite_failure_rolls_back_orphan(tmp_path, monkeypatch) -> None:
    """rewrite 在 exact_file 已写入后失败时，整单回滚须清掉 orphan。"""
    workspace = tmp_path / "workspace"
    target_dir = tmp_path / "agent-home"
    workspace.mkdir()
    target_dir.mkdir()
    user_profile = target_dir / "user_profile.md"
    assert not user_profile.exists()

    ir = MemoryIR(records=[MemoryRecord(
        memory_id="m1",
        kind=MemoryKind.FACT,
        title="rewrite 失败",
        body="sync rewrite 失败后不应留下 orphan",
    )], snapshot_id="snap")
    MemoryNormalizer(workspace).save(ir)

    def boom(*_a, **_k):
        raise OSError("rewrite failed")

    # Order: backup → copy exact_file → update paths → rewrite.
    # Patching rewrite leaves exact_file on disk; rollback must remove it.
    monkeypatch.setattr(
        "memoryguard.gui._rewrite_release_json_for_exact_file", boom,
    )

    user_profile.write_text("# tmp\n", encoding="utf-8")
    api, root_id, scope = prepare_publish_target(workspace, user_profile, ir)
    user_profile.unlink()
    published = publish(api, scope=scope, target_root_id=root_id)

    assert published["ok"] is False
    assert "exact_file sync failed" in published.get("errors", [""])[0]
    assert not user_profile.exists()
    assert not (target_dir / "memory.md").exists()
    assert not (target_dir / "index.json").exists()


def test_exact_file_sync_corrupt_release_json_still_rolls_back_orphan(
    tmp_path, monkeypatch,
) -> None:
    """rewrite 把 release JSON 截断写坏后再失败时，publish 不抛异常且清掉 orphan。"""
    workspace = tmp_path / "workspace"
    target_dir = tmp_path / "agent-home"
    workspace.mkdir()
    target_dir.mkdir()
    user_profile = target_dir / "user_profile.md"
    assert not user_profile.exists()

    ir = MemoryIR(records=[MemoryRecord(
        memory_id="m1",
        kind=MemoryKind.FACT,
        title="JSON 截断",
        body="release JSON 损坏后仍应回滚 orphan",
    )], snapshot_id="snap")
    MemoryNormalizer(workspace).save(ir)

    def corrupt_then_boom(release, workspace_arg, **_k):
        releases_dir = Path(workspace_arg) / ".memoryguard" / "releases"
        release_path = releases_dir / f"{release.release_id}.json"
        # Truncate live release JSON (simulates mid-write corruption).
        release_path.write_text("{", encoding="utf-8")
        raise OSError("rewrite failed after corrupt")

    monkeypatch.setattr(
        "memoryguard.gui._rewrite_release_json_for_exact_file",
        corrupt_then_boom,
    )

    user_profile.write_text("# tmp\n", encoding="utf-8")
    api, root_id, scope = prepare_publish_target(workspace, user_profile, ir)
    user_profile.unlink()
    published = publish(api, scope=scope, target_root_id=root_id)

    assert isinstance(published, dict)
    assert published["ok"] is False
    assert "exact_file sync failed" in published.get("errors", [""])[0]
    assert not user_profile.exists()
    assert not (target_dir / "memory.md").exists()
    assert not (target_dir / "index.json").exists()


def test_publish_exact_file_first_create_rollback_deletes_it(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    target_dir = tmp_path / "agent-home"
    workspace.mkdir()
    target_dir.mkdir()
    user_profile = target_dir / "user_profile.md"

    ir = MemoryIR(records=[MemoryRecord(
        memory_id="m1",
        kind=MemoryKind.FACT,
        title="首次 sidecar",
        body="新建 exact_file 应可回滚删除",
    )], snapshot_id="snap")
    MemoryNormalizer(workspace).save(ir)

    user_profile.write_text("# tmp\n", encoding="utf-8")
    api, root_id, scope = prepare_publish_target(workspace, user_profile, ir)
    user_profile.unlink()
    published = publish(api, scope=scope, target_root_id=root_id)

    assert published["ok"] is True
    assert user_profile.exists()
    assert (target_dir / "memory.md").exists()
    release_path = workspace / ".memoryguard" / "releases" / f"{published['release_id']}.json"
    release_data = json.loads(release_path.read_text(encoding="utf-8"))
    assert str(user_profile.resolve()) in release_data["changed_paths"]
    assert release_data.get("exact_file_existed_before") is False

    rolled_back = api.rollback_native_memory_release(
        published["release_id"], confirmed=True,
        scope=scope, agent_instance_id=scope["agent_instance_id"], target_root_id=root_id,
    )

    assert rolled_back["ok"] is True
    assert not rolled_back.get("errors")
    assert not user_profile.exists()
    assert not (target_dir / "memory.md").exists()
    assert not (target_dir / "index.json").exists()


def test_publish_exact_file_existing_rollback_restores_content(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    target_dir = tmp_path / "agent-home"
    workspace.mkdir()
    target_dir.mkdir()
    user_profile = target_dir / "user_profile.md"
    old_content = "# Profile\n\n旧 profile 内容\n"
    user_profile.write_text(old_content, encoding="utf-8")

    ir = MemoryIR(records=[MemoryRecord(
        memory_id="m1",
        kind=MemoryKind.FACT,
        title="覆盖 sidecar",
        body="发布后回滚应恢复旧内容",
    )], snapshot_id="snap")
    MemoryNormalizer(workspace).save(ir)

    api, root_id, scope = prepare_publish_target(workspace, user_profile, ir)
    published = publish(api, scope=scope, target_root_id=root_id)

    assert published["ok"] is True
    assert "覆盖 sidecar" in user_profile.read_text(encoding="utf-8")
    release_path = workspace / ".memoryguard" / "releases" / f"{published['release_id']}.json"
    release_data = json.loads(release_path.read_text(encoding="utf-8"))
    assert str(user_profile.resolve()) in release_data["changed_paths"]
    assert release_data.get("exact_file_existed_before") is True
    assert any(
        Path(bp).name.startswith("user_profile.md.") and bp.endswith(".bak")
        for bp in release_data.get("backup_paths", [])
    )

    rolled_back = api.rollback_native_memory_release(
        published["release_id"], confirmed=True,
        scope=scope, agent_instance_id=scope["agent_instance_id"], target_root_id=root_id,
    )

    assert rolled_back["ok"] is True
    assert not rolled_back.get("errors")
    assert user_profile.exists()
    assert user_profile.read_text(encoding="utf-8") == old_content


def test_rollback_first_publish_deletes_new_files(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    target_dir = tmp_path / "native"
    workspace.mkdir()
    target_dir.mkdir()
    assert not (target_dir / "memory.md").exists()
    assert not (target_dir / "index.json").exists()

    ir = MemoryIR(records=[MemoryRecord(
        memory_id="m1",
        kind=MemoryKind.FACT,
        title="首次发布",
        body="第一次写入原生记忆",
    )], snapshot_id="snap")
    MemoryNormalizer(workspace).save(ir)

    api, root_id, scope = prepare_publish_target(workspace, target_dir, ir)
    published = publish(api, scope=scope, target_root_id=root_id)

    assert published["ok"] is True
    assert (target_dir / "memory.md").exists()
    assert (target_dir / "index.json").exists()

    rolled_back = api.rollback_native_memory_release(
        published["release_id"], confirmed=True,
        scope=scope, agent_instance_id=scope["agent_instance_id"], target_root_id=root_id,
    )

    assert rolled_back["ok"] is True
    assert not rolled_back.get("errors")
    assert not (target_dir / "memory.md").exists()
    assert not (target_dir / "index.json").exists()


def test_release_json_embeds_build_manifest(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    target_dir = tmp_path / "native"
    workspace.mkdir()
    target_dir.mkdir()
    target = target_dir / "memory.md"
    target.write_text("# Memory\n\n旧原生记忆\n", encoding="utf-8")
    ir = MemoryIR(records=[MemoryRecord(
        memory_id="m1",
        kind=MemoryKind.FACT,
        title="重构事实",
        body="这是自动治理后的记忆",
    )], snapshot_id="snap")
    MemoryNormalizer(workspace).save(ir)

    api, root_id, scope = prepare_publish_target(workspace, target, ir)
    published = publish(api, scope=scope, target_root_id=root_id)

    release_path = workspace / ".memoryguard" / "releases" / f"{published['release_id']}.json"
    release_data = json.loads(release_path.read_text(encoding="utf-8"))
    manifest = release_data.get("manifest", {})

    assert manifest.get("record_mappings")
    assert manifest.get("published_record_count", 0) > 0
