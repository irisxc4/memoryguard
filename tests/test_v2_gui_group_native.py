from __future__ import annotations

from pathlib import Path

from memoryguard.access_context import AccessContext
from memoryguard.runtime_v2.group_native import GroupControlService, personal_group_id
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort, bind_native_transport_context


def _context(workspace: Path, *, group: str = "bootstrap"):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id="agent-a",
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id="group-native-session",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(workspace.resolve()),
        share_group_id=group,
        project_ref=str(workspace.resolve()),
        provider="gui",
        runtime_role="gui",
        entrypoint="gui",
        namespace_id="knowledge-group-native",
        sensitivity="normal",
        policy_class="private",
    )


def _port(workspace: Path) -> NativeV2RuntimePort:
    return NativeV2RuntimePort(
        workspace,
        state_provider=lambda: {"state": "V2_ACTIVE", "generation": 11},
    )


def test_native_group_bind_scope_personal_and_dissolve(tmp_path: Path) -> None:
    port = _port(tmp_path)
    context = _context(tmp_path)

    first = port.dispatch_gui(
        "bind_agents_to_shared_group",
        [["agent-a", "agent-b"], "shared-team", "memoryguard", {}, {}, False],
        context=context,
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert first["ok"] is True, first
    assert first["data"]["share_group_id"] == "shared-team"
    assert first["data"]["member_count"] == 2

    repeated = port.dispatch_gui(
        "bind_agents_to_shared_group",
        [["agent-a", "agent-b"], "shared-team", "memoryguard", {}, {}, False],
        context=context,
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert repeated["ok"] is True
    assert repeated["data"]["changed"] is False
    assert repeated["data"]["replayed"] is True

    bindings = port.dispatch_gui(
        "list_bindings", [False], context=context, generation=11, state="V2_ACTIVE"
    )
    assert bindings["data"]["total"] == 2

    selected = port.dispatch_gui(
        "set_governance_scope",
        [{"mode": "share_group", "share_group_id": "shared-team"}],
        context=context,
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert selected["ok"] is True, selected
    assert selected["data"]["scope"]["share_group_id"] == "shared-team"

    scope = port.dispatch_gui(
        "get_governance_scope_state", [], context=context, generation=11, state="V2_ACTIVE"
    )
    assert scope["data"]["scope"]["share_group_id"] == "shared-team"
    assert scope["data"]["principal_agent_instance_id"] == "agent-a"

    personal = port.dispatch_gui(
        "leave_shared_group_to_personal",
        ["agent-a", True],
        context=context,
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert personal["ok"] is True
    assert personal["data"]["share_group_id"] == personal_group_id("agent-a")

    preview = port.dispatch_gui(
        "get_shared_group_preview", ["shared-team"],
        context=context, generation=11, state="V2_ACTIVE",
    )
    assert preview["data"]["members"] == ["agent-b"]

    dissolved = port.dispatch_gui(
        "dissolve_shared_group", ["shared-team", True, True],
        context=context,
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert dissolved["ok"] is True
    assert dissolved["data"]["unbound_count"] == 1
    assert GroupControlService(tmp_path).group_preview("shared-team")["member_count"] == 0


def test_safe_bridge_uses_v2_control_binding_for_trusted_context(tmp_path: Path) -> None:
    from memoryguard.gui import SafeBridgeApi
    from memoryguard.runtime_v2.native_ports import resolve_native_transport_context

    GroupControlService(tmp_path, write=True).bind_agent("agent-a", "shared-team")
    access = AccessContext(
        trusted_agent_id="agent-a",
        is_admin=True,
        strict_binding=True,
        allow_anon=False,
        session_id="safe-bridge-session",
        session_source="transport",
        session_trusted=True,
    )
    bridge = SafeBridgeApi(str(tmp_path), _trusted_access_context=access)
    bound = resolve_native_transport_context(bridge._trusted_bridge_context())
    assert bound.agent_instance_id == "agent-a"
    assert bound.share_group_id == "shared-team"
    assert bound.workspace_id == str(tmp_path.resolve())
    assert bound.project_ref == str(tmp_path.resolve())
    assert bound.namespace_id.startswith("knowledge-")


def test_native_group_and_agent_registry_entries_are_all_implemented(tmp_path: Path) -> None:
    from memoryguard.cutover_v2.surfaces import GUI_OPERATION_SPECS

    expected = {
        name for name, spec in GUI_OPERATION_SPECS.items()
        if spec.domain in {"agent", "binding"}
    }
    entries = _port(tmp_path).coverage()["surfaces"]["gui"]["entries"]
    selected = [item for item in entries if item.get("domain") in {"agent", "binding"}]
    assert {item["name"] for item in selected} == expected
    assert all(item["status"] == "implemented" for item in selected), selected
    assert all(item["status"] != "retired" for item in selected)
