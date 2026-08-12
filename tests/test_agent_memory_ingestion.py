"""Agent discovery and V2-native memory ingestion coverage."""
from __future__ import annotations

from pathlib import Path

import pytest

from memoryguard.access_context import AccessContext
from memoryguard.agent_locator import AgentLocator
from memoryguard.agent_profiles import (
    _claude_code_profile,
    _codex_profile,
    _cursor_profile,
    _trae_profile,
)
from memoryguard.content.store import ContentStore
from memoryguard.evidence.store import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2
from memoryguard.memory import MemoryAtomStore, MemoryReadScope
from memoryguard.runtime_v2.extraction_native import NativeExtractionEnrichmentService
from memoryguard.runtime_v2.native_ports import bind_native_transport_context
from memoryguard.runtime_v2.source_control import SourceControlError, SourceControlService
from memoryguard.schema_v3 import IngestionPolicy, SourceCategory


FIXTURES = Path(__file__).parent / "fixtures" / "agent_memories"
HOME = FIXTURES / "home"
APPDATA = FIXTURES / "appdata"


@pytest.fixture(scope="module", autouse=True)
def _ensure_fixtures():
    from _build_agent_memory_fixtures import main

    main()


@pytest.fixture
def fake_home(monkeypatch):
    home = HOME.resolve()
    appdata = APPDATA.resolve()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.setenv("APPDATA", str(appdata))
    return home


def _context(tmp_path: Path, agent: str = "agent-a", group: str = "group-a"):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent,
            is_admin=False,
            strict_binding=True,
            allow_anon=False,
            session_id=f"session-{agent}",
            session_source="pytest",
            session_trusted=True,
        ),
        workspace_id=str(tmp_path.resolve()),
        share_group_id=group,
        project_ref=str(tmp_path.resolve()),
        provider="pytest",
        runtime_role="test",
    )


def _read_scope(tmp_path: Path, agent: str = "agent-a", group: str = "group-a") -> MemoryReadScope:
    return MemoryReadScope(
        workspace_id=str(tmp_path.resolve()),
        share_group_id=group,
        agent_instance_id=agent,
        project_ref=str(tmp_path.resolve()),
        provider="pytest",
        runtime_role="test",
    )


def _native_service(tmp_path: Path, source: Path, *, agent: str = "agent-a", group: str = "group-a"):
    ContentStore(tmp_path)
    MemoryAtomStore(tmp_path)
    EvidenceStore(tmp_path)
    GovernanceV2(tmp_path)
    SourceControlService(tmp_path).add(
        str(source),
        "selected_file" if source.is_file() else "selected_directory",
        {"admin": True, "agent_instance_id": agent},
        display_name=source.name,
    )
    return NativeExtractionEnrichmentService(tmp_path), _context(tmp_path, agent, group)


def test_claude_native_memory_import_verbatim_default(fake_home, tmp_path):
    profile = _claude_code_profile()
    memory = next(item for item in profile.surfaces if item.surface_id == "claude_project_native_memory")
    assert memory.ingestion_policy == IngestionPolicy.IMPORT_VERBATIM
    assert memory.category == SourceCategory.NATIVE_MEMORY

    locator = AgentLocator(tmp_path)
    instances, _ = locator.detect_instances()
    claude = next((item for item in instances if item.product == "claude-code"), None)
    assert claude is not None
    tree = locator.get_selection_tree(claude.instance_id)
    files = []
    for scope in tree.get("scopes", []):
        for project in scope.get("projects", []):
            for category in project.get("categories", []):
                files.extend(category.get("files", []))
        for category in scope.get("categories", []):
            files.extend(category.get("files", []))
    memory_files = [
        item for item in files
        if "memory" in item.get("path", "").replace("\\", "/") and item.get("path", "").endswith(".md")
    ]
    assert memory_files
    assert any(item.get("default_selected") for item in memory_files if item.get("ingestion_policy") == "import_verbatim")


def test_selection_tree_lists_second_third_level_files(fake_home, tmp_path):
    locator = AgentLocator(tmp_path)
    instances, _ = locator.detect_instances()
    by_product = {item.product: item for item in instances}
    assert {"claude-code", "cursor", "codex", "trae"}.issubset(by_product)

    def collect(instance_id: str) -> list[str]:
        paths = []
        tree = locator.get_selection_tree(instance_id)
        for scope in tree.get("scopes", []):
            for project in scope.get("projects", []):
                for category in project.get("categories", []):
                    paths.extend(item.get("path", "").replace("\\", "/") for item in category.get("files", []))
            for category in scope.get("categories", []):
                paths.extend(item.get("path", "").replace("\\", "/") for item in category.get("files", []))
        return paths

    assert any("/memory/user.md" in path for path in collect(by_product["claude-code"].instance_id))
    assert any("agent-transcripts" in path for path in collect(by_product["cursor"].instance_id))
    codex_paths = collect(by_product["codex"].instance_id)
    assert any("rollout-demo.jsonl" in path for path in codex_paths)
    assert any("/memories/preferences.md" in path for path in codex_paths)
    trae_paths = collect(by_product["trae"].instance_id)
    assert any(path.endswith("project_memory.md") for path in trae_paths)
    assert any("session_memory_1.jsonl" in path for path in trae_paths)


