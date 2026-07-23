from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.gui import GovernanceApi
from memoryguard.memory_ir import MemoryIR, MemoryNormalizer
from memoryguard.schema_v3 import MemoryKind, MemoryRecord, SourceRootType
from memoryguard.security import MUTATION_API_METHODS, READONLY_API_METHODS
from memoryguard.source_registry import SourceRegistry


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

    api = GovernanceApi(str(workspace))
    published = api.publish_reconstructed_memory(str(target), confirmed=True)

    assert published["ok"] is True
    release_list = api.list_native_memory_releases()
    assert any(item["release_id"] == published["release_id"] for item in release_list["releases"])
    assert "重构事实" in target.read_text(encoding="utf-8")
    assert "这是自动治理后的记忆" in target.read_text(encoding="utf-8")

    rolled_back = api.rollback_native_memory_release(published["release_id"], confirmed=True)

    assert rolled_back["ok"] is True
    assert target.read_text(encoding="utf-8") == "# Memory\n\n旧原生记忆\n"


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
    folder_root = reg.add(str(folder_target), SourceRootType.SELECTED_DIRECTORY, "Native Folder", scope="project")
    folder_root.source_category = "project_memory"
    folder_root.enabled = True
    ignored = reg.add(str(docs_target), SourceRootType.SELECTED_DIRECTORY, "Docs", scope="project")
    ignored.source_category = "knowledge_source"
    ignored.enabled = True
    reg._save()

    targets = GovernanceApi(str(tmp_path)).list_publish_targets()["targets"]
    by_name = {target["display_name"]: target for target in targets}

    assert by_name["Native File"]["target_file"] == str(file_target)
    assert by_name["Native File"]["is_agent_native_memory"] is False
    assert by_name["Native Folder"]["target_file"] == str(folder_target / "memory.md")
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
    root.agent_instance_id = "agent-1"
    root.surface_id = "trae_user_profile"
    root.enabled = True
    reg._save()

    targets = GovernanceApi(str(tmp_path)).list_publish_targets()["targets"]
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
    assert "publish_reconstructed_memory" in MUTATION_API_METHODS
    assert "rollback_native_memory_release" in MUTATION_API_METHODS
    assert "list_native_memory_releases" in READONLY_API_METHODS
    assert "list_publish_targets" in READONLY_API_METHODS
    assert "choose_publish_target_path" in READONLY_API_METHODS
