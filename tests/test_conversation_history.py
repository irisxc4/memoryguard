"""Core history CRUD coverage on the V2 Content history plane."""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3

from memoryguard.content import ContentStore
from memoryguard.content.conversation_sync import ConversationEvent, ConversationSync
from memoryguard.runtime_v2.history_store import (
    ContentHistoryStore,
    V2HistoryScope,
    content_history_schema_status,
)


def _scope(
    agent: str = "agent-a",
    *,
    project_ref: str = "",
    provider: str = "codex",
    group: str = "history-group",
    members: tuple[str, ...] = (),
) -> V2HistoryScope:
    authorized = members or (agent,)
    return V2HistoryScope(
        agent_instance_id=agent,
        project_ref=os.path.normcase(project_ref),
        provider=provider,
        share_group_id=group,
        authorized_agent_ids=authorized,
        shared_read=len(authorized) > 1,
    )


def _event(
    workspace: Path,
    *,
    session: str,
    event_id: str,
    content: str,
    ordinal: int = 0,
    agent: str = "agent-a",
    project_ref: str = "",
    provider: str = "codex",
    group: str = "history-group",
    role: str = "user",
    title: str = "Design review",
) -> ConversationEvent:
    return ConversationEvent(
        external_object_key=session,
        event_id=event_id,
        content=content,
        role=role,
        ordinal=ordinal,
        title=title,
        provider=provider,
        workspace_id=str(workspace.resolve()),
        agent_instance_id=agent,
        project_ref=os.path.normcase(project_ref),
        share_group_id=group,
    )


def _seed(
    workspace: Path,
    events: list[ConversationEvent],
    *,
    source_id: str = "history-source",
) -> tuple[str, list[str]]:
    content = ContentStore(workspace)
    ConversationSync(content).sync(source_id, events, owner_id="history-test")
    with content.connection() as conn:
        session_id = str(
            conn.execute(
                "SELECT session_id FROM conversation_sessions ORDER BY session_id LIMIT 1"
            ).fetchone()[0]
        )
        turn_ids = [
            str(row[0])
            for row in conn.execute(
                "SELECT turn_id FROM conversation_turns WHERE session_id=? ORDER BY ordinal",
                (session_id,),
            ).fetchall()
        ]
    return session_id, turn_ids


def _history(workspace: Path) -> ContentHistoryStore:
    return ContentHistoryStore(workspace, readonly=True)


def test_history_is_agent_scoped_and_progressive(tmp_path: Path):
    project = str(tmp_path.resolve())
    session_id, turn_ids = _seed(
        tmp_path,
        [
            _event(
                tmp_path,
                session="source-conversation-1",
                event_id="turn-1",
                content="We need a local history index.",
                project_ref=project,
            ),
            _event(
                tmp_path,
                session="source-conversation-1",
                event_id="turn-2",
                content="Use Content V2 and keep raw chat out of bootstrap.",
                ordinal=1,
                project_ref=project,
                role="assistant",
            ),
        ],
    )
    _seed(
        tmp_path,
        [_event(tmp_path, session="other", event_id="other-1", content="private peer history", agent="agent-b")],
        source_id="other-source",
    )
    scope = _scope(project_ref=project)
    assert _history(tmp_path).list_sessions(scope)["total"] == 1

    results = _history(tmp_path).search(scope, "Content V2")
    assert len(results["results"]) == 1
    hit = results["results"][0]
    assert hit["session_id"] == session_id
    assert "content" not in hit

    timeline = _history(tmp_path).timeline(scope, session_id, turn_ids[1], radius=1)
    assert timeline["turns"][0]["content_preview"]
    assert "content" not in timeline["turns"][0]

    raw = _history(tmp_path).read(scope, turn_id=turn_ids[1])
    assert raw["turn"]["content"].startswith("Use Content V2")


def test_list_sessions_returns_exact_scoped_total_and_summary_field(tmp_path: Path):
    project = str(tmp_path.resolve())
    events = [
        _event(tmp_path, session=f"session-{index}", event_id=f"event-{index}", content=f"message {index}", project_ref=project)
        for index in range(3)
    ]
    # Each source maps one external object to one session; use separate source
    # IDs to retain the three-session import boundary in the V2 manifest.
    for index, event in enumerate(events):
        _seed(tmp_path, [event], source_id=f"source-{index}")
    _seed(
        tmp_path,
        [_event(tmp_path, session="peer", event_id="peer-1", content="peer message", agent="agent-b", project_ref=project)],
        source_id="peer-source",
    )
    listing = _history(tmp_path).list_sessions(_scope(project_ref=project), limit=1, offset=10)
    assert listing["sessions"] == []
    assert listing["total"] == 3
    sessions = _history(tmp_path).list_sessions(_scope(project_ref=project))["sessions"]
    assert all("summary" in row for row in sessions)
    assert all("display_title" in row for row in sessions)
    assert all("preview_excerpt" in row for row in sessions)
    assert all("summarized" in row for row in sessions)


