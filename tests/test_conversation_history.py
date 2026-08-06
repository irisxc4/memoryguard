from pathlib import Path
import sqlite3

import pytest

from memoryguard.adapters import ChatGPTImportAdapter, ImportedConversation
from memoryguard.agent_binding import AgentBindingStore
from memoryguard.conversation_history import ConversationHistoryStore, HistoryScope
from memoryguard.history_api import handle_history_tool


def _conversation() -> ImportedConversation:
    return ImportedConversation(
        conv_id="source-conversation-1", title="Design review",
        messages=[
            {"role": "user", "content": "We need a local history index.", "created_at": "2026-07-30T00:00:00Z"},
            {"role": "assistant", "content": "Use SQLite FTS5 and keep raw chat outside long-term memory.", "created_at": "2026-07-30T00:01:00Z"},
        ],
    )


def _scope(agent="agent-a"):
    return HistoryScope(agent_instance_id=agent, project_ref="project-x", provider="chatgpt")


def test_history_is_agent_scoped_and_progressive(tmp_path: Path):
    store = ConversationHistoryStore(tmp_path)
    assert store.import_conversations([_conversation()], provider="chatgpt", scope=_scope()) == {"conversation_count": 1, "turn_count": 2}
    assert store.list_sessions(_scope("agent-b"))["sessions"] == []

    results = store.search(_scope(), "SQLite FTS5")
    assert len(results["results"]) == 1
    hit = results["results"][0]
    assert "content" not in hit  # search must not leak raw turns

    timeline = store.timeline(_scope(), hit["session_id"], hit["turn_id"], radius=1)
    assert "content_preview" in timeline["turns"][0]
    assert "content" not in timeline["turns"][0]

    raw = store.read(_scope(), turn_id=hit["turn_id"])
    assert raw["turn"]["content"].startswith("Use SQLite")


def test_list_sessions_returns_exact_scoped_total_even_for_empty_page(tmp_path: Path):
    store = ConversationHistoryStore(tmp_path)
    conversations = [
        ImportedConversation(conv_id=f"session-{index}", title=f"Session {index}", messages=[
            {"role": "user", "content": f"message {index}"},
        ])
        for index in range(3)
    ]
    store.import_conversations(conversations, provider="chatgpt", scope=_scope())
    # Other Agent history must not inflate this total.
    store.import_conversations([_conversation()], provider="chatgpt", scope=_scope("agent-b"))
    first_session = store.list_sessions(_scope(), limit=1)["sessions"][0]["session_id"]
    store.add_evidence_link(memory_id="m-1", session_id=first_session)

    page = store.list_sessions(_scope(), limit=1, offset=10)
    assert page["sessions"] == []
    assert page["total"] == 3
    assert store.list_sessions(_scope(), extracted=True)["total"] == 1
    assert store.list_sessions(_scope(), extracted=False)["total"] == 2
    assert store.list_sessions(_scope("agent-b"))["total"] == 1


def test_extract_preview_only_creates_evidence_backed_candidates(tmp_path: Path):
    store = ConversationHistoryStore(tmp_path)
    store.import_conversations([_conversation()], provider="chatgpt", scope=_scope())
    session_id = store.list_sessions(_scope())["sessions"][0]["session_id"]
    preview = store.extract_preview(_scope(), session_id)
    assert preview["written_to_long_term_memory"] is False
    assert preview["candidates"][0]["evidence"]["session_id"] == session_id


