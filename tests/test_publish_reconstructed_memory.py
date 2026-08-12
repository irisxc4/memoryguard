from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from _publish_helpers import (
    build_projection,
    projection_scope,
    publish,
    register_publish_target,
    seed_atom,
)
from memoryguard.governance_v2 import GovernanceV2
from memoryguard.gui import GovernanceApi
from memoryguard.memory import MemoryAtomStore
from memoryguard.projection_v2 import ProjectionStore
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.organizer import V2MemoryOrganizer
from memoryguard.runtime_v2.projection_build import (
    ProjectionBuildError,
    ProjectionBuildService,
    V2ReleaseService,
)
from memoryguard.runtime_v2.source_control import SourceControlService
from memoryguard.security import MUTATION_API_METHODS, READONLY_API_METHODS


def _fixture(
    tmp_path: Path,
    *,
    target_name: str = "memory.md",
    initial: str = "# Memory\n\nold\n",
    source_id: str = "publish-target",
) -> tuple[Path, Path, object]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "native" / target_name
    target.parent.mkdir()
    target.write_text(initial, encoding="utf-8")
    scope = projection_scope(workspace)
    register_publish_target(workspace, target, source_id=source_id)
    return workspace, target, scope


def _seed_and_publish(
    tmp_path: Path,
    body: str,
    *,
    memory_id: str = "m1",
    title: str = "V2 fixture",
    target_name: str = "memory.md",
    initial: str = "# Memory\n\nold\n",
) -> tuple[Path, Path, object, dict, dict]:
    workspace, target, scope = _fixture(
        tmp_path,
        target_name=target_name,
        initial=initial,
    )
    seed_atom(
        workspace,
        memory_id,
        body,
        metadata={"title": title, "scope": "project"},
    )
    built = build_projection(workspace, scope=scope)
    published = publish(workspace, target, scope=scope)
    return workspace, target, scope, built, published


def _rollback(workspace: Path, target: Path, scope: object, release_id: str) -> dict:
    return V2ReleaseService(workspace).rollback(
        release_id,
        str(target),
        scope=scope,
        confirmed=True,
    )


def _organize_sensitive(workspace: Path, memory_id: str, body: str) -> dict:
    memory = MemoryAtomStore(workspace, readonly=False)
    organizer = V2MemoryOrganizer(
        workspace,
        "group-test",
        memory_store=memory,
        governance=GovernanceV2(workspace, memory_store=memory),
    )
    return organizer.write(
        {
            "memory_id": memory_id,
            "event_id": f"event-{memory_id}",
            "body": body,
            "kind": "fact",
            "agent_instance_id": "agent-test",
            "share_group_id": "group-test",
            "project_ref": str(workspace.resolve()),
            "provider": "test",
            "runtime_role": "test",
            "visibility": "active",
            "idempotency_key": f"fixture-{memory_id}",
        },
    )


def test_publish_reconstructed_memory_writes_real_file_and_rollback_restores(tmp_path: Path) -> None:
    workspace, target, scope, built, published = _seed_and_publish(
        tmp_path,
        "这是自动治理后的记忆",
        title="重构事实",
    )

    assert built["status"] == "succeeded"
    assert published["ok"] is True
    releases = V2ReleaseService(workspace).list_releases(scope=scope)
    assert any(item["release_id"] == published["release_id"] for item in releases["releases"])
    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["schema"] == "memoryguard-v2-native-release-1"
    assert document["memories"][0]["memory_id"] == "m1"
    assert document["memories"][0]["body"] == "这是自动治理后的记忆"

    rolled_back = _rollback(workspace, target, scope, published["release_id"])

    assert rolled_back["ok"] is True
    assert target.read_text(encoding="utf-8") == "# Memory\n\nold\n"


