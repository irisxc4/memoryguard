"""V2 本地知识库卡片与删除映射的行为验收。"""

from memoryguard.content.store import ContentStore
from memoryguard.interactive import render_interactive_html
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.source_control import SourceControlService


def _admin_context() -> dict[str, bool]:
    return {"is_admin": True}


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
    service = SourceControlService(workspace)

    added = service.add(
        str(knowledge), "selected_directory", _admin_context(),
        display_name="项目知识库",
    )
    root_id = added["root_id"]
    listed = service.list_sources(_admin_context())["sources"]
    source = next(item for item in listed if item["root_id"] == root_id)

    assert source["path_exists"] is True
    assert source["type"] == "selected_directory"

    removed = service.remove(root_id, _admin_context())

    assert removed["ok"] is True
    assert removed["source_id"] == root_id
    assert knowledge.is_dir()
    assert note.read_text(encoding="utf-8") == "MemoryGuard 只接管长期记忆。"
    assert root_id not in {
        item["root_id"] for item in service.list_sources(_admin_context())["sources"]
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
    service = SourceControlService(workspace)

    roots = [
        service.add(str(library), "selected_directory", _admin_context())["root_id"],
        service.add(str(document), "selected_file", _admin_context())["root_id"],
        service.add(str(vault), "directory", _admin_context())["root_id"],
    ]
    sources = {
        item["root_id"]: item
        for item in service.list_sources(_admin_context())["sources"]
    }

    assert [sources[root_id]["type"] for root_id in roots] == [
        "selected_directory", "selected_file", "obsidian_vault",
    ]
    assert all(sources[root_id]["scope"] == "workspace" for root_id in roots)
    assert all(sources[root_id]["enabled"] is True for root_id in roots)

    scanned_roots = {
        group["root_id"]
        for group in service.raw_summary(_admin_context())["groups"]
    }
    assert set(roots) <= scanned_roots
    agent_view = service.list_sources(
        {"is_admin": False, "trusted_agent_id": "manual-user-root"}
    )
    assert agent_view["sources"] == []


def test_add_source_preserves_existing_agent_owned_unknown_root(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    document = tmp_path / "agent-memory.md"
    workspace.mkdir()
    document.write_text("Agent-managed source", encoding="utf-8")
    source_id = "agent-source-existing"
    ContentStore(workspace).upsert_source_connector(
        source_id=source_id,
        provider="codex",
        source_type="file",
        external_root_key=str(document.resolve()),
        workspace_id=str(workspace.resolve()),
        enabled=True,
    )
    GroupControlService(workspace, write=True).record_selection(
        "agent-1", [source_id], "agent-selection",
    )

    result = SourceControlService(workspace).add(
        str(document), "selected_file", _admin_context(),
    )
    source = next(
        item
        for item in SourceControlService(workspace).list_sources(_admin_context())["sources"]
        if item["root_id"] == result["root_id"]
    )

    assert result["root_id"] == source_id
    assert result["changed"] is False
    assert source["type"] == "selected_file"
    assert GroupControlService(workspace).selected_source_ids("agent-1") == [source_id]
