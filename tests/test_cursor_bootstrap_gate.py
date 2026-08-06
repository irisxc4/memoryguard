# -*- coding: utf-8 -*-
"""Cursor CallMcpTool bootstrap recognition + stdin BOM decode (packaged)."""
from __future__ import annotations

import json
import sys
from io import BytesIO
from pathlib import Path

from memoryguard.agent_binding import AgentBindingStore
from memoryguard.host_hooks import (
    _is_memoryguard_bootstrap,
    _is_memoryguard_write,
    _read_stdin_json,
    read_hook_stdin_json,
    run_hook,
)


def _bind(workspace: Path, agent_id: str, group_id: str) -> None:
    AgentBindingStore(workspace).bind_agent(agent_id, group_id)


def test_direct_bootstrap_name():
    assert _is_memoryguard_bootstrap("memoryguard_context_bootstrap")
    assert _is_memoryguard_bootstrap("MCP:memoryguard_context_bootstrap")


def test_callmcptool_wrapper_recognizes_bootstrap_and_write():
    assert _is_memoryguard_bootstrap(
        "CallMcpTool",
        {"server": "user-memoryguard", "toolName": "memoryguard_context_bootstrap"},
    )
    assert not _is_memoryguard_bootstrap(
        "CallMcpTool",
        {"toolName": "memoryguard_memory_search"},
    )
    assert _is_memoryguard_write(
        "CallMcpTool",
        {"toolName": "memoryguard_memory_write"},
    )


def test_read_hook_stdin_json_bom_and_plain(monkeypatch):
    payload = {"tool_name": "Shell", "tool_input": {"command": "echo test"}}
    plain = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    bomed = b"\xef\xbb\xbf" + plain

    class _Stdin:
        def __init__(self, raw: bytes):
            self.buffer = BytesIO(raw)

    monkeypatch.setattr(sys, "stdin", _Stdin(plain))
    assert read_hook_stdin_json()["tool_name"] == "Shell"
    monkeypatch.setattr(sys, "stdin", _Stdin(bomed))
    assert _read_stdin_json()["tool_name"] == "Shell"


def test_cursor_callmcptool_bootstrap_on_pre_tool_unlocks_shell(tmp_path: Path):
    """Packaged Cursor seam: CallMcpTool bootstrap on pre_tool sets bootstrap_ok."""
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "cursor-agent", "group-a")
    assert run_hook(
        provider="cursor",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={
            "conversation_id": "conversation-callmcp",
            "generation_id": "generation-1",
            "prompt": "fix hooks",
        },
    ) == {"continue": True}

    blocked = run_hook(
        provider="cursor",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={
            "conversation_id": "conversation-callmcp",
            "tool_name": "Shell",
            "tool_input": {"command": "echo blocked"},
        },
    )
    assert blocked.get("permission") == "deny"

    unlocked = run_hook(
        provider="cursor",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={
            "conversation_id": "conversation-callmcp",
            "tool_name": "CallMcpTool",
            "tool_input": {
                "server": "user-memoryguard",
                "toolName": "memoryguard_context_bootstrap",
                "arguments": {"task": "current user request"},
            },
        },
    )
    assert unlocked == {}

    allowed = run_hook(
        provider="cursor",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={
            "conversation_id": "conversation-callmcp",
            "tool_name": "Shell",
            "tool_input": {"command": "echo ok"},
        },
    )
    assert allowed == {}
