"""本地知识库卡片与删除映射的行为验收。"""

from memoryguard.gui import GovernanceApi
from memoryguard.interactive import render_interactive_html
from memoryguard.schema_v3 import SourceRootType
from memoryguard.source_registry import SourceRegistry


def test_data_page_renders_local_knowledge_library_controls() -> None:
    html = render_interactive_html()

    assert "<h2>本地知识库</h2>" in html
    assert "+ 添加文件夹或文件" in html
    assert "删除映射" in html
    assert "仅移除 MemoryGuard 映射，磁盘中的原文件不会被删除" in html
    assert "knowledgeSources.length" in html
    assert "rawGroupsByRoot" in html


def test_remove_source_deletes_mapping_but_preserves_original_folder(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    knowledge = tmp_path / "notes"
    workspace.mkdir()
    knowledge.mkdir()
    note = knowledge / "decision.md"
    note.write_text("MemoryGuard 只接管长期记忆。", encoding="utf-8")
    api = GovernanceApi(str(workspace))

    added = api.add_source(
        str(knowledge), "selected_directory", "项目知识库", confirmed=True,
    )
    root_id = added["root_id"]
    listed = api.list_sources()["sources"]
    source = next(item for item in listed if item["root_id"] == root_id)

    assert source["path_exists"] is True
    assert source["path_kind"] == "directory"

    removed = api.remove_source(root_id, confirmed=True)

    assert removed == {"ok": True}
    assert knowledge.is_dir()
    assert note.read_text(encoding="utf-8") == "MemoryGuard 只接管长期记忆。"
    assert root_id not in {
        item["root_id"] for item in api.list_sources()["sources"]
    }


def test_manual_libraries_are_independent_of_agent_scopes(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    library = tmp_path / "library"
    vault = tmp_path / "vault"
    document = tmp_path / "reference.md"
    workspace.mkdir()
    library.mkdir()
    vault.mkdir()
    (library / "note.md").write_text("需要先萃取。", encoding="utf-8")
    (vault / ".obsidian").mkdir()
    (vault / "vault-note.md").write_text("Obsidian note", encoding="utf-8")
    document.write_text("普通文档", encoding="utf-8")
    api = GovernanceApi(str(workspace))

    roots = [
        api.add_source(str(library), "selected_directory", confirmed=True)["root_id"],
        api.add_source(str(document), "selected_file", confirmed=True)["root_id"],
        api.add_source(str(vault), "directory", confirmed=True)["root_id"],
    ]
    sources = {item["root_id"]: item for item in api.list_sources()["sources"]}

    assert "src-project-default" in sources
    assert sources["src-project-default"]["type"] == "project_directory"
    assert [sources[root_id]["type"] for root_id in roots] == [
        "selected_directory", "selected_file", "obsidian_vault",
    ]
    assert all(sources[root_id]["source_category"] == "knowledge_source" for root_id in roots)
    assert all(sources[root_id]["agent_instance_id"] == "" for root_id in roots)
    assert all(sources[root_id]["scope_source"] == "fallback" for root_id in roots)

    scanned_roots = {group["root_id"] for group in api.get_raw_memory()["groups"]}
    assert set(roots) <= scanned_roots
    agent_files = [
        file_info
        for category in api.get_agent_data("manual-user-root")["categories"].values()
        for file_info in category
    ]
    assert not ({file_info["root_id"] for file_info in agent_files} & set(roots))


def test_add_source_preserves_existing_agent_owned_unknown_root(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    document = tmp_path / "agent-memory.md"
    workspace.mkdir()
    document.write_text("Agent-managed source", encoding="utf-8")
    registry = SourceRegistry(workspace)
    existing = registry.add(str(document), SourceRootType.SELECTED_FILE, "Agent memory")
    existing.agent_instance_id = "agent-1"
    existing.source_category = "unknown"
    registry._save()

    result = GovernanceApi(str(workspace)).add_source(
        str(document), "selected_file", confirmed=True,
    )
    source = next(
        item for item in GovernanceApi(str(workspace)).list_sources()["sources"]
        if item["root_id"] == result["root_id"]
    )

    assert result["root_id"] == existing.root_id
    assert source["agent_instance_id"] == "agent-1"
    assert source["source_category"] == "unknown"
