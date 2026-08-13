from __future__ import annotations

from pathlib import Path

from memoryguard.access_context import AccessContext
from memoryguard.desktop_executor import SERVER_ADMIN_AGENT_ID
from memoryguard.gui import SafeBridgeApi
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.native_ports import resolve_native_transport_context


def _access(agent: str, *, admin: bool) -> AccessContext:
    return AccessContext(
        trusted_agent_id=agent,
        is_admin=admin,
        strict_binding=True,
        allow_anon=False,
        session_id="gui-bridge-scope-test",
        session_source="transport",
        session_trusted=True,
    )


def _bridge(root: Path, access: AccessContext) -> SafeBridgeApi:
    return SafeBridgeApi(str(root), _trusted_access_context=access)


def test_admin_persisted_agent_scope_binds_target_group_but_keeps_admin_identity(
    tmp_path: Path,
) -> None:
    groups = GroupControlService(tmp_path, write=True)
    groups.bind_agent("target-agent", "target-group")
    groups.set_scope(
        SERVER_ADMIN_AGENT_ID,
        {"mode": "agent", "agent_instance_id": "target-agent"},
        admin=True,
    )

    context = _bridge(tmp_path, _access(SERVER_ADMIN_AGENT_ID, admin=True))._trusted_bridge_context()
    authority = resolve_native_transport_context(context)

    assert authority.agent_instance_id == SERVER_ADMIN_AGENT_ID
    assert authority.share_group_id == "target-group"


def test_admin_persisted_shared_scope_requires_an_active_member(tmp_path: Path) -> None:
    groups = GroupControlService(tmp_path, write=True)
    groups.bind_agent("member-agent", "shared-group")
    groups.set_scope(
        SERVER_ADMIN_AGENT_ID,
        {"mode": "share_group", "share_group_id": "shared-group"},
        admin=True,
    )

    bridge = _bridge(tmp_path, _access(SERVER_ADMIN_AGENT_ID, admin=True))
    context = resolve_native_transport_context(bridge._trusted_bridge_context())

    assert context.agent_instance_id == SERVER_ADMIN_AGENT_ID
    assert context.share_group_id == "shared-group"
    assert bridge._source_scope() == ("shared-group", "")


def test_admin_empty_or_stale_scope_keeps_unscoped_control_capability(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path, _access(SERVER_ADMIN_AGENT_ID, admin=True))
    empty_authority = resolve_native_transport_context(bridge._trusted_bridge_context())
    assert empty_authority.agent_instance_id == SERVER_ADMIN_AGENT_ID
    assert empty_authority.share_group_id == ""
    assert bridge._source_scope() == ("", "active_binding_required")

    groups = GroupControlService(tmp_path, write=True)
    groups.bind_agent("gone-agent", "gone-group")
    groups.set_scope(
        SERVER_ADMIN_AGENT_ID,
        {"mode": "agent", "agent_instance_id": "gone-agent"},
        admin=True,
    )
    binding = groups.active_binding_for_agent("gone-agent")
    assert binding is not None
    groups.unbind(binding["binding_id"])
    stale_authority = resolve_native_transport_context(bridge._trusted_bridge_context())
    assert stale_authority.agent_instance_id == SERVER_ADMIN_AGENT_ID
    assert stale_authority.share_group_id == ""
    assert bridge._source_scope() == ("", "active_binding_required")


def test_non_admin_bridge_stays_on_its_own_active_binding(tmp_path: Path) -> None:
    GroupControlService(tmp_path, write=True).bind_agent("ordinary-agent", "ordinary-group")
    bridge = _bridge(tmp_path, _access("ordinary-agent", admin=False))

    authority = resolve_native_transport_context(bridge._trusted_bridge_context())

    assert authority.agent_instance_id == "ordinary-agent"
    assert authority.share_group_id == "ordinary-group"
    assert bridge._source_scope() == ("ordinary-group", "")