def test_session_jsonl_extract_candidates_are_staged_by_native_extraction(tmp_path):
    source = FIXTURES / "home/.claude/projects/demo-proj/sess-1.jsonl"
    service, context = _native_service(tmp_path, source)
    preview = service.extract({"source_path": str(source)}, context=context)
    assert preview["staging"] == "v2_content_plane"
    assert preview["candidates"]
    bodies = "\n".join(item["preview"] for item in preview["candidates"]).lower()
    assert "prefer short" in bodies
    assert all("bash" not in item["preview"].lower() for item in preview["candidates"])
    assert MemoryAtomStore(tmp_path, readonly=True).list_atoms(scope=_read_scope(tmp_path)) == []


def test_native_extraction_preview_does_not_auto_commit(tmp_path):
    source = FIXTURES / "home/.claude/projects/demo-proj/sess-1.jsonl"
    service, context = _native_service(tmp_path, source)
    preview = service.extract({"source_path": str(source)}, context=context)
    assert preview["candidates"]
    assert not (tmp_path / ".memoryguard" / "shared-memory").exists()
    assert MemoryAtomStore(tmp_path, readonly=True).list_atoms(scope=_read_scope(tmp_path)) == []


def test_plan_docs_and_project_extract_do_not_auto_ingest(tmp_path):
    project = tmp_path / "project"
    plans = project / ".cursor" / "plans"
    plans.mkdir(parents=True)
    plan = plans / "ship.plan.md"
    plan.write_text("# Ship\n\nDo the thing.\n", encoding="utf-8")
    notes = project / "docs" / "notes.md"
    notes.parent.mkdir(parents=True)
    notes.write_text("# Notes\n\nUseful fact.\n", encoding="utf-8")
    service, context = _native_service(tmp_path, project)
    previews = [service.extract({"source_path": str(path)}, context=context) for path in (plan, notes)]
    assert all(item["staging"] == "v2_content_plane" and item["candidates"] for item in previews)
    assert MemoryAtomStore(tmp_path, readonly=True).list_atoms(scope=_read_scope(tmp_path)) == []


def test_frontmatter_kind_survives_native_extraction(tmp_path):
    source = FIXTURES / "home/.claude/projects/demo-proj/memory/user.md"
    service, context = _native_service(tmp_path, source)
    preview = service.extract({"source_path": str(source)}, context=context)
    assert preview["candidates"]
    assert any(item["kind"] == "preference" for item in preview["candidates"])
    accepted = service.accept(
        {"extract_id": preview["extract_id"], "candidate_ids": [item["candidate_id"] for item in preview["candidates"]]},
        context=context,
    )
    assert accepted["total"] == len(preview["candidates"])
    atoms = MemoryAtomStore(tmp_path, readonly=True).list_atoms(scope=_read_scope(tmp_path))
    assert any(atom.kind == "preference" for atom in atoms)


def test_sqlite_source_is_reported_as_unsupported_by_source_control(tmp_path):
    database = FIXTURES / "home/.codex/state_5.sqlite"
    ContentStore(tmp_path)
    source = SourceControlService(tmp_path).add(
        str(database), "selected_file", {"admin": True, "agent_instance_id": "agent-a"}, display_name="state"
    )
    control = SourceControlService(tmp_path)
    summary = control.scan_summary({"admin": True})
    assert summary["coverage"]["candidate_count"] == 1
    assert summary["coverage"]["unsupported"] == 1
    item = control.raw_summary({"admin": True})["groups"][0]["files"][0]
    assert item["read_status"] == "unsupported"
    assert item["media_type"] == "application/octet-stream"
    with pytest.raises(SourceControlError, match="source_file_unsupported"):
        control.content_preview(source["source_id"], "", {"admin": True})


def test_trae_policies():
    profile = _trae_profile()
    by_id = {surface.surface_id: surface for surface in profile.surfaces}
    assert by_id["trae_user_profile"].ingestion_policy == IngestionPolicy.IMPORT_VERBATIM
    assert by_id["trae_project_memory"].ingestion_policy == IngestionPolicy.IMPORT_VERBATIM
    assert by_id["trae_session_memory"].ingestion_policy == IngestionPolicy.EXTRACT_CANDIDATES
    assert by_id["trae_topics"].category == SourceCategory.CONVERSATION_HISTORY


