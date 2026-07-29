import json
from pathlib import Path

import pytest

from memoryguard.agent_binding import AgentBindingStore
from memoryguard.cli import main as cli_main
from memoryguard.host_hooks import (
    HostHookManager,
    run_hook,
    set_hook_mode,
)
from memoryguard.provider_adapters import CodexAdapter
from memoryguard.schema_v3 import (
    MemoryKind,
    SharedMemoryRecord,
    SharedMemoryStatus,
)
from memoryguard.shared_memory_store import SharedMemoryStore


def _bind(workspace: Path, agent_id: str, group_id: str) -> None:
    AgentBindingStore(workspace).bind_agent(agent_id, group_id)


def _record(memory_id: str, body: str, kind: MemoryKind) -> SharedMemoryRecord:
    return SharedMemoryRecord(
        memory_id=memory_id,
        body=body,
        kind=kind,
        status=SharedMemoryStatus.ACTIVE,
        confidence=0.9,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        agent_instance_id="source-agent",
    )


@pytest.mark.parametrize(
    ("provider", "relative_config", "event_name"),
    [
        ("claude", ".claude/settings.json", "UserPromptSubmit"),
        ("codex", ".codex/hooks.json", "UserPromptSubmit"),
        ("cursor", ".cursor/hooks.json", "beforeSubmitPrompt"),
    ],
)
def test_hook_install_is_idempotent_and_preserves_other_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    relative_config: str,
    event_name: str,
):
    home = tmp_path / "home"
    workspace = tmp_path / "control"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _bind(workspace, f"{provider}-agent", "group-a")

    config_path = home / relative_config
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if provider == "cursor":
        original = {
            "version": 1,
            "hooks": {
                "stop": [{"command": "python user-stop.py"}],
            },
        }
    else:
        original = {
            "hooks": {
                "Stop": [{
                    "hooks": [{
                        "type": "command",
                        "command": "python user-stop.py",
                    }],
                }],
            },
        }
    config_path.write_text(json.dumps(original), encoding="utf-8")

    manager = HostHookManager(workspace)
    first = manager.install(
        provider,
        agent_instance_id=f"{provider}-agent",
        share_group_id="group-a",
    )
    second = manager.install(
        provider,
        agent_instance_id=f"{provider}-agent",
        share_group_id="group-a",
    )

    data = json.loads(config_path.read_text(encoding="utf-8"))
    serialized = json.dumps(data)
    assert first["configured"] and second["configured"]
    expected_handlers = 6 if provider == "cursor" else 7
    assert serialized.count("memoryguard.host_hooks") == expected_handlers
    assert serialized.count("python user-stop.py") == 1
    assert event_name in data["hooks"]

    removed = manager.uninstall(provider)
    remaining = json.loads(config_path.read_text(encoding="utf-8"))
    remaining_text = json.dumps(remaining)
    assert removed["configured"] is False
    assert "memoryguard.host_hooks" not in remaining_text
    assert remaining_text.count("python user-stop.py") == 1


def test_trae_reports_verified_fallback_instead_of_writing_fake_hook(
    tmp_path: Path,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    result = HostHookManager(workspace).install(
        "trae",
        agent_instance_id="trae-agent",
        share_group_id="group-a",
    )
    assert result["status"] == "unsupported"
    assert result["configured"] is False
    assert result["capability"]["context_mode"] == "mcp_and_rules_only"


def test_user_prompt_injects_bounded_context_and_receipt_has_no_raw_prompt(
    tmp_path: Path,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "codex-agent", "group-a")
    store = SharedMemoryStore(workspace, "group-a")
    store.append_record(_record(
        "pref",
        "用户长期偏好：回答保持简洁",
        MemoryKind.PREFERENCE,
    ))
    store.append_record(_record(
        "project",
        "MemoryGuard 项目默认使用 RTK 运行测试",
        MemoryKind.PROJECT,
    ))
    prompt = "检查 MemoryGuard 项目的 RTK 测试规则"

    result = run_hook(
        provider="codex",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            "session_id": "session-1",
            "turn_id": "turn-1",
            "prompt": prompt,
            "cwd": str(workspace),
        },
    )

    context = result["hookSpecificOutput"]["additionalContext"]
    assert "回答保持简洁" in context
    assert "默认使用 RTK" in context
    state_files = list(
        (workspace / ".memoryguard" / "hook-runtime" / "state").rglob("*.json")
    )
    assert state_files
    assert prompt not in state_files[0].read_text(encoding="utf-8")


def test_pre_tool_blocks_native_memory_write_but_allows_project_file(
    tmp_path: Path,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "codex-agent", "group-a")
    base = {
        "session_id": "session-1",
        "tool_name": "Write",
    }

    denied = run_hook(
        provider="codex",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            **base,
            "tool_input": {
                "file_path": str(
                    tmp_path / "home" / ".codex" / "memories" / "MEMORY.md"
                ),
            },
        },
    )
    allowed = run_hook(
        provider="codex",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            **base,
            "tool_input": {"file_path": str(workspace / "README.md")},
        },
    )

    assert (
        denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    )
    assert allowed == {}


