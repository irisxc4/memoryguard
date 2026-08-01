"""MCP and Hook integration tests for the isolated raw-history archive."""
from __future__ import annotations

import json
from pathlib import Path

from memoryguard.agent_binding import AgentBindingStore
from memoryguard.conversation_history import ConversationHistoryStore, HistoryScope
from memoryguard.host_hooks import run_hook
from memoryguard.mcp_server import TOOLS, execute_tool
from memoryguard.shared_memory_store import SharedMemoryStore


def _bound_workspace(tmp_path: Path, monkeypatch, agent: str = "agent-a") -> Path:
    workspace = tmp_path / "control"
    workspace.mkdir()
    AgentBindingStore(workspace).bind_agent(agent, "history-group")
    # Create the governed store separately so bootstrap has a valid empty
    # group.  The history test never writes a SharedMemoryRecord.
    SharedMemoryStore(workspace, "history-group")
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(workspace))
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", agent)
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    return workspace


def test_mcp_history_uses_trusted_agent_scope_and_stays_out_of_bootstrap(tmp_path: Path, monkeypatch):
    workspace = _bound_workspace(tmp_path, monkeypatch)
    store = ConversationHistoryStore(workspace)
    archived = store.append_turn(
        HistoryScope(agent_instance_id="agent-a", project_ref="project-x", provider="codex"),
        external_session_id="session-a", provider="codex", role="user",
        content="历史原文不能进入长期 bootstrap", event_id="turn-a",
    )

    assert {tool["name"] for tool in TOOLS} >= {
        "memoryguard_history_search", "memoryguard_history_timeline",
        "memoryguard_history_read", "memoryguard_history_extract_preview",
        "memoryguard_history_list_sessions", "memoryguard_history_export",
        "memoryguard_history_delete",
    }
    good = execute_tool("memoryguard_history_search", {"query": "长期 bootstrap"})
    assert good.get("isError") is not True
    assert archived["turn_id"] not in execute_tool(
        "memoryguard_context_bootstrap", {"task": "bootstrap"}
    )["content"][0]["text"]
    spoofed = execute_tool("memoryguard_history_search", {
        "query": "长期 bootstrap", "scope": {"agent_instance_id": "agent-b"},
    })
    assert spoofed["isError"] is True
    assert "trusted_agent_scope_required" in spoofed["content"][0]["text"]


def test_hook_archives_bounded_utf8_turns_idempotently_and_isolates_agents(tmp_path: Path, monkeypatch):
    workspace = _bound_workspace(tmp_path, monkeypatch)
    payload = {
        "session_id": "session-1", "turn_id": "user-turn-1",
        "prompt": "请用 UTF-8 保存这段中文历史", "cwd": str(workspace),
    }
    run_hook(provider="codex", event="user_prompt", workspace=workspace,
             agent_instance_id="agent-a", share_group_id="history-group", payload=payload)
    # The same lifecycle delivery must not create a duplicate turn.
    run_hook(provider="codex", event="user_prompt", workspace=workspace,
             agent_instance_id="agent-a", share_group_id="history-group", payload=payload)
    run_hook(provider="codex", event="stop", workspace=workspace,
             agent_instance_id="agent-a", share_group_id="history-group", payload={
                 "session_id": "session-1", "generation_id": "assistant-final-1",
                 "last_assistant_message": "助手最终回答也独立归档。", "cwd": str(workspace),
             })

    store = ConversationHistoryStore(workspace)
    scope = HistoryScope(agent_instance_id="agent-a", project_ref=str(workspace), provider="codex", share_group_id="history-group")
    sessions = store.list_sessions(scope)["sessions"]
    assert len(sessions) == 1
    raw = store.read(scope, session_id=sessions[0]["session_id"])
    assert [turn["content"] for turn in raw["turns"]] == [
        "请用 UTF-8 保存这段中文历史", "助手最终回答也独立归档。",
    ]
    assert store.list_sessions(HistoryScope(agent_instance_id="agent-b"))["sessions"] == []
    receipt = next((workspace / ".memoryguard" / "hook-runtime" / "heartbeat").glob("*.json"))
    receipt_text = receipt.read_text(encoding="utf-8")
    state_text = next((workspace / ".memoryguard" / "hook-runtime" / "state").rglob("*.json")).read_text(encoding="utf-8")
    assert "请用 UTF-8" not in receipt_text and "助手最终回答" not in receipt_text
    assert "请用 UTF-8" not in state_text and "助手最终回答" not in state_text
    assert json.loads(receipt_text)["history_archive"]["archived"] is True


def test_hook_history_honors_private_flag_and_reports_missing_assistant_coverage(tmp_path: Path, monkeypatch):
    workspace = _bound_workspace(tmp_path, monkeypatch)
    run_hook(provider="claude", event="user_prompt", workspace=workspace,
             agent_instance_id="agent-a", share_group_id="history-group", payload={
                 "session_id": "private-session", "prompt": "不要归档", "private": True,
             })
    run_hook(provider="claude", event="stop", workspace=workspace,
             agent_instance_id="agent-a", share_group_id="history-group", payload={"session_id": "no-final"})
    assert ConversationHistoryStore(workspace).list_sessions(
        HistoryScope(agent_instance_id="agent-a")
    )["sessions"] == []
    receipt = next((workspace / ".memoryguard" / "hook-runtime" / "heartbeat").glob("*.json"))
    assert json.loads(receipt.read_text(encoding="utf-8"))["history_archive"]["reason"] == "assistant_content_unavailable"


def test_mcp_delete_requires_confirmation_and_always_tombstones_evidence(tmp_path: Path, monkeypatch):
    workspace = _bound_workspace(tmp_path, monkeypatch)
    store = ConversationHistoryStore(workspace)
    result = store.append_turn(HistoryScope(agent_instance_id="agent-a"), external_session_id="delete-me",
                               provider="codex", role="user", content="delete evidence", event_id="event-1")
    store.add_evidence_link(memory_id="memory-1", session_id=result["session_id"], turn_id=result["turn_id"])
    denied = execute_tool("memoryguard_history_delete", {"session_ids": [result["session_id"]]})
    assert denied["isError"] is True
    deleted = execute_tool("memoryguard_history_delete", {"session_ids": [result["session_id"]], "confirmed": True})
    assert deleted.get("isError") is not True
    with store._connect() as conn:
        link = conn.execute("SELECT status FROM evidence_links WHERE memory_id='memory-1'").fetchone()
    assert link["status"] == "invalid"
