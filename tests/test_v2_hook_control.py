from __future__ import annotations

from pathlib import Path

from memoryguard.access_context import AccessContext
from memoryguard.host_hooks import HostHookManager
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort, bind_native_transport_context


def _context(workspace: Path):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id="codex-agent",
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id="hook-control-session",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(workspace.resolve()),
        share_group_id="group-a",
        project_ref=str(workspace.resolve()),
        provider="codex",
        runtime_role="gui",
        entrypoint="gui",
    )


def _port(workspace: Path) -> NativeV2RuntimePort:
    return NativeV2RuntimePort(
        workspace,
        state_provider=lambda: {"state": "V2_ACTIVE", "generation": 11},
    )


def test_native_hook_status_mode_and_uninstall_use_v2_binding(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "control"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(
        "memoryguard.host_hooks._binding_plane_for_workspace",
        lambda _workspace: "v2",
    )

    GroupControlService(workspace, write=True).bind_agent("codex-agent", "group-a")
    HostHookManager(workspace).install(
        "codex",
        agent_instance_id="codex-agent",
        share_group_id="group-a",
    )
    port = _port(workspace)
    context = _context(workspace)

    status = port.dispatch_gui(
        "get_host_hook_status",
        ["codex", "codex-agent"],
        context=context,
        generation=11,
        state="V2_ACTIVE",
    )
    assert status["ok"] is True, status
    assert status["data"]["configured"] is True
    assert status["data"]["agent_instance_id"] == "codex-agent"
    assert "config_file" not in status["data"]

    paused = port.dispatch_gui(
        "set_host_hook_mode",
        ["codex", "codex-agent", "paused", True],
        context=context,
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert paused["ok"] is True, paused
    assert paused["data"]["mode"] == "paused"

    repeated = port.dispatch_gui(
        "set_host_hook_mode",
        ["codex", "codex-agent", "paused", True],
        context=context,
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert repeated["ok"] is True
    assert repeated["data"]["replayed"] is True
    assert repeated["data"]["changed"] is False

    removed = port.dispatch_gui(
        "uninstall_host_hook",
        ["codex", True],
        context=context,
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert removed["ok"] is True, removed
    assert removed["data"]["configured"] is False
    assert HostHookManager(workspace).status(
        "codex", agent_instance_id="codex-agent"
    )["configured"] is False


def test_hook_control_requires_real_v2_binding_and_confirmation(tmp_path: Path) -> None:
    port = _port(tmp_path)
    context = _context(tmp_path)

    denied = port.dispatch_gui(
        "set_host_hook_mode",
        ["codex", "codex-agent", "paused", True],
        context=context,
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert denied["ok"] is False
    assert denied["code"] == "active_binding_required"

    GroupControlService(tmp_path, write=True).bind_agent("codex-agent", "group-a")
    unconfirmed = port.dispatch_gui(
        "uninstall_host_hook",
        ["codex", False],
        context=context,
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert unconfirmed["ok"] is False
    assert unconfirmed["code"] == "hook_confirmation_required"
