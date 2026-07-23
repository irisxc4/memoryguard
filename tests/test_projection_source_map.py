from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.gui import GovernanceApi
from memoryguard.schema_v3 import SourceRootType
from memoryguard.security import MUTATION_API_METHODS, READONLY_API_METHODS
from memoryguard.source_registry import SourceRegistry


def test_projection_source_map_distinguishes_native_logical_and_evidence_sources(tmp_path) -> None:
    native_dir = tmp_path / "native"
    docs_dir = tmp_path / "docs"
    sessions_dir = tmp_path / "sessions"
    native_dir.mkdir()
    docs_dir.mkdir()
    sessions_dir.mkdir()
    reg = SourceRegistry(tmp_path)
    native = reg.add(str(native_dir), SourceRootType.SELECTED_DIRECTORY, "Native", scope="user")
    native.source_category = "native_memory"
    native.ingestion_policy = "extract_candidates"
    native.agent_instance_id = "agent-1"
    native.surface_id = "native-memory"
    logical = reg.add(str(docs_dir), SourceRootType.SELECTED_DIRECTORY, "Docs", scope="project")
    logical.source_category = "knowledge_source"
    logical.ingestion_policy = "extract_candidates"
    logical.project_ref = "project-a"
    evidence = reg.add(str(sessions_dir), SourceRootType.SELECTED_DIRECTORY, "Sessions", scope="project")
    evidence.source_category = "conversation_history"
    evidence.ingestion_policy = "evidence_only"
    reg._save()

    source_map = GovernanceApi(str(tmp_path)).get_projection_source_map()
    modes = {entry["display_name"]: entry["projection_mode"] for entry in source_map["entries"]}

    assert modes["Native"] == "native_memory_projection"
    assert modes["Docs"] == "logical_reconstruction_projection"
    assert modes["Sessions"] == "evidence_only"
    assert source_map["summary"]["native_memory"] == 1
    assert source_map["summary"]["logical_reconstruction"] >= 1
    assert source_map["summary"]["evidence_only"] == 1


def test_projection_source_map_binds_fallback_project_source_to_single_agent(tmp_path) -> None:
    project_dir = tmp_path / "project"
    native_dir = tmp_path / "native"
    project_dir.mkdir()
    native_dir.mkdir()
    reg = SourceRegistry(tmp_path)
    native = reg.add(str(native_dir), SourceRootType.SELECTED_DIRECTORY, "Native", scope="user")
    native.source_category = "native_memory"
    native.agent_instance_id = "agent-1"
    native.surface_id = "native-memory"
    project = reg.add(str(project_dir), SourceRootType.SELECTED_DIRECTORY, "项目目录", scope="project")
    project.source_category = "unknown"
    project.scope_source = "fallback"
    project.ingestion_policy = "extract_candidates"
    reg._save()

    source_map = GovernanceApi(str(tmp_path)).get_projection_source_map()
    entry = next(item for item in source_map["entries"] if item["root_id"] == project.root_id)

    assert entry["agent_instance_id"] == "agent-1"
    assert entry["surface_id"] == "project_workspace"
    assert entry["source_category"] == "knowledge_source"
    assert entry["scope_source"] == "project_workspace"
    assert entry["project_ref"] == "project"
    assert entry["projection_mode"] == "logical_reconstruction_projection"


def test_projection_source_enabled_toggle_is_persisted(tmp_path) -> None:
    source_dir = tmp_path / "docs"
    source_dir.mkdir()
    reg = SourceRegistry(tmp_path)
    root = reg.add(str(source_dir), SourceRootType.SELECTED_DIRECTORY, "Docs", scope="project")
    root.source_category = "knowledge_source"
    root.ingestion_policy = "extract_candidates"
    reg._save()

    result = GovernanceApi(str(tmp_path)).set_projection_source_enabled(root.root_id, False)
    reloaded = SourceRegistry(tmp_path).get(root.root_id)

    assert result["ok"] is True
    assert reloaded is not None
    assert reloaded.enabled is False
    entry = next(item for item in result["source_map"]["entries"] if item["root_id"] == root.root_id)
    assert entry["enabled"] is False


def test_projection_source_api_methods_are_registered_with_security_policy() -> None:
    assert "get_projection_source_map" in READONLY_API_METHODS
    assert "set_projection_source_enabled" in MUTATION_API_METHODS
