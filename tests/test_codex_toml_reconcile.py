import sys

from memoryguard import toml_compat as tomllib
import pytest

from memoryguard.provider_adapters import CodexAdapter
from memoryguard.runtime_v2.group_native import GroupControlService


@pytest.fixture(autouse=True)
def _v2_provider_plane(monkeypatch):
    monkeypatch.setattr(
        "memoryguard.provider_adapters._binding_plane_for_workspace",
        lambda _workspace: "v2",
    )


def test_codex_install_reconciles_owned_duplicate_and_orphan_marker(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    GroupControlService(workspace, write=True).bind_agent("codex-owned", "toml-group")
    config = workspace / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text(
        "[mcp_servers]\n\n"
        "[mcp_servers.other]\ncommand = 'keep'\n\n"
        "[mcp_servers.memoryguard]\ncommand = 'python'\nargs = ['-m', 'memoryguard.mcp_server']\n"
        "[mcp_servers.memoryguard.env]\nMEMORYGUARD_AGENT_ID = 'stale-a'\n\n"
        "[mcp_servers.memoryguard]\ncommand = 'python'\nargs = ['-m', 'memoryguard.mcp_server']\n"
        "[mcp_servers.memoryguard.env]\nMEMORYGUARD_AGENT_ID = 'stale-b'\n\n"
        "# END memoryguard:provider-redirect\n",
        encoding="utf-8",
    )
    adapter = CodexAdapter(workspace)
    adapter.install(workspace, share_group_id="toml-group", agent_instance_id="codex-owned")
    adapter.install(workspace, share_group_id="toml-group", agent_instance_id="codex-owned")

    text = config.read_text(encoding="utf-8")
    parsed = tomllib.loads(text)
    assert parsed["mcp_servers"]["other"]["command"] == "keep"
    assert parsed["mcp_servers"]["memoryguard"]["command"] == sys.executable
    assert text.count("[mcp_servers.memoryguard]") == 1
    assert text.count("[mcp_servers.memoryguard.env]") == 0
    assert text.count("# BEGIN memoryguard:provider-redirect") == 1
    assert text.count("# END memoryguard:provider-redirect") == 1


def test_codex_install_fails_closed_for_unknown_memoryguard_table(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    GroupControlService(workspace, write=True).bind_agent("codex-unknown", "toml-group")
    config = workspace / ".codex" / "config.toml"
    config.parent.mkdir()
    original = "[mcp_servers.memoryguard]\ncommand = 'someone-else'\n"
    config.write_text(original, encoding="utf-8")

    adapter = CodexAdapter(workspace)
    try:
        adapter.install(workspace, share_group_id="toml-group", agent_instance_id="codex-unknown")
    except ValueError as exc:
        assert "cannot safely reconcile" in str(exc)
    else:
        raise AssertionError("unknown same-named MCP table must fail closed")
    assert config.read_text(encoding="utf-8") == original


def test_codex_install_refuses_non_memoryguard_toml_error_without_writing(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    GroupControlService(workspace, write=True).bind_agent("codex-invalid", "toml-group")
    config = workspace / ".codex" / "config.toml"
    config.parent.mkdir()
    original = (
        "[mcp_servers.other]\n"
        "command =\n\n"
        "[mcp_servers.memoryguard]\n"
        "command = 'python'\n"
        "args = ['-m', 'memoryguard.mcp_server']\n"
    )
    config.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="invalid TOML config"):
        CodexAdapter(workspace).install(
            workspace,
            share_group_id="toml-group",
            agent_instance_id="codex-invalid",
        )
    assert config.read_text(encoding="utf-8") == original


def test_codex_install_removes_owned_inline_server_before_table_upsert(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    GroupControlService(workspace, write=True).bind_agent("codex-inline", "toml-group")
    config = workspace / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text(
        "[mcp_servers]\n"
        "other = { command = 'keep' }\n"
        "memoryguard = { command = 'python', args = ['-m', 'memoryguard.mcp_server'] }\n\n"
        "[mcp_servers.memoryguard]\n"
        "command = 'python'\n"
        "args = ['-m', 'memoryguard.mcp_server']\n",
        encoding="utf-8",
    )

    CodexAdapter(workspace).install(
        workspace,
        share_group_id="toml-group",
        agent_instance_id="codex-inline",
    )

    text = config.read_text(encoding="utf-8")
    parsed = tomllib.loads(text)
    assert parsed["mcp_servers"]["other"]["command"] == "keep"
    assert parsed["mcp_servers"]["memoryguard"]["command"] == sys.executable
    assert "memoryguard = {" not in text
    assert text.count("[mcp_servers.memoryguard]") == 1


def test_codex_install_removes_owned_root_inline_server_before_table_upsert(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    GroupControlService(workspace, write=True).bind_agent("codex-root-inline", "toml-group")
    config = workspace / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text(
        "mcp_servers = { other = { command = 'keep' }, memoryguard = { command = 'python', args = ['-m', 'memoryguard.mcp_server'] } }\n\n"
        "[mcp_servers.memoryguard]\n"
        "command = 'python'\n"
        "args = ['-m', 'memoryguard.mcp_server']\n",
        encoding="utf-8",
    )

    CodexAdapter(workspace).install(
        workspace,
        share_group_id="toml-group",
        agent_instance_id="codex-root-inline",
    )

    text = config.read_text(encoding="utf-8")
    parsed = tomllib.loads(text)
    assert parsed["mcp_servers"]["other"]["command"] == "keep"
    assert parsed["mcp_servers"]["memoryguard"]["command"] == sys.executable
    assert '"memoryguard" =' not in text
    assert text.count("[mcp_servers.memoryguard]") == 1
