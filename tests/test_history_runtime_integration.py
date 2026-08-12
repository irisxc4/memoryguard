"""Native V2 history integration at the MCP/service boundary."""
from __future__ import annotations

import json
import os
from pathlib import Path

from memoryguard.access_context import AccessContext
from memoryguard.content.conversation_sync import ConversationEvent, ConversationSync
from memoryguard.content.store import ContentStore
from memoryguard.host_hooks import run_hook
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.history_native import NativeHistoryService
from memoryguard.runtime_v2.native_ports import (
    NativeV2RuntimePort,
    bind_native_transport_context,
)


def _bound_workspace(tmp_path: Path, monkeypatch, agent: str = "agent-a") -> Path:
    workspace = tmp_path / "control"
    workspace.mkdir()
    group = "history-group"
    GroupControlService(workspace, write=True).bind_agent(
        agent, group, idempotency_key=f"test-bind:{agent}:{group}"
    )
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(workspace))
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", agent)
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    return workspace


def _context(workspace: Path, agent: str = "agent-a", provider: str = "codex") -> dict:
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent,
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id="history-runtime-test",
            session_source="test",
            session_trusted=True,
        ),
        workspace_id=str(workspace.resolve()),
        share_group_id="history-group",
        project_ref=os.path.normcase(str(workspace.resolve())),
        provider=provider,
        runtime_role="root",
    )


def _seed(workspace: Path, events: list[ConversationEvent], source_id: str = "runtime-history") -> tuple[str, list[str]]:
    ConversationSync(ContentStore(workspace)).sync(source_id, events, owner_id="history-runtime-test")
    with ContentStore(workspace).connection() as conn:
        session_id = str(conn.execute("SELECT session_id FROM conversation_sessions ORDER BY session_id LIMIT 1").fetchone()[0])
        turn_ids = [
            str(row[0])
            for row in conn.execute(
                "SELECT turn_id FROM conversation_turns WHERE session_id=? ORDER BY ordinal",
                (session_id,),
            ).fetchall()
        ]
    return session_id, turn_ids


def _event(workspace: Path, *, session: str, event_id: str, content: str, agent: str = "agent-a") -> ConversationEvent:
    return ConversationEvent(
        external_object_key=session,
        event_id=event_id,
        content=content,
        role="user",
        ordinal=0,
        title="Native history",
        provider="codex",
        workspace_id=str(workspace.resolve()),
        agent_instance_id=agent,
        project_ref=os.path.normcase(str(workspace.resolve())),
        share_group_id="history-group",
    )


def test_mcp_history_uses_trusted_agent_scope_and_stays_out_of_bootstrap(tmp_path: Path, monkeypatch):
    workspace = _bound_workspace(tmp_path, monkeypatch)
    session_id, turn_ids = _seed(
        workspace,
        [_event(workspace, session="session-a", event_id="turn-a", content="history stays outside bootstrap")],
    )
    port = NativeV2RuntimePort(workspace, state_provider=lambda: {"state": "V2_ACTIVE", "generation": 1})
    context = _context(workspace)

    search = port.dispatch_mcp(
        "memoryguard_history_search",
        {"query": "outside bootstrap"},
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )
    assert search["ok"] is True, search
    assert search["data"]["results"]
    assert "history stays outside bootstrap" not in json.dumps(search, ensure_ascii=False)

    # Search remains covered through the MCP port. Explicit raw reads use the
    # native service boundary because the generic MCP identity scrubber
    # intentionally removes session selectors from transport payloads.
    read = NativeHistoryService(workspace).dispatch(
        "read", {"session_id": session_id}, context=context,
    )
    assert read["ok"] is True, read
    assert read["data"]["turns"][0]["turn_id"] == turn_ids[0]
    assert read["data"]["turns"][0]["content"] == "history stays outside bootstrap"


def test_native_history_preserves_repeated_content_without_text_deduplication(tmp_path: Path, monkeypatch):
    workspace = _bound_workspace(tmp_path, monkeypatch)
    text = "same legitimate prompt"
    _, turn_ids = _seed(
        workspace,
        [
            _event(workspace, session="repeat-session", event_id="turn-1", content=text),
            ConversationEvent(
                **{**_event(workspace, session="repeat-session", event_id="turn-2", content=text).__dict__, "ordinal": 1}
            ),
        ],
    )
    service = NativeHistoryService(workspace)
    context = _context(workspace)
    listed = service.dispatch("list_sessions", context=context)
    assert listed["ok"] is True, listed
    assert listed["data"]["total"] == 1
    contents = []
    for turn_id in turn_ids:
        read = service.dispatch("read", {"turn_id": turn_id}, context=context)
        assert read["ok"] is True, read
        contents.append(read["data"]["turn"]["content"])
    assert contents == [text, text]


def test_hook_history_honors_private_flag_without_reaching_retired_storage(tmp_path: Path, monkeypatch):
    workspace = _bound_workspace(tmp_path, monkeypatch)
    run_hook(
        provider="codex",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="agent-a",
        share_group_id="history-group",
        payload={"session_id": "private-session", "prompt": "do not archive", "private": True},
    )
    assert not (workspace / ".memoryguard" / "history" / "history.sqlite").exists()


def test_native_delete_requires_confirmation_and_tombstones_evidence(tmp_path: Path, monkeypatch):
    workspace = _bound_workspace(tmp_path, monkeypatch)
    session_id, turn_ids = _seed(
        workspace,
        [_event(workspace, session="delete-me", event_id="event-1", content="delete evidence")],
    )
    content = ContentStore(workspace)
    ConversationSync(content).add_evidence_link(memory_id="memory-1", turn_id=turn_ids[0])
    service = NativeHistoryService(workspace)
    context = _context(workspace)

    denied = service.dispatch(
        "delete",
        {"session_ids": [session_id], "confirmed": False},
        context=context,
        generation=1,
        state="V2_ACTIVE",
        mutation_receipt={"receipt_id": "r-denied"},
        idempotency_key="delete-denied",
    )
    assert denied["ok"] is False
    assert denied["code"] == "history_delete_confirmation_required"

    deleted = service.dispatch(
        "delete",
        {"session_ids": [session_id], "confirmed": True},
        context=context,
        generation=1,
        state="V2_ACTIVE",
        mutation_receipt={"receipt_id": "r1"},
        idempotency_key="delete-1",
    )
    assert deleted["ok"] is True
    assert deleted["data"]["deleted_sessions"] == 1
    with content.connection() as conn:
        link = conn.execute("SELECT status FROM content_evidence_links WHERE memory_id='memory-1'").fetchone()
        tombstone = conn.execute("SELECT 1 FROM content_tombstones WHERE reason='history_delete'").fetchone()
    assert link[0] == "invalid"
    assert tombstone is not None