def test_cursor_requires_bootstrap_before_first_tool_and_stop_continues_once(
    tmp_path: Path,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "cursor-agent", "group-a")
    prompt_payload = {
        "conversation_id": "conversation-1",
        "generation_id": "generation-1",
        "prompt": "以后默认使用 RTK",
    }
    assert run_hook(
        provider="cursor",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload=prompt_payload,
    ) == {"continue": True}

    blocked = run_hook(
        provider="cursor",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={
            "conversation_id": "conversation-1",
            "tool_name": "Read",
            "tool_input": {"file_path": str(workspace / "README.md")},
        },
    )
    assert blocked["permission"] == "deny"
    assert "memoryguard_context_bootstrap" in blocked["agent_message"]

    run_hook(
        provider="cursor",
        event="post_tool",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={
            "conversation_id": "conversation-1",
            "tool_name": "MCP:memoryguard_context_bootstrap",
        },
    )
    allowed = run_hook(
        provider="cursor",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={
            "conversation_id": "conversation-1",
            "tool_name": "Read",
            "tool_input": {"file_path": str(workspace / "README.md")},
        },
    )
    assert allowed == {}

    first_stop = run_hook(
        provider="cursor",
        event="stop",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={"conversation_id": "conversation-1", "loop_count": 0},
    )
    second_stop = run_hook(
        provider="cursor",
        event="stop",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={"conversation_id": "conversation-1", "loop_count": 1},
    )
    assert "memoryguard_memory_write" in first_stop["followup_message"]
    assert second_stop == {}

    subagent_block = run_hook(
        provider="cursor",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={
            "conversation_id": "conversation-1",
            "subagent_id": "subagent-1",
            "tool_name": "Read",
            "tool_input": {"file_path": str(workspace / "README.md")},
        },
    )
    assert subagent_block["permission"] == "deny"
    assert "memoryguard_context_bootstrap" in subagent_block["agent_message"]


def test_global_provider_install_includes_hook_without_duplicate_handlers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    home = tmp_path / "home"
    workspace = tmp_path / "control"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _bind(workspace, "codex-agent", "group-a")

    adapter = CodexAdapter(workspace)
    first = adapter.install(
        workspace,
        share_group_id="group-a",
        agent_instance_id="codex-agent",
        global_scope=True,
    )
    second = adapter.install(
        workspace,
        share_group_id="group-a",
        agent_instance_id="codex-agent",
        global_scope=True,
    )

    hooks_text = (home / ".codex" / "hooks.json").read_text(encoding="utf-8")
    assert first["hook_configured"] is True
    assert second["hook_configured"] is True
    assert hooks_text.count("memoryguard.host_hooks") == 7


def test_paused_mode_is_emergency_bypass_for_tool_guard(
    tmp_path: Path,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "codex-agent", "group-a")
    set_hook_mode(workspace, "codex", "codex-agent", "paused")

    result = run_hook(
        provider="codex",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            "session_id": "session-1",
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(
                    tmp_path / ".codex" / "memories" / "MEMORY.md"
                ),
            },
        },
    )
    assert result == {}


def test_invalid_existing_hook_config_is_not_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    home = tmp_path / "home"
    workspace = tmp_path / "control"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _bind(workspace, "codex-agent", "group-a")
    path = home / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid JSON hook config"):
        HostHookManager(workspace).install(
            "codex",
            agent_instance_id="codex-agent",
            share_group_id="group-a",
        )
    assert path.read_text(encoding="utf-8") == "{broken"


def test_subagent_start_receives_bounded_governance_context(
    tmp_path: Path,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "claude-agent", "group-a")
    SharedMemoryStore(workspace, "group-a").append_record(_record(
        "project",
        "MemoryGuard Hook 适配必须保持配置幂等",
        MemoryKind.PROJECT,
    ))

    result = run_hook(
        provider="claude",
        event="subagent_start",
        workspace=workspace,
        agent_instance_id="claude-agent",
        share_group_id="group-a",
        payload={
            "session_id": "session-1",
            "task": "检查 MemoryGuard Hook 配置幂等",
        },
    )
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "配置幂等" in context
    assert "不得写入宿主原生记忆" in context


def test_cli_ensure_installs_only_explicit_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    home = tmp_path / "home"
    workspace = tmp_path / "control"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    _bind(workspace, "claude-agent", "group-a")

    rc = cli_main([
        "hooks",
        "ensure",
        "--provider",
        "claude",
        "--workspace",
        str(workspace),
        "--agent-id",
        "claude-agent",
        "--share-group-id",
        "group-a",
    ])
    output = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert output["configured"] is True
    assert (home / ".claude" / "settings.json").exists()
    assert not (home / ".codex" / "hooks.json").exists()
    assert not (home / ".cursor" / "hooks.json").exists()
