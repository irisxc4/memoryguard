from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.governance_scope import grant_root_to_agent
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
    grant_root_to_agent(native, "agent-1")
    native.surface_id = "native-memory"
    logical = reg.add(str(docs_dir), SourceRootType.SELECTED_DIRECTORY, "Docs", scope="project")
    logical.source_category = "knowledge_source"
    logical.ingestion_policy = "extract_candidates"
    logical.project_ref = "project-a"
    grant_root_to_agent(logical, "agent-1")
    evidence = reg.add(str(sessions_dir), SourceRootType.SELECTED_DIRECTORY, "Sessions", scope="project")
    evidence.source_category = "conversation_history"
    evidence.ingestion_policy = "evidence_only"
    grant_root_to_agent(evidence, "agent-1")
    reg._save()

    source_map = GovernanceApi(str(tmp_path)).get_projection_source_map(agent_instance_id="agent-1")
    modes = {entry["display_name"]: entry["projection_mode"] for entry in source_map["entries"]}

    assert modes["Native"] == "native_memory_projection"
    assert modes["Docs"] == "logical_reconstruction_projection"
    assert modes["Sessions"] == "evidence_only"
    assert source_map["summary"]["native_memory"] == 1
    assert source_map["summary"]["logical_reconstruction"] >= 1
    assert source_map["summary"]["evidence_only"] == 1


def test_projection_source_map_requires_explicit_project_authorization(tmp_path) -> None:
    project_dir = tmp_path / "project"
    native_dir = tmp_path / "native"
    project_dir.mkdir()
    native_dir.mkdir()
    reg = SourceRegistry(tmp_path)
    native = reg.add(str(native_dir), SourceRootType.SELECTED_DIRECTORY, "Native", scope="user")
    native.source_category = "native_memory"
    grant_root_to_agent(native, "agent-1")
    native.surface_id = "native-memory"
    project = reg.add(str(project_dir), SourceRootType.SELECTED_DIRECTORY, "项目目录", scope="project")
    project.source_category = "unknown"
    project.scope_source = "fallback"
    project.ingestion_policy = "extract_candidates"
    # 未授权时不应出现在 agent-1 的 source map
    reg._save()

    api = GovernanceApi(str(tmp_path))
    before = api.get_projection_source_map(agent_instance_id="agent-1")
    assert all(item["root_id"] != project.root_id for item in before["entries"])

    grant_root_to_agent(project, "agent-1")
    reg._save()
    source_map = api.get_projection_source_map(agent_instance_id="agent-1")
    entry = next(item for item in source_map["entries"] if item["root_id"] == project.root_id)

    assert entry["agent_instance_id"] == "agent-1"
    assert entry["surface_id"] == "project_workspace"
    assert entry["source_category"] == "knowledge_source"
    assert entry["scope_source"] == "project_workspace"
    assert entry["project_ref"] == "project"
    assert entry["projection_mode"] == "logical_reconstruction_projection"
    assert "agent-1" in entry["authorized_agent_ids"]


def test_projection_source_enabled_toggle_is_persisted(tmp_path) -> None:
    source_dir = tmp_path / "docs"
    source_dir.mkdir()
    reg = SourceRegistry(tmp_path)
    root = reg.add(str(source_dir), SourceRootType.SELECTED_DIRECTORY, "Docs", scope="project")
    root.source_category = "knowledge_source"
    root.ingestion_policy = "extract_candidates"
    grant_root_to_agent(root, "agent-1")
    reg._save()

    result = GovernanceApi(str(tmp_path)).set_projection_source_enabled(
        root.root_id, False, agent_instance_id="agent-1",
    )
    reloaded = SourceRegistry(tmp_path).get(root.root_id)

    assert result["ok"] is True
    assert reloaded is not None
    assert reloaded.enabled is False
    entry = next(item for item in result["source_map"]["entries"] if item["root_id"] == root.root_id)
    assert entry["enabled"] is False


def test_projection_source_api_methods_are_registered_with_security_policy() -> None:
    assert "get_projection_source_map" in READONLY_API_METHODS
    assert "get_governance_scope" in READONLY_API_METHODS
    assert "set_projection_source_enabled" in MUTATION_API_METHODS
    assert "set_governance_scope" in MUTATION_API_METHODS


def test_share_group_source_map_reports_actual_ingest_origins(tmp_path) -> None:
    """共享图来源必须来自 active 记录的入库证据，不能固定显示 0/0。"""
    from memoryguard.schema_v3 import (
        MemoryEvent,
        MemoryKind,
        Provenance,
        SharedMemoryRecord,
        SharedMemoryStatus,
        _now_iso,
        stable_hash,
    )
    from memoryguard.shared_memory_store import SharedMemoryStore

    source_file = tmp_path / "native-memory.md"
    source_file.write_text("真实原生记忆", encoding="utf-8")
    reg = SourceRegistry(tmp_path)
    root = reg.add(
        str(source_file),
        SourceRootType.SELECTED_FILE,
        "Agent 原生记忆",
        scope="user",
    )
    root.source_category = "native_memory"
    root.ingestion_policy = "import_verbatim"
    root.agent_instance_id = "agent-a"
    reg._save()

    gid = "source-map-group"
    now = _now_iso()
    event = MemoryEvent(
        event_id="event-import-1",
        agent_instance_id="agent-a",
        share_group_id=gid,
        raw_content="真实原生记忆",
        metadata={
            "source_root_id": root.root_id,
            "relative_path": "native-memory.md",
            "extraction_origin": "native_memory_import",
            "source_category": "native_memory",
        },
        created_at=now,
    )
    store = SharedMemoryStore(tmp_path, gid)
    store.append_event(event)
    store.append_record(SharedMemoryRecord(
        memory_id="memory-import-1",
        body="真实原生记忆",
        kind=MemoryKind.FACT,
        status=SharedMemoryStatus.ACTIVE,
        confidence=0.9,
        provenance=[Provenance(
            source_object_id=event.event_id,
            locator="event",
            excerpt_hash=stable_hash("真实原生记忆")[:16],
        )],
        created_at=now,
        updated_at=now,
        agent_instance_id="agent-a",
    ))

    result = GovernanceApi(str(tmp_path)).get_projection_source_map(
        share_group_id=gid,
    )

    assert result["projection_kind"] == "shared_memory_projection"
    assert result["summary"]["total"] == 1
    assert result["summary"]["shared_memory"] == 1
    entry = result["entries"][0]
    assert entry["root_id"] == root.root_id
    assert entry["projection_mode"] == "shared_memory_projection"
    assert entry["record_count"] == 1
    assert entry["is_shared_memory_origin"] is True
