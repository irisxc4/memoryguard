from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from memoryguard.runtime_v2.agent_native import AgentNativeService
from memoryguard.runtime_v2.group_native import GroupControlService


class _Ledger:
    def counts(self):
        return {"found": 1, "missing": 0, "unsupported": 0, "permission_denied": 0, "excluded_by_user": 0, "not_applicable": 0, "unaccounted_count": 0, "surface_count": 1}


class _Instance:
    def __init__(self, workspace: Path, data_path: Path):
        self.instance_id = "agent-instance-a"
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


class _Candidate:
    def __init__(self, path: Path):
        self.dir_path = str(path)
        self.product = "codex"

    def to_dict(self):
        return {
            "dir_path": self.dir_path,
            "dir_name": Path(self.dir_path).name,
            "product": self.product,
            "has_profile": True,
            "stale_status": "active",
            "marked_uninstalled": False,
            "mtime_iso": "",
            "size_bytes": 1,
            "file_count": 1,
            "days_since_modified": 0,
        }


class _Locator:
    def __init__(self, workspace: Path, data_path: Path):
        self.workspace = workspace
        self.data_path = data_path
        self.context = SimpleNamespace(platform="test", host_id="host-a")
        self.registry = SimpleNamespace(
            list_profiles=lambda: [SimpleNamespace(product="codex"), SimpleNamespace(product="cursor")]
        )

    def detect_instances(self):
        instance = _Instance(self.workspace, self.data_path)
        return [instance], {instance.instance_id: _Ledger()}

    def discover_candidates(self, **_kwargs):
        return [_Candidate(self.data_path)]

    def get_selection_tree(self, instance_id: str):
        if instance_id != "agent-instance-a":
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


def _service(tmp_path: Path, data_path: Path, opened: list[str] | None = None) -> AgentNativeService:
    return AgentNativeService(
        tmp_path,
        opener=(lambda path: opened.append(str(path))) if opened is not None else (lambda _path: None),
        locator_factory=lambda workspace: _Locator(workspace, data_path),
    )


def test_discovery_selection_and_content_connector_are_v2_native(tmp_path: Path) -> None:
    data = tmp_path / "agent-data"
    data.mkdir()
    (data / "memory.md").write_text("hello", encoding="utf-8")
    service = _service(tmp_path, data)

    discovered = service.discover_agents()
    assert discovered["instances"][0]["instance_id"] == "agent-instance-a"
    assert discovered["known_profile_count"] == 2
    assert discovered["known_products"] == ["codex", "cursor"]
    tree = service.get_selection_tree("agent-instance-a")
    source_id = tree["scopes"][0]["categories"][0]["files"][0]["source_root_id"]
    committed = service.commit_selection("agent-instance-a", [{"path": str(data)}])
    assert committed["source_ids"] == [source_id]
    assert GroupControlService(tmp_path).selected_source_ids("agent-instance-a") == [source_id]
    connectors = service.content.list_source_connectors(workspace_id=str(tmp_path.resolve()))
    assert connectors[0]["source_id"] == source_id
    assert connectors[0]["enabled"] == 1

    cleared = service.commit_selection("agent-instance-a", [])
    assert cleared["disabled_source_count"] == 1
    connectors = service.content.list_source_connectors(workspace_id=str(tmp_path.resolve()))
    assert connectors[0]["enabled"] == 0


def test_selection_commit_accepts_opaque_source_root_id(tmp_path: Path) -> None:
    """The GUI must submit the redaction-safe source token, not an absolute path."""
    data = tmp_path / "agent-data"
    data.mkdir()
    (data / "memory.md").write_text("hello", encoding="utf-8")
    service = _service(tmp_path, data)
    tree = service.get_selection_tree("agent-instance-a")
    source_id = tree["scopes"][0]["categories"][0]["files"][0]["source_root_id"]

    committed = service.commit_selection(
        "agent-instance-a",
        [{"source_root_id": source_id, "category": "native_memory"}],
    )

    assert committed["source_ids"] == [source_id]


def test_v2_agent_mark_archive_restore_delete_and_open(tmp_path: Path) -> None:
    data = tmp_path / "agent-data"
    data.mkdir()
    (data / "state.txt").write_text("state", encoding="utf-8")
    opened: list[str] = []
    service = _service(tmp_path, data, opened)
    candidate = service.discover_agents()["instances"][0]["candidate_id"]

    marked = service.mark_uninstalled(candidate, product="codex", dir_path=str(data), reason="test")
    assert marked["marked_uninstalled"] is True
    assert service.discover_agents()["instances"][0]["lifecycle_state"] == "ignored"
    service.unmark_uninstalled(candidate, product="codex")
    assert service.discover_agents()["instances"][0]["lifecycle_state"] == "installed"

    service.open_folder(dir_path=str(data), candidate_id=candidate)
    assert opened == [str(data.resolve())]

    archived = service.archive(candidate, dir_path=str(data), reason="cleanup")
    assert not data.exists()
    archive_id = archived["archive_id"]
    assert service.list_archives(candidate_id=candidate)["total"] == 1
    restored = service.restore(archive_id)
    assert restored["restored_to"] == str(data.resolve())
    assert data.exists()

    archived_again = service.archive(candidate, dir_path=str(data), reason="cleanup-2")
    deleted = service.delete_archive(archived_again["archive_id"])
    assert deleted["deleted"] is True
    assert deleted["cleanup_pending"] is False
    assert not data.exists()
    assert service.list_archives(candidate_id=candidate)["total"] == 0
    assert len(service.cleanup_history()["history"]) >= 4


def test_residual_cleanup_returns_safe_discovered_paths_and_archives(tmp_path: Path) -> None:
    data = tmp_path / "agent-data"
    data.mkdir()
    service = _service(tmp_path, data)
    result = service.residual_cleanup(instance_id="agent-instance-a")
    assert result["candidate_id"].startswith("agent-candidate-")
    assert result["items"][0]["path"] == str(data.resolve())
    assert result["archives"] == []


def test_agent_native_has_no_legacy_control_store_imports() -> None:
    source = Path("src/memoryguard/runtime_v2/agent_native.py").read_text(encoding="utf-8")
    for text in (
        "from ..agent_cleanup import", "from ..source_registry import",
        "from ..agent_binding import", "from ..shared_memory_store import",
    ):
        assert text not in source


def test_active_binding_keeps_private_data_agent_out_of_residual_bucket(tmp_path: Path) -> None:
    data = tmp_path / "agent-data"
    data.mkdir()
    service = _service(tmp_path, data)
    GroupControlService(tmp_path, write=True).bind_agent(
        agent_instance_id="agent-instance-a",
        share_group_id="shared-existing",
        mcp_server_name="memoryguard",
        native_memory_mode="redirected",
        redirect_paths=[],
    )

    listed = service.list_agents()

    assert listed["residuals"] == []
    assert listed["agents"][0]["binding_status"] == "active"
    assert listed["agents"][0]["binding"]["share_group_id"] == "shared-existing"