def test_delete_requires_explicit_scope_and_never_deletes_long_term_memory(tmp_path: Path):
    store = ConversationHistoryStore(tmp_path)
    store.import_conversations([_conversation()], provider="chatgpt", scope=_scope())
    session_id = store.list_sessions(_scope())["sessions"][0]["session_id"]
    store.add_evidence_link(memory_id="long-term-1", session_id=session_id)
    with pytest.raises(ValueError, match="history_delete_scope_required"):
        store.delete(_scope(), session_ids=[])
    deleted = store.delete(_scope(), session_ids=[session_id], invalidate_evidence=True)
    assert deleted["deleted_sessions"] == 1
    assert deleted["invalidated_evidence_links"] == 1
    assert deleted["long_term_memories_deleted"] == 0
    with store._connect() as conn:  # verify durable tombstone, not a dangling shared-memory delete
        row = conn.execute("SELECT status FROM evidence_links WHERE memory_id='long-term-1'").fetchone()
    assert row["status"] == "invalid"


def test_chatgpt_normalize_no_longer_turns_raw_messages_into_episodes():
    assert ChatGPTImportAdapter().normalize([_conversation()]) == []


def test_history_api_rejects_untrusted_requested_agent(tmp_path: Path):
    store = ConversationHistoryStore(tmp_path)
    store.import_conversations([_conversation()], provider="chatgpt", scope=_scope())
    args = {"query": "local history", "scope": {"agent_instance_id": "agent-b", "project_ref": "project-x"}}
    with pytest.raises(PermissionError, match="trusted_agent_scope_required"):
        handle_history_tool("memoryguard_history_search", args, workspace=str(tmp_path), trusted_agent_id="agent-a")


def test_history_api_defaults_scope_to_trusted_agent(tmp_path: Path):
    AgentBindingStore(tmp_path).ensure_personal_memory_group("agent-a")
    store = ConversationHistoryStore(tmp_path)
    store.import_conversations([_conversation()], provider="chatgpt", scope=HistoryScope(agent_instance_id="agent-a"))
    result = handle_history_tool("memoryguard_history_search", {"query": "local history"}, workspace=str(tmp_path), trusted_agent_id="agent-a")
    assert result["results"]


def test_observation_is_indexed_without_raw_turn_content_in_search(tmp_path: Path):
    store = ConversationHistoryStore(tmp_path)
    store.import_conversations([_conversation()], provider="chatgpt", scope=_scope())
    session_id = store.list_sessions(_scope())["sessions"][0]["session_id"]
    store.add_observation(_scope(), session_id=session_id, summary="Decision: history remains isolated.")
    hit = store.search(_scope(), "history isolated")["results"][0]
    assert hit["result_type"] == "observation"
    assert "content" not in hit


