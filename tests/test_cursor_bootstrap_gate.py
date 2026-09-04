# -*- coding: utf-8 -*-
"""Cursor CallMcpTool bootstrap recognition + stdin BOM decode (packaged)."""
from __future__ import annotations

import json
import sys
from io import BytesIO
from pathlib import Path

import memoryguard.host_hooks as host_hooks
from memoryguard.evidence import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2
from memoryguard.host_hooks import (
    _is_memoryguard_bootstrap,
    _is_memoryguard_write,
    _read_stdin_json,
    read_hook_stdin_json,
    run_hook,
)
from memoryguard.memory import MemoryAtomStore
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.storage.layout import WorkspaceV2Layout
from memoryguard.storage.schema import initialize_all
from memoryguard.system.manifest import ManifestManager, ManifestState


def _bind(workspace: Path, agent_id: str, group_id: str) -> None:
    initialize_all(WorkspaceV2Layout(workspace))
    memory = MemoryAtomStore(workspace)
    evidence = EvidenceStore(workspace)
    GovernanceV2(workspace, memory_store=memory, evidence_store=evidence)
    manager = ManifestManager(workspace)
    manager.transition(ManifestState.V2_BUILDING, migration_id="cursor-bootstrap-gate")
    manager.transition(
        ManifestState.V2_READY,
        source_digest="cursor-source",
        target_digest="cursor-target",
        manifest_digest="cursor-manifest",
        digests={"validator_passed": True, "checkpoints": {"cursor": True}},
    )
    manager.transition(ManifestState.V2_ACTIVE)
    GroupControlService(workspace, write=True).bind_agent(agent_id, group_id)


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
        {"server": "user-memoryguard", "toolName": "memoryguard_memory_write"},
    )
    assert not _is_memoryguard_bootstrap(
        "CallMcpTool",
        {"server": "memoryguard-extra", "toolName": "memoryguard_context_bootstrap"},
    )
    assert _is_memoryguard_bootstrap(
        "CallMcpTool",
        json.dumps({"server": "user-memoryguard", "toolName": "memoryguard_context_bootstrap"}),
    )
    assert not _is_memoryguard_bootstrap(
        "Bash",
        {"command": "memoryguard_context_bootstrap"},
    )


def test_other_memory_writers_require_exact_verified_memoryguard_identity():
    assert not host_hooks._is_other_memory_write(
        "CallMcpTool",
        {"server": "user-memoryguard", "toolName": "memoryguard_memory_update"},
    )
    for tool_name, tool_input in (
        (
            "CallMcpTool",
            {"server": "other-memoryguard", "toolName": "memoryguard_memory_write"},
        ),
        (
            "CallMcpTool",
            {"toolName": "memoryguard_memory_write"},
        ),
        ("mcp__memoryguard__memoryguard_memory_write_extra", {}),
        ("CallMcpTool", "not-json"),
    ):
        assert host_hooks._is_other_memory_write(tool_name, tool_input)
    assert not host_hooks._is_other_memory_write(
        "CallMcpTool",
        json.dumps({"server": "user-memoryguard", "toolName": "memoryguard_memory_write"}),
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


def test_cursor_callmcptool_bootstrap_requires_verified_post_tool_success(tmp_path: Path):
    """Cursor opens its gate only after a verified bootstrap result."""
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
    state = host_hooks._load_state(workspace, "cursor", "conversation-callmcp")
    assert state["bootstrap_pending"] is True
    assert state["bootstrap_ok"] is False

    still_blocked = run_hook(
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
    assert still_blocked.get("permission") == "deny"

    assert run_hook(
        provider="cursor",
        event="post_tool",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={
            "conversation_id": "conversation-callmcp",
            "tool_name": "CallMcpTool",
            "tool_input": {
                "server": "user-memoryguard",
                "toolName": "memoryguard_context_bootstrap",
            },
            "tool_result": {
                "isError": False,
                "content": [{"type": "text", "text": json.dumps({"ok": True})}],
            },
        },
    ) == {}

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


def test_cursor_post_tool_credits_calldynamictool_tool_output_string(tmp_path: Path):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "cursor-agent", "group-a")
    session_id = "cursor-tool-output-bootstrap"
    assert run_hook(
        provider="cursor",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={"conversation_id": session_id, "prompt": "验钩子"},
    ) == {"continue": True}
    assert run_hook(
        provider="cursor",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={
            "conversation_id": session_id,
            "tool_name": "CallDynamicTool",
            "tool_input": {
                "namespace": "user-memoryguard",
                "toolName": "memoryguard_context_bootstrap",
                "arguments": {"task": "验钩子"},
            },
        },
    ) == {}
    assert run_hook(
        provider="cursor",
        event="post_tool",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={
            "conversation_id": session_id,
            "tool_name": "CallDynamicTool",
            "tool_input": {
                "namespace": "user-memoryguard",
                "toolName": "memoryguard_context_bootstrap",
                "arguments": {"task": "验钩子"},
            },
            "tool_output": json.dumps({"ok": True, "status": "ok", "error": ""}),
        },
    ) == {}
    allowed = run_hook(
        provider="cursor",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={
            "conversation_id": session_id,
            "tool_name": "Read",
            "tool_input": {"file_path": "README.md"},
        },
    )
    assert allowed == {}


def test_cursor_post_tool_credits_status_ok_ready_and_tool_response(tmp_path: Path):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "cursor-agent", "group-a")
    for key, body in (
        ("tool_output", {"status": "ok", "ready": True, "error": ""}),
        ("tool_response", {"status": "ok", "ready": True, "error": ""}),
    ):
        session_id = f"cursor-{key}-ready"
        assert run_hook(
            provider="cursor",
            event="user_prompt",
            workspace=workspace,
            agent_instance_id="cursor-agent",
            share_group_id="group-a",
            payload={"conversation_id": session_id, "prompt": "验钩子"},
        ) == {"continue": True}
        assert run_hook(
            provider="cursor",
            event="pre_tool",
            workspace=workspace,
            agent_instance_id="cursor-agent",
            share_group_id="group-a",
            payload={
                "conversation_id": session_id,
                "tool_name": "CallDynamicTool",
                "tool_input": {
                    "namespace": "user-memoryguard",
                    "toolName": "memoryguard_context_bootstrap",
                    "arguments": {"task": "验钩子"},
                },
            },
        ) == {}
        assert run_hook(
            provider="cursor",
            event="post_tool",
            workspace=workspace,
            agent_instance_id="cursor-agent",
            share_group_id="group-a",
            payload={
                "conversation_id": session_id,
                "tool_name": "CallDynamicTool",
                "tool_input": {
                    "namespace": "user-memoryguard",
                    "toolName": "memoryguard_context_bootstrap",
                    "arguments": {"task": "验钩子"},
                },
                key: json.dumps(body),
            },
        ) == {}
        allowed = run_hook(
            provider="cursor",
            event="pre_tool",
            workspace=workspace,
            agent_instance_id="cursor-agent",
            share_group_id="group-a",
            payload={
                "conversation_id": session_id,
                "tool_name": "Read",
                "tool_input": {"file_path": "README.md"},
            },
        )
        assert allowed == {}


