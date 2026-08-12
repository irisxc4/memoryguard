from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.content.store import ContentStore, stable_id
from memoryguard.runtime_v2.agent_native import AgentNativeService
from memoryguard.runtime_v2.group_native import GroupControlService


class FakeAgentLocator:
    def __init__(self, workspace):
        self.workspace = workspace

    def detect_instances(self):
        return [SimpleNamespace(
            instance_id="agent-1",
            product="fake-agent",
            surfaces=[{
                "status": "found",
                "resolved_path": str(Path(self.workspace) / "native.md"),
            }],
        )], {}

    def get_selection_tree(self, instance_id):
        return {
            "profile_id": "fake-profile",
            "instance_id": instance_id,
            "scopes": [{
                "scope": "user",
                "categories": [{
                    "category": "native_memory",
                    "files": [{
                        "discovery_object_id": "disc-native",
                        "path": str(Path(self.workspace) / "native.md"),
                        "display_name": "Native",
                        "default_selected": True,
                        "scope": "user",
                        "scope_source": "test",
                        "project_ref": "",
                        "ingestion_policy": "extract_candidates",
                    }],
                }],
                "projects": [],
            }],
        }

    def validate_discovery_objects(self, instance_id, discovery_object_ids):
        return {
            "disc-native": {
                "valid": True,
                "surface": {
                    "surface_id": "native-surface",
                    "display_name": "Native",
                    "path": str(Path(self.workspace) / "native.md"),
                    "category": "native_memory",
                    "ingestion_policy": "extract_candidates",
                    "ownership": "owned",
                    "target_role": "primary",
                    "scope": "user",
                    "scope_source": "test",
                    "project_ref": "",
                    "discovery_object_id": "disc-native",
                },
            }
        }


def _source_id(target: Path) -> str:
    return stable_id("agent-source", "agent-1", str(target.resolve()))


def _seed_source(target: Path, *, enabled: bool, selected: bool) -> str:
    workspace = target.parent
    source_id = _source_id(target)
    ContentStore(workspace).upsert_source_connector(
        source_id=source_id,
        provider="fake-agent",
        source_type="file",
        external_root_key=str(target.resolve()),
        workspace_id=str(workspace.resolve()),
        enabled=enabled,
    )
    GroupControlService(workspace, write=True).record_selection(
        "agent-1", [source_id] if selected else [],
        "selected" if selected else "empty",
    )
    return source_id


def _service(workspace: Path) -> AgentNativeService:
    return AgentNativeService(
        workspace,
        locator_factory=FakeAgentLocator,
        opener=lambda _path: None,
    )


def test_selection_tree_replays_persisted_disabled_state(tmp_path, monkeypatch) -> None:
    target = tmp_path / "native.md"
    target.write_text("memory", encoding="utf-8")
    source_id = _seed_source(target, enabled=False, selected=False)

    tree = _service(tmp_path).get_selection_tree("agent-1")
    file_item = tree["scopes"][0]["categories"][0]["files"][0]

    assert file_item["default_selected"] is True
    assert file_item["saved_selected"] is False
    assert file_item["source_root_id"] == source_id


def test_commit_selection_can_disable_all_visible_sources(tmp_path, monkeypatch) -> None:
    target = tmp_path / "native.md"
    target.write_text("memory", encoding="utf-8")
    source_id = _seed_source(target, enabled=True, selected=True)

    result = _service(tmp_path).commit_selection("agent-1", [])
    reloaded = next(
        row for row in ContentStore(tmp_path).list_source_connectors(
            workspace_id=str(tmp_path.resolve())
        ) if row["source_id"] == source_id
    )

    assert result["source_count"] == 0
    assert result["disabled_source_count"] == 1
    assert reloaded["enabled"] == 0
    assert GroupControlService(tmp_path).selected_source_ids("agent-1") == []


def test_commit_selection_reenables_persisted_disabled_source(tmp_path, monkeypatch) -> None:
    target = tmp_path / "native.md"
    target.write_text("memory", encoding="utf-8")
    source_id = _seed_source(target, enabled=False, selected=False)

    result = _service(tmp_path).commit_selection(
        "agent-1", [{"path": str(target)}]
    )
    reloaded = next(
        row for row in ContentStore(tmp_path).list_source_connectors(
            workspace_id=str(tmp_path.resolve())
        ) if row["source_id"] == source_id
    )

    assert result["source_count"] == 1
    assert reloaded["enabled"] == 1
    assert GroupControlService(tmp_path).selected_source_ids("agent-1") == [source_id]
