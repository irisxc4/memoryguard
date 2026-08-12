from __future__ import annotations

from memoryguard.cutover_v2.facade import V2RuntimeFacade
from memoryguard.runtime_v2.group_native import GroupControlService, personal_group_id
from memoryguard.runtime_v2.history_store import V2HistoryAccessResolver as HistoryAccessResolver
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort
from memoryguard.governance_scope import (
    GovernanceScope,
    list_active_scope_options,
    resolve_active_scope,
    save_scope_preference,
)
from memoryguard.gui import GovernanceApi
from memoryguard.access_context import AccessContext


class _V2ActiveManifest:
    def current(self):
        return {"state": "V2_ACTIVE", "generation": 11}


def _trusted_gui(tmp_path, agent: str) -> GovernanceApi:
    manifest = _V2ActiveManifest()
    native = NativeV2RuntimePort(tmp_path, state_provider=manifest)
    facade = V2RuntimeFacade(
        manifest=manifest,
        v2=native,
        workspace=str(tmp_path),
    )
    return GovernanceApi(
        tmp_path,
        _v2_port=facade,
        _trusted_access_context=AccessContext(
            trusted_agent_id=agent,
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id="runtime-governance-gui",
            session_source="transport",
            session_trusted=True,
        ),
    )


def test_runtime_scope_requires_an_active_binding(tmp_path):
    result = resolve_active_scope(
        tmp_path,
        {"mode": "agent", "agent_instance_id": "agent-a"},
    )

    assert result.ok is False
    assert result.status == "stale_selection"
    assert result.error == "active_binding_required"


def test_runtime_scope_resolves_a_gui_selected_agent(tmp_path):
    binding = GroupControlService(tmp_path, write=True).bind_agent(
        "agent-a", personal_group_id("agent-a"), idempotency_key="test-agent-a"
    )

    result = resolve_active_scope(
        tmp_path,
        {"mode": "agent", "agent_instance_id": "agent-a"},
    )

    assert result.ok is True
    assert result.principal_agent_id == "agent-a"
    assert result.authorized_agent_ids == ("agent-a",)
    assert result.binding_ids == (binding["binding_id"],)


def test_history_resolver_uses_the_same_runtime_scope_for_gui_selection(tmp_path):
    GroupControlService(tmp_path, write=True).bind_agent(
        "agent-a", personal_group_id("agent-a"), idempotency_key="test-agent-a"
    )

    scope = HistoryAccessResolver(tmp_path).resolve(
        "agent-a",
        {"agent_instance_id": "agent-a"},
    )

    assert scope.agent_instance_id == "agent-a"
    assert scope.share_group_id == ""
    assert scope.authorized_agent_ids == ("agent-a",)
    assert scope.shared_read is False


def test_shared_runtime_scope_selects_a_principal_and_authorizes_members(tmp_path):
    GroupControlService(tmp_path, write=True).bind_agents(
        ["agent-a", "agent-b"], share_group_id="shared-a", idempotency_key="test-shared-a"
    )

    result = resolve_active_scope(
        tmp_path,
        {"mode": "share_group", "share_group_id": "shared-a"},
    )

    assert result.ok is True
    assert result.principal_agent_id == "agent-a"
    assert result.authorized_agent_ids == ("agent-a", "agent-b")
    assert len(result.binding_ids) == 2


def test_gui_scope_state_marks_stale_preference_and_lists_recovery_options(tmp_path):
    save_scope_preference(
        tmp_path,
        GovernanceScope(mode="agent", agent_instance_id="agent-stale"),
    )
    GroupControlService(tmp_path, write=True).bind_agent(
        "agent-live", personal_group_id("agent-live"), idempotency_key="test-agent-live"
    )

    # The public GUI path is V2-only and must be exercised with a real active
    # manifest plus a process-issued trusted capability.  UI preference data
    # is not authorization; stale selection recovery remains the shared
    # resolver contract below.
    state = _trusted_gui(tmp_path, "agent-live").get_governance_scope_state()
    assert state["ok"] is True, state
    stale = resolve_active_scope(
        tmp_path,
        {"mode": "agent", "agent_instance_id": "agent-stale"},
    )
    assert stale.ok is False
    assert stale.status == "stale_selection"
    assert stale.error == "active_binding_required"
    options = list_active_scope_options(tmp_path)
    assert options["agents"] == [{"agent_instance_id": "agent-live"}]
    assert options["share_groups"] == []


def test_agent_selection_promotes_a_shared_binding_to_shared_runtime_scope(tmp_path):
    GroupControlService(tmp_path, write=True).bind_agents(
        ["agent-a", "agent-b"], share_group_id="shared-a", idempotency_key="test-shared-a"
    )

    result = resolve_active_scope(
        tmp_path,
        {"mode": "agent", "agent_instance_id": "agent-a"},
    )

    assert result.ok is True
    assert result.scope == GovernanceScope(
        mode="share_group",
        share_group_id="shared-a",
    )
    assert result.principal_agent_id == "agent-a"
    assert result.authorized_agent_ids == ("agent-a", "agent-b")

    history_scope = HistoryAccessResolver(tmp_path).resolve(
        "agent-a",
        {"mode": "agent", "agent_instance_id": "agent-a"},
    )
    assert history_scope.share_group_id == "shared-a"
    assert history_scope.shared_read is True
    assert history_scope.authorized_agent_ids == ("agent-a", "agent-b")


def test_shared_binding_persists_selected_scope_for_a_reopened_gui(tmp_path):
    # A GUI mutation is bound to the caller's currently active V2 group;
    # establish that control-plane identity before changing the membership.
    GroupControlService(tmp_path, write=True).bind_agent(
        "agent-a", personal_group_id("agent-a"), idempotency_key="test-agent-a"
    )
    gui = _trusted_gui(tmp_path, "agent-a")
    created = gui.bind_agents_to_shared_group(
        ["agent-a", "agent-b"], "shared-persisted"
    )

    assert created["ok"] is True, created
    selected = gui.set_governance_scope(
        {"mode": "share_group", "share_group_id": "shared-persisted"}
    )
    assert selected["ok"] is True, selected

    reopened = _trusted_gui(tmp_path, "agent-a").get_governance_scope_state()
    assert reopened["ok"] is True, reopened
    assert reopened["data"]["scope"] == {
        "mode": "share_group",
        "agent_instance_id": "",
        "share_group_id": "shared-persisted",
        "revision": 1,
        "updated_at": reopened["data"]["scope"]["updated_at"],
    }
    assert reopened["data"]["active_binding"]["share_group_id"] == "shared-persisted"


def test_active_binding_keeps_group_visible_when_statistics_db_is_missing(tmp_path):
    GroupControlService(tmp_path, write=True).bind_agents(
        ["agent-a", "agent-b"], share_group_id="ledger-only", idempotency_key="test-ledger-only"
    )

    groups = GroupControlService(tmp_path, write=False).list_share_groups()
    group = next(item for item in groups["groups"] if item["share_group_id"] == "ledger-only")
    assert group["group_kind"] == "shared"
    assert group["members"] == ["agent-a", "agent-b"]
    assert group["active_records"] == 0