def test_publish_quarantines_secrets_before_release(tmp_path: Path) -> None:
    workspace, target, scope = _fixture(tmp_path)
    result = _organize_sensitive(
        workspace,
        "m-secret",
        "Store credentials safely: api_key=super-secret-value and sk-abcdefghijklmnopqrstuvwxyz123456",
    )

    published = publish(workspace, target, scope=scope)

    assert result["mutation_kind"] == "quarantined"
    assert result["status"] == "quarantined"
    assert published == {
        "ok": False,
        "status": "NO_SOURCE",
        "error": "projection_required",
    }
    assert target.read_text(encoding="utf-8") == "# Memory\n\nold\n"


def test_publish_quarantines_pem_private_key_before_release(tmp_path: Path) -> None:
    workspace, target, scope = _fixture(tmp_path)
    result = _organize_sensitive(
        workspace,
        "m-pem",
        (
            "Deploy key:\n"
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEpAIBAAKCAQEA7fakebase64line1\n"
            "MIIEpAIBAAKCAQEA7fakebase64line2\n"
            "-----END RSA PRIVATE KEY-----\n"
            "End of notes."
        ),
    )

    published = publish(workspace, target, scope=scope)

    assert result["mutation_kind"] == "quarantined"
    assert result["status"] == "quarantined"
    assert published["status"] == "NO_SOURCE"
    assert not target.read_text(encoding="utf-8").startswith("{")


