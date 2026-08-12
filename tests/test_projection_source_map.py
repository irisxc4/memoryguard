from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.content.store import ContentStore
from memoryguard.projection_v2 import ProjectionReadScope
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.projection_build import ProjectionBuildService
from memoryguard.runtime_v2.source_control import SourceControlService
from memoryguard.security import MUTATION_API_METHODS, READONLY_API_METHODS


def _scope(workspace: Path, *, agent: str = "agent-1", group: str = "") -> ProjectionReadScope:
    return ProjectionReadScope(
        workspace_id=str(workspace.resolve()),
        agent_instance_id=agent,
        project_ref=str(workspace.resolve()),
        provider="test",
        share_group_id=group,
    )


def _connector(
    workspace: Path,
    source_id: str,
    path: Path,
    *,
    provider: str,
    source_type: str,
    enabled: bool = True,
) -> str:
    ContentStore(workspace).upsert_source_connector(
        source_id=source_id,
        provider=provider,
        source_type=source_type,
        external_root_key=str(path.resolve()),
        workspace_id=str(workspace.resolve()),
        enabled=enabled,
    )
    return source_id


def _select(workspace: Path, agent: str, source_ids: list[str]) -> None:
    GroupControlService(workspace, write=True).record_selection(
        agent, source_ids, f"selection-{agent}-{'-'.join(sorted(source_ids))}",
    )


def test_projection_source_map_distinguishes_v2_connector_origins(tmp_path) -> None:
    native_dir = tmp_path / "native"
    docs_dir = tmp_path / "docs"
    sessions_dir = tmp_path / "sessions"
    native_dir.mkdir()
    docs_dir.mkdir()
    sessions_dir.mkdir()
    native_id = _connector(
        tmp_path, "source-native", native_dir,
        provider="agent-native", source_type="directory",
    )
    logical_id = _connector(
        tmp_path, "source-knowledge", docs_dir,
        provider="knowledge-library", source_type="directory",
    )
    evidence_id = _connector(
        tmp_path, "source-evidence", sessions_dir,
        provider="conversation-history", source_type="directory",
    )
    _select(tmp_path, "agent-1", [native_id, logical_id, evidence_id])

    source_map = ProjectionBuildService(tmp_path).source_map(scope=_scope(tmp_path))
    entries = {entry["source_id"]: entry for entry in source_map["entries"]}

    assert entries[native_id]["provider"] == "agent-native"
    assert entries[logical_id]["provider"] == "knowledge-library"
    assert entries[evidence_id]["provider"] == "conversation-history"
    assert all(entry["source_type"] == "directory" for entry in entries.values())
    assert source_map["summary"] == {"total": 3, "enabled": 3}
    visible = SourceControlService(tmp_path).list_sources({
        "is_admin": False, "trusted_agent_id": "agent-1",
    })
    assert {item["source_id"] for item in visible["sources"]} == set(entries)


def test_projection_source_map_requires_explicit_project_authorization(tmp_path) -> None:
    project_dir = tmp_path / "project"
    native_dir = tmp_path / "native"
    project_dir.mkdir()
    native_dir.mkdir()
    native_id = _connector(
        tmp_path, "source-native", native_dir,
        provider="agent-native", source_type="directory",
    )
    project_id = _connector(
        tmp_path, "source-project", project_dir,
        provider="project-workspace", source_type="directory",
    )
    _select(tmp_path, "agent-1", [native_id])

    source_map = ProjectionBuildService(tmp_path).source_map(scope=_scope(tmp_path))
    assert {item["source_id"] for item in source_map["entries"]} == {
        native_id, project_id,
    }
    before = SourceControlService(tmp_path).list_sources({
        "is_admin": False, "trusted_agent_id": "agent-1",
    })
    assert {item["source_id"] for item in before["sources"]} == {native_id}

    _select(tmp_path, "agent-1", [native_id, project_id])
    after = SourceControlService(tmp_path).list_sources({
        "is_admin": False, "trusted_agent_id": "agent-1",
    })
    assert {item["source_id"] for item in after["sources"]} == {
        native_id, project_id,
    }


def test_projection_source_enabled_toggle_is_persisted(tmp_path) -> None:
    source_dir = tmp_path / "docs"
    source_dir.mkdir()
    source_id = _connector(
        tmp_path, "source-toggle", source_dir,
        provider="knowledge-library", source_type="directory",
    )
    result = ProjectionBuildService(tmp_path).set_source_enabled(
        source_id, False, scope=_scope(tmp_path),
    )
    reloaded = next(
        row for row in ContentStore(tmp_path).list_source_connectors(
            workspace_id=str(tmp_path.resolve())
        ) if row["source_id"] == source_id
    )

    assert result["ok"] is True
    assert reloaded["enabled"] == 0
    entry = next(
        item for item in ProjectionBuildService(tmp_path).source_map(
            scope=_scope(tmp_path)
        )["entries"] if item["source_id"] == source_id
    )
    assert entry["enabled"] is False


def test_projection_source_api_methods_are_registered_with_security_policy() -> None:
    assert "get_projection_source_map" in READONLY_API_METHODS
    assert "get_governance_scope" in READONLY_API_METHODS
    assert "set_projection_source_enabled" in MUTATION_API_METHODS
    assert "set_governance_scope" in MUTATION_API_METHODS


def test_share_group_source_map_reports_v2_connector_origins(tmp_path) -> None:
    """共享组 source map 读取 V2 connector 元数据。"""
    source_file = tmp_path / "native-memory.md"
    source_file.write_text("真实原生记忆", encoding="utf-8")
    source_id = _connector(
        tmp_path, "source-shared-native", source_file,
        provider="agent-native", source_type="file",
    )
    gid = "source-map-group"
    _select(tmp_path, "agent-a", [source_id])

    result = ProjectionBuildService(tmp_path).source_map(
        scope=_scope(tmp_path, agent="agent-a", group=gid),
    )

    assert result["summary"] == {"total": 1, "enabled": 1}
    entry = result["entries"][0]
    assert entry["source_id"] == source_id
    assert entry["provider"] == "agent-native"
    assert entry["source_type"] == "file"
    assert entry["enabled"] is True
