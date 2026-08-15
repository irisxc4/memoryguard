"""V2 control-plane seams for cwd-independent runtime entrypoints."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from memoryguard.cli import _cli_workspace, build_parser
from memoryguard.data_home import resolve_runtime_data_home
from memoryguard.mcp_server import _effective_agent_context, _resolve_memory_workspace
from memoryguard.rule_scope import canonical_project_ref
from memoryguard.workspace_resolver import discover_migration_source


def _legacy_project(root: Path) -> Path:
    (root / ".memoryguard" / "shared-memory" / "legacy-group").mkdir(
        parents=True,
    )
    (root / ".memoryguard" / "shared-memory" / "legacy-group" / "memory.db").write_bytes(
        b"legacy-v1"
    )
    (root / ".memoryguard" / "agent-bindings").mkdir(parents=True)
    (root / ".memoryguard" / "agent-bindings" / "binding.json").write_text(
        json.dumps({"binding_id": "legacy-binding", "status": "active"}),
        encoding="utf-8",
    )
    return root


def _v2_home(root: Path) -> Path:
    (root / ".memoryguard" / "system").mkdir(parents=True)
    (root / ".memoryguard" / "system" / "manifest.db").write_bytes(b"v2")
    return root


def test_runtime_control_resolution_ignores_project_v1_and_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _legacy_project(tmp_path / "project")
    data_home = _v2_home(tmp_path / "data-home")
    monkeypatch.chdir(project)
    monkeypatch.setenv("MEMORYGUARD_HOME", str(data_home))
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(project))

    assert resolve_runtime_data_home(project) == data_home.resolve()
    assert _resolve_memory_workspace({}) == data_home.resolve()
    assert _resolve_memory_workspace({"workspace": str(project)}) == data_home.resolve()

    for argv in (("doctor",), ("mcp-status",), ("hooks", "status"), ("desktop",)):
        args = build_parser().parse_args(list(argv))
        assert _cli_workspace(args) == data_home.resolve(), argv


def test_v2_governance_lock_does_not_reclassify_configured_data_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_home = tmp_path / "data-home"
    data_home.mkdir()
    monkeypatch.setenv("MEMORYGUARD_HOME", str(data_home))

    assert resolve_runtime_data_home() == data_home.resolve()
    runtime_root = data_home / ".memoryguard"
    runtime_root.mkdir()
    (runtime_root / "governance.lock").write_bytes(b"\0")

    # V2 creates this lock before every domain has necessarily created the
    # manifest. The control plane must not jump to the default user home just
    # because its own synchronization artifact now exists.
    assert resolve_runtime_data_home() == data_home.resolve()
    assert _resolve_memory_workspace({}) == data_home.resolve()


def test_mcp_workspace_is_project_ref_only_and_never_data_plane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    data_home = _v2_home(tmp_path / "data-home")
    project.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MEMORYGUARD_HOME", str(data_home))
    monkeypatch.delenv("MEMORYGUARD_WORKSPACE", raising=False)
    access = SimpleNamespace(
        session_id="session-1",
        session_trusted=True,
        session_source="test",
    )

    context = _effective_agent_context(
        {"workspace": str(project), "agent_instance_id": "agent-1"},
        "group-1",
        access_context=access,
    )

    assert context.project_ref == canonical_project_ref(str(project))
    assert _resolve_memory_workspace({"workspace": str(project)}) == data_home.resolve()


def test_bare_migration_source_finds_trusted_v1_ancestor_but_skips_v2_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = _legacy_project(tmp_path / "legacy-project")
    project = _v2_home(legacy / "project")
    data_home = _v2_home(tmp_path / "data-home")
    monkeypatch.chdir(project)

    source = discover_migration_source(cwd=project, data_home=data_home)

    assert source == legacy.resolve()


def test_bare_upgrade_routes_to_v1_ancestor_not_ambient_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from memoryguard import cli
    from memoryguard.migration import upgrade

    legacy = _legacy_project(tmp_path / "legacy-project")
    nested = legacy / "project" / "src"
    nested.mkdir(parents=True)
    data_home = tmp_path / "data-home"
    data_home.mkdir()
    ambient = tmp_path / "ambient-project"
    ambient.mkdir()
    monkeypatch.chdir(nested)
    monkeypatch.setenv("MEMORYGUARD_HOME", str(data_home))
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(ambient))

    captured: list[str] = []

    def fake_upgrade_main(argv=None):
        captured.extend(str(item) for item in (argv or []))
        return 0

    monkeypatch.setattr(upgrade, "main", fake_upgrade_main)

    assert cli.main(["upgrade", "--preview"]) == 0
    assert captured[captured.index("--workspace") + 1] == str(data_home.resolve())
    assert captured[captured.index("--data-home") + 1] == str(legacy.resolve())


def test_hook_ensure_keeps_current_v2_binding_and_removes_stale_project_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from memoryguard import host_hooks

    home = tmp_path / "home"
    data_home = _v2_home(tmp_path / "data-home")
    stale_project = _legacy_project(tmp_path / "stale-project")
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(host_hooks, "_validate_binding", lambda *args, **kwargs: None)

    config_path = home / ".codex" / "hooks.json"
    config_path.parent.mkdir(parents=True)
    current = host_hooks._command(
        "codex", "session_start", data_home, "current-agent", "current-group",
        windows=False,
    )
    stale = host_hooks._command(
        "codex", "session_start", stale_project, "stale-agent", "stale-group",
        windows=False,
    )
    config_path.write_text(
        json.dumps({
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": current}]},
                    {"hooks": [{"type": "command", "command": stale}]},
                ],
            },
        }),
        encoding="utf-8",
    )

    result = host_hooks.HostHookManager(data_home).install(
        "codex",
        agent_instance_id="current-agent",
        share_group_id="current-group",
    )
    data = json.loads(config_path.read_text(encoding="utf-8"))
    commands = [
        handler["command"]
        for entries in data["hooks"].values()
        for group in entries
        for handler in group.get("hooks", [])
        if isinstance(handler, dict) and "command" in handler
    ]

    assert result["configured"] is True
    assert commands
    assert all("stale-project" not in command for command in commands)
    assert all("--workspace" in command for command in commands)
    assert sum("--agent-id current-agent" in command for command in commands) == 7
