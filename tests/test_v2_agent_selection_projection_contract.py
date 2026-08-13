from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from memoryguard.content.store import ContentStore
from memoryguard.runtime_v2.agent_native import AgentNativeService
from memoryguard.runtime_v2.group_native import GroupControlService


class _Ledger:
    def counts(self):
        return {
            "found": 1,
            "missing": 0,
            "unsupported": 0,
            "permission_denied": 0,
            "excluded_by_user": 0,
            "not_applicable": 0,
            "unaccounted_count": 0,
            "surface_count": 1,
        }


class _Instance:
    def __init__(self, workspace: Path, data_path: Path, instance_id: str = "agent-a"):
        self.instance_id = instance_id
        self.product = "codex"
        self.profile_id = "codex-profile"
        self.target_capability = SimpleNamespace(value="mcp")
        self.surfaces = [{
            "surface_id": "native-memory",
            "resolved_path": str(data_path),
            "path_template": str(data_path),
            "status": "found",
            "surface_role": "memory",
            "scope": "user",
            "category": "native_memory",
            "ingestion_policy": "import_verbatim",
            "ownership": "agent_managed",
            "target_role": "takeover_input",
        }]
        self.workspace = str(workspace)

    def to_dict(self):
        return {
            "instance_id": self.instance_id,
            "product": self.product,
            "profile_id": self.profile_id,
            "target_capability": "mcp",
            "surfaces": list(self.surfaces),
            "platform": "test",
            "host_id": "host-a",
        }


class _Locator:
    def __init__(self, workspace: Path, data_path: Path):
        self.workspace = workspace
        self.data_path = data_path
        self.context = SimpleNamespace(platform="test", host_id="host-a")
        self.registry = SimpleNamespace(list_profiles=lambda: [SimpleNamespace(product="codex")])

    def detect_instances(self):
        instance = _Instance(self.workspace, self.data_path)
        return [instance], {instance.instance_id: _Ledger()}

    def get_selection_tree(self, instance_id: str):
        if instance_id != "agent-a":
            return {"error": "not found"}
        return {
            "instance_id": instance_id,
            "product": "codex",
            "scopes": [{
                "scope": "user",
                "categories": [{
                    "category": "native_memory",
                    "files": [{
                        "path": str(self.data_path),
                        "surface_id": "native-memory",
                        "discovery_object_id": "disc-a",
                        "default_selected": True,
                    }],
                }],
                "projects": [],
            }],
        }


def _service(workspace: Path, data_path: Path) -> AgentNativeService:
    return AgentNativeService(
        workspace,
        locator_factory=lambda root: _Locator(root, data_path),
        opener=lambda _path: None,
    )


def _visible_file(workspace: Path, data_path: Path) -> dict[str, str]:
    tree = _service(workspace, data_path).get_selection_tree("agent-a")
    return tree["scopes"][0]["categories"][0]["files"][0]


def _connectors(workspace: Path, *, enabled: bool | None = None) -> list[dict]:
    return ContentStore(workspace).list_source_connectors(
        workspace_id=str(workspace.resolve()), enabled=enabled,
    )


def test_agent_card_without_tree_confirmation_is_not_a_build_source(tmp_path: Path) -> None:
    data = tmp_path / "agent-data"
    data.mkdir()
    (data / "memory.md").write_text("native memory", encoding="utf-8")
    service = _service(tmp_path, data)

    listed = service.list_agents()

    assert listed["agents"][0]["instance_id"] == "agent-a"
    assert listed["agents"][0]["bound_source_count"] == 0
    assert _connectors(tmp_path) == []


def test_bound_source_count_matches_enabled_connector_and_clear_is_visible(tmp_path: Path) -> None:
    data = tmp_path / "agent-data"
    data.mkdir()
    (data / "memory.md").write_text("native memory", encoding="utf-8")
    service = _service(tmp_path, data)
    file_row = _visible_file(tmp_path, data)

    committed = service.commit_selection("agent-a", [{"source_root_id": file_row["source_root_id"]}])
    listed = service.list_agents()["agents"][0]

    assert committed["source_count"] == 1
    assert listed["bound_source_count"] == 1
    assert len(_connectors(tmp_path, enabled=True)) == 1
    assert _connectors(tmp_path, enabled=True)[0]["source_id"] == committed["source_ids"][0]

    ContentStore(tmp_path).set_source_connector_enabled(
        committed["source_ids"][0], False, workspace_id=str(tmp_path.resolve()),
    )
    assert service.list_agents()["agents"][0]["bound_source_count"] == 0
    assert service.get_agent_data("agent-a")["source_count"] == 0

    cleared = service.commit_selection("agent-a", [])
    assert cleared["source_count"] == 0
    assert cleared["disabled_source_count"] == 1
    assert service.list_agents()["agents"][0]["bound_source_count"] == 0
    assert len(_connectors(tmp_path, enabled=False)) == 1


def test_manifest_failure_rolls_back_new_source_connector(tmp_path: Path, monkeypatch) -> None:
    data = tmp_path / "agent-data"
    data.mkdir()
    (data / "memory.md").write_text("native memory", encoding="utf-8")
    service = _service(tmp_path, data)
    file_row = _visible_file(tmp_path, data)

    def fail_record_selection(*_args, **_kwargs):
        raise RuntimeError("injected manifest failure")

    monkeypatch.setattr(GroupControlService, "record_selection", fail_record_selection)
    with pytest.raises(RuntimeError, match="injected manifest failure"):
        service.commit_selection("agent-a", [{"source_root_id": file_row["source_root_id"]}])

    assert GroupControlService(tmp_path).selected_source_ids("agent-a") == []
    assert _connectors(tmp_path) == []
