"""Provider MCP 配置的可信 Agent 身份传播回归测试。"""
from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.agent_binding import AgentBindingStore
from memoryguard.gui import GovernanceApi
from memoryguard.provider_adapters import (
    ClaudeAdapter,
    CodexAdapter,
    CursorAdapter,
    TraeAdapter,
)
from memoryguard.schema_v3 import AgentInstance


def _patch_home(monkeypatch, home: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))


def test_json_install_writes_real_identity_without_fake_binding(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_path = workspace / ".mcp.json"
    config_path.write_text(
        json.dumps({
            "mcpServers": {"other": {"command": "other-server"}},
            "userSetting": {"keep": True},
        }),
        encoding="utf-8",
    )
    instruction_path = workspace / "CLAUDE.md"
    instruction_path.write_text("# User instructions\n", encoding="utf-8")

    store = AgentBindingStore(workspace)
    binding = store.bind_agent("claude-real-7", "team-json")
    adapter = ClaudeAdapter(workspace)
    first = adapter.install(
        workspace,
        share_group_id="team-json",
        agent_instance_id="claude-real-7",
    )
    first_config = config_path.read_text(encoding="utf-8")
    second = adapter.install(
        workspace,
        share_group_id="team-json",
        agent_instance_id="claude-real-7",
    )

    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["mcpServers"]["memoryguard"]["env"] == {
        "MEMORYGUARD_AGENT_ID": "claude-real-7",
        "MEMORYGUARD_WORKSPACE": str(workspace.resolve()),
    }
    assert config["mcpServers"]["other"] == {"command": "other-server"}
    assert config["userSetting"] == {"keep": True}
    assert config_path.read_text(encoding="utf-8") == first_config
    instruction_text = instruction_path.read_text(encoding="utf-8")
    assert instruction_text.count(
        "<!-- BEGIN memoryguard:provider-redirect -->"
    ) == 1
    assert "`memoryguard_context_bootstrap`" in instruction_text
    assert "同一任务不得重复调用" in instruction_text
    assert "宿主当前对话上下文保持原样，不替换、不重复注入" in instruction_text
    assert "历史对话文件只是可选来源，必须先萃取为长期记忆" in instruction_text
    assert 'query="用户 长期 偏好"' not in instruction_text
    assert first["binding_id"] == binding.binding_id
    assert second["binding_id"] == binding.binding_id

    bindings = store.list_bindings(include_inactive=True)
    assert [item.agent_instance_id for item in bindings] == ["claude-real-7"]
    assert not store.find_by_agent("claude", include_inactive=True)
    assert adapter.status()["installed"] is True
    adapter.uninstall()
    assert adapter.status()["installed"] is False
    assert json.loads(config_path.read_text(encoding="utf-8")) == {
        "mcpServers": {"other": {"command": "other-server"}},
        "userSetting": {"keep": True},
    }
    assert "# User instructions" in instruction_path.read_text(encoding="utf-8")
    assert store.get_binding(binding.binding_id).status.value == "active"


def test_claude_global_scope_uses_user_config_and_stable_workspace(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    _patch_home(monkeypatch, home)
    AgentBindingStore(workspace).bind_agent("claude-global", "team-global")

    result = ClaudeAdapter(workspace).install(
        workspace,
        share_group_id="team-global",
        agent_instance_id="claude-global",
        global_scope=True,
    )

    config = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    assert config["mcpServers"]["memoryguard"]["env"] == {
        "MEMORYGUARD_AGENT_ID": "claude-global",
        "MEMORYGUARD_WORKSPACE": str(workspace.resolve()),
    }
    assert result["mcp_config_file"] == str(home / ".claude.json")
    assert (home / ".claude" / "CLAUDE.md").exists()
    assert not (workspace / ".mcp.json").exists()


def test_codex_toml_install_writes_real_identity_and_is_idempotent(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    _patch_home(monkeypatch, home)

    global_config_path = home / ".codex" / "config.toml"
    global_config_path.parent.mkdir(parents=True)
    global_config_path.write_text(
        '[mcp_servers.global_only]\ncommand = "global-server"\n\n'
        '[mcp_servers.memoryguard]\ncommand = "legacy-global"\n',
        encoding="utf-8",
    )
    config_path = workspace / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        '[mcp_servers.other]\ncommand = "other-server"\n\n'
        "[features]\nkeep = true\n",
        encoding="utf-8",
    )
    instruction_path = workspace / "AGENTS.md"
    instruction_path.write_text("# User agents\n", encoding="utf-8")

    store = AgentBindingStore(workspace)
    binding = store.bind_agent("codex-real-9", "team-toml")
    adapter = CodexAdapter(workspace)
    result = adapter.install(
        workspace,
        share_group_id="team-toml",
        agent_instance_id="codex-real-9",
    )
    first_config = config_path.read_text(encoding="utf-8")
    adapter.install(
        workspace,
        share_group_id="team-toml",
        agent_instance_id="codex-real-9",
    )

    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert parsed["mcp_servers"]["memoryguard"]["env"] == {
        "MEMORYGUARD_AGENT_ID": "codex-real-9",
        "MEMORYGUARD_WORKSPACE": str(workspace.resolve()),
    }
    assert parsed["mcp_servers"]["other"]["command"] == "other-server"
    assert parsed["features"]["keep"] is True
    assert tomllib.loads(
        global_config_path.read_text(encoding="utf-8")
    )["mcp_servers"]["memoryguard"]["command"] == "legacy-global"
    assert any("旧用户级" in warning for warning in result["warnings"])
    assert config_path.read_text(encoding="utf-8") == first_config
    assert result["binding_id"] == binding.binding_id
    assert [item.agent_instance_id for item in store.list_bindings()] == [
        "codex-real-9",
    ]

    assert adapter.status()["installed"] is True
    adapter.uninstall()
    assert adapter.status()["installed"] is False
    remaining = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert remaining["mcp_servers"]["other"]["command"] == "other-server"
    assert remaining["features"]["keep"] is True
    assert store.get_binding(binding.binding_id).status.value == "active"


def test_codex_global_install_migrates_unmarked_legacy_section(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    _patch_home(monkeypatch, home)
    AgentBindingStore(workspace).bind_agent("codex-legacy", "team-legacy")

    config_path = home / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        '[mcp_servers.other]\ncommand = "keep-me"\n\n'
        '[mcp_servers.memoryguard]\n'
        'enabled = true\n'
        'command = "python"\n'
        'args = ["-m", "memoryguard.mcp_server"]\n'
        'env = { MEMORYGUARD_AGENT_ID = "old-agent" }\n\n'
        "# preserve this unrelated comment\n"
        '[features]\nkeep = true\n',
        encoding="utf-8",
    )

    adapter = CodexAdapter(workspace)
    adapter.install(
        workspace,
        share_group_id="team-legacy",
        agent_instance_id="codex-legacy",
        global_scope=True,
    )
    first_text = config_path.read_text(encoding="utf-8")
    adapter.install(
        workspace,
        share_group_id="team-legacy",
        agent_instance_id="codex-legacy",
        global_scope=True,
    )

    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert parsed["mcp_servers"]["memoryguard"]["env"] == {
        "MEMORYGUARD_AGENT_ID": "codex-legacy",
        "MEMORYGUARD_WORKSPACE": str(workspace.resolve()),
    }
    assert parsed["mcp_servers"]["other"]["command"] == "keep-me"
    assert parsed["features"]["keep"] is True
    assert "# preserve this unrelated comment" in first_text
    assert first_text.count("[mcp_servers.memoryguard]") == 1
    assert first_text.count("# BEGIN memoryguard:provider-redirect") == 1
    assert config_path.read_text(encoding="utf-8") == first_text


def test_governance_configures_trae_and_reports_other_adapter_errors(
    tmp_path, monkeypatch,
):
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    _patch_home(monkeypatch, home)
    appdata = home / "AppData" / "Roaming"
    monkeypatch.setenv("APPDATA", str(appdata))

    store = AgentBindingStore(workspace)
    store.bind_agents_to_group(
        ["codex-real", "claude-error", "trae-real"],
        "team-mixed",
    )

    instances = [
        AgentInstance("codex-real", "codex", "codex"),
        AgentInstance("claude-error", "claude", "claude"),
        AgentInstance("trae-real", "trae", "trae"),
    ]
    monkeypatch.setattr(
        "memoryguard.agent_locator.AgentLocator.detect_instances",
        lambda self: (instances, {}),
    )
    def fail_install(self, *args, **kwargs):
        raise RuntimeError("simulated adapter error")

    monkeypatch.setattr(
        "memoryguard.provider_adapters.ClaudeAdapter.install",
        fail_install,
    )

    result = GovernanceApi(workspace).install_shared_group_mcp_redirects(
        "team-mixed",
        confirmed=True,
        _admin_override=True,
    )

    assert result["ok"] is False
    assert result["status"] == "partial"
    assert result["installed_count"] == 2
    assert result["skipped_count"] == 0
    assert result["error_count"] == 1
    by_agent = {item["agent_instance_id"]: item for item in result["installed"]}
    assert by_agent["codex-real"]["status"] == "configured"
    assert by_agent["claude-error"]["status"] == "error"
    assert by_agent["claude-error"]["error"] == "simulated adapter error"
    assert by_agent["trae-real"]["status"] == "configured"
    assert by_agent["trae-real"]["provider"] == "trae"
    trae_global_config = appdata / "TRAE SOLO CN" / "User" / "mcp.json"
    assert json.loads(
        trae_global_config.read_text(encoding="utf-8")
    )["mcpServers"]["memoryguard"]["env"] == {
        "MEMORYGUARD_AGENT_ID": "trae-real",
        "MEMORYGUARD_WORKSPACE": str(workspace.resolve()),
    }

    parsed = tomllib.loads(
        (home / ".codex" / "config.toml").read_text(encoding="utf-8")
    )
    assert parsed["mcp_servers"]["memoryguard"]["env"] == {
        "MEMORYGUARD_AGENT_ID": "codex-real",
        "MEMORYGUARD_WORKSPACE": str(workspace.resolve()),
    }
    assert {
        binding.agent_instance_id
        for binding in store.list_bindings(include_inactive=True)
    } == {"codex-real", "claude-error", "trae-real"}
    assert not store.find_by_agent("codex", include_inactive=True)


def test_cursor_workspace_install_uses_project_config(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    _patch_home(monkeypatch, home)

    global_config = home / ".cursor" / "mcp.json"
    global_config.parent.mkdir(parents=True)
    global_config.write_text(
        json.dumps({"mcpServers": {"global-only": {"command": "global"}}}),
        encoding="utf-8",
    )
    binding = AgentBindingStore(workspace).bind_agent(
        "cursor-real", "team-cursor"
    )

    adapter = CursorAdapter(workspace)
    result = adapter.install(
        workspace,
        share_group_id="team-cursor",
        agent_instance_id="cursor-real",
    )
    first_config = (workspace / ".cursor" / "mcp.json").read_text(
        encoding="utf-8"
    )
    adapter.install(
        workspace,
        share_group_id="team-cursor",
        agent_instance_id="cursor-real",
    )

    project_config = workspace / ".cursor" / "mcp.json"
    parsed = json.loads(project_config.read_text(encoding="utf-8"))
    assert parsed["mcpServers"]["memoryguard"]["env"] == {
        "MEMORYGUARD_AGENT_ID": "cursor-real",
        "MEMORYGUARD_WORKSPACE": str(workspace.resolve()),
    }
    assert json.loads(global_config.read_text(encoding="utf-8")) == {
        "mcpServers": {"global-only": {"command": "global"}},
    }
    assert result["binding_id"] == binding.binding_id
    assert result["status"] == "configured"
    assert result["restart_required"] is True
    assert result["runtime_verified"] is False
    assert project_config.read_text(encoding="utf-8") == first_config
    assert list(parsed["mcpServers"]).count("memoryguard") == 1
    assert (
        workspace / ".cursor" / "rules" / "memoryguard.mdc"
    ).read_text(encoding="utf-8").count(
        "<!-- BEGIN memoryguard:provider-redirect -->"
    ) == 1


def test_trae_workspace_install_uses_project_config_and_preserves_servers(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    _patch_home(monkeypatch, home)

    project_config = workspace / ".trae" / "mcp.json"
    project_config.parent.mkdir(parents=True)
    project_config.write_text(
        json.dumps({
            "mcpServers": {"existing": {"command": "existing-server"}},
            "projectSetting": {"keep": True},
        }),
        encoding="utf-8-sig",
    )
    binding = AgentBindingStore(workspace).bind_agent(
        "trae-real", "team-trae"
    )

    adapter = TraeAdapter(workspace)
    first = adapter.install(
        workspace,
        share_group_id="team-trae",
        agent_instance_id="trae-real",
    )
    first_config = project_config.read_text(encoding="utf-8")
    second = adapter.install(
        workspace,
        share_group_id="team-trae",
        agent_instance_id="trae-real",
    )

    parsed = json.loads(project_config.read_text(encoding="utf-8"))
    assert parsed["mcpServers"]["memoryguard"]["env"] == {
        "MEMORYGUARD_AGENT_ID": "trae-real",
        "MEMORYGUARD_WORKSPACE": str(workspace.resolve()),
    }
    assert parsed["mcpServers"]["existing"] == {"command": "existing-server"}
    assert parsed["projectSetting"] == {"keep": True}
    assert project_config.read_text(encoding="utf-8") == first_config
    assert list(parsed["mcpServers"]).count("memoryguard") == 1
    assert (workspace / "AGENTS.md").read_text(encoding="utf-8").count(
        "<!-- BEGIN memoryguard:provider-redirect -->"
    ) == 1
    assert first["binding_id"] == binding.binding_id
    assert second["binding_id"] == binding.binding_id
    assert adapter.status()["installed"] is True

    adapter.uninstall()
    remaining = json.loads(project_config.read_text(encoding="utf-8"))
    assert remaining == {
        "mcpServers": {"existing": {"command": "existing-server"}},
        "projectSetting": {"keep": True},
    }
    assert adapter.status()["installed"] is False
    assert AgentBindingStore(workspace).get_binding(
        binding.binding_id
    ).status.value == "active"


def test_reinstall_is_idempotent_and_shared_agents_rule_survives_single_uninstall(
    tmp_path, monkeypatch,
):
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    _patch_home(monkeypatch, home)
    appdata = home / "AppData" / "Roaming"
    monkeypatch.setenv("APPDATA", str(appdata))
    store = AgentBindingStore(workspace)
    store.bind_agents_to_group(["codex-real", "trae-real"], "team-shared")
    monkeypatch.setattr(
        "memoryguard.agent_locator.AgentLocator.detect_instances",
        lambda self: ([
            AgentInstance("codex-real", "codex", "codex"),
            AgentInstance("trae-real", "trae", "trae"),
        ], {}),
    )

    api = GovernanceApi(workspace)
    first = api.install_shared_group_mcp_redirects(
        "team-shared", confirmed=True, _admin_override=True,
    )
    second = api.install_shared_group_mcp_redirects(
        "team-shared", confirmed=True, _admin_override=True,
    )

    assert first["configured_count"] == second["configured_count"] == 2
    agents_text = (home / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
    assert agents_text.count(
        "<!-- BEGIN memoryguard:provider-redirect -->"
    ) == 1
    codex_text = (home / ".codex" / "config.toml").read_text(
        encoding="utf-8"
    )
    assert codex_text.count("# BEGIN memoryguard:provider-redirect") == 1
    assert codex_text.count("[mcp_servers.memoryguard]") == 1
    trae_data = json.loads(
        (appdata / "TRAE SOLO CN" / "User" / "mcp.json").read_text(
            encoding="utf-8"
        )
    )
    assert list(trae_data["mcpServers"]).count("memoryguard") == 1
    assert not (workspace / ".codex" / "config.toml").exists()
    assert not (workspace / ".trae" / "mcp.json").exists()


def test_install_requires_real_active_binding_before_writing(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = ClaudeAdapter(workspace)

    for agent_id in ("", "missing-agent"):
        try:
            adapter.install(
                workspace,
                share_group_id="team-required",
                agent_instance_id=agent_id,
            )
        except ValueError as exc:
            assert "agent_instance_id" in str(exc) or "active binding" in str(exc)
        else:
            raise AssertionError("install must fail without a real active binding")

    assert not (workspace / "CLAUDE.md").exists()
    assert not (workspace / ".mcp.json").exists()


def test_install_rolls_back_first_file_when_second_write_fails(
    tmp_path, monkeypatch,
):
    import memoryguard.provider_adapters as provider_adapters

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    instruction_path = workspace / "CLAUDE.md"
    config_path = workspace / ".mcp.json"
    instruction_path.write_text("# original instruction\n", encoding="utf-8")
    config_path.write_text(
        json.dumps({"mcpServers": {"other": {"command": "other"}}}),
        encoding="utf-8",
    )
    AgentBindingStore(workspace).bind_agent("claude-real", "team-rollback")

    original_atomic_write = provider_adapters._atomic_write_bytes
    calls = {"count": 0}

    def fail_second_write(path, data):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("simulated second write failure")
        return original_atomic_write(path, data)

    monkeypatch.setattr(
        provider_adapters, "_atomic_write_bytes", fail_second_write
    )

    try:
        ClaudeAdapter(workspace).install(
            workspace,
            share_group_id="team-rollback",
            agent_instance_id="claude-real",
        )
    except OSError as exc:
        assert "simulated second write failure" in str(exc)
    else:
        raise AssertionError("install must surface the failed config write")

    assert instruction_path.read_text(encoding="utf-8") == "# original instruction\n"
    assert json.loads(config_path.read_text(encoding="utf-8")) == {
        "mcpServers": {"other": {"command": "other"}},
    }


def test_install_refuses_corrupt_existing_config(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_path = workspace / ".mcp.json"
    config_path.write_text("{broken-json", encoding="utf-8")
    AgentBindingStore(workspace).bind_agent("claude-real", "team-corrupt")

    try:
        ClaudeAdapter(workspace).install(
            workspace,
            share_group_id="team-corrupt",
            agent_instance_id="claude-real",
        )
    except ValueError as exc:
        assert "invalid JSON" in str(exc)
    else:
        raise AssertionError("corrupt user config must not be overwritten")

    assert config_path.read_text(encoding="utf-8") == "{broken-json"
    assert not (workspace / "CLAUDE.md").exists()


def test_trusted_env_supplies_missing_tool_identity_and_rejects_mismatch(
    tmp_path, monkeypatch,
):
    from memoryguard.mcp_server import (
        TOOLS,
        _handle_memory_status,
        _handle_memory_write,
    )
    from memoryguard.shared_memory_store import SharedMemoryStore

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    AgentBindingStore(workspace).bind_agent("trusted-real", "trusted-group")
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "trusted-real")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    monkeypatch.delenv("MEMORYGUARD_ALLOW_ANON", raising=False)

    write_args = {
        "workspace": str(workspace),
        "body": "trusted environment identity",
        "share_group_id": "attacker-selected-group",
    }
    written = _handle_memory_write(write_args)
    assert not written.get("isError"), written
    assert write_args["agent_instance_id"] == "trusted-real"
    memory_id = json.loads(written["content"][0]["text"])["memory_id"]
    record = SharedMemoryStore(
        workspace, "trusted-group", read_only=True,
    ).get_record(memory_id)
    assert record is not None
    assert record.agent_instance_id == "trusted-real"
    assert not (
        workspace / ".memoryguard" / "shared-memory" / "attacker-selected-group"
    ).exists()

    status = _handle_memory_status({"workspace": str(workspace)})
    assert not status.get("isError"), status
    mismatch = _handle_memory_status({
        "workspace": str(workspace),
        "agent_instance_id": "impostor",
    })
    assert mismatch.get("isError") is True
    assert "mismatch" in mismatch["content"][0]["text"]

    schemas = {
        tool["name"]: tool["inputSchema"]["properties"]
        for tool in TOOLS
    }
    for name in (
        "memoryguard_memory_read",
        "memoryguard_memory_search",
        "memoryguard_memory_write",
        "memoryguard_memory_update",
        "memoryguard_memory_delete",
        "memoryguard_memory_status",
    ):
        assert "agent_instance_id" in schemas[name]