def test_legacy_evidence_fk_migrates_to_tombstone_table(tmp_path: Path):
    db = tmp_path / ".memoryguard" / "history" / "history.sqlite"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE conversation_sessions (session_id TEXT PRIMARY KEY, external_id TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', provider TEXT NOT NULL DEFAULT '', agent_instance_id TEXT NOT NULL, project_ref TEXT NOT NULL DEFAULT '', share_group_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT '', imported_at TEXT NOT NULL, deleted_at TEXT NOT NULL DEFAULT '');
            CREATE TABLE conversation_turns (turn_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES conversation_sessions(session_id) ON DELETE CASCADE, ordinal INTEGER NOT NULL, role TEXT NOT NULL DEFAULT 'unknown', content TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT '', content_type TEXT NOT NULL DEFAULT 'text');
            CREATE TABLE evidence_links (link_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, session_id TEXT NOT NULL REFERENCES conversation_sessions(session_id) ON DELETE CASCADE, turn_id TEXT REFERENCES conversation_turns(turn_id) ON DELETE SET NULL, status TEXT NOT NULL DEFAULT 'valid', created_at TEXT NOT NULL, invalidated_at TEXT NOT NULL DEFAULT '');
            CREATE VIRTUAL TABLE history_fts USING fts5(session_id UNINDEXED, turn_id UNINDEXED, title, content);
        """)
    store = ConversationHistoryStore(tmp_path)
    with store._connect() as conn:
        foreign = conn.execute("PRAGMA foreign_key_list(evidence_links)").fetchall()
        cols = [row[1] for row in conn.execute("PRAGMA table_info(history_fts)").fetchall()]
    assert foreign == []
    assert "result_type" in cols


def test_every_sqlite_connection_is_closed_and_database_can_move(tmp_path: Path, monkeypatch):
    real_connect = sqlite3.connect
    opened = 0
    closed = 0

    class TrackingConnection(sqlite3.Connection):
        _history_closed = False

        def close(self):
            nonlocal closed
            if not self._history_closed:
                closed += 1
                self._history_closed = True
            return super().close()

    def tracked_connect(*args, **kwargs):
        nonlocal opened
        opened += 1
        kwargs["factory"] = TrackingConnection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    store = ConversationHistoryStore(tmp_path)
    store.import_conversations([_conversation()], provider="chatgpt", scope=_scope())
    for _ in range(25):
        store.list_sessions(_scope())
        store.search(_scope(), "local history")
    assert opened == closed
    moved = store.db_path.with_name("history-moved.sqlite")
    store.db_path.replace(moved)
    assert moved.exists()


def test_reimport_tombstones_evidence_before_content_or_order_changes(tmp_path: Path):
    store = ConversationHistoryStore(tmp_path)
    original = _conversation()
    store.import_conversations([original], provider="chatgpt", scope=_scope())
    session_id = store.list_sessions(_scope())["sessions"][0]["session_id"]
    first_turn = store.read(_scope(), session_id=session_id)["turns"][0]
    store.add_evidence_link(memory_id="memory-1", session_id=session_id, turn_id=first_turn["turn_id"])

    # Exact replay preserves both immutable turn IDs and valid evidence.
    store.import_conversations([original], provider="chatgpt", scope=_scope())
    with store._connect() as conn:
        assert conn.execute("SELECT status FROM evidence_links WHERE memory_id='memory-1'").fetchone()["status"] == "valid"

    changed = ImportedConversation(
        conv_id=original.conv_id, title=original.title,
        messages=[
            {"role": "user", "content": "The source content changed.", "created_at": "2026-07-30T00:00:00Z"},
            original.messages[1],
        ],
    )
    store.import_conversations([changed], provider="chatgpt", scope=_scope())
    with store._connect() as conn:
        link = conn.execute("SELECT status FROM evidence_links WHERE memory_id='memory-1'").fetchone()
    assert link["status"] == "invalid"
    assert store.read(_scope(), session_id=session_id)["turns"][0]["turn_id"] != first_turn["turn_id"]


def test_append_turn_strict_and_degraded_idempotency_contract(tmp_path: Path):
    store = ConversationHistoryStore(tmp_path)
    scope = HistoryScope(agent_instance_id="agent-a", provider="codex")
    first = store.append_turn(scope, external_session_id="s1", provider="codex",
                              role="user", content="same content", event_id="host-turn-1")
    replay = store.append_turn(scope, external_session_id="s1", provider="codex",
                               role="user", content="same content", event_id="host-turn-1")
    conflict = store.append_turn(scope, external_session_id="s1", provider="codex",
                                 role="user", content="changed delivery", event_id="host-turn-1")
    assert first["inserted"] is True
    assert replay["replayed"] is True and replay["turn_id"] == first["turn_id"]
    assert conflict["event_conflict"] is True and conflict["inserted"] is False

    degraded_1 = store.append_turn(scope, external_session_id="s1", provider="codex",
                                   role="user", content="legitimate repeat", event_stable=False)
    degraded_2 = store.append_turn(scope, external_session_id="s1", provider="codex",
                                   role="user", content="legitimate repeat", event_stable=False)
    assert degraded_1["idempotency"] == "degraded"
    assert degraded_1["turn_id"] != degraded_2["turn_id"]


def test_search_returns_bounded_match_and_explicit_result_routing(tmp_path: Path):
    store = ConversationHistoryStore(tmp_path)
    store.import_conversations([_conversation()], provider="chatgpt", scope=_scope())
    session_id = store.list_sessions(_scope())["sessions"][0]["session_id"]
    turn_id = store.read(_scope(), session_id=session_id)["turns"][0]["turn_id"]
    store.add_observation(_scope(), session_id=session_id, turn_id=turn_id,
                          summary="Anchored routing observation.")
    store.add_observation(_scope(), session_id=session_id,
                          summary="Session-only routing observation.")

    turn_hit = store.search(_scope(), "local history")["results"][0]
    assert len(turn_hit["matched_summary"]) <= 320
    assert turn_hit["read_target"] == "turn" and turn_hit["can_timeline"] is True
    anchored = store.search(_scope(), "Anchored routing")["results"][0]
    assert anchored["result_type"] == "observation" and anchored["anchor_turn_id"] == turn_id
    session_only = store.search(_scope(), "Session-only routing")["results"][0]
    assert session_only["result_type"] == "observation"
    assert session_only["read_target"] == "session" and session_only["can_timeline"] is False


def test_add_observation_is_fts_idempotent_and_schema_is_versioned(tmp_path: Path):
    store = ConversationHistoryStore(tmp_path)
    store.import_conversations([_conversation()], provider="chatgpt", scope=_scope())
    session_id = store.list_sessions(_scope())["sessions"][0]["session_id"]
    first = store.add_observation(_scope(), session_id=session_id, summary="One indexed observation.")
    second = store.add_observation(_scope(), session_id=session_id, summary="One indexed observation.")
    assert first == second
    assert len(store.search(_scope(), "indexed observation")["results"]) == 1
    with store._connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] >= 2


def test_scope_normalizes_project_paths_and_rejects_unbounded_identity(tmp_path: Path):
    alias = tmp_path / "folder" / ".." / "project"
    scope = HistoryScope(agent_instance_id="agent-a", project_ref=str(alias), provider="CODEX")
    from memoryguard.rule_scope import canonical_project_ref
    assert scope.project_ref == canonical_project_ref(str(tmp_path / "project"))
    assert scope.provider == "codex"
    with pytest.raises(ValueError, match="external_session_id_too_long"):
        ConversationHistoryStore(tmp_path).append_turn(
            scope, external_session_id="x" * 1025, provider="codex",
            role="user", content="bounded", event_id="event",
        )


def test_titles_prefer_explicit_host_or_first_visible_user_turn(tmp_path: Path):
    store = ConversationHistoryStore(tmp_path)
    scope = HistoryScope(agent_instance_id="agent-a", project_ref="project-x", provider="codex")
    explicit = ImportedConversation(
        conv_id="explicit", title="Host supplied design review",
        messages=[{"role": "user", "content": "this must not replace the host title"}],
    )
    derived = ImportedConversation(
        conv_id="derived", title="",
        messages=[
            {"role": "system", "content": "private system instruction"},
            {"role": "tool", "content": "private tool payload"},
            {"role": "user", "content": "# **重构 神经图**\n\n请保留图内治理。"},
        ],
    )
    store.import_conversations([explicit, derived], provider="codex", scope=scope)
    titles = {row["session_id"]: row["title"] for row in store.list_sessions(scope, limit=10)["sessions"]}
    assert "Host supplied design review" in titles.values()
    assert any(title.startswith("重构 神经图") for title in titles.values())
    assert not any("private system" in title or "private tool" in title for title in titles.values())


def test_missing_title_fallback_is_nonempty_stable_and_bounded(tmp_path: Path):
    store = ConversationHistoryStore(tmp_path)
    scope = HistoryScope(agent_instance_id="agent-a", project_ref="project-x", provider="codex")
    content = "# " + "中文标题" * 80
    store.import_conversations([
        ImportedConversation(conv_id="fallback", title="", messages=[
            {"role": "assistant", "content": "assistant first", "created_at": "2026-07-30T08:09:10Z"},
        ]),
        ImportedConversation(conv_id="long", title="", messages=[
            {"role": "user", "content": content, "created_at": "2026-07-30T08:09:10Z"},
        ]),
    ], provider="codex", scope=scope)
    rows = store.list_sessions(scope, limit=10)["sessions"]
    fallback = next(row["title"] for row in rows if row["session_id"] == store._session_id("fallback", scope, "codex"))
    long_title = next(row["title"] for row in rows if row["session_id"] == store._session_id("long", scope, "codex"))
    assert fallback == "Codex 对话 · 2026-07-30 08:09"
    assert "未命名" not in fallback and fallback
    assert len(long_title) <= 88 and long_title.startswith("中文标题")


def test_reimport_never_downgrades_good_title_and_append_user_upgrades_fallback(tmp_path: Path):
    store = ConversationHistoryStore(tmp_path)
    scope = HistoryScope(agent_instance_id="agent-a", project_ref="project-x", provider="codex")
    original = ImportedConversation(
        conv_id="reimport", title="明确的宿主标题",
        messages=[{"role": "user", "content": "initial visible prompt"}],
    )
    store.import_conversations([original], provider="codex", scope=scope)
    store.import_conversations([
        ImportedConversation(conv_id="reimport", title="", messages=[
            {"role": "assistant", "content": "incomplete reimport"},
        ])
    ], provider="codex", scope=scope)
    assert store.list_sessions(scope)["sessions"][0]["title"] == "明确的宿主标题"

    hook_scope = HistoryScope(agent_instance_id="hook-agent", provider="codex")
    store.append_turn(hook_scope, external_session_id="hook", provider="codex", role="assistant",
                      content="assistant first", event_id="a", created_at="2026-07-30T08:00:00Z")
    initial = store.list_sessions(hook_scope)["sessions"][0]
    assert initial["title"].startswith("Codex 对话 ·")
    store.append_turn(hook_scope, external_session_id="hook", provider="codex", role="user",
                      content="请为会话生成可读摘要标题", event_id="u", created_at="2026-07-30T08:01:00Z")
    upgraded = store.list_sessions(hook_scope)["sessions"][0]
    assert upgraded["title"] == "请为会话生成可读摘要标题"
    with store._connect() as conn:
        fts_titles = {row["title"] for row in conn.execute("SELECT title FROM history_fts WHERE session_id=?", (upgraded["session_id"],))}
    assert fts_titles == {"请为会话生成可读摘要标题"}


def test_init_backfills_legacy_low_quality_titles_and_fts(tmp_path: Path):
    store = ConversationHistoryStore(tmp_path)
    session_id = "hist-0123456789abcdef01234567"
    with store._connect() as conn:
        conn.execute("INSERT INTO conversation_sessions(session_id,external_id,title,provider,agent_instance_id,project_ref,share_group_id,created_at,imported_at,deleted_at) VALUES (?,?,?,?,?,?,?,?,?, '')",
                     (session_id, "rollout-20260730-foo", "rollout-20260730-foo.jsonl", "codex", "agent-a", "", "", "2026-07-30T08:00:00Z", "2026-07-30T08:00:00Z"))
        conn.execute("INSERT INTO conversation_turns(turn_id,session_id,ordinal,role,content,created_at,content_type,event_key,content_hash) VALUES (?,?,?,?,?,?,?,?,?)",
                     ("turn-1", session_id, 1, "user", "迁移后应显示这条用户摘要", "", "text", "legacy", "hash"))
        conn.execute("INSERT INTO history_fts(session_id,turn_id,result_type,title,content) VALUES (?,?,'turn',?,?)",
                     (session_id, "turn-1", "rollout-20260730-foo.jsonl", "迁移后应显示这条用户摘要"))
    ConversationHistoryStore(tmp_path)  # initialization migration is idempotent
    session = store.list_sessions(HistoryScope(agent_instance_id="agent-a"))["sessions"][0]
    assert session["title"] == "迁移后应显示这条用户摘要"
    with store._connect() as conn:
        assert conn.execute("SELECT title FROM history_fts WHERE session_id=?", (session_id,)).fetchone()["title"] == session["title"]


def test_shared_history_reads_follow_active_bindings_and_owner_delete(tmp_path: Path):
    bindings = AgentBindingStore(tmp_path)
    bindings.bind_agents_to_group(["agent-a", "agent-b"], "shared-history")
    store = ConversationHistoryStore(tmp_path)
    one = store.append_turn(HistoryScope(agent_instance_id="agent-a"), external_session_id="a",
                            provider="codex", role="user", content="shared alpha", event_id="a1")
    two = store.append_turn(HistoryScope(agent_instance_id="agent-b"), external_session_id="b",
                            provider="codex", role="user", content="shared beta", event_id="b1")
    from memoryguard.conversation_history import HistoryAccessResolver
    resolver = HistoryAccessResolver(tmp_path)
    default_shared = resolver.resolve("agent-a", {})
    shared = resolver.resolve("agent-a", {"share_group_id": "shared-history"})
    assert {row["session_id"] for row in store.list_sessions(default_shared)["sessions"]} == {one["session_id"], two["session_id"]}
    assert {row["session_id"] for row in store.list_sessions(shared)["sessions"]} == {one["session_id"], two["session_id"]}
    assert store.read(shared, session_id=two["session_id"])["turns"][0]["content"] == "shared beta"
    assert store.extract_preview(shared, two["session_id"])["candidates"]
    assert store.export(shared, session_ids=[one["session_id"], two["session_id"]])["sessions"]
    assert store.delete(shared, session_ids=[two["session_id"]])["deleted_sessions"] == 0
    assert store.delete(resolver.resolve("agent-b", {"share_group_id": "shared-history"}), session_ids=[two["session_id"]])["deleted_sessions"] == 1

    # Leaving updates the next request's membership snapshot immediately.
    bindings.leave_shared_group_to_personal("agent-a", confirmed=True)
    with pytest.raises(PermissionError, match="trusted_share_group_scope_required"):
        resolver.resolve("agent-a", {"share_group_id": "shared-history"})
    b_shared = resolver.resolve("agent-b", {"share_group_id": "shared-history"})
    assert store.list_sessions(b_shared)["sessions"] == []


def test_shared_history_forged_group_fails_closed(tmp_path: Path):
    bindings = AgentBindingStore(tmp_path)
    bindings.bind_agents_to_group(["agent-a", "agent-b"], "group-one")
    bindings.bind_agents_to_group(["agent-c", "agent-d"], "group-two")
    from memoryguard.conversation_history import HistoryAccessResolver
    with pytest.raises(PermissionError, match="trusted_share_group_scope_required"):
        HistoryAccessResolver(tmp_path).resolve("agent-a", {"share_group_id": "group-two"})


def test_history_api_shared_search_uses_binding_not_claimed_members(tmp_path: Path):
    bindings = AgentBindingStore(tmp_path)
    bindings.bind_agents_to_group(["agent-a", "agent-b"], "shared-history")
    store = ConversationHistoryStore(tmp_path)
    store.append_turn(HistoryScope(agent_instance_id="agent-b"), external_session_id="b",
                      provider="codex", role="user", content="peer searchable", event_id="b1")
    result = handle_history_tool(
        "memoryguard_history_search",
        {"query": "peer searchable", "scope": {"share_group_id": "shared-history"}},
        workspace=str(tmp_path), trusted_agent_id="agent-a",
    )
    assert result["results"][0]["owner_agent_instance_id"] == "agent-b"
    default_result = handle_history_tool(
        "memoryguard_history_search", {"query": "peer searchable"},
        workspace=str(tmp_path), trusted_agent_id="agent-a",
    )
    assert default_result["results"][0]["owner_agent_instance_id"] == "agent-b"
    with pytest.raises(PermissionError, match="trusted_share_group_scope_required"):
        handle_history_tool(
            "memoryguard_history_search",
            {"query": "peer searchable", "scope": {"share_group_id": "other-group"}},
            workspace=str(tmp_path), trusted_agent_id="agent-a",
        )


def test_project_projection_canonical_unknown_and_removed(tmp_path: Path):
    store = ConversationHistoryStore(tmp_path)
    project = tmp_path / "same-name"
    project.mkdir()
    alias = str(project / ".." / project.name)
    scope = HistoryScope(agent_instance_id="agent-a", provider="codex")
    store.import_conversations([
        ImportedConversation("known", "", [{"role": "user", "content": "known"}], project_ref=str(project)),
        ImportedConversation("known", "", [{"role": "user", "content": "known"}], project_ref=alias),
        ImportedConversation("unknown", "", [{"role": "user", "content": "unknown"}]),
    ], provider="codex", scope=scope)
    listing = store.list_sessions(scope, limit=10)
    assert listing["total"] == 2  # aliases are one canonical session
    assert {item["project_status"] for item in listing["project_groups"]} == {"available", "unknown"}
    project.rmdir()
    assert any(item["project_status"] == "removed" for item in store.list_sessions(scope)["project_groups"])


def test_project_metadata_backfill_keeps_existing_session_turn_and_evidence_ids(tmp_path: Path):
    store = ConversationHistoryStore(tmp_path)
    scope = HistoryScope(agent_instance_id="agent-a", provider="codex")
    original = ImportedConversation("stable", "", [{"role": "user", "content": "stable turn"}])
    store.import_conversations([original], provider="codex", scope=scope)
    session_id = store.list_sessions(scope)["sessions"][0]["session_id"]
    turn_id = store.read(scope, session_id=session_id)["turns"][0]["turn_id"]
    store.add_evidence_link(memory_id="memory-stable", session_id=session_id, turn_id=turn_id)
    project = tmp_path / "project"
    store.import_conversations([
        ImportedConversation("stable", "", original.messages, project_ref=str(project), project_source="metadata")
    ], provider="codex", scope=scope)
    moved = store.list_sessions(HistoryScope(agent_instance_id="agent-a", project_ref=str(project)))["sessions"]
    assert [row["session_id"] for row in moved] == [session_id]
    assert store.read(HistoryScope(agent_instance_id="agent-a", project_ref=str(project)), session_id=session_id)["turns"][0]["turn_id"] == turn_id
    with store._connect() as conn:
        assert conn.execute("SELECT status FROM evidence_links WHERE memory_id='memory-stable'").fetchone()["status"] == "valid"


def test_legacy_project_aliases_merge_in_projection_and_project_filter(tmp_path: Path):
    store = ConversationHistoryStore(tmp_path)
    project = tmp_path / "游戏项目"
    project.mkdir()
    raw_refs = [
        str(project),
        str(project).replace("\\", "/").lower(),
        str(project / ".." / project.name),
    ]
    with store._connect() as conn:
        for index, raw_ref in enumerate(raw_refs):
            conn.execute(
                "INSERT INTO conversation_sessions(session_id,external_id,title,provider,agent_instance_id,project_ref,share_group_id,created_at,imported_at,deleted_at) "
                "VALUES (?,?,?,?,?,?,?,?,?, '')",
                (f"legacy-{index}", f"external-{index}", "legacy", "codex", "agent-a", raw_ref, "", "2026-07-30", "2026-07-30"),
            )
    scope = HistoryScope(agent_instance_id="agent-a", project_ref=str(project), provider="codex")
    listing = store.list_sessions(scope, limit=10)
    assert listing["total"] == 3
    assert len(listing["project_groups"]) == 1
    assert listing["project_groups"][0]["session_count"] == 3
    assert {row["project_ref"] for row in listing["sessions"]} == {scope.project_ref}


# --- Part B2: cross-provider session dedup on append -----------------------

def test_append_turn_dedups_same_external_id_across_providers(tmp_path: Path):
    store = ConversationHistoryStore(tmp_path)
    scope_a = HistoryScope(agent_instance_id="agent-a", provider="claude")
    scope_b = HistoryScope(agent_instance_id="agent-a", provider="cursor")
    first = store.append_turn(scope_a, external_session_id="dual-session", provider="claude",
                              role="user", content="同一物理会话", event_id="turn-1")
    second = store.append_turn(scope_b, external_session_id="dual-session", provider="cursor",
                               role="user", content="同一物理会话", event_id="turn-1")
    # Both writes land on the SAME session row despite differing provider;
    # the canonical row now carries the host-proven provider (cursor).
    assert second["session_id"] == first["session_id"]
    sessions = store.list_sessions(scope_b)["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["provider"] == "cursor"
    raw = store.read(scope_b, session_id=first["session_id"])
    assert len(raw["turns"]) == 1


def test_append_turn_dedups_across_agents_on_second_append(tmp_path: Path):
    store = ConversationHistoryStore(tmp_path)
    scope_a = HistoryScope(agent_instance_id="agent-a", provider="claude")
    scope_b = HistoryScope(agent_instance_id="agent-b", provider="cursor")
    first = store.append_turn(scope_a, external_session_id="agent-dual", provider="claude",
                              role="user", content="跨 agent 双写", event_id="turn-1")
    second = store.append_turn(scope_b, external_session_id="agent-dual", provider="cursor",
                               role="user", content="跨 agent 双写", event_id="turn-1")
    assert second["session_id"] == first["session_id"]
    # The canonical row was folded into agent-b's identity; scope_b sees it once.
    assert len(store.list_sessions(scope_b)["sessions"]) == 1
    raw = store.read(scope_b, session_id=first["session_id"])
    assert len(raw["turns"]) == 1


# --- Part B3: read-side duplicate collapse ---------------------------------

def test_list_sessions_collapses_same_provider_agent_duplicates(tmp_path: Path):
    store = ConversationHistoryStore(tmp_path)
    scope = HistoryScope(agent_instance_id="agent-a", provider="claude")
    # Insert the same physical session under two rows directly (as the old
    # dual-write bug produced), then confirm the reader folds them.
    s1 = store.append_turn(scope, external_session_id="dup-session", provider="claude",
                           role="user", content="重复行 A", event_id="turn-1")
    s2 = store.append_turn(scope, external_session_id="dup-session", provider="claude",
                           role="user", content="重复行 B", event_id="turn-2")
    # Same agent + same project + same external_id -> the append dedup already
    # routes both to one row (B2).  list_sessions must show exactly one.
    assert s1["session_id"] == s2["session_id"]
    sessions = store.list_sessions(scope)["sessions"]
    assert len(sessions) == 1
    assert sessions[0].get("duplicate_count", 0) == 0


def test_import_conversations_dedups_prior_across_providers(tmp_path: Path):
    store = ConversationHistoryStore(tmp_path)
    conv = _conversation()
    chat_scope = HistoryScope(agent_instance_id="agent-a", project_ref="project-x", provider="chatgpt")
    cur_scope = HistoryScope(agent_instance_id="agent-a", project_ref="project-x", provider="cursor")
    # Import once under chatgpt, then re-import the same source conversation
    # under a different provider label; B2's prior lookup must ignore provider.
    first = store.import_conversations([conv], provider="chatgpt", scope=chat_scope)
    second = store.import_conversations([conv], provider="cursor", scope=cur_scope)
    # Re-import rewrites the SAME canonical row (B2 prior redirect across
    # provider) rather than creating a second session with duplicated turns.
    sessions = store.list_sessions(cur_scope)["sessions"]
    assert len(sessions) == 1
    raw = store.read(cur_scope, session_id=sessions[0]["session_id"])
    assert len(raw["turns"]) == first["turn_count"]