def test_list_publish_targets_returns_enabled_v2_sources(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    file_target = tmp_path / "native.md"
    file_target.write_text("old", encoding="utf-8")
    folder_target = tmp_path / "native-folder"
    folder_target.mkdir()
    docs_target = tmp_path / "docs"
    docs_target.mkdir()

    file_id = register_publish_target(workspace, file_target, source_id="native-file")
    folder_id = register_publish_target(
        workspace,
        folder_target,
        source_id="native-folder",
        source_type="selected_directory",
    )
    docs_id = register_publish_target(
        workspace,
        docs_target,
        source_id="docs",
        source_type="selected_directory",
    )

    source_control = SourceControlService(workspace)
    visible = source_control.list_sources({"is_admin": True})
    by_id = {item["source_id"]: item for item in visible["sources"]}
    assert {file_id, folder_id, docs_id} <= set(by_id)
    assert by_id[file_id]["type"] == "selected_file"
    assert by_id[folder_id]["type"] == "selected_directory"
    assert all(by_id[item]["state"] == "READY" for item in (file_id, folder_id, docs_id))

    targets = V2ReleaseService(workspace).list_targets(scope=projection_scope(workspace))
    assert {item["target_root_id"] for item in targets["targets"]} >= {
        file_id,
        folder_id,
        docs_id,
    }
    assert source_control.remove(docs_id, {"is_admin": True})["ok"] is True
    remaining = V2ReleaseService(workspace).list_targets(scope=projection_scope(workspace))
    assert docs_id not in {item["target_root_id"] for item in remaining["targets"]}


def test_source_control_publish_target_visibility_is_agent_scoped(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "user_profile.md"
    target.write_text("old", encoding="utf-8")
    source_id = register_publish_target(workspace, target, source_id="agent-native")
    GroupControlService(workspace, write=True).record_selection(
        "agent-1", [source_id], "selection-agent-1-agent-native",
    )

    selected = SourceControlService(workspace).list_sources(
        {"is_admin": False, "trusted_agent_id": "agent-1"},
    )
    other = SourceControlService(workspace).list_sources(
        {"is_admin": False, "trusted_agent_id": "agent-2"},
    )

    assert {item["source_id"] for item in selected["sources"]} == {source_id}
    assert other["sources"] == []


def test_choose_publish_target_path_uses_windows_dialog_without_tkinter(
    tmp_path: Path,
    monkeypatch,
) -> None:
    selected_folder = tmp_path / "selected"
    selected_folder.mkdir()

    class Completed:
        returncode = 0
        stdout = str(selected_folder) + "\n"
        stderr = ""

    class V2HostPort:
        def state_snapshot(self):
            return {"state": "V2_ACTIVE", "generation": 1}

        def dispatch_gui(self, method, args, **_kwargs):
            assert method == "choose_publish_target_path"
            return {
                "ok": True,
                "path": "v2",
                "data": {
                    "host_action": "choose_publish_target_path",
                    "kind": args[0],
                },
            }

    import memoryguard.gui as gui_module

    monkeypatch.setattr(gui_module.sys, "platform", "win32")
    monkeypatch.setattr(gui_module.subprocess, "run", lambda *args, **kwargs: Completed())

    result = GovernanceApi(str(tmp_path), _v2_port=V2HostPort()).choose_publish_target_path(
        "folder",
    )

    assert result["ok"] is True
    assert result["target_file"] == str(selected_folder / "memory.md")


def test_publish_and_rollback_native_memory_apis_are_registered_as_mutations() -> None:
    assert "publish_reconstructed_memory" in MUTATION_API_METHODS
    assert "rollback_native_memory_release" in MUTATION_API_METHODS
    assert "publish_reconstructed_memory" not in READONLY_API_METHODS
    assert "rollback_native_memory_release" not in READONLY_API_METHODS
    assert "list_native_memory_releases" in READONLY_API_METHODS
    assert "list_publish_targets" in READONLY_API_METHODS
    assert "choose_publish_target_path" in READONLY_API_METHODS


def test_publish_writes_release_to_projection_ledger(tmp_path: Path) -> None:
    workspace, target, scope, _built, published = _seed_and_publish(
        tmp_path,
        "这是自动治理后的记忆",
    )

    assert published["ok"] is True
    assert published["release_id"].startswith("release-")
    counts = ProjectionStore(workspace, initialize=False).counts("scenario")
    assert counts["ledger"] >= 2
    assert not (workspace / ".memoryguard" / "releases").exists()
    releases = V2ReleaseService(workspace).list_releases(scope=scope)
    assert releases["total"] == 1
    assert releases["releases"][0]["release_id"] == published["release_id"]
    assert str(target.resolve()) not in releases["releases"][0]


def test_publish_projection_record_has_scoped_evidence(tmp_path: Path) -> None:
    workspace, target, scope, built, published = _seed_and_publish(
        tmp_path,
        "这是自动治理后的记忆",
    )
    projection = built["projection"]
    record = ProjectionStore(workspace, initialize=False).get_projection(
        "scenario",
        projection["key"],
        scope=scope,
    )

    assert published["ok"] is True
    assert record is not None
    assert record.projection_id == projection["projection_id"]
    assert record.projection_digest == projection["projection_digest"]
    assert len(record.evidence_links) >= 1
    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["projection_id"] == record.projection_id
    assert {item["memory_id"] for item in document["memories"]} == {"m1"}


def test_publish_writes_requested_non_memory_md_file(tmp_path: Path) -> None:
    workspace, target, scope, _built, published = _seed_and_publish(
        tmp_path,
        "喜欢深色主题界面",
        title="用户偏好",
        target_name="user_profile.md",
        initial="# Profile\n\nold profile\n",
    )

    assert published["ok"] is True
    assert target.name == "user_profile.md"
    profile = json.loads(target.read_text(encoding="utf-8"))
    assert profile["schema"] == "memoryguard-v2-native-release-1"
    assert profile["memories"][0]["body"] == "喜欢深色主题界面"
    assert not (target.parent / "memory.md").exists()
    assert V2ReleaseService(workspace).list_releases(scope=scope)["total"] == 1


def test_v2_release_rejects_target_drift_without_writing(tmp_path: Path) -> None:
    workspace, target, scope = _fixture(tmp_path)
    seed_atom(workspace, "m-drift", "target drift fixture")
    build_projection(workspace, scope=scope)
    release = V2ReleaseService(workspace)
    plan = release.create_plan(str(target), scope=scope)
    target.write_text("changed after plan", encoding="utf-8")

    with pytest.raises(ProjectionBuildError, match="release_target_drift"):
        release.apply(
            str(plan["plan_id"]),
            str(target),
            scope=scope,
            confirmed=True,
        )

    assert target.read_text(encoding="utf-8") == "changed after plan"


def test_v2_release_rejects_projection_drift_without_writing(tmp_path: Path) -> None:
    workspace, target, scope = _fixture(tmp_path)
    seed_atom(workspace, "m-first", "first projection fixture")
    build_projection(workspace, scope=scope)
    release = V2ReleaseService(workspace)
    plan = release.create_plan(str(target), scope=scope)
    seed_atom(workspace, "m-second", "second projection fixture")
    assert build_projection(workspace, scope=scope)["status"] == "succeeded"

    with pytest.raises(ProjectionBuildError, match="release_plan_stale"):
        release.apply(
            str(plan["plan_id"]),
            str(target),
            scope=scope,
            confirmed=True,
        )

    assert target.read_text(encoding="utf-8") == "# Memory\n\nold\n"


def test_publish_first_create_rollback_deletes_new_file(tmp_path: Path) -> None:
    workspace, target, scope = _fixture(
        tmp_path,
        target_name="user_profile.md",
    )
    target.unlink()
    seed_atom(workspace, "m-first", "首次写入原生记忆")
    published = publish(workspace, target, scope=scope)

    assert published["ok"] is True
    assert target.exists()
    rolled_back = _rollback(workspace, target, scope, published["release_id"])

    assert rolled_back["ok"] is True
    assert not target.exists()
    assert not (target.parent / "memory.md").exists()
    assert not (target.parent / "index.json").exists()


def test_publish_existing_target_rollback_restores_content(tmp_path: Path) -> None:
    old_content = "# Profile\n\n旧 profile 内容\n"
    workspace, target, scope = _fixture(
        tmp_path,
        target_name="user_profile.md",
        initial=old_content,
    )
    previous_digest = hashlib.sha256(target.read_bytes()).hexdigest()
    seed_atom(workspace, "m-existing", "发布后应恢复旧内容")
    published = publish(workspace, target, scope=scope)
    receipt = V2ReleaseService(workspace).list_releases(scope=scope)["releases"][0]

    assert published["ok"] is True
    assert receipt["existed_before"] is True
    assert receipt["previous_blob_id"]
    assert receipt["previous_occurrence_id"]
    assert receipt["previous_digest"] == previous_digest
    assert "发布后应恢复旧内容" in target.read_text(encoding="utf-8")

    rolled_back = _rollback(workspace, target, scope, published["release_id"])

    assert rolled_back["ok"] is True
    assert target.read_text(encoding="utf-8") == old_content


def test_rollback_first_publish_deletes_new_files(tmp_path: Path) -> None:
    workspace, target, scope = _fixture(
        tmp_path,
        target_name="memory.md",
    )
    target.unlink()
    seed_atom(workspace, "m-new", "第一次写入原生记忆")
    published = publish(workspace, target, scope=scope)

    assert published["ok"] is True
    assert target.exists()
    rolled_back = _rollback(workspace, target, scope, published["release_id"])

    assert rolled_back["ok"] is True
    assert not target.exists()
    assert not (target.parent / "index.json").exists()


def test_release_document_embeds_projection_manifest(tmp_path: Path) -> None:
    workspace, target, _scope, built, published = _seed_and_publish(
        tmp_path,
        "这是自动治理后的记忆",
    )
    document = json.loads(target.read_text(encoding="utf-8"))

    assert published["ok"] is True
    assert document["schema"] == "memoryguard-v2-native-release-1"
    assert document["projection_id"] == built["projection"]["projection_id"]
    assert document["projection_digest"] == built["projection"]["projection_digest"]
    assert document["scope_digest"]
    assert document["memories"]
    assert document["memories"][0]["memory_id"] == "m1"
