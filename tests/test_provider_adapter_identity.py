"""Provider MCP 配置的可信 Agent 身份传播回归测试。"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import pytest
from memoryguard import toml_compat as tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.evidence import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2
from memoryguard.memory import MemoryAtom, MemoryAtomStore, MemoryReadScope
from memoryguard.migration.memory import V1GroupReader
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.provider_adapters import (
    ClaudeAdapter,
    CodexAdapter,
    CursorAdapter,
    TraeAdapter,
    repair_global_provider_configs,
)
from memoryguard.schema_v3 import AgentInstance
from memoryguard.storage.layout import WorkspaceV2Layout
from memoryguard.storage.schema import initialize_all
from memoryguard.system.manifest import ManifestManager, ManifestState


def _patch_home(monkeypatch, home: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CODEXROUTER_DATA", raising=False)
    monkeypatch.delenv("CODEX_ROUTER_DATA", raising=False)
    monkeypatch.delenv("CODEXROUTER_HOME", raising=False)
    monkeypatch.delenv("CODEX_ROUTER_HOME", raising=False)


def _activate_v2_workspace(workspace: Path) -> None:
    manager = ManifestManager(workspace)
    if manager.current().state is ManifestState.V2_ACTIVE:
        return
    initialize_all(WorkspaceV2Layout(workspace))
    memory = MemoryAtomStore(workspace)
    evidence = EvidenceStore(workspace)
    GovernanceV2(workspace, memory_store=memory, evidence_store=evidence)
    manager.transition(ManifestState.V2_BUILDING, migration_id="provider-identity-fixture")
    manager.transition(
        ManifestState.V2_READY,
        source_digest="provider-identity-source",
        target_digest="provider-identity-target",
        manifest_digest="provider-identity-manifest",
        digests={"validator_passed": True, "checkpoints": {"provider_identity": True}},
    )
    assert manager.transition(ManifestState.V2_ACTIVE).state is ManifestState.V2_ACTIVE


def _v2_store(workspace: Path) -> GroupControlService:
    _activate_v2_workspace(workspace)
    return GroupControlService(workspace, write=True)


def _v2_bind(workspace: Path, agent_id: str, group_id: str) -> dict:
    return _v2_store(workspace).bind_agent(
        agent_id,
        group_id,
        idempotency_key=f"provider-identity-bind:{agent_id}:{group_id}",
    )


def _v2_bind_agents(workspace: Path, agent_ids: list[str], group_id: str) -> GroupControlService:
    store = _v2_store(workspace)
    store.bind_agents(agent_ids, share_group_id=group_id)
    return store


def _legacy_row(memory_id: str, body: str, **extra) -> tuple:
    values = {
        "memory_id": memory_id,
        "body": body,
        "kind": "fact",
        "status": "active",
        "confidence": 0.9,
        "locked": 0,
        "injection_policy": "relevant",
        "priority": 0,
        "supersedes": "[]",
        "provenance": "[]",
        "agent_instance_id": "old-codex",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "canonical_hash": hashlib.sha256(body.encode()).hexdigest(),
        "dedup_domain": "relevant",
    }
    values.update(extra)
    return tuple(values[key] for key in (
        "memory_id", "body", "kind", "status", "confidence", "locked",
        "injection_policy", "priority", "supersedes", "provenance",
        "agent_instance_id", "created_at", "updated_at", "canonical_hash",
        "dedup_domain",
    ))


def _legacy_group(root: Path, group_id: str, rows: list[tuple]) -> Path:
    path = root / ".memoryguard" / "shared-memory" / group_id / "memory.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE records ("
            "memory_id TEXT PRIMARY KEY, body TEXT, kind TEXT, status TEXT, "
            "confidence REAL, locked INTEGER, injection_policy TEXT, priority INTEGER, "
            "supersedes TEXT, provenance TEXT, agent_instance_id TEXT, created_at TEXT, "
            "updated_at TEXT, canonical_hash TEXT, dedup_domain TEXT)"
        )
        conn.execute(
            "CREATE TABLE decisions (event_id TEXT PRIMARY KEY, actor TEXT, "
            "action TEXT, target_ids TEXT, created_at TEXT)"
        )
        conn.executemany("INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.execute(
            "INSERT INTO decisions VALUES (?,?,?,?,?)",
            (f"decision-{group_id}", "operator", "inventory", "[]", "now"),
        )
    return path


def _seed_v2_atom(
    workspace: Path,
    *,
    memory_id: str,
    body: str,
    agent: str,
    group: str,
    source_ref: str | None = None,
    evidence_authority: str = "system",
) -> None:
    _activate_v2_workspace(workspace)
    memory = MemoryAtomStore(workspace)
    evidence = EvidenceStore(workspace)
    governance = GovernanceV2(workspace, memory_store=memory, evidence_store=evidence)
    workspace_id = str(workspace.resolve())
    scope = {
        "workspace_id": workspace_id,
        "share_group_id": group,
        "agent_instance_id": agent,
        "project_ref": workspace_id.casefold(),
        "provider": "codex",
        "runtime_role": "root",
        "actor": "provider-identity-fixture",
        "authority": "manual",
    }
    atom = MemoryAtom(
        memory_id=memory_id,
        body=body,
        kind="fact",
        status="active",
        confidence=0.9,
        workspace_id=workspace_id,
        share_group_id=group,
        agent_instance_id=agent,
        project_ref=workspace_id.casefold(),
        provider="codex",
        runtime_role="root",
    )
    persisted, _ = governance.put_atom(
        atom,
        context=scope,
        evidence=[{
            "source_ref": source_ref or f"fixture:{memory_id}",
            "authority": evidence_authority,
        }],
        reason="provider identity V2 fixture",
        confidence=0.9,
        idempotency_key=f"provider-identity-memory:{memory_id}",
    )
    memory.project_evidence(evidence)
    memory.set_visibility("active", atom_ids=[persisted.atom_id])


@pytest.fixture(autouse=True)
def _v2_provider_binding_plane(monkeypatch: pytest.MonkeyPatch) -> None:
    """This module exercises the V2 provider control plane explicitly."""
    monkeypatch.setattr(
        "memoryguard.provider_adapters._binding_plane_for_workspace",
        lambda _workspace: "v2",
    )
    monkeypatch.setattr(
        "memoryguard.host_hooks._binding_plane_for_workspace",
        lambda _workspace: "v2",
    )


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

    binding = _v2_bind(workspace, "claude-real-7", "team-json")
    store = _v2_store(workspace)
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
        "MEMORYGUARD_PROVIDER": "claude",
        "MEMORYGUARD_CONTROL_SCOPE": "project",
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
    assert "injection_policy" in instruction_text
    assert "不得把所有 procedure 自动设为强制" in instruction_text
    assert "强制包敏感或超限会失败封闭" in instruction_text
    assert "`memoryguard_memory_search`：按 query、status、limit 搜索 governed memories" in instruction_text
    assert "不支持 kind 或 semantic 过滤" in instruction_text
    assert "`memoryguard_memory_update`：更新已知记忆的 body / kind / injection_policy / priority / audience" in instruction_text
    assert "不支持 status" in instruction_text
    assert "不要用 `memoryguard_memory_update` 恢复 deleted 记录" in instruction_text
    assert "按 query / kind / status 搜索" not in instruction_text
    assert "更新 body / kind / status" not in instruction_text
    assert 'query="用户 长期 偏好"' not in instruction_text
    assert first["binding_id"] == binding["binding_id"]
    assert second["binding_id"] == binding["binding_id"]

    bindings = store.list_bindings(include_inactive=True)["bindings"]
    assert [item["agent_instance_id"] for item in bindings] == ["claude-real-7"]
    assert store.active_binding_for_agent("claude") is None
    assert adapter.status()["installed"] is True
    adapter.uninstall()
    assert adapter.status()["installed"] is False
    assert json.loads(config_path.read_text(encoding="utf-8")) == {
        "mcpServers": {"other": {"command": "other-server"}},
        "userSetting": {"keep": True},
    }
    assert "# User instructions" in instruction_path.read_text(encoding="utf-8")
    assert store.active_binding_for_agent("claude-real-7")["status"] == "active"


def test_claude_global_scope_uses_user_config_and_stable_workspace(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    _patch_home(monkeypatch, home)
    monkeypatch.setenv("MEMORYGUARD_HOME", str(workspace))
    _v2_bind(workspace, "claude-global", "team-global")

    result = ClaudeAdapter(workspace).install(
        workspace,
        share_group_id="team-global",
        agent_instance_id="claude-global",
        global_scope=True,
    )

    config = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    assert config["mcpServers"]["memoryguard"]["env"] == {
        "MEMORYGUARD_AGENT_ID": "claude-global",
        "MEMORYGUARD_PROVIDER": "claude",
        "MEMORYGUARD_CONTROL_SCOPE": "global",
        "MEMORYGUARD_WORKSPACE": str(workspace.resolve()),
        "MEMORYGUARD_HOME": str(workspace.resolve()),
        "MEMORYGUARD_ADMIN": "1",
        "MEMORYGUARD_SESSION_ID": "provider-claude-claude-global",
        "MEMORYGUARD_SESSION_SOURCE": "host",
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

    binding = _v2_bind(workspace, "codex-real-9", "team-toml")
    store = _v2_store(workspace)
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
        "MEMORYGUARD_PROVIDER": "codex",
        "MEMORYGUARD_CONTROL_SCOPE": "project",
        "MEMORYGUARD_WORKSPACE": str(workspace.resolve()),
    }
    assert parsed["mcp_servers"]["other"]["command"] == "other-server"
    assert parsed["features"]["keep"] is True
    assert tomllib.loads(
        global_config_path.read_text(encoding="utf-8")
    )["mcp_servers"]["memoryguard"]["command"] == "legacy-global"
    assert any("旧用户级" in warning for warning in result["warnings"])
    assert config_path.read_text(encoding="utf-8") == first_config
    assert result["binding_id"] == binding["binding_id"]
    assert [item["agent_instance_id"] for item in store.list_bindings()["bindings"]] == [
        "codex-real-9",
    ]

    assert adapter.status()["installed"] is True
    adapter.uninstall()
    assert adapter.status()["installed"] is False
    remaining = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert remaining["mcp_servers"]["other"]["command"] == "other-server"
    assert remaining["features"]["keep"] is True
    assert store.active_binding_for_agent("codex-real-9")["status"] == "active"


def test_codex_global_install_migrates_unmarked_legacy_section(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    _patch_home(monkeypatch, home)
    monkeypatch.setenv("MEMORYGUARD_HOME", str(workspace))
    _v2_bind(workspace, "codex-legacy", "team-legacy")

    config_path = home / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        '[mcp_servers.other]\ncommand = "keep-me"\n\n'
        '[mcp_servers.memoryguard]\n'
        'enabled = true\n'
        'command = "python"\n'
        'args = ["-m", "memoryguard.mcp_server"]\n'
        '[mcp_servers.memoryguard.env]\n'
        'MEMORYGUARD_AGENT_ID = "old-agent"\n\n'
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
        "MEMORYGUARD_PROVIDER": "codex",
        "MEMORYGUARD_CONTROL_SCOPE": "global",
        "MEMORYGUARD_WORKSPACE": str(workspace.resolve()),
        "MEMORYGUARD_HOME": str(workspace.resolve()),
        "MEMORYGUARD_ADMIN": "1",
        "MEMORYGUARD_SESSION_ID": "provider-codex-codex-legacy",
        "MEMORYGUARD_SESSION_SOURCE": "host",
    }
    assert parsed["mcp_servers"]["other"]["command"] == "keep-me"
    assert parsed["features"]["keep"] is True
    assert "# preserve this unrelated comment" in first_text
    assert first_text.count("[mcp_servers.memoryguard]") == 1
    assert first_text.count("# BEGIN memoryguard:provider-redirect") == 1
    assert config_path.read_text(encoding="utf-8") == first_text


def test_repair_global_provider_configs_rebuilds_from_canonical_binding(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    data_home = tmp_path / "data-home"
    home.mkdir()
    data_home.mkdir()
    _patch_home(monkeypatch, home)
    monkeypatch.setenv("MEMORYGUARD_HOME", str(data_home))
    _v2_bind(data_home, "codex-current", "canonical-group")

    monkeypatch.setattr(
        "memoryguard.agent_locator.AgentLocator.detect_instances",
        lambda self: ([AgentInstance("codex-current", "codex", "codex")], {}),
    )
    result = repair_global_provider_configs(["codex"])

    assert result["ok"] is True
    assert result["configured"] == 1
    config = tomllib.loads(
        (home / ".codex" / "config.toml").read_text(encoding="utf-8")
    )
    assert config["mcp_servers"]["memoryguard"]["env"] == {
        "MEMORYGUARD_AGENT_ID": "codex-current",
        "MEMORYGUARD_PROVIDER": "codex",
        "MEMORYGUARD_CONTROL_SCOPE": "global",
        "MEMORYGUARD_WORKSPACE": str(data_home.resolve()),
        "MEMORYGUARD_HOME": str(data_home.resolve()),
        "MEMORYGUARD_ADMIN": "1",
        "MEMORYGUARD_SESSION_ID": "provider-codex-codex-current",
        "MEMORYGUARD_SESSION_SOURCE": "host",
    }


def test_codex_global_takeover_removes_superseded_project_override(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    data_home = tmp_path / "data-home"
    project = tmp_path / "project"
    home.mkdir()
    data_home.mkdir()
    project.mkdir()
    _patch_home(monkeypatch, home)
    monkeypatch.setenv("MEMORYGUARD_HOME", str(data_home))
    # A project-initiated global takeover is authorized by the V2 control
    # workspace that initiated it; the adapter then writes the stable data-home
    # path into the user-level configuration.
    binding = _v2_bind(project, "codex-current", "canonical-group")

    project_config = project / ".codex" / "config.toml"
    project_config.parent.mkdir(parents=True)
    project_config.write_text(
        '# BEGIN memoryguard:provider-redirect\n'
        '[mcp_servers.memoryguard]\n'
        'command = "python"\n'
        'args = ["-m", "memoryguard.mcp_server"]\n'
        'env = { MEMORYGUARD_AGENT_ID = "old-codex" }\n'
        '# END memoryguard:provider-redirect\n',
        encoding="utf-8",
    )
    (project / "AGENTS.md").write_text(
        '<!-- BEGIN memoryguard:provider-redirect -->\nold\n'
        '<!-- END memoryguard:provider-redirect -->\n',
        encoding="utf-8",
    )

    result = CodexAdapter(project).install(
        project,
        share_group_id="canonical-group",
        agent_instance_id="codex-current",
        global_scope=True,
    )

    global_text = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
    parsed = tomllib.loads(global_text)
    assert parsed["mcp_servers"]["memoryguard"]["env"] == {
        "MEMORYGUARD_AGENT_ID": "codex-current",
        "MEMORYGUARD_PROVIDER": "codex",
        "MEMORYGUARD_CONTROL_SCOPE": "global",
        "MEMORYGUARD_WORKSPACE": str(data_home.resolve()),
        "MEMORYGUARD_HOME": str(data_home.resolve()),
        "MEMORYGUARD_ADMIN": "1",
        "MEMORYGUARD_SESSION_ID": "provider-codex-codex-current",
        "MEMORYGUARD_SESSION_SOURCE": "host",
    }
    assert not project_config.exists()
    assert not (project / "AGENTS.md").exists()
    assert any("项目级 MemoryGuard 覆盖" in item for item in result["warnings"])

    status = CodexAdapter(project).status()
    assert status["configured"] is True
    assert status["installed"] is True
    assert status["status"] == "configured"
    assert status["configured_agent_instance_id"] == "codex-current"
    assert status["binding_id"] == binding["binding_id"]
    assert status["binding_status"] == "active"
    assert status["runtime_verified"] is False
    assert status["mcp_configured"] is True
    assert status["instruction_file"] == str((home / ".codex" / "AGENTS.md").resolve())


def _write_codex_managed_home(home: Path, agent_id: str) -> None:
    config = home / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        "# BEGIN memoryguard:provider-redirect\n"
        "[mcp_servers.memoryguard]\n"
        'command = "python"\n'
        'args = ["-m", "memoryguard.mcp_server"]\n'
        f'env = {{ MEMORYGUARD_AGENT_ID = "{agent_id}" }}\n'
        "# END memoryguard:provider-redirect\n",
        encoding="utf-8",
    )
    (home / "AGENTS.md").write_text(
        "<!-- BEGIN memoryguard:provider-redirect -->\n"
        "global\n"
        "<!-- END memoryguard:provider-redirect -->\n",
        encoding="utf-8",
    )


def test_codex_status_falls_back_to_current_home_after_cleaned_override(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    _patch_home(monkeypatch, home)
    binding = _v2_bind(workspace, "codex-global", "team-status")

    leftover = workspace / ".codex" / "config.toml"
    leftover.parent.mkdir(parents=True)
    leftover.write_text(
        '[mcp_servers.other]\ncommand = "keep-me"\n',
        encoding="utf-8",
    )
    (workspace / "AGENTS.md").write_text("# project agents\n", encoding="utf-8")
    _write_codex_managed_home(home / ".codex", "codex-global")

    status = CodexAdapter(workspace).status()
    assert status["configured"] is True
    assert status["status"] == "configured"
    assert status["configured_agent_instance_id"] == "codex-global"
    assert status["binding_id"] == binding["binding_id"]
    assert status["binding_status"] == "active"
    assert status["runtime_verified"] is False
    assert status["instruction_file"] == str((home / ".codex" / "AGENTS.md").resolve())


def test_codex_status_prefers_valid_project_block_over_global(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    _patch_home(monkeypatch, home)
    binding = _v2_bind(workspace, "codex-project", "team-status")
    _write_codex_managed_home(workspace / ".codex", "codex-project")
    (workspace / "AGENTS.md").write_text(
        "<!-- BEGIN memoryguard:provider-redirect -->\n"
        "project\n"
        "<!-- END memoryguard:provider-redirect -->\n",
        encoding="utf-8",
    )
    _write_codex_managed_home(home / ".codex", "codex-global")

    status = CodexAdapter(workspace).status()
    assert status["configured"] is True
    assert status["configured_agent_instance_id"] == "codex-project"
    assert status["binding_id"] == binding["binding_id"]
    assert status["runtime_verified"] is False
    assert status["instruction_file"] == str((workspace / "AGENTS.md").resolve())


@pytest.mark.parametrize(
    "project_toml",
    [
        (
            "# BEGIN memoryguard:provider-redirect\n"
            "[mcp_servers.memoryguard]\n"
            "command = 'python'\n"
        ),
        (
            "# BEGIN memoryguard:provider-redirect\n"
            "[mcp_servers.memoryguard]\n"
            "command =\n"
            "# END memoryguard:provider-redirect\n"
        ),
        (
            "[mcp_servers.memoryguard]\n"
            "command = 'python'\n"
            "args = ['-m', 'memoryguard.mcp_server']\n"
            "env = { MEMORYGUARD_AGENT_ID = 'stale' }\n"
        ),
        (
            "# BEGIN memoryguard:provider-redirect\n"
            "[mcp_servers.memoryguard]\n"
            "command = 'python'\n"
            "args = ['-m', 'memoryguard.mcp_server']\n"
            "# END memoryguard:provider-redirect\n"
        ),
    ],
    ids=["partial-no-end", "malformed-toml", "unmarked-leftover", "markers-without-agent"],
)
def test_codex_status_does_not_fallback_past_broken_project_block(
    tmp_path, monkeypatch, project_toml,
):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    _patch_home(monkeypatch, home)
    _v2_bind(workspace, "codex-global", "team-status")
    project_config = workspace / ".codex" / "config.toml"
    project_config.parent.mkdir(parents=True)
    project_config.write_text(project_toml, encoding="utf-8")
    (workspace / "AGENTS.md").write_text(
        "<!-- BEGIN memoryguard:provider-redirect -->\n"
        "project\n"
        "<!-- END memoryguard:provider-redirect -->\n",
        encoding="utf-8",
    )
    _write_codex_managed_home(home / ".codex", "codex-global")

    status = CodexAdapter(workspace).status()
    assert status["configured"] is False
    assert status["installed"] is False
    assert status["status"] == "not_configured"
    assert status["mcp_configured"] is False
    assert status["configured_agent_instance_id"] == ""
    assert status["runtime_verified"] is False


def test_governance_configures_trae_and_reports_other_adapter_errors(
    tmp_path, monkeypatch,
):
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    _patch_home(monkeypatch, home)
    monkeypatch.setenv("MEMORYGUARD_HOME", str(workspace))
    monkeypatch.setenv("MEMORYGUARD_ADMIN", "1")
    appdata = home / "AppData" / "Roaming"
    monkeypatch.setenv("APPDATA", str(appdata))

    store = _v2_bind_agents(
        workspace,
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

    result = store.install_redirects("team-mixed")

    # V2 treats partial host installation as a failed transaction boundary and
    # rolls back providers newly configured by this attempt.
    assert result["ok"] is False
    assert result["status"] == "failure"
    assert result["installed_count"] == 0
    assert result["skipped_count"] == 0
    assert result["error_count"] == 1
    by_agent = {item["agent_instance_id"]: item for item in result["installed"]}
    assert by_agent["codex-real"]["status"] == "configured"
    assert by_agent["claude-error"]["status"] == "error"
    assert by_agent["claude-error"]["error"] == "RuntimeError"
    assert by_agent["trae-real"]["status"] == "configured"
    assert by_agent["trae-real"]["provider"] == "trae"
    # The attempt is atomic from the group command perspective: successful
    # providers configured earlier in the batch are uninstalled again.
    trae_global_config = appdata / "TRAE SOLO CN" / "User" / "mcp.json"
    if trae_global_config.exists():
        assert "memoryguard" not in json.loads(
            trae_global_config.read_text(encoding="utf-8")
        ).get("mcpServers", {})
    codex_config = home / ".codex" / "config.toml"
    if codex_config.exists():
        assert "memoryguard" not in tomllib.loads(
            codex_config.read_text(encoding="utf-8")
        ).get("mcp_servers", {})
    assert {
        binding["agent_instance_id"]
        for binding in store.list_bindings(include_inactive=True)["bindings"]
    } == {"codex-real", "claude-error", "trae-real"}
    assert store.active_binding_for_agent("codex") is None


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
    binding = _v2_bind(workspace, "cursor-real", "team-cursor")

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
        "MEMORYGUARD_PROVIDER": "cursor",
        "MEMORYGUARD_CONTROL_SCOPE": "project",
        "MEMORYGUARD_WORKSPACE": str(workspace.resolve()),
    }
    assert json.loads(global_config.read_text(encoding="utf-8")) == {
        "mcpServers": {"global-only": {"command": "global"}},
    }
    assert result["binding_id"] == binding["binding_id"]
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
    binding = _v2_bind(workspace, "trae-real", "team-trae")

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
        "MEMORYGUARD_PROVIDER": "trae",
        "MEMORYGUARD_CONTROL_SCOPE": "project",
        "MEMORYGUARD_WORKSPACE": str(workspace.resolve()),
    }
    assert parsed["mcpServers"]["existing"] == {"command": "existing-server"}
    assert parsed["projectSetting"] == {"keep": True}
    assert project_config.read_text(encoding="utf-8") == first_config
    assert list(parsed["mcpServers"]).count("memoryguard") == 1
    assert (workspace / "AGENTS.md").read_text(encoding="utf-8").count(
        "<!-- BEGIN memoryguard:provider-redirect -->"
    ) == 1
    assert first["binding_id"] == binding["binding_id"]
    assert second["binding_id"] == binding["binding_id"]
    assert adapter.status()["installed"] is True

    adapter.uninstall()
    remaining = json.loads(project_config.read_text(encoding="utf-8"))
    assert remaining == {
        "mcpServers": {"existing": {"command": "existing-server"}},
        "projectSetting": {"keep": True},
    }
    assert adapter.status()["installed"] is False
    assert GroupControlService(workspace).active_binding_for_agent("trae-real")["status"] == "active"


def test_reinstall_is_idempotent_and_shared_agents_rule_survives_single_uninstall(
    tmp_path, monkeypatch,
):
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    _patch_home(monkeypatch, home)
    monkeypatch.setenv("MEMORYGUARD_HOME", str(workspace))
    monkeypatch.setenv("MEMORYGUARD_ADMIN", "1")
    appdata = home / "AppData" / "Roaming"
    monkeypatch.setenv("APPDATA", str(appdata))
    store = _v2_bind_agents(workspace, ["codex-real", "trae-real"], "team-shared")
    monkeypatch.setattr(
        "memoryguard.agent_locator.AgentLocator.detect_instances",
        lambda self: ([
            AgentInstance("codex-real", "codex", "codex"),
            AgentInstance("trae-real", "trae", "trae"),
        ], {}),
    )

    first = store.install_redirects("team-shared")
    second = store.install_redirects("team-shared")

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
    _v2_bind(workspace, "claude-real", "team-rollback")

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
    _v2_bind(workspace, "claude-real", "team-corrupt")

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


def test_legacy_global_runtime_redirect_requires_migration_evidence(
    tmp_path, monkeypatch,
):
    from memoryguard.mcp_server import _resolve_memory_workspace

    legacy = tmp_path / "legacy-control"
    canonical = tmp_path / "canonical-home"
    legacy.mkdir()
    canonical.mkdir()
    legacy_db = _legacy_group(
        legacy,
        "legacy-group",
        [_legacy_row("migrated-record", "migrated memory")],
    )
    _v2_bind(canonical, "new-codex", "canonical-group")
    _seed_v2_atom(
        canonical,
        memory_id="migrated-record",
        body="migrated memory",
        agent="new-codex",
        group="canonical-group",
        source_ref="migration:legacy-group/migrated-record",
        evidence_authority="memory_migration",
    )

    # The formal immutable reader is the only V1 inspection seam.  Its result
    # is evidence for the explicit upgrade, not permission for runtime redirect.
    inventory = V1GroupReader(
        legacy,
        "legacy-group",
        legacy_db,
        immutable=True,
    ).inventory()
    assert inventory.ok and inventory.records == 1 and inventory.active == 1

    monkeypatch.setenv("MEMORYGUARD_HOME", str(canonical))
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(legacy))
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "old-codex")
    monkeypatch.delenv("MEMORYGUARD_CONTROL_SCOPE", raising=False)
    # A legacy project remains migration evidence only; runtime control stays
    # on the canonical user-level V2 data home even without an explicit scope.
    assert _resolve_memory_workspace({}) == canonical.resolve()

    monkeypatch.setenv("MEMORYGUARD_CONTROL_SCOPE", "global")
    assert _resolve_memory_workspace({}) == canonical.resolve()
    scope = MemoryReadScope(
        workspace_id=str(canonical.resolve()),
        share_group_id="canonical-group",
        agent_instance_id="new-codex",
        project_ref=str(canonical.resolve()).casefold(),
        provider="codex",
        runtime_role="root",
        admin=True,
    )
    atoms = MemoryAtomStore(canonical).list_atoms(scope=scope, include_building=True)
    assert [atom.memory_id for atom in atoms] == ["migrated-record"]

    # Without an explicit global scope, a legacy cwd remains a migration
    # source hint; it never becomes the runtime control plane.
    monkeypatch.delenv("MEMORYGUARD_CONTROL_SCOPE", raising=False)
    monkeypatch.delenv("MEMORYGUARD_WORKSPACE", raising=False)
    monkeypatch.chdir(legacy)
    assert _resolve_memory_workspace({}) == canonical.resolve()


def test_explicit_project_scope_never_auto_redirects_legacy_workspace(
    tmp_path, monkeypatch,
):
    from memoryguard.mcp_server import _resolve_memory_workspace

    project = tmp_path / "project-control"
    canonical = tmp_path / "canonical-home"
    project.mkdir()
    canonical.mkdir()
    monkeypatch.setenv("MEMORYGUARD_HOME", str(canonical))
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(project))
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "project-agent")
    monkeypatch.setenv("MEMORYGUARD_CONTROL_SCOPE", "project")

    assert _resolve_memory_workspace({}) == project.resolve()
    assert os.environ["MEMORYGUARD_AGENT_ID"] == "project-agent"


def test_trusted_env_supplies_missing_tool_identity_and_rejects_mismatch(
    tmp_path, monkeypatch,
):
    from memoryguard.mcp_server import TOOLS
    from memoryguard.runtime_v2.native_ports import (
        NativeV2RuntimePort,
        bind_native_transport_context,
    )
    from memoryguard.access_context import AccessContext

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _v2_bind(workspace, "trusted-real", "trusted-group")
    context = bind_native_transport_context(
        AccessContext(
            trusted_agent_id="trusted-real",
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id="provider-identity-session",
            session_source="provider-test",
            session_trusted=True,
        ),
        workspace_id=str(workspace.resolve()),
        share_group_id="trusted-group",
        project_ref=str(workspace.resolve()).casefold(),
        provider="codex",
        runtime_role="root",
        entrypoint="mcp",
    )
    port = NativeV2RuntimePort(
        workspace,
        state_provider=lambda: {"state": "V2_ACTIVE", "generation": 1},
    )

    write_args = {
        "memory_id": "trusted-memory",
        "body": "trusted environment identity",
        "share_group_id": "attacker-selected-group",
        "agent_instance_id": "attacker-selected-agent",
        "idempotency_key": "trusted-memory-write",
    }
    written = port.dispatch_mcp(
        "memoryguard_memory_write",
        write_args,
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )
    assert written["ok"] is False
    assert written["code"] == "context_identity_spoof"

    trusted_args = {
        "memory_id": "trusted-memory",
        "body": "trusted environment identity",
        "idempotency_key": "trusted-memory-write",
    }
    written = port.dispatch_mcp(
        "memoryguard_memory_write",
        trusted_args,
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )
    assert written["ok"] is True, written
    assert written["data"]["atom"]["agent_instance_id"] == "trusted-real"
    assert written["data"]["atom"]["share_group_id"] == "trusted-group"

    status = port.dispatch_mcp(
        "memoryguard_memory_status",
        {},
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )
    assert status["ok"] is True, status
    mismatch = port.dispatch_mcp(
        "memoryguard_memory_status",
        {"agent_instance_id": "impostor"},
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )
    assert mismatch["ok"] is False
    assert mismatch["code"] == "context_identity_spoof"

    def identity_properties(schema):
        if not isinstance(schema, dict):
            return {}
        properties = schema.get("properties")
        merged = dict(properties) if isinstance(properties, dict) else {}
        for branch_name in ("oneOf", "anyOf", "allOf"):
            branches = schema.get(branch_name)
            if isinstance(branches, list):
                for branch in branches:
                    merged.update(identity_properties(branch))
        return merged

    target_names = {
        "memoryguard_memory_write",
        "memoryguard_memory_status",
    }
    schemas = {
        tool.get("name"): identity_properties(tool.get("inputSchema"))
        for tool in TOOLS
        if isinstance(tool, dict) and tool.get("name") in target_names
    }
    assert set(schemas) == target_names
    for properties in schemas.values():
        # Group identity is transport-only.  An optional agent field is allowed
        # only as a consistency check whose description makes the trusted
        # environment authoritative; the native mismatch assertions above are
        # the enforcement proof.
        assert "share_group_id" not in properties
        if "agent_instance_id" in properties:
            description = str(properties["agent_instance_id"].get("description", ""))
            assert "trusted" in description.casefold()
            assert "authoritative" in description.casefold()