def test_list_sessions_builds_readable_fallback_title_from_first_user_turn(tmp_path: Path):
    project = str(tmp_path.resolve())
    raw = "我需要你帮我查看现在项目再修的问题，会话ID：019fc183-3e8f-72d0-974e-ce9c74c3c561"
    _seed(
        tmp_path,
        [_event(
            tmp_path,
            session="fallback-title",
            event_id="fallback-title-1",
            content=raw,
            project_ref=project,
            title="未命名会话",
        )],
        source_id="fallback-title-source",
    )

    item = _history(tmp_path).list_sessions(_scope(project_ref=project))["sessions"][0]
    assert item["display_title"] == "查看现在项目再修的问题"
    assert item["source_title"] == "未命名会话"
    assert item["preview_excerpt"] == raw
    assert item["summarized"] is False


def test_list_sessions_fallback_title_discards_paths_and_long_cli_args(tmp_path: Path):
    project = str(tmp_path.resolve())
    raw = "我需要你帮我排查历史读取问题 H:/ai/workspace/tools/history.py --session-id=019fc183-extra"
    _seed(
        tmp_path,
        [_event(
            tmp_path,
            session="clean-fallback-title",
            event_id="clean-fallback-title-1",
            content=raw,
            project_ref=project,
            title="untitled",
        )],
        source_id="clean-fallback-title-source",
    )

    item = _history(tmp_path).list_sessions(_scope(project_ref=project))["sessions"][0]
    assert item["display_title"] == "排查历史读取问题"
    assert "H:/" not in item["display_title"]
    assert "--session-id" not in item["display_title"]


def test_read_and_extract_preview_keep_raw_content_at_explicit_boundaries(tmp_path: Path):
    project = str(tmp_path.resolve())
    session_id, turn_ids = _seed(
        tmp_path,
        [_event(tmp_path, session="preview", event_id="preview-1", content="evidence-backed body", project_ref=project)],
    )
    scope = _scope(project_ref=project)
    preview = _history(tmp_path).extract_preview(scope, session_id)
    assert preview["written_to_long_term_memory"] is False
    assert preview["candidates"][0]["evidence"]["session_id"] == session_id
    assert preview["candidates"][0]["evidence"]["turn_id"] == turn_ids[0]
    assert _history(tmp_path).read(scope, session_id=session_id)["turns"][0]["content"] == "evidence-backed body"


def test_delete_requires_scope_idempotency_and_preserves_long_term_memory(tmp_path: Path):
    project = str(tmp_path.resolve())
    session_id, turn_ids = _seed(
        tmp_path,
        [_event(tmp_path, session="delete", event_id="delete-1", content="delete me", project_ref=project)],
    )
    content = ContentStore(tmp_path)
    ConversationSync(content).add_evidence_link(memory_id="long-term-1", turn_id=turn_ids[0])
    store = ContentHistoryStore(tmp_path, readonly=False)
    scope = _scope(project_ref=project)
    try:
        store.delete(scope, session_ids=[], idempotency_key="missing", operation_digest="digest")
    except ValueError as exc:
        assert str(exc) == "history_delete_scope_required"
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("empty delete scope must be rejected")

    deleted = store.delete(
        scope,
        session_ids=[session_id],
        invalidate_evidence=True,
        idempotency_key="delete-1",
        operation_digest="digest-1",
    )
    assert deleted["deleted_sessions"] == 1
    assert deleted["invalidated_evidence_links"] == 1
    assert deleted["long_term_memories_deleted"] == 0
    with content.connection() as conn:
        link = conn.execute(
            "SELECT status FROM content_evidence_links WHERE memory_id='long-term-1'"
        ).fetchone()
        tombstone = conn.execute(
            "SELECT 1 FROM content_tombstones WHERE reason='history_delete'"
        ).fetchone()
        receipt = conn.execute(
            "SELECT operation,payload_digest FROM history_mutation_receipts WHERE idempotency_key='delete-1'"
        ).fetchone()
    assert link[0] == "invalid"
    assert tombstone is not None
    assert tuple(receipt) == ("delete", "digest-1")

    replay = store.delete(
        scope,
        session_ids=[session_id],
        invalidate_evidence=True,
        idempotency_key="delete-1",
        operation_digest="digest-1",
    )
    assert replay["idempotent_replay"] is True


def test_v2_history_scope_isolation_covers_group_and_provider(tmp_path: Path):
    project = str(tmp_path.resolve())
    _seed(
        tmp_path,
        [_event(tmp_path, session="codex", event_id="codex-1", content="codex body", project_ref=project)],
        source_id="codex-source",
    )
    _seed(
        tmp_path,
        [_event(tmp_path, session="claude", event_id="claude-1", content="claude body", project_ref=project, provider="claude")],
        source_id="claude-source",
    )
    assert _history(tmp_path).search(_scope(project_ref=project), "codex body")["results"]
    assert _history(tmp_path).search(_scope(project_ref=project), "claude-only")["results"] == []
    assert _history(tmp_path).search(_scope(project_ref=project, provider="claude"), "claude body")["results"]
    assert content_history_schema_status(ContentStore(tmp_path).db_path) == "valid"


def test_history_schema_probe_explicitly_closes_connection(tmp_path: Path, monkeypatch):
    path = ContentStore(tmp_path).db_path
    original_connect = sqlite3.connect
    opened = []

    class TrackingConnection(sqlite3.Connection):
        closed = False

        def close(self):
            self.closed = True
            return super().close()

    def tracked_connect(*args, **kwargs):
        kwargs.setdefault("factory", TrackingConnection)
        connection = original_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    assert content_history_schema_status(path) == "valid"
    assert opened
    assert all(connection.closed for connection in opened)