def test_cursor_failed_or_unknown_bootstrap_result_keeps_tool_gate_closed(tmp_path: Path):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "cursor-agent", "group-a")

    for suffix, tool_result in (
        ("failed", {"isError": True, "content": [{"type": "text", "text": "error"}]}),
        ("unknown", None),
    ):
        session_id = f"cursor-bootstrap-{suffix}"
        assert run_hook(
            provider="cursor",
            event="user_prompt",
            workspace=workspace,
            agent_instance_id="cursor-agent",
            share_group_id="group-a",
            payload={"conversation_id": session_id, "prompt": "repair hooks"},
        ) == {"continue": True}
        assert run_hook(
            provider="cursor",
            event="pre_tool",
            workspace=workspace,
            agent_instance_id="cursor-agent",
            share_group_id="group-a",
            payload={
                "conversation_id": session_id,
                "tool_name": "CallMcpTool",
                "tool_input": {
                    "server": "user-memoryguard",
                    "toolName": "memoryguard_context_bootstrap",
                },
            },
        ) == {}
        post_payload = {
            "conversation_id": session_id,
            "tool_name": "CallMcpTool",
            "tool_input": {
                "server": "user-memoryguard",
                "toolName": "memoryguard_context_bootstrap",
            },
        }
        if tool_result is not None:
            post_payload["tool_result"] = tool_result
        assert run_hook(
            provider="cursor",
            event="post_tool",
            workspace=workspace,
            agent_instance_id="cursor-agent",
            share_group_id="group-a",
            payload=post_payload,
        ) == {}
        state = host_hooks._load_state(workspace, "cursor", session_id)
        assert state["bootstrap_ok"] is False
        assert state["bootstrap_pending"] is False
        assert state["context_hash"] == ""
        assert run_hook(
            provider="cursor",
            event="pre_tool",
            workspace=workspace,
            agent_instance_id="cursor-agent",
            share_group_id="group-a",
            payload={
                "conversation_id": session_id,
                "tool_name": "Shell",
                "tool_input": {"command": "echo must-stay-blocked"},
            },
        ).get("permission") == "deny"


def test_cursor_post_tool_credits_salvaged_mcp_bootstrap_name(tmp_path: Path):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "cursor-agent", "group-a")
    session_id = "cursor-salvaged-mcp-name"
    assert run_hook(
        provider="cursor",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={"conversation_id": session_id, "prompt": "验钩子"},
    ) == {"continue": True}
    assert run_hook(
        provider="cursor",
        event="post_tool",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={
            "conversation_id": session_id,
            "tool_name": "MCP:memoryguard_context_bootstrap",
            "tool_input": {"task": "验钩子"},
            "tool_output": {"ok": True, "status": "ok", "ready": True},
        },
    ) == {}
    assert run_hook(
        provider="cursor",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={
            "conversation_id": session_id,
            "tool_name": "Read",
            "tool_input": {"file_path": "README.md"},
        },
    ) == {}


def test_cursor_unverified_bootstrap_does_not_clear_existing_ok(tmp_path: Path):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "cursor-agent", "group-a")
    session_id = "cursor-keep-existing-ok"
    assert run_hook(
        provider="cursor",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={"conversation_id": session_id, "prompt": "验钩子"},
    ) == {"continue": True}
    assert run_hook(
        provider="cursor",
        event="post_tool",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={
            "conversation_id": session_id,
            "tool_name": "CallDynamicTool",
            "tool_input": {
                "namespace": "user-memoryguard",
                "toolName": "memoryguard_context_bootstrap",
                "arguments": {"task": "验钩子"},
            },
            "tool_output": json.dumps({"ok": True, "status": "ok"}),
        },
    ) == {}
    assert host_hooks._load_state(workspace, "cursor", session_id)["bootstrap_ok"] is True
    assert run_hook(
        provider="cursor",
        event="post_tool",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={
            "conversation_id": session_id,
            "tool_name": "MCP:memoryguard_context_bootstrap",
            "tool_input": {"task": "验钩子"},
        },
    ) == {}
    state = host_hooks._load_state(workspace, "cursor", session_id)
    assert state["bootstrap_ok"] is True
    assert run_hook(
        provider="cursor",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={
            "conversation_id": session_id,
            "tool_name": "Read",
            "tool_input": {"file_path": "README.md"},
        },
    ) == {}
