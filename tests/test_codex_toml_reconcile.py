from memoryguard import toml_compat as tomllib

from memoryguard.agent_binding import AgentBindingStore
from memoryguard.provider_adapters import CodexAdapter


def test_codex_install_reconciles_owned_duplicate_and_orphan_marker(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    AgentBindingStore(workspace).bind_agent("codex-owned", "toml-group")
    config = workspace / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text(
        "[mcp_servers]\n\n"
        "[mcp_servers.other]\ncommand = 'keep'\n\n"
        "[mcp_servers.memoryguard]\ncommand = 'python'\nargs = ['-m', 'memoryguard.mcp_server']\n\n"
        "[mcp_servers.memoryguard]\ncommand = 'python'\nargs = ['-m', 'memoryguard.mcp_server']\n\n"
        "# END memoryguard:provider-redirect\n",
        encoding="utf-8",
    )
    adapter = CodexAdapter(workspace)
    adapter.install(workspace, share_group_id="toml-group", agent_instance_id="codex-owned")
    adapter.install(workspace, share_group_id="toml-group", agent_instance_id="codex-owned")

    text = config.read_text(encoding="utf-8")
    parsed = tomllib.loads(text)
    assert parsed["mcp_servers"]["other"]["command"] == "keep"
    assert parsed["mcp_servers"]["memoryguard"]["command"] == "python"
    assert text.count("[mcp_servers.memoryguard]") == 1
    assert text.count("# BEGIN memoryguard:provider-redirect") == 1
    assert text.count("# END memoryguard:provider-redirect") == 1


def test_codex_install_fails_closed_for_unknown_memoryguard_table(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    AgentBindingStore(workspace).bind_agent("codex-unknown", "toml-group")
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
