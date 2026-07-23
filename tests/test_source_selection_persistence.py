from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.gui import GovernanceApi
from memoryguard.schema_v3 import SourceRootType
from memoryguard.source_registry import SourceRegistry


class FakeAgentLocator:
    def __init__(self, workspace):
        self.workspace = workspace

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


def test_selection_tree_replays_persisted_disabled_state(tmp_path, monkeypatch) -> None:
    target = tmp_path / "native.md"
    target.write_text("memory", encoding="utf-8")
    reg = SourceRegistry(tmp_path)
    root = reg.add(str(target), SourceRootType.SELECTED_FILE, "Native", scope="user")
    root.agent_instance_id = "agent-1"
    root.discovery_object_id = "disc-native"
    root.enabled = False
    reg._save()
    import memoryguard.agent_locator as agent_locator
    monkeypatch.setattr(agent_locator, "AgentLocator", FakeAgentLocator)

    tree = GovernanceApi(str(tmp_path)).get_selection_tree("agent-1")
    file_item = tree["scopes"][0]["categories"][0]["files"][0]

    assert file_item["default_selected"] is False
    assert file_item["saved_selected"] is False
    assert file_item["source_root_id"] == root.root_id


def test_commit_selection_can_disable_all_visible_sources(tmp_path, monkeypatch) -> None:
    target = tmp_path / "native.md"
    target.write_text("memory", encoding="utf-8")
    reg = SourceRegistry(tmp_path)
    root = reg.add(str(target), SourceRootType.SELECTED_FILE, "Native", scope="user")
    root.agent_instance_id = "agent-1"
    root.discovery_object_id = "disc-native"
    root.enabled = True
    reg._save()
    import memoryguard.agent_locator as agent_locator
    monkeypatch.setattr(agent_locator, "AgentLocator", FakeAgentLocator)

    result = GovernanceApi(str(tmp_path)).commit_selection("agent-1", [], confirmed=True)
    reloaded = SourceRegistry(tmp_path).get(root.root_id)

    assert result["total_selected"] == 0
    assert result["disabled_source_count"] == 1
    assert reloaded is not None
    assert reloaded.enabled is False


def test_commit_selection_reenables_persisted_disabled_source(tmp_path, monkeypatch) -> None:
    target = tmp_path / "native.md"
    target.write_text("memory", encoding="utf-8")
    reg = SourceRegistry(tmp_path)
    root = reg.add(str(target), SourceRootType.SELECTED_FILE, "Native", scope="user")
    root.agent_instance_id = "agent-1"
    root.discovery_object_id = "disc-native"
    root.enabled = False
    reg._save()
    import memoryguard.agent_locator as agent_locator
    monkeypatch.setattr(agent_locator, "AgentLocator", FakeAgentLocator)

    result = GovernanceApi(str(tmp_path)).commit_selection("agent-1", [{"discovery_object_id": "disc-native"}], confirmed=True)
    reloaded = SourceRegistry(tmp_path).get(root.root_id)

    assert result["total_selected"] == 1
    assert reloaded is not None
    assert reloaded.enabled is True
