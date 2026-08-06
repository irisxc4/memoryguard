from __future__ import annotations

from memoryguard.agent_binding import AgentBindingStore, personal_group_id
from memoryguard.conversation_history import HistoryAccessResolver
from memoryguard.governance_scope import (
    GovernanceScope,
    resolve_active_scope,
    save_scope_preference,
)
from memoryguard.gui import GovernanceApi


def test_runtime_scope_requires_an_active_binding(tmp_path):
    result = resolve_active_scope(
        tmp_path,
        {"mode": "agent", "agent_instance_id": "agent-a"},
    )

    assert result.ok is False
    assert result.status == "stale_selection"
    assert result.error == "active_binding_required"


def test_runtime_scope_resolves_a_gui_selected_agent(tmp_path):
    binding = AgentBindingStore(tmp_path).bind_agent(
        "agent-a", personal_group_id("agent-a")
    )

    result = resolve_active_scope(
        tmp_path,
        {"mode": "agent", "agent_instance_id": "agent-a"},
    )

    assert result.ok is True
    assert result.principal_agent_id == "agent-a"
    assert result.authorized_agent_ids == ("agent-a",)
    assert result.binding_ids == (binding.binding_id,)


def test_history_resolver_uses_the_same_runtime_scope_for_gui_selection(tmp_path):
    AgentBindingStore(tmp_path).bind_agent(
        "agent-a", personal_group_id("agent-a")
    )

    scope = HistoryAccessResolver(tmp_path).resolve(
        "",
        {"agent_instance_id": "agent-a"},
    )

    assert scope.agent_instance_id == "agent-a"
    assert scope.share_group_id == ""
    assert scope.authorized_agent_ids == ("agent-a",)
    assert scope.shared_read is False


def test_shared_runtime_scope_selects_a_principal_and_authorizes_members(tmp_path):
    bindings = AgentBindingStore(tmp_path)
    bindings.bind_agents_to_group(["agent-a", "agent-b"], "shared-a")

    result = resolve_active_scope(
        tmp_path,
        {"mode": "share_group", "share_group_id": "shared-a"},
    )

    assert result.ok is True
    assert result.principal_agent_id == "agent-a"
    assert result.authorized_agent_ids == ("agent-a", "agent-b")
    assert len(result.binding_ids) == 2


def test_gui_scope_state_marks_stale_preference_and_lists_recovery_options(tmp_path):
    api = GovernanceApi(tmp_path)
    save_scope_preference(
        tmp_path,
        GovernanceScope(mode="agent", agent_instance_id="agent-stale"),
    )
    AgentBindingStore(tmp_path).bind_agent(
        "agent-live", personal_group_id("agent-live")
    )

    state = api.get_governance_scope_state()

    assert state["ok"] is False
    assert state["status"] == "stale_selection"
    assert state["error"] == "active_binding_required"
    assert state["options"]["agents"] == [{"agent_instance_id": "agent-live"}]
    assert state["options"]["share_groups"] == []


def test_agent_selection_promotes_a_shared_binding_to_shared_runtime_scope(tmp_path):
    bindings = AgentBindingStore(tmp_path)
    bindings.bind_agents_to_group(["agent-a", "agent-b"], "shared-a")

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