def test_codex_native_memories_surface():
    surface = {item.surface_id: item for item in _codex_profile().surfaces}["codex_native_memories"]
    assert surface.category == SourceCategory.NATIVE_MEMORY
    assert surface.ingestion_policy == IngestionPolicy.IMPORT_VERBATIM
    assert "MEMORY.md" in surface.file_globs
    assert any("rollout_summaries" in glob for glob in surface.file_globs)


def test_empty_native_memory_dir_still_selectable(tmp_path, monkeypatch):
    from memoryguard.schema_v3 import AgentInstance, DiscoveryLedger, TargetCapability

    home = tmp_path / "home"
    memories = home / ".codex" / "memories"
    memories.mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text(
        "[memories]\ngenerate_memories = false\nuse_memories = false\n", encoding="utf-8"
    )
    monkeypatch.setattr(Path, "home", lambda: home)
    instance = AgentInstance(
        instance_id="codex-test",
        profile_id="codex@profile-1",
        product="codex",
        profile_version="2",
        surfaces=[{
            "surface_id": "codex_native_memories",
            "resolved_path": str(memories),
            "status": "found",
            "scope": "user",
            "category": "native_memory",
            "ingestion_policy": "import_verbatim",
            "ownership": "agent_managed",
            "target_role": "takeover_input",
            "classification_confidence": 0.95,
            "file_globs": ["MEMORY.md", "memory_summary.md", "raw_memories.md", "*.md"],
        }],
        target_capability=TargetCapability.EXPORT_ONLY,
    )
    locator = AgentLocator(tmp_path)
    monkeypatch.setattr(AgentLocator, "detect_instances", lambda self: ([instance], {"codex-test": DiscoveryLedger(instance_id="codex-test")}))
    tree = locator.get_selection_tree("codex-test")
    files = [file for scope in tree.get("scopes", []) for category in scope.get("categories", []) for file in category.get("files", [])]
    assert files
    assert any(file.get("empty_glob_match") and file.get("selectable") for file in files)
    assert any(item.get("code") == "codex_memories_empty" for item in tree.get("discovery_notes", []))


def test_empty_file_globs_do_not_fallback_to_directory(fake_home, tmp_path):
    locator = AgentLocator(tmp_path)
    surface = {
        "surface_id": "empty_glob",
        "resolved_path": str(HOME / ".claude" / "projects"),
        "status": "found",
        "scope": "user",
        "category": "native_memory",
        "ingestion_policy": "import_verbatim",
        "file_globs": ["memory/DOES_NOT_EXIST_*.md"],
    }
    assert locator._expand_project_root(surface["resolved_path"], surface) == []


def test_native_accept_preserves_imported_body(tmp_path):
    source = FIXTURES / "home/.claude/projects/demo-proj/memory/user.md"
    content = source.read_text(encoding="utf-8")
    expected = content.split("---", 2)[-1].strip()
    service, context = _native_service(tmp_path, source)
    preview = service.extract({"source_path": str(source)}, context=context)
    accepted = service.accept(
        {"extract_id": preview["extract_id"], "candidate_ids": [item["candidate_id"] for item in preview["candidates"]]},
        context=context,
    )
    atoms = MemoryAtomStore(tmp_path, readonly=True).list_atoms(scope=_read_scope(tmp_path))
    assert accepted["total"] == len(atoms) > 0
    assert expected in {atom.body for atom in atoms}


def test_codex_sessions_not_year_as_project(fake_home, tmp_path):
    locator = AgentLocator(tmp_path)
    instances, _ = locator.detect_instances()
    codex = next(item for item in instances if item.product == "codex")
    tree = locator.get_selection_tree(codex.instance_id)
    refs = []
    for scope in tree.get("scopes", []):
        for project in scope.get("projects", []):
            refs.append(project.get("project_ref"))
        for category in scope.get("categories", []):
            refs.extend(file.get("project_ref", "") for file in category.get("files", []) if "rollout" in file.get("path", ""))
    assert "2026" not in refs


def test_profile_versions_bumped():
    assert _claude_code_profile().profile_version == "2"
    assert _codex_profile().profile_version == "2"
    assert _cursor_profile().profile_version == "2"
    assert _trae_profile().profile_version == "2"


def test_interactive_renders_user_scope_projects():
    source = Path(__file__).resolve().parents[1] / "src" / "memoryguard" / "interactive.py"
    text = source.read_text(encoding="utf-8")
    assert "MEMORY_SELECT_CATS" in text
    assert "renderScopeCategories" in text
    assert "for (const proj of projects)" in text
